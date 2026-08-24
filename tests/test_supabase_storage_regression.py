"""
Regression tests for Supabase bootstrap and adapter loading.

These tests cover the startup failure where Python resolved the local
`supabase/` directory instead of a client package, causing app import to crash.
"""

import asyncio
from types import SimpleNamespace

import httpx
import routers.common as _common


def test_app_imports_with_supabase_storage_available():
    """The API app should import without requiring the external supabase package."""
    import app  # noqa: F401


def test_startup_imports_match_app_dependencies():
    """The storage package exports used by app.py should stay importable."""
    from storage import (
        SupabaseStorage,
        RawMessage,
        ParsedObservation,
        ResolverDecision,
        Evaluation,
        LLMProvider,
    )

    assert SupabaseStorage is not None
    assert RawMessage is not None
    assert ParsedObservation is not None
    assert ResolverDecision is not None
    assert Evaluation is not None
    assert LLMProvider is not None

    import storage.supabase  # noqa: F401


def test_tenant_context_rejects_another_users_tenant(monkeypatch):
    """A stale or forged browser tenant header must not cross organizations."""
    import app  # noqa: F401 — wiring side effects

    class FakeStorage:
        def get_user_organizations(self, user_id):
            assert user_id == "user-2"
            return [{"id": "org-2"}]

    monkeypatch.setattr(_common, "storage", FakeStorage())
    monkeypatch.setattr(_common, "_resolve_user_organization_id", lambda user: "org-2")

    tenant_id = asyncio.run(_common.get_tenant_context(
        user={"id": "user-2", "email": "user2@example.com"},
        x_tenant_id="org-1",
    ))

    assert tenant_id == "org-2"


def test_tenant_context_accepts_a_users_own_tenant(monkeypatch):
    import app  # noqa: F401 — wiring side effects

    class FakeStorage:
        def get_user_organizations(self, user_id):
            return [{"id": "org-2"}, {"id": "org-3"}]

    monkeypatch.setattr(_common, "storage", FakeStorage())

    tenant_id = asyncio.run(_common.get_tenant_context(
        user={"id": "user-2", "email": "user2@example.com"},
        x_tenant_id="org-3",
    ))

    assert tenant_id == "org-3"


def test_market_feed_endpoints_forward_the_active_tenant(monkeypatch):
    import routers.common as _common
    import routers.brokers as _brokers

    calls = []

    class FakeStorage:
        def get_brokers_feed(self, limit, offset, min_observations, tenant_id):
            calls.append(("brokers", limit, offset, min_observations, tenant_id))
            return []

        def get_observations_feed(self, limit, offset, broker_key, intent, tenant_id):
            calls.append(("observations", limit, offset, broker_key, intent, tenant_id))
            return []

    fake = FakeStorage()
    monkeypatch.setattr(_common, "storage", fake)
    monkeypatch.setattr(_brokers, "storage", fake)

    asyncio.run(_brokers.get_brokers_feed(
        user={"id": "user-2"},
        limit=25,
        offset=0,
        min_observations=1,
        tenant_id="org-2",
    ))

    assert calls == [
        ("brokers", 25, 0, 1, "org-2"),
    ]


def test_market_broker_feed_prefers_database_aggregation():
    """The brokers directory aggregates typed market rows database-side."""
    from types import SimpleNamespace

    from storage.supabase import SupabaseStorage, set_tenant_id

    tables_queried = []

    class FakeQuery:
        def __init__(self, rows):
            self.rows = list(rows)

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)

            def _chain(*args, **kwargs):
                return self

            return _chain

        def execute(self):
            return SimpleNamespace(data=self.rows)

    parsed_rows = [
        {
            "id": 1,
            "raw_message_id": 101,
            "intent": "SELL",
            "broker_name": "Deepak Jagasia",
            "broker_phone": "",
            "created_at": "2026-08-01T10:00:00+00:00",
            "summary_title": "First listing",
        },
        {
            "id": 2,
            "raw_message_id": 102,
            "intent": "SELL",
            "broker_name": "Private Broker",
            "visibility": "workspace_private",
            "tenant_id": "org-other",
            "created_at": "2026-08-01T11:00:00+00:00",
            "summary_title": "Other workspace row",
        },
    ]

    class FakeClient:
        def table(self, name):
            tables_queried.append(name)
            if name == "residential_sale_listings":
                return FakeQuery(parsed_rows)
            # Other typed tables plus the superseded-raw probe are empty.
            return FakeQuery([])

    storage = object.__new__(SupabaseStorage)
    storage._client = FakeClient()

    set_tenant_id("org-2")
    try:
        result = storage.get_brokers_feed(25, 0, min_observations=1)
    finally:
        set_tenant_id(None)

    assert result
    assert result[0]["identity_key"] == "name:deepak jagasia"
    assert result[0]["canonical_name"] == "Deepak Jagasia"
    # Another workspace's private rows must not leak into the shared feed.
    assert all(row["canonical_name"] != "Private Broker" for row in result)
    assert "residential_sale_listings" in tables_queried


