import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app
import routers.common as _common
from routers import audit as audit_mod
from routers import whatsapp_sync as ws_mod


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


def test_capture_health_uses_one_tenant_scoped_query(monkeypatch):
    calls = []

    class Database:
        def execute(self, sql, params=()):
            calls.append((sql, params))
            return _Result([{
                "total_raw": 20,
                "raw_today": 5,
                "last_msg": "2026-07-17T04:30:00Z",
                "total_parsed": 18,
                "parsed_today": 4,
                "total_kr": 18,
                "total_obs": 18,
                "total_oe": 18,
                "total_brokers": 3,
                "pending_enrich": 2,
                "pending_ai": 1,
            }])

    monkeypatch.setattr(audit_mod, "storage", SimpleNamespace(db=Database()))
    monkeypatch.setattr(_common, "storage", SimpleNamespace(db=Database()))
    result = audit_mod.audit_capture_health(user={"id": "user"}, tenant_id="tenant")

    assert len(calls) == 1
    assert calls[0][1][0] == "tenant"
    assert "COUNT(DISTINCT raw_message_id)" in calls[0][0]
    assert result["queue_backlog"] == 3
    assert result["total_msgs_today"] == 5
    assert result["total_parsed_today"] == 4
    assert result["degraded"] is False


def test_capture_health_caps_parser_ready(monkeypatch):
    class Database:
        def execute(self, sql, params=()):
            return _Result([{
                "total_raw": 100,
                "raw_today": 5,
                "last_msg": "2026-07-17T04:30:00Z",
                "total_parsed": 287,
                "parsed_today": 4,
                "total_kr": 18,
                "total_obs": 18,
                "total_oe": 18,
                "total_brokers": 3,
                "pending_enrich": 2,
                "pending_ai": 1,
            }])

    monkeypatch.setattr(audit_mod, "storage", SimpleNamespace(db=Database()))
    monkeypatch.setattr(_common, "storage", SimpleNamespace(db=Database()))
    result = audit_mod.audit_capture_health(user={"id": "user"}, tenant_id="tenant")

    assert result["parser_success_rate"] == 100.0


def test_duplicate_audit_reads_current_tenant_messages(monkeypatch):
    calls = []

    def rows(sql, params=()):
        calls.append((sql, params))
        return [
            {"group_id": "Dubai Marina Brokers", "group_name": "Dubai Marina Brokers", "error": "", "status": "captured"},
            {"group_id": "Dubai Marina Brokers West", "group_name": "Dubai Marina Brokers West", "error": "", "status": "captured"},
        ]

    monkeypatch.setattr(audit_mod, "_audit_rows", rows)
    result = audit_mod.audit_duplicates(user={"id": "user"}, tenant_id="tenant")

    assert len(result) == 1
    assert calls[0][1] == ("tenant",)
    assert "raw_messages" in calls[0][0]
    assert "source_sync_jobs" not in calls[0][0]


def test_audit_timestamp_normalizes_datetime_values():
    from datetime import datetime, timezone

    assert audit_mod._audit_timestamp(datetime(2026, 7, 17, 4, 30, tzinfo=timezone.utc)) == "2026-07-17T04:30:00Z"


def test_audit_group_display_name_does_not_query_storage(monkeypatch):
    class Database:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("display formatting must not query the database")

    monkeypatch.setattr(audit_mod, "storage", SimpleNamespace(db=Database()))
    monkeypatch.setattr(_common, "storage", SimpleNamespace(db=Database()))

    assert audit_mod._audit_group_display_name("Dubai Marina Brokers") == "Dubai Marina Brokers"
    assert audit_mod._audit_group_display_name("120363123456789@g.us") == "WhatsApp Group 6789"


def test_audit_insights_is_tenant_scoped(monkeypatch):
    row_calls = []
    result_sets = iter([[], [], [], [], [], []])

    def rows(sql, params=()):
        row_calls.append((sql, params))
        return next(result_sets)

    monkeypatch.setattr(audit_mod, "_table_exists", lambda table: True)
    monkeypatch.setattr(audit_mod, "_audit_count", lambda table: 0)
    monkeypatch.setattr(audit_mod, "_audit_rows", rows)

    result = audit_mod.audit_insights(user={"id": "user"}, tenant_id="tenant-a")

    assert len(row_calls) == 4
    assert all("tenant_id = $" in sql.lower() for sql, _ in row_calls)
    assert all(params and params[0] == "tenant-a" for _, params in row_calls)
    assert result["daily_flow"] == []
    assert result["markets"] == []
    assert result["brokers"] == []
    assert result["exclusive_members"] == {}


