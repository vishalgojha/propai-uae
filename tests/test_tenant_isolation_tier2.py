"""
Tenant-isolation regression tests for Tier 2 gaps:
ai_chat_sessions, ai_chat_messages, user_profiles, saved_inbox_views, llm_providers.

These verify that (a) the API endpoints forward the resolved tenant_id into
storage, and (b) the storage adapter scopes every read/write on these tables
by tenant_id so one organization cannot read another's rows.
"""

import asyncio


def test_ai_chat_endpoints_forward_tenant(monkeypatch):
    import routers.ai_chat as ai_chat
    import routers.common as _common

    calls = []

    class FakeStorage:
        def get_user_organizations(self, user_id):
            return [{"id": "org-A"}]

        def get_user_profile(self, phone="", auth_user_id="", tenant_id=None):
            calls.append(("profile", auth_user_id, tenant_id))
            return {"phone": "919999999999"}

        def adopt_chat_session_owners(self, aliases, owner_key, tenant_id=None):
            calls.append(("adopt", owner_key, tenant_id))

        def list_chat_sessions(self, owner_key, limit=50, tenant_id=None):
            calls.append(("list", owner_key, limit, tenant_id))
            return []

        def create_chat_session(self, owner_key, title="New chat", source="parsed", tenant_id=None):
            calls.append(("create", owner_key, title, source, tenant_id))
            return {"id": "s2", "broker_phone": owner_key}

        def get_chat_session(self, session_id, tenant_id=None):
            calls.append(("get", session_id, tenant_id))
            return {"id": session_id, "broker_phone": "user:u1"}

        def get_ai_chat_messages(self, session_id, limit=200, tenant_id=None):
            calls.append(("messages", session_id, limit, tenant_id))
            return []

        def delete_chat_session(self, session_id, tenant_id=None):
            calls.append(("delete", session_id, tenant_id))

    fake = FakeStorage()
    monkeypatch.setattr(_common, "storage", fake)
    monkeypatch.setattr(ai_chat, "storage", fake)

    user = {"id": "u1", "phone": "919999999999"}
    asyncio.run(ai_chat.list_chat_sessions(user=user, tenant_id="org-A"))
    asyncio.run(ai_chat.create_chat_session(title="t", user=user, tenant_id="org-A"))
    asyncio.run(ai_chat.get_chat_session_messages(session_id="s1", user=user, tenant_id="org-A"))
    asyncio.run(ai_chat.delete_chat_session(session_id="s1", user=user, tenant_id="org-A"))

    # Sessions are owned by the authenticated user key, not a raw phone.
    assert ("list", "user:u1", 50, "org-A") in calls
    assert ("create", "user:u1", "t", "parsed", "org-A") in calls
    assert ("get", "s1", "org-A") in calls
    assert ("messages", "s1", 200, "org-A") in calls
    assert ("delete", "s1", "org-A") in calls
    tenant_forwarding = [c for c in calls if c[0] in {"list", "create", "get", "messages", "delete"}]
    assert all(c[-1] == "org-A" for c in tenant_forwarding)


