from storage.supabase import (
    SupabaseStorage,
    _TYPED_READ_COLUMNS_BY_TABLE,
    _coerce_typed_boolean,
    _typed_route,
)
from storage.base import ParsedObservation
from extraction import _ai_extraction_to_typed


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, client, table):
        self.client = client
        self.table = table

    def select(self, *args, **kwargs): return self
    def eq(self, *args, **kwargs): return self
    def limit(self, *args, **kwargs): return self
    def upsert(self, payload, **kwargs):
        self.client.writes.append((self.table, payload, kwargs))
        return self
    def insert(self, payload, **kwargs):
        self.client.writes.append((self.table, payload, kwargs))
        return self
    def delete(self): return self
    def execute(self): return _Result([{"id": 101}])


class _Client:
    def __init__(self): self.writes = []
    def table(self, name): return _Query(self, name)


def _storage():
    storage = SupabaseStorage.__new__(SupabaseStorage)
    storage._client = _Client()
    storage._tenant_id = "tenant-a"
    return storage


def test_unknown_typed_boolean_becomes_sql_null():
    assert _coerce_typed_boolean("has_lift", "Unknown") is None
    assert _coerce_typed_boolean("has_lift", "not available") is None


def test_typed_boolean_coercion_preserves_explicit_values():
    assert _coerce_typed_boolean("has_lift", True) is True
    assert _coerce_typed_boolean("has_lift", "yes") is True
    assert _coerce_typed_boolean("has_lift", "false") is False
    assert _coerce_typed_boolean("broker_name", "Unknown") == "Unknown"


def test_typed_listing_drops_unknown_boolean_before_postgrest_write():
    storage = _storage()
    storage.save_typed_listing(
        "residential_rent_listings",
        {"source_fingerprint": "boolean-regression", "has_lift": "Unknown"},
    )
    _table, payload, _options = storage.client.writes[0]
    assert "has_lift" not in payload


def test_route_separates_rent_supply_and_sale_demand():
    assert _typed_route({"asset_type": "residential", "transaction_type": "rent"})[0] == "residential_rent_listings"
    assert _typed_route({"asset_type": "commercial", "transaction_type": "sale", "message_type": "requirement"})[0] == "commercial_sale_requirements"
    assert _typed_route({
        "asset_type": "residential",
        "transaction_type": "sale",
        "message_type": "requirement",
        "normalized_message": "Require 3 BHK on lease in JVC",
    })[0] == "residential_rent_requirements"


def test_listing_type_wins_over_conflicting_provider_transaction_label():
    table, row = _ai_extraction_to_typed(
        {
            "listing_type": "sale",
            "classified_transaction_type": "rent",
            "classified_asset_type": "residential",
            "building_name": "Tower On Call",
            "bhk": 3,
            "price": {"amount": 5700000, "unit": "total", "raw_price_text": "AED 5.7M"},
        },
        "3 BHK flat FOR SALE in Business Bay. Price AED 5.7M",
        raw_message_id=25025,
    )
    assert table == "residential_sale_listings"
    assert row["transaction_type"] == "sale"
    assert row["total_asking_price"] == 5700000


def test_save_parsed_writes_directly_to_typed_rent_table():
    storage = _storage()
    source_id = storage.save_parsed(ParsedObservation(
        raw_message_id=77,
        listing_index=0,
        asset_type="residential",
        transaction_type="rent",
        intent="RENT",
        bhk="3 BHK",
        price=110000,
        price_unit=None,
        area_sqft=1200,
        location_raw="JVC",
        broker_name="Broker A",
    ))
    # The typed table owns the new identity; the legacy observation id is
    # retained only as provenance in legacy_source_id.
    assert source_id == 101
    assert len(storage.client.writes) == 1
    table, payload, options = storage.client.writes[0]
    assert table == "residential_rent_listings"
    assert "id" not in payload
    assert payload["legacy_source_id"] == 77001
    assert payload["monthly_rent"] == 110000
    assert payload["bhk"] == 3.0
    assert options["on_conflict"] == "source_fingerprint"


def test_save_parsed_raw_price_text_overrides_prefilled_bad_rent_value():
    storage = _storage()
    storage.save_parsed(ParsedObservation(
        raw_message_id=79,
        listing_index=0,
        asset_type="residential",
        transaction_type="rent",
        intent="RENT",
        bhk="3 BHK",
        price=6,
        price_unit=None,
        monthly_rent=6,
        price_per_sqft=None,
        confidence=0.95,
        ai_extraction={"price": {"amount": 6, "unit": None, "raw_price_text": "6.5M"}},
    ))

    table, payload, _ = storage.client.writes[0]
    assert table == "residential_rent_listings"
    assert payload["monthly_rent"] == 6_500_000
    assert payload["price_raw_text"] == "6.5M"
    assert payload["needs_review"] is True
    assert payload["extraction_confidence"] == "low"


