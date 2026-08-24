import asyncio
from types import SimpleNamespace

from routers import phone_directory as directory


def test_directory_add_request_accepts_blank_optional_label():
    request = directory.DirectoryAddRequest(
        phone_number="+919820056180",
        display_label=None,
    )

    assert request.display_label is None


def test_list_directory_returns_frontend_count_contract(monkeypatch):
    async def allow(*_args, **_kwargs):
        return "org-1"

    monkeypatch.setattr(directory, "_require_org_permission", allow)
    monkeypatch.setattr(directory, "_request_organization_id", allow)
    monkeypatch.setattr(
        directory,
        "storage",
        SimpleNamespace(
            list_org_whatsapp_phone_directory=lambda _org_id: [
                {
                    "id": "entry-1",
                    "broker_id": "broker-1",
                    "phone_number": "919820056180",
                    "display_label": "",
                    "is_active": True,
                }
            ],
            is_super_admin=lambda _user_id: False,
        ),
    )

    result = asyncio.run(
        directory.list_directory(
            "org-1",
            user={"id": "user-1"},
            tenant_id="org-1",
        )
    )

    assert result["cap"] == 3
    assert result["used"] == 1
    assert result["entries"][0]["phone_number"] == "919820056180"


def test_validate_phone_string_accepts_uae_formats():
    from fastapi import HTTPException

    # All common UAE presentations canonicalise to 971-prefixed digits.
    for value in ("+971501234567", "971501234567", "0501234567", "501234567"):
        assert directory._validate_phone_string(value) == "971501234567"

    # Indian formats keep their existing canonical form.
    assert directory._validate_phone_string("9820056180") == "919820056180"
    assert directory._validate_phone_string("+91 98200 56180") == "919820056180"

    for bad in ("12345", "0221234567", "97150", "abcdefghij"):
        try:
            directory._validate_phone_string(bad)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError(f"{bad!r} should have been rejected")


def test_normalize_phone_dedupe_key_ignores_formatting():
    uae = {directory._normalize_phone(v) for v in ("+971501234567", "971501234567", "0501234567", "501234567")}
    assert len(uae) == 1
    indian = {directory._normalize_phone(v) for v in ("+919820056180", "919820056180", "9820056180")}
    assert len(indian) == 1