def test_ai_chat_persist_uses_tenant(monkeypatch):
    import routers.ai_chat as ai_chat
    import routers.common as _common

    calls = []

    class RecordingStorage:
        """Satisfy the whole chat flow while recording tenant kwargs."""

        def __getattr__(self, name):
            def _record(*args, **kwargs):
                calls.append((name, args, kwargs))
                if name == "get_user_organizations":
                    return [{"id": "org-A"}]
                if name == "get_user_profile":
                    return {"phone": "919999999999"}
                if name == "get_chat_session":
                    session_id = args[0] if args else kwargs.get("session_id")
                    return {"id": session_id, "broker_phone": "user:u"}
                if name in {"get_ai_chat_messages", "list_chat_sessions"}:
                    return []
                if name == "get_workspace_ai_settings":
                    return {}
                return None

            return _record

    fake = RecordingStorage()
    monkeypatch.setattr(_common, "storage", fake)
    monkeypatch.setattr(ai_chat, "storage", fake)
    monkeypatch.setattr(
        ai_chat.chat_engine,
        "get_conversational_reply",
        lambda *args, **kwargs: type("Reply", (), {"content": "Hello"})(),
    )

    class FakeReq:
        session_id = "s1"
        broker_phone = "919999999999"
        model = ""
        api_key = None
        source = "chat"
        persist_user_turn = True
        messages = [{"role": "user", "content": "hi"}]

    asyncio.run(ai_chat.ai_chat(FakeReq(), user={"id": "u"}, tenant_id="org-A"))

    def forwarded(name):
        return [
            entry for entry in calls
            if entry[0] == name and _tenant_of(entry) == "org-A"
        ]

    def _tenant_of(entry):
        _, args, kwargs = entry
        if kwargs.get("tenant_id") is not None:
            return kwargs["tenant_id"]
        for value in args:
            if value == "org-A":
                return value
        return None

    assert forwarded("add_chat_message_if_new"), "user turn not persisted under tenant"
    assert forwarded("touch_chat_session"), "session not touched under tenant"
    assert forwarded("update_chat_session_title"), "title not updated under tenant"
    assert any(
        entry[0] == "get_ai_chat_messages" and (entry[2].get("limit") == 3)
        for entry in forwarded("get_ai_chat_messages")
    ), "title probe did not run under tenant"


def test_profile_endpoints_forward_tenant(monkeypatch):
    import routers.auth_org as _auth
    import app  # noqa: F401 — wiring side effects

    calls = []

    class FakeStorage:
        def get_user_profile(self, phone="", auth_user_id="", tenant_id=None):
            calls.append(("get", phone, auth_user_id, tenant_id))
            return {"phone": phone, "auth_user_id": auth_user_id, "tenant_id": tenant_id}

        def save_user_profile(self, phone, data, auth_user_id="", tenant_id=None):
            calls.append(("save", phone, auth_user_id, tenant_id))
            return {"phone": phone, "auth_user_id": auth_user_id, "tenant_id": tenant_id}

    monkeypatch.setattr(_auth, "storage", FakeStorage())

    asyncio.run(_auth.get_profile(user={"id": "u1"}, tenant_id="org-A"))
    asyncio.run(_auth.save_profile(
        body=type("B", (), {"model_dump": lambda self: {}})(),
        user={"id": "u1", "phone": "919999999999"},
        tenant_id="org-A",
    ))

    assert ("get", "", "u1", "org-A") in calls
    assert ("save", "919999999999", "u1", "org-A") in calls


def test_user_profile_save_uses_auth_user_id_globally(monkeypatch):
    from storage.supabase import SupabaseStorage
    from types import MethodType

    calls = []

    class FakeQuery:
        def __init__(self, table):
            self.table = table
            self.filters = []
            self.payload = None

        def update(self, payload):
            self.payload = payload
            return self

        def eq(self, key, value):
            self.filters.append((key, value))
            return self

        def execute(self):
            calls.append((self.table, tuple(self.filters), dict(self.payload or {})))
            return type("R", (), {"data": [{"id": 1, **(self.payload or {})}]})()

    class FakeClient:
        def table(self, name):
            return FakeQuery(name)

    s = object.__new__(SupabaseStorage)
    s._client = FakeClient()
    s._SupabaseStorage__tenant_id_fallback = None

    def fake_get_user_profile(self, phone="", auth_user_id="", tenant_id=None):
        calls.append(("lookup", phone, auth_user_id, tenant_id))
        if auth_user_id == "u1":
            return {
                "id": 1,
                "phone": "919999999999",
                "auth_user_id": "u1",
                "tenant_id": "org-B",
            }
        return None

    s.get_user_profile = MethodType(fake_get_user_profile, s)

    result = s.save_user_profile(
        "",
        {"first_name": "A", "last_name": "B", "email": "a@example.com", "city": "Mumbai"},
        auth_user_id="u1",
        tenant_id="org-A",
    )

    assert result["auth_user_id"] == "u1"
    assert ("lookup", "", "u1", None) in calls
    assert any(entry[0] == "user_profiles" and ("auth_user_id", "u1") in entry[1] for entry in calls if len(entry) == 3)
    assert all(("tenant_id", "org-A") not in entry[1] for entry in calls if len(entry) == 3 and entry[0] == "user_profiles")
    update_call = next(entry for entry in calls if len(entry) == 3 and entry[0] == "user_profiles")
    assert update_call[2]["phone"] == "919999999999"
    assert update_call[2]["tenant_id"] == "org-B"