def test_market_identity_links_name_only_rows_to_one_phone():
    from storage.supabase import _resolve_market_identity

    identity, phone = _resolve_market_identity(
        "",
        "Deepak Jagasia",
        {"deepak jagasia": {"9222772277"}},
    )

    assert identity == "9222772277"
    assert phone == "9222772277"


def test_market_identity_does_not_merge_an_ambiguous_name():
    from storage.supabase import _resolve_market_identity

    identity, phone = _resolve_market_identity(
        "",
        "Amit Shah",
        {"amit shah": {"9820011111", "9820022222"}},
    )

    assert identity == "name:amit shah"
    assert phone == ""


def test_parsed_market_fallback_merges_phone_and_name_rows():
    from types import SimpleNamespace
    from storage.supabase import SupabaseStorage

    parsed_rows = [
        {
            "id": 1,
            "raw_message_id": 101,
            "intent": "SELL",
            "broker_name": "Deepak Jagasia",
            "broker_phone": "9222772277",
            "created_at": "2026-07-18T10:00:00+00:00",
            "summary_title": "First listing",
        },
        {
            "id": 2,
            "raw_message_id": 102,
            "intent": "SELL",
            "broker_name": "Deepak Jagasia",
            "created_at": "2026-07-18T11:00:00+00:00",
            "summary_title": "Latest listing",
        },
    ]

    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def __getattr__(self, _name):
            return lambda *args, **kwargs: self

        def execute(self):
            return SimpleNamespace(data=self.rows)

    class FakeClient:
        def table(self, name):
            if name == "residential_sale_listings":
                return FakeQuery(parsed_rows)
            # Other typed tables plus the superseded-raw probe are empty.
            return FakeQuery([])

    storage = object.__new__(SupabaseStorage)
    storage._client = FakeClient()
    storage._SupabaseStorage__tenant_id_fallback = None

    result = storage._get_parsed_market_threads(25, 0)

    assert len(result) == 1
    assert result[0]["conversation_key"] == "9222772277"
    assert result[0]["broker_phone"] == "9222772277"
    assert result[0]["message_count"] == 2


def test_parsed_market_specialties_do_not_count_reposts_twice():
    from storage.supabase import SupabaseStorage

    parsed_rows = [
        {
            "id": 1,
            "raw_message_id": 101,
            "intent": "RENT",
            "broker_name": "Market Broker",
            "broker_phone": "9820012345",
            "micro_market": "Deira",
            "property_type": "Apartment",
            "bhk": "2 BHK",
            "price": "85K",
            "created_at": "2026-07-18T10:00:00+00:00",
        },
        {
            "id": 2,
            "raw_message_id": 102,
            "intent": "RENT",
            "broker_name": "Market Broker",
            "broker_phone": "9820012345",
            "micro_market": "Deira",
            "property_type": "Apartment",
            "bhk": "2 BHK",
            "price": "85K",
            "created_at": "2026-07-18T11:00:00+00:00",
        },
        {
            "id": 3,
            "raw_message_id": 103,
            "intent": "SELL",
            "broker_name": "Market Broker",
            "broker_phone": "9820012345",
            "micro_market": "JVC",
            "property_type": "Office",
            "area_sqft": 900,
            "price": "4M",
            "created_at": "2026-07-18T12:00:00+00:00",
        },
    ]

    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def __getattr__(self, _name):
            return lambda *args, **kwargs: self

        def execute(self):
            return SimpleNamespace(data=self.rows)

    class FakeClient:
        def table(self, name):
            if name == "residential_sale_listings":
                return FakeQuery(parsed_rows)
            # Other typed tables plus the superseded-raw probe are empty.
            return FakeQuery([])

    storage = object.__new__(SupabaseStorage)
    storage._client = FakeClient()
    storage._SupabaseStorage__tenant_id_fallback = None

    result = storage._get_parsed_market_threads(25, 0)

    assert result[0]["message_count"] == 2
    assert result[0]["specialty_localities"] == ["Deira", "JVC"]
    assert result[0]["specialty_property_types"] == ["Apartment", "Office"]