def test_audit_groups_uses_named_columns_from_supabase_json_rows(monkeypatch):
    """JSONB key order must never be mistaken for SQL select order."""
    calls = []
    result_sets = iter([
        [{
            "last_activity": "2026-07-18T12:00:00Z",
            "group_name": "Dubai Marina Brokers",
            "senders_count": 4,
            "messages": 12,
        }],
        [{
            "unknown_locations": 1,
            "markets_count": 2,
            "listings": 6,
            "group_name": "Dubai Marina Brokers",
            "requirements": 2,
            "observations": 8,
            "identities": 4,
        }],
        [{"total_unique_senders": 4}],
        [],
    ])

    def rows(sql, params=()):
        calls.append((sql, params))
        return next(result_sets)

    monkeypatch.setattr(audit_mod, "_table_exists", lambda table: True)
    monkeypatch.setattr(audit_mod, "_audit_rows", rows)

    result = audit_mod.audit_groups_v2(user={"id": "user"}, tenant_id="tenant-a")

    assert len(calls) == 4
    assert "po.raw_message_id::text || ':' || COALESCE(po.listing_index, 0)::text" in calls[1][0]
    assert result["total_unique_senders"] == 4
    assert result["groups"][0]["name"] == "Dubai Marina Brokers"
    assert result["groups"][0]["messages"] == 12
    assert result["groups"][0]["observations"] == 8
    assert result["groups"][0]["active_brokers"] == 4


def test_audit_building_names_reject_parser_style_false_positives():
    assert audit_mod._clean_audit_building_name(" *BURJ VISTA` ") == "BURJ VISTA"
    assert audit_mod._clean_audit_building_name(": Cayan Tower*") == "Cayan Tower"
    assert audit_mod._clean_audit_building_name("Floor: Call") is None
    assert audit_mod._clean_audit_building_name("Photo Available") is None
    assert audit_mod._clean_audit_building_name("Well-Maintained") is None
    assert audit_mod._clean_audit_building_name("388") is None


def test_audit_buildings_use_explicit_tenant_scoped_mentions(monkeypatch):
    calls = []

    def rows(sql, params=()):
        calls.append((sql, params))
        return [
            {"building_name": "Marina Gate", "occurrences": 3},
            {"occurrences": 2, "building_name": " marina gate* "},
            {"building_name": "on call", "occurrences": 12},
            {"building_name": ": Cayan Tower*", "occurrences": 2},
        ]

    # _audit_buildings_for_group is defined in routers.common, so the row
    # loader it closes over lives there rather than on the audit router.
    monkeypatch.setattr(_common, "_audit_rows", rows)

    result = audit_mod._audit_buildings_for_group(
        "tenant-a", "group-jid", "Royal Realtors"
    )

    assert result == [
        {"building_name": "Marina Gate", "occurrences": 5},
        {"building_name": "Cayan Tower", "occurrences": 2},
    ]
    assert len(calls) == 1
    assert "r.tenant_id = ?" in calls[0][0]
    assert calls[0][1][1:] == ("tenant-a", "group-jid", "Royal Realtors")


def test_audit_overlap_uses_named_columns_from_supabase_json_rows(monkeypatch):
    monkeypatch.setattr(audit_mod, "_table_exists", lambda table: True)
    monkeypatch.setattr(audit_mod, "_audit_rows", lambda *_args, **_kwargs: [
        {"sender": "broker-1", "group_name": "Group A"},
        {"group_name": "Group B", "sender": "broker-1"},
        {"sender": "broker-2", "group_name": "Group A"},
        {"group_name": "Group B", "sender": "broker-2"},
    ])

    result = audit_mod.audit_group_overlap(user={"id": "user"}, tenant_id="tenant-a")

    assert result["pairs"][0]["shared_senders"] == 2
    assert {item["name"] for item in result["groups"]} == {"Group A", "Group B"}


