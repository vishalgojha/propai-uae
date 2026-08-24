import asyncio
import pytest


def test_progress_rpc_failure_never_falls_back_to_exact_table_scans():
    from storage.supabase import SupabaseStorage

    class FailedRPC:
        def execute(self):
            raise RuntimeError("schema cache miss")

    class Client:
        def rpc(self, name, payload):
            assert name == "get_extraction_progress"
            assert payload["p_hours"] == 1
            return FailedRPC()

        def table(self, _name):
            raise AssertionError("exact-count fallback must never run")

    storage = object.__new__(SupabaseStorage)
    storage._client = Client()

    with pytest.raises(RuntimeError, match="Canonical extraction progress RPC"):
        storage.get_extraction_progress(1, "00000000-0000-0000-0000-000000000001")


def test_progress_rpc_accepts_single_row_jsonb_response():
    from storage.supabase import SupabaseStorage

    class Response:
        data = [{
            "total_raw_messages": 3,
            "processed": 2,
            "unprocessed": 1,
            "processed_recent": 1,
            "extraction_cache_rows": 4,
        }]

        def execute(self):
            return self

    class Client:
        def rpc(self, name, payload):
            assert name == "get_extraction_progress"
            assert payload["p_hours"] == 24
            return Response()

    storage = object.__new__(SupabaseStorage)
    storage._client = Client()

    result = storage.get_extraction_progress(24, "00000000-0000-0000-0000-000000000001")

    assert result["total_raw_messages"] == 3
    assert result["unprocessed"] == 1


def test_progress_rpc_accepts_propai_direct_rest_response():
    from storage.supabase import SupabaseStorage

    class Client:
        def rpc(self, name, payload):
            assert name == "get_extraction_progress"
            assert payload["p_hours"] == 24
            return {
                "total_raw_messages": 5,
                "processed": 5,
                "unprocessed": 0,
                "processed_recent": 2,
                "extraction_cache_rows": 7,
            }

    storage = object.__new__(SupabaseStorage)
    storage._client = Client()

    result = storage.get_extraction_progress(24, "00000000-0000-0000-0000-000000000001")

    assert result["processed"] == 5
    assert result["extraction_cache_rows"] == 7


def test_progress_endpoint_coalesces_concurrent_workspace_requests(monkeypatch):
    from routers import dashboard

    calls = 0

    class Storage:
        def get_workspace_extraction_progress(self, rate_window_hours=24, tenant_id=None):
            nonlocal calls
            calls += 1
            return {
                "total_raw_messages": 10,
                "processed": 7,
                "unprocessed": 3,
                "processed_recent": 2,
                "suppressed": 0,
            }

    monkeypatch.setattr(dashboard, "storage", Storage())
    async def inline_to_thread(function, *args, **kwargs):
        await asyncio.sleep(0.01)
        return function(*args, **kwargs)

    monkeypatch.setattr(dashboard.asyncio, "to_thread", inline_to_thread)
    dashboard._extraction_progress_cache.clear()

    async def run():
        return await asyncio.gather(
            dashboard.extraction_progress(user={"id": "u1"}, tenant_id="org-1"),
            dashboard.extraction_progress(user={"id": "u1"}, tenant_id="org-1"),
        )

    results = asyncio.run(run())

    assert calls == 1
    assert results[0] == results[1]
    assert results[0]["pending"] == 3