def test_user_profile_auth_lookup_falls_back_across_active_workspace():
    from storage.supabase import SupabaseStorage

    calls = []

    class FakeQuery:
        def __init__(self):
            self.filters = []

        def select(self, _fields):
            return self

        def eq(self, key, value):
            self.filters.append((key, value))
            return self

        def limit(self, _value):
            return self

        def execute(self):
            calls.append(tuple(self.filters))
            if ("tenant_id", "org-current") in self.filters:
                return type("R", (), {"data": []})()
            return type("R", (), {"data": [{"auth_user_id": "u1", "tenant_id": "org-original", "city": "Mumbai"}]})()

    class FakeClient:
        def table(self, name):
            assert name == "user_profiles"
            return FakeQuery()

    storage = object.__new__(SupabaseStorage)
    storage._client = FakeClient()
    storage._SupabaseStorage__tenant_id_fallback = None

    profile = storage.get_user_profile(auth_user_id="u1", tenant_id="org-current")

    assert profile["city"] == "Mumbai"
    assert calls == [
        (("auth_user_id", "u1"), ("tenant_id", "org-current")),
        (("auth_user_id", "u1"),),
    ]


def test_saved_inbox_views_forward_tenant(monkeypatch):
    import app  # noqa: F401 — wiring side effects

    calls = []

    class FakeStorage:
        def get_saved_inbox_views(self, tenant_id=None):
            calls.append(("list", tenant_id))
            return []

        def get_saved_inbox_view(self, slug, tenant_id=None):
            calls.append(("get", slug, tenant_id))
            return {}

        def create_saved_inbox_view(self, slug, name, filters, description="", is_default=False, is_shared=False, tenant_id=None):
            calls.append(("create", slug, name, tenant_id))
            return 1

        def update_saved_inbox_view(self, slug, name=None, filters=None, description=None, is_default=None, is_shared=None, tenant_id=None):
            calls.append(("update", slug, tenant_id))
            return True

        def delete_saved_inbox_view(self, slug, tenant_id=None):
            calls.append(("delete", slug, tenant_id))
            return True

    import routers.workspace as _ws
    monkeypatch.setattr(_ws, "storage", FakeStorage())

    from routers.workspace import (
        get_saved_inbox_views, get_saved_inbox_view,
        create_saved_inbox_view, update_saved_inbox_view,
        delete_saved_inbox_view,
    )

    asyncio.run(get_saved_inbox_views(user={"id": "u"}, tenant_id="org-A"))
    asyncio.run(get_saved_inbox_view(slug="v", user={"id": "u"}, tenant_id="org-A"))
    asyncio.run(create_saved_inbox_view(user={"id": "u"}, tenant_id="org-A", slug="v", name="n", filters={}))
    asyncio.run(update_saved_inbox_view(user={"id": "u"}, tenant_id="org-A", slug="v"))
    asyncio.run(delete_saved_inbox_view(slug="v", user={"id": "u"}, tenant_id="org-A"))

    assert all(c[-1] == "org-A" for c in calls)
    assert ("list", "org-A") in calls
    assert ("get", "v", "org-A") in calls
    assert ("create", "v", "n", "org-A") in calls
    assert ("update", "v", "org-A") in calls
    assert ("delete", "v", "org-A") in calls