def test_search_coverage_audit_flags_missing_listing_cards(monkeypatch):
    monkeypatch.setattr(audit_mod, "storage", SimpleNamespace(db=None))
    monkeypatch.setattr(
        audit_mod.chat_engine,
        "execute_tool",
        lambda *_args, **_kwargs: '{"results":[{"listing_id":101},{"listing_id":102}]}',
    )

    result = audit_mod.audit_search_coverage(
        audit_mod.SearchCoverageRequest(
            query="any office space on rent in difc?",
            response={
                "blocks": [
                    {
                        "type": "listing_cards",
                        "items": [
                            {"listing_id": 101, "title": "Office A"},
                        ],
                    }
                ]
            },
        ),
        user={"id": "user"},
        tenant_id="tenant-a",
    )

    assert result["auditable"] is True
    assert result["complete"] is False
    assert result["expected_count"] == 2
    assert result["rendered_count"] == 1
    assert result["missing_count"] == 1
    assert result["missing_ids"] == ["102"]
    assert result["extra_ids"] == []


def test_phone_list_resolves_authenticated_workspace(monkeypatch):
    seen = []

    class FakeStorage:
        def list_org_whatsapp_connections(self, org_id):
            seen.append(org_id)
            return [{"id": 13, "broker_id": "phone-real", "phone_number": "971501234567"}]

        def list_org_whatsapp_phone_directory(self, org_id):
            assert org_id == "workspace-real"
            return [{"broker_id": "phone-real", "phone_number": "971501234567", "display_label": "Owner"}]

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(ws_mod, "storage", FakeStorage())
    monkeypatch.setattr(ws_mod, "_resolve_active_organization_id", lambda user, tenant_id: "workspace-real")
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    monkeypatch.setattr(ws_mod, "_merged_ingestor_list", lambda **kw: ({}, False, ""))

    result = asyncio.run(ws_mod.list_phones(
        user={"id": "user"}, tenant_id="00000000-0000-0000-0000-000000000010", include_live=False,
    ))

    assert seen == ["workspace-real"]
    assert result["phones"][0]["phone_number"] == "971501234567"
    assert result["phones"][0]["registered_phone_number"] == "971501234567"


def test_create_phone_reuses_workspace_placeholder(monkeypatch):
    connection_calls = []

    class FakeStorage:
        def list_org_whatsapp_connections(self, org_id):
            return [{"id": 19, "broker_id": "phone-placeholder", "phone_number": "Unpaired:phone-placeholder", "instance_name": ""}]

        def update_org_whatsapp_connection(self, conn_id, updates):
            return None

        def add_org_whatsapp_connection(self, *a, **kw):
            return None

    async def ingestor(method, path, **kwargs):
        connection_calls.append((method, path, kwargs))
        return None, None

    async def allow_phone_management(user, org_id, permission):
        assert (user["id"], org_id, permission) == ("user", "workspace-real", "manage_whatsapp")

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(ws_mod, "storage", FakeStorage())
    monkeypatch.setattr(ws_mod, "_resolve_active_organization_id", lambda user, tenant_id: "workspace-real")
    monkeypatch.setattr(ws_mod, "_first_ingestor_response", ingestor)
    monkeypatch.setattr(ws_mod, "_require_org_permission", allow_phone_management)
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)

    result = asyncio.run(ws_mod.create_phone(
        {"instance_name": ""}, user={"id": "user"}, tenant_id="workspace-real",
    ))

    assert result["id"] == 19
    assert connection_calls[0][2]["headers"]["X-Broker-Id"] == "phone-placeholder"