def test_phone_observation_fallback_includes_linked_name_only_rows():
    from datetime import datetime, timedelta, timezone

    from types import SimpleNamespace
    from storage.supabase import SupabaseStorage

    def parsed_row(row_id, phone):
        seen_at = (datetime.now(timezone.utc) - timedelta(days=1, hours=row_id)).isoformat()
        return {
            "id": row_id,
            "raw_message_id": 100 + row_id,
            "intent": "SELL",
            "broker_name": "Deepak Jagasia",
            "broker_phone": phone,
            # Distinct listings so the repost projection keeps them separate.
            "bhk": f"{row_id} BHK",
            "created_at": seen_at,
            "raw_messages": {
                "group_name": "Dubai Marina Brokers",
                "sender": "Deepak Jagasia",
                "message": f"Listing {row_id}",
                "timestamp": seen_at,
                "is_group": True,
            },
        }

    class FakeQuery:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: self

        def execute(self):
            return SimpleNamespace(data=[parsed_row(1, "9222772277"), parsed_row(2, "")])

    class FakeClient:
        def table(self, name):
            query = FakeQuery()
            if name != "residential_sale_listings":
                query.execute = lambda: SimpleNamespace(data=[])
            return query

    storage = object.__new__(SupabaseStorage)
    storage._client = FakeClient()
    storage._SupabaseStorage__tenant_id_fallback = None

    result = storage._get_parsed_observations_for_broker(
        25,
        0,
        broker_key="9222772277",
    )

    assert len(result) == 2
    assert {row["broker_key"] for row in result} == {"9222772277"}
    assert {row["broker_phone"] for row in result} == {"9222772277"}


