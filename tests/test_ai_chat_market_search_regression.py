import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_plain_inventory_query_uses_grounded_market_search(monkeypatch):
    import routers.ai_chat as ai_chat
    import agent_tools

    calls = []
    monkeypatch.setattr(
        ai_chat,
        "storage",
        SimpleNamespace(
            db=None,
            client=object(),
            get_workspace_ai_settings=lambda tenant_id: {},
        ),
    )
    
    async def _to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr(ai_chat, "_resolve_active_organization_id", lambda user, tenant_id: tenant_id)
    monkeypatch.setattr(ai_chat, "_workspace_provider_candidates", lambda tenant_id, model: [{"api_key": "k", "model": "m", "base_url": "https://example.invalid/v1"}])
    monkeypatch.setattr(ai_chat.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(ai_chat, "_wrap_chat_response", lambda response, is_inbox=False: response)
    monkeypatch.setattr(ai_chat.chat_engine, "load_data", lambda: {"overview": {"total_listings": 1}, "unique_listings": {"items": []}, "buildings": {}, "brokers": {}})
    monkeypatch.setattr(ai_chat.chat_engine, "load_live_data", lambda db: {})
    monkeypatch.setattr(
        ai_chat.chat_engine,
        "parse_market_search_request",
        lambda *args, **kwargs: {"intent": "RENT", "bhk": 3, "micro_markets": ["JVC"]},
    )
    monkeypatch.setattr(ai_chat, "_run_with_provider_failover", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM fallback should not run")))
    monkeypatch.setattr(
        agent_tools,
        "execute_tool",
        lambda tool_name, tool_args, client, tenant_id, **kwargs: calls.append(
            (tool_name, tool_args, tenant_id)
        ) or {
            "status": "ok",
            "results": [{
                "listing_id": 1,
                "building_name": "Marina Sail",
                "micro_market": "JVC",
                "bhk": "3",
                "price": 95000,
                "carpet_area_sqft": 1200,
                "broker_name": "Ravi",
            }],
        },
    )

    req = SimpleNamespace(
        session_id=None,
        source="groups",
        broker_phone="",
        model="",
        api_key="",
        persist_user_turn=False,
        messages=[{"role": "user", "content": "any 3 bhk for rent in JVC"}],
    )

    response = asyncio.run(ai_chat.ai_chat(req, user={"id": "u1"}, tenant_id="org-1"))

    assert calls, "search_listings was not called"
    assert calls[0][0] == "search_listings"
    assert calls[0][1]["bhk"] == 3
    assert calls[0][1]["listing_type"] == "rent"
    assert calls[0][1]["locality"] == "JVC"
    assert calls[0][2] == "org-1"
    assert ai_chat._is_analytics_or_ops_query("how many listings in Dubai Marina") is True
    assert ai_chat._is_analytics_or_ops_query("any 3 bhk for rent in JVC") is False
    assert response["trace"]["route"] == "deterministic_market_search"
    assert response["trace"]["inventory_scope"] == "shared_network"
    assert response["content"].startswith("Found 1 active match")
    assert "WhatsApp group" not in response["content"]


def test_conceptual_sale_rent_question_is_conversational():
    import routers.ai_chat as ai_chat

    assert ai_chat._is_conversational_explanation(
        "Do you know the difference between sale and rent?"
    ) is True
    assert ai_chat._is_conversational_explanation(
        "show me 3 bhk for rent in Bandra West"
    ) is False