def test_phone_list_marks_missing_session_as_stopped_when_ingestor_is_reachable(monkeypatch):
    class FakeStorage:
        def list_org_whatsapp_connections(self, org_id):
            assert org_id == "workspace-real"
            return [{"id": 13, "broker_id": "phone-real", "phone_number": "971501234567"}]

        def list_org_whatsapp_phone_directory(self, org_id):
            return []

    async def merged_list(**kw):
        return {"phone-real": {"connected": False, "connection_state": "stopped"}}, True, ""

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(ws_mod, "storage", FakeStorage())
    monkeypatch.setattr(ws_mod, "_resolve_active_organization_id", lambda user, tenant_id: "workspace-real")
    monkeypatch.setattr(ws_mod, "_merged_ingestor_list", merged_list)
    monkeypatch.setattr(ws_mod, "_broker_live_statuses", {})
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)

    result = asyncio.run(ws_mod.list_phones(
        user={"id": "user"}, tenant_id="workspace-real", include_live=True,
    ))

    phone = result["phones"][0]
    assert phone["connected"] is False
    assert phone["connection_state"] == "stopped"
    assert phone["live_status_available"] is True
    assert phone["live_status_error"] == ""


def test_phone_list_exposes_ingestor_auth_configuration_error(monkeypatch):
    class FakeStorage:
        def list_org_whatsapp_connections(self, org_id):
            return [{"id": 13, "broker_id": "phone-real", "phone_number": "971501234567"}]

        def list_org_whatsapp_phone_directory(self, org_id):
            return []

    async def merged_list(**kw):
        return {}, False, "WhatsApp service authentication failed. PROPAI_INTERNAL_TOKEN must match on the API and ingestor services."

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(ws_mod, "storage", FakeStorage())
    monkeypatch.setattr(ws_mod, "_resolve_active_organization_id", lambda user, tenant_id: "workspace-real")
    monkeypatch.setattr(ws_mod, "_merged_ingestor_list", merged_list)
    monkeypatch.setattr(ws_mod, "_broker_live_statuses", {})
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)

    result = asyncio.run(ws_mod.list_phones(
        user={"id": "user"}, tenant_id="workspace-real", include_live=True,
    ))

    phone = result["phones"][0]
    assert phone["connected"] is None
    assert phone["connection_state"] == "unavailable"
    assert phone["live_status_available"] is False
    assert "PROPAI_INTERNAL_TOKEN" in phone["live_status_error"]


def test_delete_phone_removes_ingestor_session_and_workspace_record(monkeypatch):
    calls = []

    class FakeStorage:
        def remove_org_whatsapp_connection(self, phone_id):
            calls.append(("storage-delete", phone_id))
            return True

    async def allow_phone_management(user, org_id, permission):
        assert (org_id, permission) == ("workspace-real", "manage_whatsapp")

    async def scoped_phone(phone_id, org_id):
        return {"id": phone_id, "organization_id": org_id, "broker_id": "phone-real"}

    async def ingestor(method, path, **kwargs):
        calls.append((method, path, kwargs["headers"]["X-Broker-Id"]))
        return "http://ingestor:3001", httpx.Response(200, json={"ok": True})

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(ws_mod, "storage", FakeStorage())
    monkeypatch.setattr(ws_mod, "_resolve_active_organization_id", lambda user, tenant_id: "workspace-real")
    monkeypatch.setattr(ws_mod, "_require_org_permission", allow_phone_management)
    monkeypatch.setattr(ws_mod, "_scoped_phone", scoped_phone)
    monkeypatch.setattr(ws_mod, "_first_ingestor_response", ingestor)
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)

    result = asyncio.run(ws_mod.delete_phone(
        13, user={"id": "user"}, tenant_id="workspace-real",
    ))

    assert result == {"ok": True}
    assert calls == [
        ("POST", "/delete-session", "phone-real"),
        ("storage-delete", 13),
    ]


def test_connect_phone_maps_ingestor_unauthorized_to_dependency_error(monkeypatch):
    async def allow_phone_management(user, org_id, permission):
        return None

    async def scoped_phone(phone_id, org_id):
        return {"id": phone_id, "organization_id": org_id, "broker_id": "phone-real"}

    async def ingestor(method, path, **kwargs):
        return "http://ingestor:3001", httpx.Response(401, json={"error": "invalid token"})

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(ws_mod, "_resolve_active_organization_id", lambda user, tenant_id: "workspace-real")
    monkeypatch.setattr(ws_mod, "_require_org_permission", allow_phone_management)
    monkeypatch.setattr(ws_mod, "_scoped_phone", scoped_phone)
    monkeypatch.setattr(ws_mod, "_first_ingestor_response", ingestor)
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)

    with pytest.raises(ws_mod.HTTPException) as exc:
        asyncio.run(ws_mod.connect_phone(13, user={"id": "user"}, tenant_id="workspace-real"))

    assert exc.value.status_code == 502
    assert "PROPAI_INTERNAL_TOKEN" in exc.value.detail


