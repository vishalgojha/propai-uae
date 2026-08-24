import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers import whatsapp_group_controls as onboarding


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._rows), count=len(self._rows))


class FakeSupabase:
    """Tiny in-memory stand-in. Always answers by AND-matching all filters
    we've set. Used only for the opted_out semantics tests below."""

    def __init__(self):
        self.rows = []

    def table(self, _name):
        outer = self

        class _T:
            def __init__(self):
                self._filters = {}

            def select(self, *_args, **_kwargs):
                return self

            def eq(self, column, value):
                self._filters[column] = value
                return self

            def limit(self, _n):
                return self

            def execute(self):
                matches = [
                    r for r in outer.rows
                    if all(r.get(k) == v for k, v in self._filters.items())
                ]
                return SimpleNamespace(data=matches, count=len(matches))

        return _T()


def _row(org_id, group_jid, group_name="G", opted_out=False, is_active=True):
    return {
        "organization_id": org_id,
        "whatsapp_connection_id": 1,
        "group_jid": group_jid,
        "group_name": group_name,
        "opted_out": opted_out,
        "is_active": is_active,
    }


def test_extraction_allowed_by_default(monkeypatch):
    fake = SimpleNamespace()
    storage = SimpleNamespace(client=FakeSupabase())
    storage.client.rows = []
    monkeypatch.setattr(onboarding, "storage", storage)

    assert onboarding.extraction_allowed_for_group("org-1", "12345@g.us", "Family Chat") is True


def test_extraction_blocked_for_opted_out_jid(monkeypatch):
    storage = SimpleNamespace(client=FakeSupabase())
    storage.client.rows = [_row("org-1", "12345@g.us", "Family Chat", opted_out=True)]
    monkeypatch.setattr(onboarding, "storage", storage)

    assert onboarding.extraction_allowed_for_group("org-1", "12345@g.us", "Family Chat") is False


def test_extraction_allowed_for_unrelated_groups(monkeypatch):
    storage = SimpleNamespace(client=FakeSupabase())
    storage.client.rows = [_row("org-1", "OTHER@g.us", "Other Group", opted_out=True)]
    monkeypatch.setattr(onboarding, "storage", storage)

    assert onboarding.extraction_allowed_for_group("org-1", "REAL@g.us", "Real Group") is True


def test_extraction_blocked_by_name_fallback(monkeypatch):
    """Older raw rows may not carry a JID. The name-fallback ensures the user
    opting-out by name still suppresses matching messages."""
    storage = SimpleNamespace(client=FakeSupabase())
    storage.client.rows = [_row("org-1", "", "Family Chat", opted_out=True)]
    monkeypatch.setattr(onboarding, "storage", storage)

    assert onboarding.extraction_allowed_for_group("org-1", "UNKNOWN@g.us", "Family Chat") is False


def test_broker_own_message_requires_selected_group(monkeypatch):
    """Deny-by-default: even the connected broker's own messages are ignored
    until the group is explicitly confirmed on the Connections screen."""
    storage = SimpleNamespace(client=FakeSupabase())
    storage.get_org_whatsapp_connection_by_broker_id = lambda _broker_id: {
        "id": 1,
        "organization_id": "org-1",
        "phone_number": "919773757759",
        "is_active": True,
    }
    monkeypatch.setattr(onboarding, "storage", storage)

    assert onboarding.extraction_allowed_for_group(
        "org-1",
        "unselected@g.us",
        "Unselected Group",
        "phone-1",
        message_from_me=True,
        sender_phone="919773757759@s.whatsapp.net",
    ) is False
