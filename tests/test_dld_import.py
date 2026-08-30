"""Tests for scripts/dld_import.py — pure helpers + write path against a fake storage.

These tests never touch the network or Supabase: DubaiPulse blocks datacenter
egress, and --apply is only smoke-verified through the in-memory FakeStorage.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dld_import import (  # noqa: E402
    _aggregate_rent,
    _apply_building,
    _bool,
    _building_type,
    _completion_status,
    _enrichment_fields,
    _float,
    _int,
    _nkeys,
    _percentile,
    _pick,
    _preview_building,
    _rooms_bucket,
)


def test_nkeys_normalizes_headers():
    assert _nkeys({"Area Name En": "JLT", "Project_Name (EN)": "X"}) == {
        "area_name_en": "JLT",
        "project_name_en": "X",
    }


def test_pick_resolves_across_revisions():
    nk = _nkeys({"project_name_en": "Marina Gate"})
    assert _pick(nk, ("project_name", "project_name_en")) == "Marina Gate"
    assert _pick(_nkeys({}), ("project_name", "project_name_en")) is None


def test_numeric_helpers():
    assert _int("12 ") == 12
    assert _int("1,250") == 1250
    assert _int(None) is None
    assert _float("AED 2,400.50") == 2400.5
    assert _float("") is None
    assert _bool("1") is True
    assert _bool("false") is False
    assert _bool("") is None


def test_percentile():
    assert _percentile([1.0], 50) == 1.0
    vals = [1.0, 2.0, 3.0, 4.0]
    assert _percentile(vals, 50) == 2.5
    assert _percentile([], 50) is None


def test_rooms_bucket():
    assert _rooms_bucket("Studio") == "Studio"
    assert _rooms_bucket("studio") == "Studio"
    assert _rooms_bucket("0") == "Studio"
    assert _rooms_bucket("2") == "2BR"
    assert _rooms_bucket("3 Bedrooms") == "3BR"
    assert _rooms_bucket("") == ""


def test_aggregate_rent_buckets_and_min_contracts():
    rows = []
    for i in range(20):
        rows.append({"area_name_en": "JLT", "rooms": "1", "annual_contract_amount": 60000 + i})
    for i in range(20):
        rows.append({"area_name_en": "JLT", "rooms": "Studio", "annual_contract_amount": 40000 + i})
    rows.append({"area_name_en": "JLT", "rooms": "1", "annual_contract_amount": 100})  # < min 1000 => dropped
    agg = _aggregate_rent(rows, min_contracts=10)
    assert set(agg) == {"JLT"}
    doc = agg["JLT"]
    assert set(doc["by_rooms"]) == {"1BR", "Studio"}
    assert doc["contract_count"] == 40
    assert doc["overall"]["count"] == 40
    assert doc["by_rooms"]["1BR"]["median"] == 60009.5


def test_aggregate_rent_area_filter_and_floor():
    rows = [{"area_name_en": "OTHER", "rooms": "2", "annual_contract_amount": 80000} for _ in range(12)]
    agg = _aggregate_rent(rows, min_contracts=5, areas={"JLT"})
    assert agg == {}
    rows2 = [{"area_name_en": "JLT", "rooms": "2", "annual_contract_amount": 80000} for _ in range(4)]
    assert _aggregate_rent(rows2, min_contracts=5) == {}  # below floor


def test_aggregate_rent_since_window():
    rows = [
        {"area_name_en": "JLT", "rooms": "1", "annual_contract_amount": 60000, "registration_date": "2025-01-10"},
        {"area_name_en": "JLT", "rooms": "1", "annual_contract_amount": 65000, "registration_date": "2019-01-10"},
    ]
    agg = _aggregate_rent(rows, min_contracts=1, since="2024-01-01")
    assert agg["JLT"]["by_rooms"]["1BR"]["count"] == 1
    assert agg["JLT"]["by_rooms"]["1BR"]["median"] == 60000

    # undated contracts are dropped inside a since window
    assert _aggregate_rent([{"area_name_en": "JLT", "rooms": "1",
                             "annual_contract_amount": 60000}], min_contracts=1, since="2024-01-01") == {}


def test_enrichment_fields_buildings():
    row = {
        "project_name_en": "Marina Gate",
        "area_name_en": "JLT",
        "bld_levels": "42",
        "flats": "120",
        "car_parks": "3",
        "swimming_pools": "2",
        "elevators": "4",
        "is_free_hold": "1",
        "actual_area": "950",
        "pre_registration_number": "REG-123",
        "creation_date": "2015-06-01",
        "property_usage": "residential",
        "project_status": "completed",
    }
    columns, amenities = _enrichment_fields(_nkeys(row), "buildings")
    assert columns["rera_number"] == "REG-123"
    assert columns["completion_status"] == "Completed"
    assert columns["building_type"] == "Residential"
    assert "building_age" in columns
    assert amenities["floors"] == 42
    assert amenities["units"] == 120
    assert amenities["free_hold"] is True
    assert amenities["actual_area_sqft"] == 950.0


def test_enrichment_fields_projects():
    row = {
        "project_name_en": "Damac Hills",
        "developer": "DAMAC",
        "project_status": "Under Construction",
        "completed_percent": "40",
        "escrow_account": "ACC-7",
        "total_villas": "150",
    }
    columns, amenities = _enrichment_fields(_nkeys(row), "projects")
    assert columns["developer"] == "DAMAC"
    assert columns["completion_status"] == "Under Construction"
    assert amenities["escrow_account"] == "ACC-7"
    assert amenities["total_villas"] == 150


def test_completion_status_and_type():
    nk = _nkeys({"completed_percent": "100"})
    assert _completion_status(nk, "100") == "Completed"
    nk2 = _nkeys({"completed_percent": "25"})
    assert _completion_status(nk2, "") == "Planned"
    assert _building_type("Mixed Use") == "Mixed Use"
    assert _building_type("office building") == "Commercial"
    assert _building_type(None) is None


class FakeClient:
    def __init__(self, storage):
        self.storage = storage
        self.buildings = {}
        self.last_building_id = 1

    def table(self, name):
        return FakeQuery(self, name)


class FakeQuery:
    def __init__(self, client, name):
        self.client = client
        self.name = name
        self._eq = None
        self._payload = None
        self._updating = False

    def select(self, *a):
        return self

    def eq(self, k, v):
        self._eq = (k, v)
        return self

    def limit(self, *a):
        return self

    def order(self, *a):
        return self

    def range(self, *a):
        return self

    def insert(self, payload):
        self._payload = payload
        self.client.last_building_id += 1
        return self

    def update(self, payload):
        self._payload = payload
        self._updating = True
        return self

    def upsert(self, payload, **kwargs):
        self._payload = payload
        return self

    def execute(self):
        if self._payload is not None:
            if self.name == "buildings" and self._updating and self._eq and self._eq[0] == "id":
                row = self.client.buildings[self._eq[1]]
                row.update(self._payload)
                return SimpleNamespace(data=[row])
            return SimpleNamespace(data=[{"id": self.client.last_building_id}])
        if self.name == "buildings" and self._eq and self._eq[0] == "id":
            row = self.client.buildings.get(self._eq[1])
            return SimpleNamespace(data=[row] if row else [])
        return SimpleNamespace(data=[], count=0)


class FakeStorage:
    def __init__(self):
        self.client = FakeClient(self)
        self.created = []
        self.sources = []
        self.history = []

    def create_building(self, canonical_name, micro_market=None, tenant_id=None):
        bid = self.client.last_building_id
        self.client.last_building_id += 1
        existing = next(
            (r for r in self.client.buildings.values()
             if r["canonical_name"].casefold() == canonical_name.casefold()), None)
        if existing:
            return existing
        row = {
            "id": bid,
            "building_id": f"BLD-{bid}",
            "canonical_name": canonical_name,
            "micro_market": micro_market,
            "rera_number": None,
            "developer": None,
            "completion_status": None,
            "building_type": None,
            "building_age": None,
            "amenities": None,
            "status": "discovered",
            "updated_at": None,
        }
        self.client.buildings[bid] = row
        self.created.append((canonical_name, micro_market))
        return row

    def record_enrichment_sources(self, building_db_id, provider, fields, confidence,
                                  source_url="", source_record_id=""):
        self.sources.append((building_db_id, provider, dict(fields), confidence, source_url, source_record_id))

    def add_enrichment_history(self, *args, **kwargs):
        self.history.append(args)


def test_apply_building_writes_and_never_clobbers():
    storage = FakeStorage()
    nk = _nkeys({
        "project_name_en": "Marina Gate Tower",
        "area_name_en": "JLT",
        "bld_levels": "42",
        "flats": "120",
        "pre_registration_number": "REG-123",
        "property_usage": "residential",
    })
    outcome, name = _apply_building(storage, nk, "buildings", source_url="http://example/buildings.csv")
    assert outcome == "apply"
    asserting_row = list(storage.client.buildings.values())[-1]
    assert asserting_row["rera_number"] == "REG-123"
    assert asserting_row["completion_status"] is None  # absent field untouched
    assert asserting_row["amenities"]["floors"] == 42
    assert any(bid == asserting_row["id"] for bid, *_ in storage.sources)
    assert storage.history and storage.history[0][0] == asserting_row["id"]

    # Second pass: evidence is recorded, profile values never overwritten.
    storage2_row = nk  # same row
    outcome2, _ = _apply_building(storage, storage2_row, "buildings", source_url="http://example/buildings.csv")
    later_row = [row for row in storage.client.buildings.values() if row["id"] == asserting_row["id"]][0]
    assert later_row["rera_number"] == "REG-123"
    assert len(storage.created) == 1


def test_apply_building_skips_invalid():
    storage = FakeStorage()
    nk = _nkeys({"project_name_en": "Flat For Rent", "area_name_en": "JLT"})
    outcome, _ = _apply_building(storage, nk, "buildings", source_url="")
    assert outcome == "skip"
    assert storage.created == []


def test_apply_building_skips_missing_identity():
    storage = FakeStorage()
    assert _apply_building(storage, _nkeys({}), "buildings", source_url="")[0] == "skip"
    nk = _nkeys({"project_name_en": "Some Tower"})
    assert _apply_building(storage, nk, "buildings", source_url="")[0] == "skip"


def test_preview_building_matches_apply():
    nk = _nkeys({"project_name_en": "Marina Gate Tower", "area_name_en": "JLT", "bld_levels": "5"})
    assert _preview_building(nk, "buildings")[0] == "apply"
    bad = _nkeys({"project_name_en": "Flat For Rent", "area_name_en": "JLT"})
    assert _preview_building(bad, "buildings")[0] == "skip"