def test_pair_code_rejects_phone_already_saved_in_workspace(monkeypatch):
    ingestor_calls = []

    class FakeStorage:
        def list_org_whatsapp_connections(self, org_id):
            assert org_id == "workspace-real"
            return [
                {
                    "id": 18,
                    "broker_id": "phone-canonical",
                    "phone_number": "+971 50 123 4567",
                    "instance_name": "Kapil Gopal Ojha",
                },
                {
                    "id": 22,
                    "broker_id": "phone-placeholder",
                    "phone_number": "Unpaired:phone-placeholder",
                    "instance_name": "971501234567",
                },
            ]

    async def allow_phone_management(user, org_id, permission):
        assert (org_id, permission) == ("workspace-real", "manage_whatsapp")

    async def scoped_phone(phone_id, org_id):
        return {"id": phone_id, "organization_id": org_id, "broker_id": "phone-placeholder"}

    async def ingestor(*args, **kwargs):
        ingestor_calls.append((args, kwargs))
        return None, None

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def disconnected_status(*args, **kwargs):
        return {"connected": False}

    monkeypatch.setattr(ws_mod, "storage", FakeStorage())
    monkeypatch.setattr(ws_mod, "_resolve_active_organization_id", lambda user, tenant_id: "workspace-real")
    monkeypatch.setattr(ws_mod, "_require_org_permission", allow_phone_management)
    monkeypatch.setattr(ws_mod, "_scoped_phone", scoped_phone)
    monkeypatch.setattr(ws_mod, "_first_ingestor_response", ingestor)
    monkeypatch.setattr(ws_mod, "_best_ingestor_status_for_broker", disconnected_status)
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)

    with pytest.raises(ws_mod.HTTPException) as exc:
        asyncio.run(ws_mod.pair_code_phone(
            22,
            {"phone": "971501234567"},
            user={"id": "user"},
            tenant_id="workspace-real",
        ))

    assert exc.value.status_code == 409
    assert "already saved as Kapil Gopal Ojha" in exc.value.detail
    assert ingestor_calls == []


def test_pair_code_calls_pairing_without_connecting_first(monkeypatch):
    calls = []

    class FakeStorage:
        def list_org_whatsapp_connections(self, org_id):
            return [{
                "id": 22,
                "broker_id": "phone-placeholder",
                "phone_number": "Unpaired:phone-placeholder",
            }]

    async def allow_phone_management(user, org_id, permission):
        return None

    async def scoped_phone(phone_id, org_id):
        return {"id": phone_id, "organization_id": org_id, "broker_id": "phone-placeholder"}

    async def ingestor(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return "http://ingestor:3001", httpx.Response(200, json={"pairing_code": "1234-5678"})

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def disconnected_status(*args, **kwargs):
        return {"connected": False}

    monkeypatch.setattr(ws_mod, "storage", FakeStorage())
    monkeypatch.setattr(ws_mod, "_resolve_active_organization_id", lambda user, tenant_id: "workspace-real")
    monkeypatch.setattr(ws_mod, "_require_org_permission", allow_phone_management)
    monkeypatch.setattr(ws_mod, "_scoped_phone", scoped_phone)
    monkeypatch.setattr(ws_mod, "_first_ingestor_response", ingestor)
    monkeypatch.setattr(ws_mod, "_best_ingestor_status_for_broker", disconnected_status)
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    ws_mod._phone_pair_tasks.clear()
    ws_mod._phone_pair_results.clear()

    async def run_pairing():
        result = await ws_mod.pair_code_phone(
            22,
            {"phone": "971501234567"},
            user={"id": "user"},
            tenant_id="workspace-real",
        )
        await ws_mod._phone_pair_tasks[22]
        return result

    result = asyncio.run(run_pairing())

    assert result == {"ok": True, "accepted": True, "state": "generating"}
    assert ws_mod._phone_pair_results[22]["pairing_code"] == "1234-5678"
    assert len(calls) == 1
    assert calls[0][1] == "/pair-code/start"
