import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers import whatsapp_group_controls as onboarding


class _RowsQuery:
    def __init__(self, rows):
        self.rows = rows
        self.offset = 0
        self.end = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def in_(self, *_args, **_kwargs):
        return self

    def range(self, start, end):
        self.offset = start
        self.end = end
        return self

    def execute(self):
        end = self.end + 1 if self.end is not None else len(self.rows)
        return SimpleNamespace(data=self.rows[self.offset:end])


class _RowsClient:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return _RowsQuery(self.rows)


def test_legacy_super_admin_workspace_is_unlimited(monkeypatch):
    monkeypatch.setattr(
        onboarding,
        "storage",
        SimpleNamespace(
            get_organization=lambda _org_id: {"owner_user_id": None},
            organization_has_super_admin=lambda _org_id: True,
        ),
    )

    assert onboarding._organization_has_unlimited_group_access("legacy-admin-org") is True


def test_super_admin_has_no_group_count_cap_but_still_uses_explicit_selection(monkeypatch):
    monkeypatch.setattr(
        onboarding,
        "_connection",
        lambda _org_id, _connection_id: {"broker_id": "admin-phone"},
    )

    cap = onboarding._cap_state("admin-org", 41, unlimited=True)

    assert cap["tier"] == "platform_admin"
    assert cap["cap"] is None
    assert cap["unlimited"] is False
    assert cap["hard_block"] is False


def test_directory_novelty_uses_server_side_global_broker_match(monkeypatch):
    class FakeDb:
        def execute(self, sql, params):
            assert "FROM brokers" in sql
            assert "gm.tenant_id = ?" in sql
            assert params == ("org-1",)
            return SimpleNamespace(fetchall=lambda: [
                {"group_id": "market@g.us", "member_count": 176, "novel_member_count": 44}
            ])

    monkeypatch.setattr(onboarding, "storage", SimpleNamespace(db=FakeDb()))

    result = onboarding._directory_novelty("org-1", ["market@g.us"])

    assert result["market@g.us"] == {
        "member_count": 176,
        "tracked_member_count": 132,
        "overlap_percent": 75.0,
        "novel_member_count": 44,
        "novelty_percent": 25.0,
    }


def test_directory_novelty_pages_past_postgrest_row_cap(monkeypatch):
    members = [
        {"group_id": "market@g.us", "member_phone": f"{index:010d}"}
        for index in range(1, 1002)
    ]
    monkeypatch.setattr(onboarding, "storage", SimpleNamespace(client=_RowsClient(members)))
    monkeypatch.setattr(onboarding, "_tracked_broker_phones", lambda: set())

    result = onboarding._directory_novelty("org-1", ["market@g.us"])

    assert result["market@g.us"]["member_count"] == 1001
    assert result["market@g.us"]["tracked_member_count"] == 0
    assert result["market@g.us"]["overlap_percent"] == 0.0
    assert result["market@g.us"]["novel_member_count"] == 1001
    assert result["market@g.us"]["novelty_percent"] == 100.0


def test_group_member_recovery_aggregates_and_persists_directory(monkeypatch):
    persisted = {}

    class FakeDb:
        def execute(self, sql, params):
            assert "FROM group_members gm" in sql
            assert params == ("org-1", 1000)
            return SimpleNamespace(fetchall=lambda: [
                {
                    "conversation_jid": "market@g.us",
                    "display_name": "S Realty Market",
                    "participants": 176,
                    "member_snapshot_at": "2026-08-11T00:00:00Z",
                }
            ])

    def persist(tenant_id, broker_id, instance, conversations):
        persisted.update({
            "tenant_id": tenant_id,
            "broker_id": broker_id,
            "instance": instance,
            "conversations": conversations,
        })
        return len(conversations)

    monkeypatch.setattr(
        onboarding,
        "storage",
        SimpleNamespace(db=FakeDb(), upsert_whatsapp_conversations=persist),
    )

    result = onboarding._recover_group_directory_from_members("org-1", "phone-sanjay", "ingestor")

    assert result == [{
        "conversation_jid": "market@g.us",
        "display_name": "S Realty Market",
        "metadata": {"participants": 176},
        "last_message_at": None,
    }]
    assert persisted["tenant_id"] == "org-1"
    assert persisted["broker_id"] == "phone-sanjay"
    assert persisted["conversations"][0]["source"] == "group_members_recovery"


def test_group_member_recovery_requires_unambiguous_active_connection(monkeypatch):
    rows = [
        {"id": 34, "broker_id": "phone-sanjay", "instance_name": "one", "is_active": True},
        {"id": 35, "broker_id": "phone-two", "instance_name": "two", "is_active": True},
    ]
    monkeypatch.setattr(onboarding, "storage", SimpleNamespace(client=_RowsClient(rows)))

    assert onboarding._single_connection_directory_context("org-1", 34, "phone-sanjay") is None