def test_observation_detail_dedupes_repeated_listings():
    from types import SimpleNamespace
    from storage.supabase import SupabaseStorage

    parsed_rows = [
        {
            "id": 1,
            "raw_message_id": 101,
            "listing_index": 0,
            "intent": "SELL",
            "bhk": "3 BHK",
            "price": 2500000,
            "price_unit": "AED",
            "area_sqft": 1200,
            "furnishing": "Furnished",
            "building_name": "Marina Gate",
            "landmark_name": "Near Metro Station",
            "micro_market": "Dubai Marina",
            "location_raw": "Dubai Marina",
            "summary_title": "3 BHK | Marina Gate | AED 2.5M",
            "created_at": "2026-07-19T10:00:00Z",
        },
        {
            "id": 2,
            "raw_message_id": 101,
            "listing_index": 0,
            "intent": "SELL",
            "bhk": "3 BHK",
            "price": 2500000,
            "price_unit": "AED",
            "area_sqft": 1200,
            "furnishing": "Furnished",
            "building_name": "Marina Gate",
            "landmark_name": "Near Metro Station",
            "micro_market": "Dubai Marina",
            "location_raw": "Dubai Marina",
            "summary_title": "3 BHK | Marina Gate | AED 2.5M",
            "created_at": "2026-07-19T10:01:00Z",
        },
    ]

    class FakeQuery:
        def __init__(self, table_name):
            self.table_name = table_name
            self._superseded_probe = False

        def select(self, *args, **kwargs):
            return self

        def eq(self, column, *values):
            if column == "extraction_superseded":
                self._superseded_probe = True
            return self

        def in_(self, *_args, **_kwargs):
            return self

        def order(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def execute(self):
            if self._superseded_probe:
                # Nothing is marked superseded in this fixture.
                return SimpleNamespace(data=[])
            if self.table_name == "residential_sale_listings":
                return SimpleNamespace(data=parsed_rows)
            if self.table_name == "raw_messages":
                return SimpleNamespace(data=[{"id": 101, "message": "x"}])
            return SimpleNamespace(data=[])

    class FakeClient:
        def table(self, name):
            return FakeQuery(name)

    storage = object.__new__(SupabaseStorage)
    storage._client = FakeClient()
    storage._SupabaseStorage__tenant_id_fallback = None

    result = storage.get_inbox_evidence_detail(101)

    assert len(result["listings"]) == 1
    assert result["listings"][0]["times_seen"] == 2


def test_get_parsed_collapses_same_raw_slice_even_when_listing_index_differs():
    from types import SimpleNamespace
    from storage.supabase import SupabaseStorage

    duplicate_rows = [
        {
            "id": 36533, "raw_message_id": 32160, "listing_index": 0,
            "asset_type": "commercial", "transaction_type": "sale",
            "broker_phone": "9820724988", "carpet_area_sqft": 2000,
            "fitout_status": "bare_shell", "raw_payload": {"slice_text": "Available Commercial Office On Sale At Dadar West Area 2000 Carpet"},
            "created_at": "2026-08-05T01:00:00Z", "extraction_confidence": "high",
        },
        {
            "id": 36534, "raw_message_id": 32160, "listing_index": 1,
            "asset_type": "commercial", "transaction_type": "sale",
            "broker_phone": "9820724988", "carpet_area_sqft": None,
            "raw_payload": {"slice_text": "Available Commercial Office On Sale At Dadar West Area 2000 Carpet"},
            "created_at": "2026-08-05T01:01:00Z", "extraction_confidence": "high",
        },
    ]

    class FakeQuery:
        def __init__(self, table_name): self.table_name = table_name
        def select(self, *args, **kwargs): return self
        def eq(self, *args, **kwargs): return self
        def order(self, *args, **kwargs): return self
        def limit(self, *args, **kwargs): return self
        def execute(self):
            if self.table_name == "commercial_sale_listings":
                return SimpleNamespace(data=duplicate_rows)
            if self.table_name == "raw_messages":
                return SimpleNamespace(data=[{"id": 32160, "message": "source"}])
            return SimpleNamespace(data=[])

    class FakeClient:
        def table(self, name): return FakeQuery(name)

    storage = object.__new__(SupabaseStorage)
    storage._client = FakeClient()
    storage._SupabaseStorage__tenant_id_fallback = None

    result = storage.get_parsed(limit=50)

    assert [row.id for row in result] == [36533]


def test_name_identity_market_item_lookup_is_typed_only():
    from storage.supabase import SupabaseStorage, set_tenant_id

    class FakeRawQuery:
        def __init__(self):
            self.batch = []
        def select(self, *args, **kwargs): return self
        def in_(self, _field, batch): self.batch = batch; return self
        def eq(self, *args, **kwargs): return self
        def execute(self):
            return SimpleNamespace(data=[])

    class FakeTypedQuery:
        def select(self, *args, **kwargs): return self
        def order(self, *args, **kwargs): return self
        def limit(self, *args, **kwargs): return self
        def eq(self, *args, **kwargs): return self
        def execute(self):
            return SimpleNamespace(data=[])

    class FakeClient:
        def table(self, name):
            if name == "raw_messages":
                return FakeRawQuery()
            return FakeTypedQuery()

    storage = object.__new__(SupabaseStorage)
    storage._client = FakeClient()
    storage._SupabaseStorage__tenant_id_fallback = None
    try:
        set_tenant_id("org-2")
        result = storage.get_market_items_feed(
            200,
            0,
            broker_key="name:deepak jagasia",
        )
    finally:
        set_tenant_id(None)

    assert result == []


def test_market_observation_reposts_merge_across_raw_message_ids():
    from storage.supabase import _merge_observation_rows

    base = {
        "observation_type": "LISTING",
        "intent": "RENT",
        "bhk": "3 BHK",
        "building_name": "Ten BKC",
        "micro_market": "BKC",
        "area_sqft": 1360,
        "price": 300000,
        "broker_phone": "919773757759",
    }
    rows = [
        {**base, "id": 1, "raw_message_id": 101, "last_seen": "2026-07-23T08:00:00Z", "source_message": "first post"},
        {
            **base,
            "broker_phone": "",
            "broker_name": "Kapil Ojha",
            "id": 2,
            "raw_message_id": 205,
            "last_seen": "2026-07-23T12:00:00Z",
            "source_message": "latest post",
        },
    ]

    merged = _merge_observation_rows(rows)

    assert len(merged) == 1
    assert merged[0]["times_seen"] == 2
    assert merged[0]["raw_message_id"] == 205
    assert merged[0]["source_message"] == "latest post"


def test_market_observation_dedupe_keeps_requirements_and_distinct_floors_separate():
    from storage.supabase import _merge_observation_rows

    base = {
        "intent": "RENT",
        "bhk": "3 BHK",
        "building_name": "Ten BKC",
        "area_sqft": 1360,
        "price": 300000,
        "broker_phone": "919773757759",
    }
    rows = [
        {**base, "observation_type": "LISTING", "floor_range": "17", "raw_message_id": 1},
        {**base, "observation_type": "LISTING", "floor_range": "24", "raw_message_id": 2},
        {**base, "observation_type": "REQUIREMENT", "floor_range": "17", "raw_message_id": 3},
    ]

    assert len(_merge_observation_rows(rows)) == 3


def test_find_broker_refreshes_stale_profile_graph(monkeypatch):
    import routers.brokers as _brokers

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeDB:
        def __init__(self):
            self.lookups = 0

        def execute(self, query, params):
            assert "FROM brokers" in query
            assert params == ("name:sunil rajwani",)
            self.lookups += 1
            return Result(None if self.lookups == 1 else {"id": 73})

    class FakeStorage:
        def __init__(self):
            self.db = FakeDB()
            self.rebuilds = 0

        def rebuild_broker_graph(self):
            self.rebuilds += 1

    fake_storage = FakeStorage()
    monkeypatch.setattr(_brokers, "storage", fake_storage)

    result = asyncio.run(_brokers.find_broker(
        name="Sunil Rajwani",
        phone="",
        user={"id": "user-2"},
    ))

    assert result == {"broker_id": 73}
    assert fake_storage.rebuilds == 1
    assert fake_storage.db.lookups == 2


def test_activity_log_uses_the_authenticated_member(monkeypatch):
    import app  # noqa: F401 — wiring side effects

    captured = {}

    class FakeStorage:
        def log_activity(self, **kwargs):
            captured.update(kwargs)
            return 41

    import routers.workspace as _ws
    monkeypatch.setattr(_ws, "storage", FakeStorage())

    from routers.workspace import log_activity

    result = asyncio.run(log_activity(
        body={
            "team_member_id": 999,
            "action": "broker_whatsapp_opened",
            "target_type": "broker",
            "target_id": "9999999999",
        },
        member={"id": 7},
    ))

    assert result == {"id": 41}
    assert captured["team_member_id"] == 7


def test_whatsapp_status_is_scoped_to_the_users_workspace():
    """A global ingestor session must not leak into another workspace."""
    import routers.dashboard as _dashboard

    status = _dashboard._select_workspace_whatsapp_status(
        [{"broker_id": "broker-owned"}],
        [
            {
                "broker_id": "broker-other",
                "connected": True,
                "phone_number": "919820056180",
            },
            {
                "broker_id": "broker-owned",
                "connected": True,
                "phone_number": "919999999774",
            },
        ],
    )

    assert status["connected"] is True
    assert status["phone_number"] == "919999999774"


def test_legacy_storage_tenant_reads_use_request_context():
    from storage.supabase import SupabaseStorage, set_tenant_id

    storage = object.__new__(SupabaseStorage)
    storage._SupabaseStorage__tenant_id_fallback = None
    try:
        set_tenant_id("org-request")
        assert storage.tenant_id == "org-request"
        assert storage._tenant_id == "org-request"

        storage.tenant_id = "org-next"
        assert storage._tenant_id == "org-next"
        assert storage._SupabaseStorage__tenant_id_fallback is None
    finally:
        set_tenant_id(None)


def test_inbox_threads_uses_parsed_market_threads(monkeypatch):
    from storage.supabase import SupabaseStorage

    calls = []

    class Query:
        def __init__(self, table_name: str):
            self.table_name = table_name

        def select(self, *args, **kwargs):
            calls.append(("select", self.table_name, args, kwargs))
            return self

        def order(self, *args, **kwargs):
            calls.append(("order", self.table_name, args, kwargs))
            return self

        def limit(self, *args, **kwargs):
            calls.append(("limit", self.table_name, args, kwargs))
            return self

        def eq(self, *args, **kwargs):
            calls.append(("eq", self.table_name, args, kwargs))
            return self

        def in_(self, *args, **kwargs):
            calls.append(("in_", self.table_name, args, kwargs))
            return self

        def execute(self):
            calls.append(("execute", self.table_name, (), {}))
            return SimpleNamespace(data=[{
                "id": 1,
                "tenant_id": "tenant-a",
                "group_name": "Royal Realtors @g.us",
                "sender": "Broker One",
                "sender_phone": "919820000000",
                "sender_jid": "919820000000@s.whatsapp.net",
                "timestamp": "2026-07-19T04:00:00Z",
                "created_at": "2026-07-19T04:00:00Z",
                "message_uid": "uid-1",
                "message": "Hello",
                "raw_payload": {},
                "is_group": True,
            }])

    class Client:
        def table(self, name):
            calls.append(("table", name, (), {}))
            return Query(name)

    storage = object.__new__(SupabaseStorage)
    storage._SupabaseStorage__tenant_id_fallback = None
    storage._client = Client()
    storage._get_parsed_market_threads = lambda *args, **kwargs: [{"tenant_id": "tenant-a", "broker_name": "Broker One"}]

    threads = storage.get_inbox_threads(limit=10, offset=0, tenant_id="tenant-a")

    assert threads == [{"tenant_id": "tenant-a", "broker_name": "Broker One"}]
    assert calls == []


def test_inbox_threads_does_not_read_raw_messages_directly(monkeypatch):
    from types import SimpleNamespace
    from storage.supabase import SupabaseStorage

    calls = []

    class Query:
        def __init__(self, table_name: str):
            self.table_name = table_name

        def select(self, *args, **kwargs):
            calls.append(("select", self.table_name, args, kwargs))
            return self

        def order(self, *args, **kwargs):
            calls.append(("order", self.table_name, args, kwargs))
            return self

        def limit(self, *args, **kwargs):
            calls.append(("limit", self.table_name, args, kwargs))
            return self

        def eq(self, *args, **kwargs):
            calls.append(("eq", self.table_name, args, kwargs))
            return self

        def in_(self, *args, **kwargs):
            calls.append(("in_", self.table_name, args, kwargs))
            return self

        def execute(self):
            calls.append(("execute", self.table_name, (), {}))
            if self.table_name == "raw_messages":
                return SimpleNamespace(data=[{
                    "id": 11,
                    "tenant_id": "tenant-a",
                    "group_name": "Royal Realtors @g.us",
                    "sender": "Broker One",
                    "sender_phone": "919820000000",
                    "sender_jid": "919820000000@s.whatsapp.net",
                    "timestamp": "2026-07-19T04:00:00Z",
                    "created_at": "2026-07-19T04:00:00Z",
                    "message_uid": "uid-11",
                    "message": "Hello from raw",
                    "raw_payload": {},
                    "is_group": True,
                }])
            if self.table_name == "parsed_output_unified":
                return SimpleNamespace(data=[{
                    "raw_message_id": 11,
                    "intent": "RENT",
                    "broker_name": "Broker One",
                    "broker_phone": "919820000000",
                    "confidence": 0.9,
                }])
            return SimpleNamespace(data=[])

    class Client:
        def table(self, name):
            calls.append(("table", name, (), {}))
            return Query(name)

    storage = object.__new__(SupabaseStorage)
    storage._SupabaseStorage__tenant_id_fallback = None
    storage._client = Client()
    storage._get_parsed_market_threads = lambda *args, **kwargs: [{
        "message": "Parsed source evidence",
        "intent": "RENT",
        "broker_name": "Broker One",
    }]

    threads = storage.get_inbox_threads(limit=10, offset=0, tenant_id="tenant-a")

    assert threads and threads[0]["message"] == "Parsed source evidence"
    assert threads[0]["intent"] == "RENT"
    assert ("table", "raw_messages", (), {}) not in calls
    assert ("table", "parsed_output_unified", (), {}) not in calls


def test_connection_details_is_safe_without_storage(monkeypatch):
    """The WhatsApp connection endpoint should not assume storage.db exists."""
    import app  # noqa: F401 — wiring side effects
    from types import SimpleNamespace

    monkeypatch.setattr(_common, "storage", SimpleNamespace())
    monkeypatch.setattr(_common, "_status_file", lambda: {})
    details = _common._connection_details()


def test_connection_details_falls_back_when_status_file_is_unknown(monkeypatch):
    """A stale/missing status file should not lock a synced workspace out."""
    import app  # noqa: F401 — wiring side effects

    class Row(dict):
        def __getitem__(self, key):
            return self.get(key)

    class FakeDb:
        def execute(self, sql, params=None):
            class Result:
                def fetchone(self_inner):
                    if "COUNT(*) AS c" in sql and "group_name LIKE" in sql:
                        return Row(c=12)
                    if "COUNT(*) AS c" in sql:
                        return Row(c=345)
                    return Row(created_at="2026-07-13T09:00:00Z", timestamp="2026-07-13T09:05:00Z")

            return Result()

    class FakeStorage:
        db = FakeDb()

    monkeypatch.setattr(_common, "storage", FakeStorage())
    monkeypatch.setattr(_common, "_status_file", lambda: {"connection_state": "unknown", "connected": False})

    details = _common._connection_details()

    assert details["connected"] is False
    assert details["connection_state"] == "unknown"
    assert details["messages_captured"] == 345


def test_connection_details_uses_whatsapp_jobs_when_status_file_is_unknown(monkeypatch):
    """Existing WhatsApp sync jobs are enough to unlock the connected workspace."""
    import app  # noqa: F401 — wiring side effects
    from types import SimpleNamespace

    class FakeStorage:
        def get_sync_jobs(self, limit=500, source="whatsapp"):
            assert source == "whatsapp"
            return [
                SimpleNamespace(finished_at="2026-07-13T09:00:00Z"),
                SimpleNamespace(finished_at="2026-07-13T09:05:00Z"),
            ]

    monkeypatch.setattr(_common, "storage", FakeStorage())
    monkeypatch.setattr(_common, "_status_file", lambda: {"connection_state": "unknown", "connected": False})

    details = _common._connection_details()

    assert details["connected"] is False
    assert details["connection_state"] == "unknown"
    assert details["total_groups"] == 2


def test_supabase_create_client_supports_basic_query_flow():
    """The local adapter should support the query shape used by storage code."""
    from storage.supabase import create_client

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "prefer": request.headers.get("prefer"),
                "body": request.content.decode(),
            }
        )
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 1, "name": "demo"}], headers={"content-range": "0-0/1"})
        if request.method == "POST":
            return httpx.Response(201, json=[{"id": 2, "name": "created"}])
        raise AssertionError(f"unexpected method: {request.method}")

    client = create_client("https://example.supabase.co", "service-key")
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.supabase.co")

    result = client.table("items").select("*", count="exact").eq("name", "demo").limit(1).execute()
    assert result.data == [{"id": 1, "name": "demo"}]
    assert result.count == 1

    created = client.table("items").insert({"name": "created"}).execute()
    assert created.data == [{"id": 2, "name": "created"}]

    assert requests == [
        {
            "method": "GET",
            "url": "https://example.supabase.co/rest/v1/items?select=%2A&name=eq.demo&limit=1",
            "prefer": "count=exact",
            "body": "",
        },
        {
            "method": "POST",
            "url": "https://example.supabase.co/rest/v1/items",
            "prefer": "return=representation",
            "body": '{"name": "created"}',
        },
    ]


