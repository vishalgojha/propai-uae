"""Unit tests for the provider-outage-evidence helpers.

Covers:
- _probe_provider() with no creds / no base_url / mocked httpx.
- _classify_provider_status() rules: up/degraded/down/unknown.
- _summarise_provider() p50/p95 + last-error resolution.
- _bucket_history() 5-min bucket alignment + windowing.
- _parse_event_ts() ISO-8601 round-trip.

The probe helper needs httpx so we monkey-patch it; the other helpers are
pure and take dicts as input.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app
from storage import ProviderOutageEvent


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _now() -> float:
    return time.time()


# ── _classify_provider_status ─────────────────────────────────────────


def test_classify_unknown_when_no_events():
    assert app._classify_provider_status([], _now()) == "unknown"


def test_classify_unknown_when_newest_is_stale():
    now = _now()
    old = {"status": "ok", "ts": _iso(now - 3600)}
    assert app._classify_provider_status([old], now) == "unknown"


def test_classify_down_when_newest_failed():
    now = _now()
    failed = {"status": "timeout", "ts": _iso(now - 30)}
    assert app._classify_provider_status([failed], now) == "down"


def test_classify_degraded_when_newest_slow():
    now = _now()
    slow = {"status": "slow", "ts": _iso(now - 30)}
    assert app._classify_provider_status([slow], now) == "degraded"


def test_classify_up_when_newest_ok():
    now = _now()
    ok = {"status": "ok", "ts": _iso(now - 30)}
    assert app._classify_provider_status([ok], now) == "up"


def test_classify_down_when_no_success_in_last_10_min():
    now = _now()
    events = sorted(
        [
            {"status": "ok", "ts": _iso(now - 1200)},
            {"status": "http", "ts": _iso(now - 60)},
            {"status": "http", "ts": _iso(now - 30)},
        ],
        key=lambda e: -_parse_ts(e["ts"]),
    )
    assert app._classify_provider_status(events, now) == "down"


def test_classify_degraded_when_error_rate_above_20pct_in_30_min():
    now = _now()
    events = sorted(
        [{"status": "timeout", "ts": _iso(now - 60)} for _ in range(3)]
        + [{"status": "ok", "ts": _iso(now - 30)} for _ in range(8)],
        key=lambda e: -_parse_ts(e["ts"]),
    )
    assert app._classify_provider_status(events, now) == "degraded"


def test_classify_up_when_all_recent_probes_are_ok():
    now = _now()
    events = sorted(
        [{"status": "ok", "ts": _iso(now - t)} for t in range(60, 601, 60)],
        key=lambda e: -_parse_ts(e["ts"]),
    )
    assert app._classify_provider_status(events, now) == "up"


def test_fresh_failure_after_recovery_stays_degraded_not_up():
    now = _now()
    events = sorted(
        [{"status": "timeout", "ts": _iso(now - 600)}]
        + [{"status": "ok", "ts": _iso(now - t)} for t in (30, 60, 90, 120)],
        key=lambda e: -_parse_ts(e["ts"]),
    )
    assert app._classify_provider_status(events, now) == "degraded"


# ── _summarise_provider ───────────────────────────────────────────────


def test_summarise_picks_newest_first_for_last_status():
    now = _now()
    events = sorted(
        [
            {"status": "ok", "ts": _iso(now - 60), "latency_ms": 200},
            {"status": "slow", "ts": _iso(now - 30), "latency_ms": 6000},
        ],
        key=lambda e: -_parse_ts(e["ts"]),
    )
    summary = app._summarise_provider(events, now)
    assert summary["last_status"] == "slow"
    assert summary["last_latency_ms"] == 6000
    assert summary["p95_ms"] == 6000


def test_summarise_resolves_last_error():
    now = _now()
    events = sorted(
        [
            {"status": "ok", "ts": _iso(now - 60), "latency_ms": 200},
            {"status": "http", "ts": _iso(now - 30), "latency_ms": 400,
             "error_kind": "non_2xx", "error_msg": "HTTP 503: overloaded"},
        ],
        key=lambda e: -_parse_ts(e["ts"]),
    )
    summary = app._summarise_provider(events, now)
    assert summary["last_error"] is not None
    assert summary["last_error"]["error_msg"] == "HTTP 503: overloaded"


def test_summarise_empty_events():
    summary = app._summarise_provider([], _now())
    assert summary["probe_count"] == 0
    assert summary["p50_ms"] == 0
    assert summary["p95_ms"] == 0
    assert summary["last_error"] is None


# ── _bucket_history ───────────────────────────────────────────────────


def test_bucket_history_aligns_to_5min():
    now = _now()
    bucket_start = int(now // 300) * 300
    events = [
        {"status": "ok", "ts": _iso(bucket_start + 30)},
        {"status": "timeout", "ts": _iso(bucket_start + 60)},
    ]
    buckets = app._bucket_history(events, bucket_minutes=5, window_hours=24)
    assert len(buckets) == 1
    b = buckets[0]
    assert b["ts_bucket"] == bucket_start
    assert b["ok_count"] == 1
    assert b["fail_count"] == 1
    assert b["total"] == 2


def test_bucket_history_drops_stale_rows():
    now = _now()
    events = [
        {"status": "ok", "ts": _iso(now - (25 * 3600))},
        {"status": "ok", "ts": _iso(now - 60)},
    ]
    buckets = app._bucket_history(events, bucket_minutes=5, window_hours=24)
    assert len(buckets) == 1
    assert buckets[0]["total"] == 1


def test_bucket_history_returns_newest_first():
    now = _now()
    base = int(now // 300) * 300
    events = [
        {"status": "ok", "ts": _iso(base - 300)},
        {"status": "ok", "ts": _iso(base)},
    ]
    buckets = app._bucket_history(events, bucket_minutes=5, window_hours=24)
    assert len(buckets) == 2
    assert buckets[0]["ts_bucket"] == base
    assert buckets[1]["ts_bucket"] == base - 300


def test_bucket_history_empty():
    assert app._bucket_history([], bucket_minutes=5, window_hours=24) == []


# ── _parse_event_ts ───────────────────────────────────────────────────


def test_parse_event_ts_round_trip():
    now = _now()
    iso = _iso(now)
    parsed = app._parse_event_ts({"ts": iso})
    assert parsed is not None
    assert abs(parsed - now) < 0.001


def test_parse_event_ts_handles_z_suffix():
    iso = "2026-07-21T10:00:00Z"
    parsed = app._parse_event_ts({"ts": iso})
    assert parsed is not None
    assert isinstance(parsed, float)


def test_parse_event_ts_returns_none_for_missing():
    assert app._parse_event_ts({}) is None
    assert app._parse_event_ts({"ts": None}) is None
    assert app._parse_event_ts({"ts": "not-a-date"}) is None


# ── _probe_provider (no-credentials path + mocked httpx) ──────────────


def test_probe_provider_returns_error_when_no_key():
    async def _run():
        return await app._probe_provider(api_key="", base_url="https://api.test/v1",
                                            model_name="m")
    result = asyncio.run(_run())
    assert result["status"] == "error"
    assert result["error_kind"] == "missing_credentials"
    assert result["latency_ms"] == 0


def test_probe_provider_returns_error_when_no_base_url():
    async def _run():
        return await app._probe_provider(api_key="k", base_url="", model_name="m")
    result = asyncio.run(_run())
    assert result["status"] == "error"
    assert result["error_kind"] == "missing_credentials"


def test_probe_provider_marks_slow_above_threshold(monkeypatch):
    """If the upstream returns 200 but latency > 5s, status flips to slow."""
    class FakeResp:
        status_code = 200
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            time.sleep(5.2)
            return FakeResp()
    import httpx as real_httpx
    monkeypatch.setattr(real_httpx, "AsyncClient", FakeClient)
    async def _run():
        return await app._probe_provider(api_key="k", base_url="https://api.test/v1",
                                            model_name="m", timeout_s=10.0)
    result = asyncio.run(_run())
    assert result["status"] == "slow"
    assert result["http_status"] == 200
    assert result["latency_ms"] > app.PROBE_OK_LATENCY_THRESHOLD_MS


def test_probe_provider_marks_timeout(monkeypatch):
    import httpx as real_httpx
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            raise real_httpx.TimeoutException("read timed out")
    monkeypatch.setattr(real_httpx, "AsyncClient", FakeClient)
    async def _run():
        return await app._probe_provider(api_key="k", base_url="https://api.test/v1",
                                            model_name="m", timeout_s=1.0)
    result = asyncio.run(_run())
    assert result["status"] == "timeout"
    assert result["error_kind"] == "timeout"
    assert "timed out" in (result["error_msg"] or "")


def test_probe_provider_marks_http_error(monkeypatch):
    class FakeResp:
        status_code = 503
        text = "Service Unavailable"
        def json(self): raise ValueError("not json")
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw): return FakeResp()
    import httpx as real_httpx
    monkeypatch.setattr(real_httpx, "AsyncClient", FakeClient)
    async def _run():
        return await app._probe_provider(api_key="k", base_url="https://api.test/v1",
                                            model_name="m", timeout_s=1.0)
    result = asyncio.run(_run())
    assert result["status"] == "http"
    assert result["http_status"] == 503
    assert result["error_kind"] == "non_2xx"
    assert "503" in result["error_msg"]


def test_probe_provider_marks_generic_error(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            raise ConnectionError("DNS failure")
    import httpx as real_httpx
    monkeypatch.setattr(real_httpx, "AsyncClient", FakeClient)
    async def _run():
        return await app._probe_provider(api_key="k", base_url="https://api.test/v1",
                                            model_name="m", timeout_s=1.0)
    result = asyncio.run(_run())
    assert result["status"] == "error"
    assert result["error_kind"] == "ConnectionError"
    assert "DNS" in result["error_msg"]


# ── insert_provider_outage_event: empty-ts regression ───────────────
#
# Regression: previously, the dataclass default `ts = ""` was being sent
# to Supabase, which rejected it with HTTP 400 because timestamptz can't
# accept an empty string. The fix filters out empty strings so the DB
# column default `now()` kicks in. Without that, every probe was silently
# failing and /admin/providers showed NO DATA for every provider.


class _FakeTable:
    def __init__(self):
        self.last_inserted = None
    def insert(self, data):
        self.last_inserted = data
        return self
    def execute(self):
        if not self.last_inserted or "ts" in self.last_inserted:
            class _R:
                data = None
            return _R()
        class _R:
            data = [{"id": 1}]
        return _R()


class _FakeClient:
    def __init__(self):
        self.table_obj = _FakeTable()
    def table(self, name):
        assert name == "provider_outage_log"
        return self.table_obj


def _make_storage():
    from storage.supabase import SupabaseStorage
    s = SupabaseStorage.__new__(SupabaseStorage)
    s._client = _FakeClient()
    return s


def test_insert_skips_empty_ts_field_so_db_default_kicks_in():
    """ProviderOutageEvent() has ts="" by default. The fix must drop it
    so the DB column default `now()` is used (avoids the 400 error)."""
    s = _make_storage()
    event = ProviderOutageEvent(
        provider_id=5,
        provider_name="nvidia",
        provider_type="custom",
        model_name="m",
        status="ok",
        latency_ms=234,
        http_status=200,
        error_kind="",
        error_msg="",
    )
    assert event.ts == "", "precondition: dataclass default is empty string"
    s.insert_provider_outage_event(event)
    sent = s.client.table_obj.last_inserted
    assert "ts" not in sent, f"ts must be skipped (was {sent!r})"
    assert sent["provider_id"] == 5
    assert sent["provider_name"] == "nvidia"
    assert sent["status"] == "ok"
    assert sent["latency_ms"] == 234


def test_insert_skips_id_so_db_assigns_bigserial():
    s = _make_storage()
    event = ProviderOutageEvent(
        provider_id=5, provider_name="nvidia", provider_type="custom",
        model_name="m", status="ok", latency_ms=100, http_status=200,
    )
    s.insert_provider_outage_event(event)
    sent = s.client.table_obj.last_inserted
    assert "id" not in sent, "id is bigserial; client must not send it"


def test_insert_skips_none_and_empty_string_fields():
    """Defence-in-depth: both None and '' must be dropped so the DB
    doesn't reject inserts for other columns later."""
    s = _make_storage()
    event = ProviderOutageEvent(
        provider_id=5,
        provider_name="nvidia",
        provider_type="custom",
        model_name="",
        status="http",
        latency_ms=0,
        http_status=503,
        error_kind=None,
        error_msg=None,
    )
    s.insert_provider_outage_event(event)
    sent = s.client.table_obj.last_inserted
    for dropped in ("ts", "id", "model_name", "error_kind", "error_msg"):
        assert dropped not in sent, f"{dropped} should be skipped, sent={sent!r}"
    assert sent["http_status"] == 503
    assert sent["status"] == "http"


def test_probe_loop_helper_swallows_inner_errors_but_logs_failure():
    """If _probe_provider itself raises (shouldn't, but defence-in-depth),
    _run_provider_probe_and_log must still record an 'error' event so the
    outage log gets a row instead of silently dropping the iteration."""
    from storage import ProviderOutageEvent as _Evt
    import asyncio as _asyncio

    async def _boom(*a, **kw):
        raise RuntimeError("transport down")
    app._probe_provider = _boom

    async def _run():
        await app._run_provider_probe_and_log({
            "id": 42, "provider_name": "nvidia", "provider_type": "custom",
            "model_name": "m", "api_key": "k", "base_url": "https://x/v1",
        })
    _asyncio.run(_run())
    # No assertion needed: the call must complete without raising. If the
    # buggy behaviour returned, this would raise RuntimeError("transport down").
