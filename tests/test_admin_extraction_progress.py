"""Super-admin extraction progress endpoint — auth gate + response shape."""
import asyncio

import app  # noqa: F401 — wiring side effects
import routers.common as _common
from routers import admin as admin_mod


class FakeQuery:
    def __init__(self, count, data=None):
        self._count = count
        self._data = data or []

    def eq(self, *a, **k):
        return self

    def not_(self, *a, **k):
        return self

    def is_(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        from types import SimpleNamespace
        return SimpleNamespace(count=self._count, data=self._data)


class FakeTable:
    def __init__(self, query):
        self._query = query

    def select(self, *a, **k):
        return self._query


class Storage:
    def __init__(self, progress):
        self._progress = progress

    @staticmethod
    def is_super_admin(user_id):
        return user_id == "super-user"

    def get_extraction_progress(self, rate_window_hours=24, tenant_id=None):
        return {**self._progress, "rate_window_hours": rate_window_hours}

    def client(self):
        return self

    def table(self, _name):
        return FakeTable(FakeQuery(count=100))


PROGRESS = {
    "total_raw_messages": 780_000,
    "unprocessed": 779_872,
    "processed": 128,
    "stuck": 0,
    "extraction_cache_rows": 42,
    "processed_recent_24h": 270,
    "ai_calls": 516,
    "est_cost_usd": 0.407,
    "percent_drained": 0.02,
}


def test_extraction_progress_rejects_non_super_admin(monkeypatch):
    fake = Storage(PROGRESS)
    monkeypatch.setattr(admin_mod, "storage", fake)
    monkeypatch.setattr(_common, "storage", fake)

    try:
        asyncio.run(admin_mod.admin_extraction_progress(user={"id": "normal-user"}))
        raise AssertionError("expected HTTPException 403")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403


def test_extraction_progress_returns_live_counts_for_super_admin(monkeypatch):
    fake = Storage(PROGRESS)
    monkeypatch.setattr(admin_mod, "storage", fake)
    monkeypatch.setattr(_common, "storage", fake)

    out = asyncio.run(admin_mod.admin_extraction_progress(user={"id": "super-user"}))
    assert out["total_raw_messages"] == 780_000
    assert out["unprocessed"] == 779_872
    assert out["processed"] == 128
    assert out["extraction_cache_rows"] == 42
    assert out["est_cost_usd"] == 0.407
    assert out["percent_drained"] == 0.02
    assert out["rate_window_hours"] == 24


def test_extraction_progress_clamps_window(monkeypatch):
    fake = Storage(PROGRESS)
    monkeypatch.setattr(admin_mod, "storage", fake)
    monkeypatch.setattr(_common, "storage", fake)

    out = asyncio.run(admin_mod.admin_extraction_progress(hours=9999, user={"id": "super-user"}))
    assert out["rate_window_hours"] == 168
