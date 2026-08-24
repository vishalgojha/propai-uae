import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# ai_chat_engine imports pandas for analytics helpers that these endpoint tests
# never call. Keep this focused regression runnable in the lightweight test env.
sys.modules.setdefault("pandas", ModuleType("pandas"))


def test_typed_requirement_contact_uses_source_identity_and_requirement_copy(monkeypatch):
    import routers.ai_chat as ai_chat

    calls = []

    def get_market_item_detail(item_id, source_schema, raw_message_id, tenant_id):
        calls.append((item_id, source_schema, raw_message_id, tenant_id))
        return {
            "id": item_id,
            "broker_phone": "+971 50 123 4567",
            "bhk": "1.0",
            "building_name": "2 Bathrooms",
            "micro_market": "Dubai Marina",
            "message_type": "requirement",
            "visibility": "shared_market",
            "tenant_id": "another-workspace",
        }

    async def to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(
        ai_chat,
        "storage",
        SimpleNamespace(get_market_item_detail=get_market_item_detail),
    )
    monkeypatch.setattr(ai_chat.asyncio, "to_thread", to_thread)

    response = asyncio.run(
        ai_chat.resolve_broker_contact(
            42,
            ai_chat.BrokerContactRequest(
                source_schema="residential_rent_requirements",
                raw_message_id=9001,
            ),
            user={"id": "user-1"},
            tenant_id="workspace-1",
        )
    )

    assert calls == [(42, "residential_rent_requirements", 9001, "workspace-1")]
    parsed = urlparse(response["contact_url"])
    assert parsed.netloc == "wa.me"
    assert parsed.path == "/971501234567"
    message = parse_qs(parsed.query)["text"][0]
    assert "your 1 BHK requirement" in message
    assert "still active" in message
    assert "1 BHK" in message
    assert "Dubai Marina" in message
    assert "Bathrooms" not in message


def test_workspace_private_contact_is_not_exposed_to_another_tenant(monkeypatch):
    import routers.ai_chat as ai_chat
    from fastapi import HTTPException

    async def to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(
        ai_chat,
        "storage",
        SimpleNamespace(
            get_market_item_detail=lambda *args: {
                "id": 7,
                "broker_phone": "9876543210",
                "message_type": "listing",
                "visibility": "workspace_private",
                "tenant_id": "workspace-owner",
            }
        ),
    )
    monkeypatch.setattr(ai_chat.asyncio, "to_thread", to_thread)

    try:
        asyncio.run(
            ai_chat.resolve_broker_contact(
                7,
                ai_chat.BrokerContactRequest(source_schema="commercial_sale_listings"),
                user={"id": "user-2"},
                tenant_id="other-workspace",
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("workspace-private broker contact was exposed cross-tenant")
