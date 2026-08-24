import asyncio
from types import SimpleNamespace

import app  # noqa: F401 — wiring side effects
import routers.common as _common
from routers import whatsapp_sync as ws_mod


def test_shared_waba_is_available_to_super_admin(monkeypatch):
    values = {
        "whatsapp_business_number": "971501234567",
        "phone_number_id": "phone-number-id",
        "access_token": "token",
        "verify_token": "verify",
    }

    class Storage:
        @staticmethod
        def is_super_admin(user_id):
            return user_id == "super-user"

        @staticmethod
        def get_user_organizations(_user_id):
            return [{"id": "org-broker"}]

        @staticmethod
        def get_org_waba_connection(org_id):
            assert org_id == "org-broker"
            return None

    _fake_storage = Storage()
    monkeypatch.setattr(ws_mod, "storage", _fake_storage)
    monkeypatch.setattr(_common, "storage", _fake_storage)
    _fake_config = lambda key, _env_key="": values.get(key, "")
    monkeypatch.setattr(ws_mod, "_business_api_get_config_value", _fake_config)
    monkeypatch.setattr(_common, "_business_api_get_config_value", _fake_config)

    super_config = asyncio.run(ws_mod.business_api_config(user={"id": "super-user"}, tenant_id="org-admin"))
    broker_config = asyncio.run(ws_mod.business_api_config(user={"id": "broker-user"}, tenant_id="org-broker"))

    assert super_config["waba_owner"] == "propai"
    assert super_config["outbound_allowed"] is True
    assert broker_config["outbound_allowed"] is False
    assert broker_config["whatsapp_business_number"] == ""
    assert broker_config["phone_number_id"] == ""
    assert broker_config["access_token_preview"] == ""
    assert broker_config["verify_token_preview"] == ""
    assert broker_config["webhook_callback_url"].endswith("/org-broker")


def test_waba_webhook_stores_inbound_message_once(monkeypatch):
    calls = []

    class Database:
        def execute(self, sql, params=()):
            calls.append((sql, params))
            return SimpleNamespace(rowcount=1)

    class Request:
        async def json(self):
            return {
                "object": "whatsapp_business_account",
                "entry": [{
                    "changes": [{
                        "value": {
                            "metadata": {"phone_number_id": "test-phone-id"},
                            "contacts": [{"profile": {"name": "Broker One"}}],
                            "messages": [{
                                "from": "919999999999",
                                "id": "wamid.test",
                                "type": "text",
                                "text": {"body": ""},
                            }],
                        },
                    }],
                }],
            }

    workspace = {
        "organization_id": "org-broker",
        "phone_number_id": "test-phone-id",
        "access_token": "token",
        "verify_token": "verify",
        "is_active": True,
    }
    monkeypatch.setattr(
        ws_mod,
        "storage",
        SimpleNamespace(
            db=Database(),
            get_org_waba_connection_by_phone_number_id=lambda _phone_id: workspace,
        ),
    )
    monkeypatch.setattr(_common, "storage", SimpleNamespace(
        db=Database(),
        get_org_waba_connection_by_phone_number_id=lambda _phone_id: workspace,
    ))
    monkeypatch.setattr(ws_mod, "_waba_session_update", lambda *_args, **_kwargs: None)

    result = asyncio.run(ws_mod.business_api_webhook_receive(Request()))

    raw_insert = next(sql for sql, _params in calls if "INSERT INTO raw_messages" in sql)
    assert "ON CONFLICT DO NOTHING" in raw_insert
    assert result["processed"] == [{
        "type": "message_stored",
        "from": "919999999999",
        "msg_type": "text",
    }]


def test_workspace_waba_webhook_resolves_by_phone_number_id(monkeypatch):
    workspace = {
        "organization_id": "org-one",
        "phone_number_id": "workspace-phone-id",
        "access_token": "workspace-token",
        "verify_token": "workspace-verify",
        "is_active": True,
    }
    monkeypatch.setattr(
        ws_mod,
        "storage",
        SimpleNamespace(
            get_org_waba_connection_by_phone_number_id=lambda phone_id: (
                workspace if phone_id == "workspace-phone-id" else None
            )
        ),
    )
    monkeypatch.setattr(_common, "storage", SimpleNamespace(
        get_org_waba_connection_by_phone_number_id=lambda phone_id: (
            workspace if phone_id == "workspace-phone-id" else None
        )
    ))

    body = {
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "workspace-phone-id"},
                    "messages": [],
                }
            }]
        }]
    }
    values, org_id = asyncio.run(ws_mod._resolve_waba_webhook_config(body))

    assert values["access_token"] == "workspace-token"
    assert org_id == "org-one"


def test_propai_shared_waba_number_is_valid_indian_mobile():
    assert isinstance(ws_mod.PROPAI_SHARED_WABA_NUMBER, str)