def test_save_parsed_implausible_prefilled_rent_needs_review_without_raw_price_text():
    storage = _storage()
    storage.save_parsed(ParsedObservation(
        raw_message_id=80,
        listing_index=0,
        asset_type="residential",
        transaction_type="rent",
        intent="RENT",
        bhk="3 BHK",
        monthly_rent=2_500_000_000,
        price=None,
        price_unit=None,
        confidence=0.95,
    ))

    table, payload, _ = storage.client.writes[0]
    assert table == "residential_rent_listings"
    assert payload["monthly_rent"] == 2_500_000_000
    assert "price_raw_text" not in payload
    assert payload["needs_review"] is True
    assert payload["extraction_confidence"] == "low"


def test_save_parsed_residential_requirement_omits_commercial_fields():
    storage = _storage()
    storage.save_parsed(ParsedObservation(
        raw_message_id=78,
        listing_index=0,
        asset_type="residential",
        transaction_type="sale",
        message_type="requirement",
        intent="BUY",
        bhk="4 BHK",
        price=6500000,
        price_unit=None,
        location_raw="Business Bay, Downtown, JVC",
    ))

    table, payload, _ = storage.client.writes[0]
    assert table == "residential_sale_requirements"
    assert "id" not in payload
    assert "commercial_use_type" not in payload
    assert payload["bhk_options"] == [4.0]


def test_requirement_read_columns_match_table_shapes():
    requirement_tables = {
        "residential_sale_requirements",
        "residential_rent_requirements",
        "commercial_sale_requirements",
        "commercial_rent_requirements",
    }
    for table in requirement_tables:
        cols = set(_TYPED_READ_COLUMNS_BY_TABLE[table].split(","))
        assert "available_from" not in cols

    commercial_only_bad = {
        "bhk",
        "configuration_type",
        "bathroom_count",
        "bhk_options",
        "configuration_preference",
    }
    for table in {"commercial_sale_requirements", "commercial_rent_requirements"}:
        cols = set(_TYPED_READ_COLUMNS_BY_TABLE[table].split(","))
        assert not (cols & commercial_only_bad)


def test_residential_rent_read_columns_cover_rental_broker_facts():
    cols = set(_TYPED_READ_COLUMNS_BY_TABLE["residential_rent_listings"].split(","))
    assert {
        "availability_status",
        "availability_date_raw",
        "balcony_area_sqft",
        "terrace_area_sqft",
        "has_lift",
        "contacts",
        "company_lease_criteria",
        "plus_one_deal",
        "fee_sharing_required",
        "client_profile_required",
        "lease_term_min_months",
        "lease_term_max_months",
    } <= cols


def test_commercial_rent_read_columns_cover_commercial_schema():
    cols = set(_TYPED_READ_COLUMNS_BY_TABLE["commercial_rent_listings"].split(","))
    assert {
        "chargeable_area_sqft", "rent_per_sqft", "broker_rera_number",
        "terrace_area_sqft", "frontage_ft", "permitted_use_types",
        "automatic_shutter_count", "room_count", "price_math",
    } <= cols


def test_broker_rera_is_available_on_every_listing_route():
    for table in {
        "residential_sale_listings", "residential_rent_listings",
        "commercial_sale_listings", "commercial_rent_listings",
    }:
        assert "broker_rera_number" in set(_TYPED_READ_COLUMNS_BY_TABLE[table].split(","))


def test_commercial_sale_read_columns_cover_sale_schema():
    cols = set(_TYPED_READ_COLUMNS_BY_TABLE["commercial_sale_listings"].split(","))
    assert {
        "super_built_up_area_sqft", "saleable_area_sqft", "price_math",
        "project_inventory", "area_min_sqft", "area_max_sqft",
        "floor_plate_sqft", "project_status", "inspection_notice_minutes",
    } <= cols


def test_commercial_rent_requirement_read_columns_cover_demand_constraints():
    cols = set(_TYPED_READ_COLUMNS_BY_TABLE["commercial_rent_requirements"].split(","))
    assert {
        "intended_use_details", "area_basis_preference", "floor_min", "floor_max",
        "floor_count_max", "consecutive_floors_required", "entrance_requirement",
        "signage_required", "loading_access_required", "power_requirements",
        "budget_includes_maintenance",
        "needs_attached_washroom", "needs_washroom", "needs_pantry",
        "min_cabin_count", "min_workstation_count", "needs_conference_room",
        "brokerage_context", "contacts",
    } <= cols