def test_onboarding_groups_falls_back_on_internal_failure(monkeypatch):
    monkeypatch.setattr(onboarding, "_resolve_active_organization_id", lambda user, tenant_id: "org-1")
    async def allow_permission(*args, **kwargs):
        return None

    monkeypatch.setattr(onboarding, "_require_org_permission", allow_permission)
    monkeypatch.setattr(onboarding, "_connection", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(onboarding, "_group_directory", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not run")))
    monkeypatch.setattr(onboarding, "_cap_state", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not run")))

    try:
        asyncio.run(onboarding.onboarding_groups(whatsapp_connection_id=1, user={"id": "u1"}, tenant_id="org-1"))
    except onboarding.HTTPException as exc:
        assert exc.status_code == 503
        assert "directory" in str(exc.detail).lower()
    else:
        raise AssertionError("directory failure must not be returned as an empty success")


def test_group_cap_falls_back_on_internal_failure(monkeypatch):
    monkeypatch.setattr(onboarding, "_resolve_active_organization_id", lambda user, tenant_id: "org-1")
    monkeypatch.setattr(onboarding, "_require_org_permission", lambda *args, **kwargs: None)
    monkeypatch.setattr(onboarding, "_connection", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(onboarding, "_cap_state", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not run")))

    try:
        asyncio.run(onboarding.group_cap(whatsapp_connection_id=1, user={"id": "u1"}, tenant_id="org-1"))
    except onboarding.HTTPException as exc:
        assert exc.status_code == 503
    else:
        raise AssertionError("group-cap failure must not be returned as an unlimited success")


def test_onboarding_groups_loads_directory_without_overlap_work(monkeypatch):
    calls = {}
    monkeypatch.setattr(onboarding, "_resolve_active_organization_id", lambda user, tenant_id: "org-1")

    async def allow_permission(*args, **kwargs):
        return None

    monkeypatch.setattr(onboarding, "_require_org_permission", allow_permission)
    monkeypatch.setattr(onboarding, "_connection", lambda *args, **kwargs: {"broker_id": 42})
    monkeypatch.setattr(
        onboarding,
        "storage",
        SimpleNamespace(is_super_admin=lambda _user_id: False),
    )

    def fake_directory(*args, **kwargs):
        calls["directory"] = kwargs
        return [{"group_jid": "1@g.us"}]

    monkeypatch.setattr(
        onboarding,
        "_group_directory",
        fake_directory,
    )

    result = asyncio.run(onboarding.onboarding_groups(whatsapp_connection_id=33, user={"id": "u1"}, tenant_id="org-1"))

    assert result["groups"] == [{"group_jid": "1@g.us"}]
    assert calls["directory"]["include_overlap"] is False
    assert result["tier"] == "workspace"
    assert result["cap"] is None
    assert result["unlimited"] is False


def test_opt_out_persists_when_directory_refresh_is_unavailable(monkeypatch):
    class Result:
        data = [{"group_jid": "123@g.us", "opted_out": True}]

    class Table:
        def upsert(self, *args, **kwargs):
            return self

        def execute(self):
            return Result()

    class Client:
        def table(self, name):
            assert name == "organization_group_connections"
            return Table()

    monkeypatch.setattr(onboarding, "_resolve_active_organization_id", lambda user, tenant_id: "org-1")
    async def allow_permission(*args, **kwargs):
        return None

    monkeypatch.setattr(onboarding, "_require_org_permission", allow_permission)
    monkeypatch.setattr(onboarding, "_connection", lambda *args, **kwargs: {"broker_id": 42})
    monkeypatch.setattr(onboarding, "_group_directory", lambda *args, **kwargs: [])
    monkeypatch.setattr(onboarding, "_overlap", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("directory down")))
    monkeypatch.setattr(onboarding, "_set_group_extraction_suppressed", lambda *args, **kwargs: None)
    monkeypatch.setattr(onboarding, "_cap_state", lambda *args, **kwargs: {"unlimited": True})
    monkeypatch.setattr(onboarding.storage, "_real", type("FakeStorage", (), {"client": Client()})())

    result = asyncio.run(onboarding.opt_out_group(
        onboarding.GroupRequest(
            whatsapp_connection_id=33,
            group_jid="123@g.us",
            group_name="Family group",
        ),
        user={"id": "u1"},
        tenant_id="org-1",
    ))

    assert result["ok"] is True
    assert result["group"]["group_name"] == "Family group"
    assert result["opted_out"] is True