def test_llm_providers_forward_tenant(monkeypatch):
    import app  # noqa: F401 — wiring side effects

    calls = []

    class FakeStorage:
        def get_llm_providers(self, tenant_id=None):
            calls.append(("list", tenant_id))
            return []

        def get_active_llm_provider(self, tenant_id=None):
            calls.append(("active", tenant_id))
            return None

        def save_llm_provider(self, provider, tenant_id=None):
            calls.append(("save", tenant_id))
            return 1

        def delete_llm_provider(self, provider_id, tenant_id=None):
            calls.append(("delete", provider_id, tenant_id))
            return True

    import routers.workspace as _ws
    monkeypatch.setattr(_ws, "storage", FakeStorage())

    from routers.workspace import (
        list_llm_providers, get_active_llm_provider,
        save_llm_provider, delete_llm_provider,
    )

    asyncio.run(list_llm_providers(user={"id": "u"}, tenant_id="org-A"))
    asyncio.run(get_active_llm_provider(user={"id": "u"}, tenant_id="org-A"))
    asyncio.run(save_llm_provider(body={"provider_name": "p"}, user={"id": "u"}, tenant_id="org-A"))
    asyncio.run(delete_llm_provider(provider_id=5, user={"id": "u"}, tenant_id="org-A"))

    assert ("list", "org-A") in calls
    assert ("active", "org-A") in calls
    assert ("save", "org-A") in calls
    assert ("delete", 5, "org-A") in calls


def test_storage_scopes_chat_tables_by_tenant():
    """The adapter must scope every read/write on chat tables by tenant_id."""
    from storage.supabase import SupabaseStorage, set_tenant_id, LLMProvider

    execs = []
    writes = []

    class FakeQuery:
        def __init__(self, table, op=""):
            self.table = table
            self.op = op
            self.filters = []
        def select(self, *a, **k):
            self.op = "select"
            return self
        def insert(self, payload):
            self.op = "insert"
            writes.append((self.table, payload))
            return self
        def update(self, payload):
            self.op = "update"
            writes.append((self.table, payload))
            return self
        def delete(self):
            self.op = "delete"
            return self
        def eq(self, col, val):
            self.filters.append((col, val))
            return self
        def order(self, *a, **k):
            return self
        def limit(self, *a, **k):
            return self
        def execute(self):
            if self.op in ("select", "delete", "update"):
                execs.append((self.table, self.filters))
            return type("R", (), {"data": []})()

    class FakeClient:
        def table(self, name):
            return FakeQuery(name)

    s = object.__new__(SupabaseStorage)
    s._client = FakeClient()
    s._SupabaseStorage__tenant_id_fallback = None
    set_tenant_id("org-A")
    try:
        s.list_chat_sessions("919999999999", tenant_id="org-A")
        s.create_chat_session("919999999999", title="t", tenant_id="org-A")
        s.get_chat_session("s1", tenant_id="org-A")
        s.get_ai_chat_messages("s1", tenant_id="org-A")
        s.add_chat_message("s1", "user", "hi", tenant_id="org-A")
        s.delete_chat_session("s1", tenant_id="org-A")
        s.get_llm_providers(tenant_id="org-A")
        s.get_active_llm_provider(tenant_id="org-A")
        s.save_llm_provider(
            LLMProvider(id=0, provider_name="p", provider_type="openai",
                        api_key="", base_url="", model_name="", is_active=0),
            tenant_id="org-A",
        )
        s.get_saved_inbox_views(tenant_id="org-A")
        s.create_saved_inbox_view("v", "n", {}, tenant_id="org-A")
        s.get_user_profile("919999999999", tenant_id="org-A")
        s.save_user_profile("919999999999", {}, auth_user_id="u", tenant_id="org-A")

        # user_profiles is intentionally NOT tenant-scoped: profiles are
        # identity-owned across workspaces (see the cross-workspace fallback
        # and save tests below), so only phone/auth filters apply there.
        scoped = ("ai_chat_sessions", "ai_chat_messages",
                  "llm_providers", "saved_inbox_views")
        assert execs, "no read/delete queries ran"
        for table, filters in execs:
            if table in scoped:
                assert ("tenant_id", "org-A") in filters, f"{table} missing tenant filter: {filters}"
        for table, payload in writes:
            if table in scoped:
                assert isinstance(payload, dict) and payload.get("tenant_id") == "org-A", \
                    f"{table} insert/update missing tenant_id in payload: {payload}"
    finally:
        set_tenant_id(None)