def test_supabase_storage_raw_message_round_trip():
    """SupabaseStorage should be able to save and fetch raw messages via REST."""
    from storage import RawMessage
    from storage.supabase import SupabaseStorage, create_client

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "prefer": request.headers.get("prefer"),
                "body": body,
            }
        )

        if request.method == "POST" and request.url.path.endswith("/raw_messages"):
            return httpx.Response(201, json=[{"id": 7}])

        if request.method == "GET" and request.url.path.endswith("/raw_messages"):
            if "message_uid=eq.msg-123" in str(request.url):
                return httpx.Response(
                    200,
                    json=[{
                        "id": 7,
                        "message_uid": "msg-123",
                        "group_name": "Test Group",
                        "sender": "Broker",
                    }],
                )
            return httpx.Response(
                200,
                json=[{
                    "id": 7,
                    "message_uid": "msg-123",
                    "group_name": "Test Group",
                    "sender": "Broker",
                }],
                headers={"content-range": "0-0/1"},
            )

        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = create_client("https://example.supabase.co", "service-key")
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.supabase.co")

    storage = SupabaseStorage("https://example.supabase.co", "service-key")
    storage._client = client

    message = RawMessage(
        group_name="Test Group",
        sender="Broker",
        message="Hello",
        message_uid="msg-123",
        attachments='[{"type":"image"}]',
        reply_context='{"foo":"bar"}',
        raw_payload='{"source":"whatsapp"}',
    )

    saved_id = storage.save_raw_message(message)
    assert saved_id == 7

    fetched = storage.get_raw_by_uid("msg-123")
    assert fetched is not None
    assert fetched.id == 7
    assert fetched.message_uid == "msg-123"
    assert fetched.group_name == "Test Group"

    rows = storage.get_raw_messages(limit=1)
    assert len(rows) == 1
    assert rows[0].id == 7

    assert requests[0]["method"] == "POST"
    assert requests[0]["url"] == "https://example.supabase.co/rest/v1/raw_messages"
    assert requests[0]["prefer"] == "return=representation"
    assert '"attachments": [{"type": "image"}]' in requests[0]["body"]
    assert '"reply_context": {"foo": "bar"}' in requests[0]["body"]
    assert '"raw_payload": {"source": "whatsapp"}' in requests[0]["body"]
    assert any("message_uid=eq.msg-123" in r["url"] for r in requests if r["method"] == "GET")


def test_supabase_storage_parsed_and_listing_writes():
    """SupabaseStorage should normalize parsed and listing payloads before write."""
    from lab.storage.base import Listing, ParsedObservation
    from storage.supabase import SupabaseStorage, create_client
    from lab.inventory import listing_fingerprint, listing_label

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "prefer": request.headers.get("prefer"),
                "body": request.content.decode(),
            }
        )

        if request.method == "POST" and request.url.path.endswith("/residential_rent_listings"):
            return httpx.Response(201, json=[{"id": 11}])
        if request.method == "GET" and request.url.path.endswith("/raw_messages"):
            # The typed observation write resolves WhatsApp freshness from
            # the raw message before inserting.
            return httpx.Response(200, json=[{"timestamp": "2026-08-01T09:00:00+00:00"}])
        if request.method == "GET" and request.url.path.endswith("_listings"):
            # Parsed reads resolve through the typed source tables.
            url = str(request.url)
            if request.url.path.endswith("/residential_rent_listings"):
                if "raw_message_id=eq." in url:
                    return httpx.Response(
                        200,
                        json=[{
                            "id": 11,
                            "raw_message_id": 99,
                            "listing_index": 0,
                            "transaction_type": "rent",
                            "micro_market": "Dubai Marina",
                            "locality_raw": "Dubai Marina",
                        }],
                    )
                if "source_fingerprint=eq." in url:
                    return httpx.Response(
                        200,
                        json=[{
                            "id": 22,
                            "transaction_type": "rent",
                            "building_name": "Marina Gate",
                            "micro_market": "Dubai Marina",
                        }],
                    )
            # Other typed listing tables have no matching rows.
            return httpx.Response(200, json=[])

        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = create_client("https://example.supabase.co", "service-key")
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://example.supabase.co")

    storage = SupabaseStorage("https://example.supabase.co", "service-key")
    storage._client = client

    parsed = ParsedObservation(
        raw_message_id=99,
        intent="RENT",
        raw_payload='{"source":"whatsapp"}',
        location='{"area":"Dubai Marina"}',
        embedding=b"abc",
    )
    parsed_id = storage.save_parsed(parsed)
    assert parsed_id != 0

    fetched_parsed = storage.get_parsed_by_raw(99)
    assert fetched_parsed is not None
    assert fetched_parsed.location_raw == "Dubai Marina"

    listing = Listing(
        intent="RENT",
        bhk="2BHK",
        price=150000,
        price_unit="AED",
        area_sqft=1000,
        building_name="Marina Gate",
        micro_market="Dubai Marina",
    )
    expected_fp = listing_fingerprint({k: v for k, v in listing.__dict__.items() if v is not None})
    expected_label = listing_label({k: v for k, v in listing.__dict__.items() if v is not None})

    listing_id = storage.save_listing(listing)
    assert listing_id != 0

    fetched_listing = storage.get_listing_by_fingerprint(expected_fp)
    assert fetched_listing is not None
    assert fetched_listing.micro_market == "Dubai Marina"

    # save_parsed resolves raw freshness, probes the sibling typed tables
    # for an existing (raw_message_id, listing_index) row, then inserts.
    assert requests[0]["method"] == "GET"
    assert requests[0]["url"].startswith("https://example.supabase.co/rest/v1/raw_messages")

    for probe_index in (1, 2, 3):
        assert requests[probe_index]["method"] == "GET"
        assert "raw_message_id=eq.99" in requests[probe_index]["url"]

    assert requests[4]["method"] == "POST"
    assert requests[4]["url"].startswith("https://example.supabase.co/rest/v1/residential_rent_listings")
    assert '"embedding"' not in requests[4]["body"]
    assert '"raw_payload": {"source": "whatsapp"}' in requests[4]["body"]
    assert '"raw_message_id": 99' in requests[4]["body"]

    # get_parsed_by_raw fans out over all eight typed tables.
    assert len(requests) == 16
    for probe_index in range(5, 13):
        assert requests[probe_index]["method"] == "GET"
        assert "raw_message_id=eq.99" in requests[probe_index]["url"]

    assert requests[13]["method"] == "POST"
    assert requests[13]["url"].startswith("https://example.supabase.co/rest/v1/residential_rent_listings")
    assert requests[13]["prefer"] == "return=representation"
    assert f'"source_fingerprint": "{expected_fp}"' in requests[13]["body"]
    assert '"micro_market": "Dubai Marina"' in requests[13]["body"]

    # The fingerprint lookup fans out over the typed listing tables.
    assert requests[14]["method"] == "GET"
    assert requests[14]["url"].startswith("https://example.supabase.co/rest/v1/residential_sale_listings")
    assert "source_fingerprint=eq." in requests[14]["url"]
    assert requests[15]["method"] == "GET"
    assert requests[15]["url"].startswith("https://example.supabase.co/rest/v1/residential_rent_listings")
    assert f"source_fingerprint=eq.{expected_fp}" in requests[15]["url"]


def test_resolver_decision_uses_database_timestamp_default():
    from lab.storage.base import ResolverDecision
    from storage.supabase import SupabaseStorage, create_client

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content.decode())
        return httpx.Response(201, json=[{"id": 31}])

    client = create_client("https://example.supabase.co", "service-key")
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://example.supabase.co",
    )
    storage = SupabaseStorage("https://example.supabase.co", "service-key")
    storage._client = client

    decision_id = storage.save_resolver_decision(ResolverDecision(parsed_id=11))

    assert decision_id == 31
    assert '"created_at"' not in requests[0]


def test_stale_building_job_recovery_uses_supported_comparison_filter():
    from storage.supabase import SupabaseStorage, create_client

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json=[])

    client = create_client("https://example.supabase.co", "service-key")
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://example.supabase.co",
    )
    storage = SupabaseStorage("https://example.supabase.co", "service-key")
    storage._client = client

    assert storage.recover_stale_building_jobs(max_attempts=3, stale_minutes=10) == 0
    assert "status=eq.running" in requests[0]
    assert "started_at=lt." in requests[0]
    assert "limit=1000" in requests[0]
