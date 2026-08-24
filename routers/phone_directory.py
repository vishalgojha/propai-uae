"""Phone directory — broker profile is the single source of truth for
WhatsApp numbers. Add/remove are restricted to this router; pair/reset/re-pair
stay on /api/phones/* in routers.whatsapp_sync. The directory also pre-reserves
an org_whatsapp_connections row and broker_id slot at insert time so adding a
phone here sets up the same downstream shape that Connections expects.
"""
import asyncio
import re
import uuid as _uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from routers.common import (
    storage,
    require_user,
    get_tenant_context,
    _resolve_active_organization_id,
    _require_org_permission,
)

router = APIRouter(tags=["phone_directory"])


_phone_re = re.compile(r"^\+?\d{9,15}$")


def _normalize_phone(value: object) -> str:
    """Format-insensitive dedupe key: drop country code and trunk zero."""
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("971"):
        digits = digits[3:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 10 and digits.startswith("0"):
        digits = digits[1:]
    return digits


async def _request_organization_id(user: dict, tenant_id: str | None) -> str:
    try:
        org_id = await asyncio.to_thread(_resolve_active_organization_id, user, tenant_id)
    except Exception as exc:
        raise HTTPException(503, "Workspace lookup is temporarily unavailable") from exc
    if not org_id:
        raise HTTPException(403, "No organization membership found")
    return str(org_id)


async def _scoped_directory_entry(entry_id: str, org_id: str) -> dict:
    entry = await asyncio.to_thread(storage.get_org_whatsapp_phone_directory, entry_id)
    if not entry or str(entry.get("organization_id")) != str(org_id):
        raise HTTPException(404, "Phone directory entry not found")
    return entry


async def _scoped_directory_entry_by_broker(broker_id: str, org_id: str) -> dict:
    entry = await asyncio.to_thread(storage.get_org_whatsapp_phone_directory_by_broker_id, broker_id)
    if not entry or str(entry.get("organization_id")) != str(org_id):
        raise HTTPException(404, "Phone directory entry not found")
    return entry


# Placeholders wired in by app.py at startup
_first_ingestor_response = None


class DirectoryAddRequest(BaseModel):
    phone_number: str = Field(..., description="E.164 or local 10-digit subscriber number")
    display_label: str | None = None


class DirectoryPatchRequest(BaseModel):
    phone_number: str | None = None
    display_label: str | None = None
    is_active: bool | None = None


def _validate_phone_string(value: str) -> str:
    """Canonicalise a directory number to country-code-prefixed digits.

    Accepts UAE (+971 5X XXX XXXX / 971XXXXXXXXX / 05X XXX XXXX / 5X XXX XXXX)
    and Indian (91XXXXXXXXXX / 10-digit mobile) formats.
    """
    raw = re.sub(r"\s+", "", str(value or ""))
    if not _phone_re.match(raw):
        raise HTTPException(400, "Enter a WhatsApp phone number with country code")
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("971"):
        return digits
    if len(digits) == 10 and digits.startswith("05"):
        return "971" + digits[1:]
    if len(digits) == 9 and digits[0] == "5":
        return "971" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    if len(digits) == 10 and digits[0] in "6789":
        return "91" + digits
    raise HTTPException(400, "Enter a WhatsApp number as 9715XXXXXXX (UAE) or 91XXXXXXXXXX (India)")


@router.get("/api/orgs/{org_id}/phone-directory")
async def list_directory(
    org_id: str,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    await _require_org_permission(user, org_id, "manage_whatsapp")
    requested_org = await _request_organization_id(user, tenant_id)
    if requested_org != org_id and not await asyncio.to_thread(storage.is_super_admin, user["id"]):
        raise HTTPException(403, "Workspace mismatch")
    entries = await asyncio.to_thread(storage.list_org_whatsapp_phone_directory, org_id)
    return {
        "entries": [
            {
                "id": row.get("id"),
                "broker_id": row.get("broker_id"),
                "phone_number": row.get("phone_number"),
                "display_label": row.get("display_label") or "",
                "is_active": bool(row.get("is_active", True)),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
            for row in entries
        ],
        "cap": 3,
        "used": len(entries),
    }


@router.post("/api/orgs/{org_id}/phone-directory")
async def add_directory_entry(
    org_id: str,
    body: DirectoryAddRequest,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    await _require_org_permission(user, org_id, "manage_whatsapp")
    requested_org = await _request_organization_id(user, tenant_id)
    if requested_org != org_id and not await asyncio.to_thread(storage.is_super_admin, user["id"]):
        raise HTTPException(403, "Workspace mismatch")
    normalized_phone = _validate_phone_string(body.phone_number)
    label = (body.display_label or "").strip()[:100]
    existing = await asyncio.to_thread(storage.list_org_whatsapp_phone_directory, org_id)
    dup = next(
        (row for row in existing if _normalize_phone(row.get("phone_number")) == normalized_phone),
        None,
    )
    if dup:
        raise HTTPException(
            409,
            f"This WhatsApp number is already saved as {dup.get('display_label') or 'another phone'}. "
            "Edit the existing entry instead of adding it again.",
        )
    broker_id = f"phone-{_uuid.uuid4().hex[:12]}"
    entry = None
    try:
        entry = await asyncio.to_thread(
            storage.add_org_whatsapp_phone_directory,
            org_id,
            broker_id,
            normalized_phone,
            label,
            True,
        )
    except Exception as exc:
        message = str(exc).lower()
        if "maximum" in message or "p0001" in message:
            raise HTTPException(400, "Maximum 3 WhatsApp phone numbers per workspace") from exc
        raise
    if not entry:
        raise HTTPException(500, "Failed to register phone directory entry")
    await asyncio.to_thread(
        storage.add_org_whatsapp_connection,
        org_id,
        normalized_phone,
        label,
        broker_id,
    )
    if _first_ingestor_response is not None:
        try:
            await _first_ingestor_response(
                "POST", "/connect", timeout=10,
                headers={"X-Broker-Id": broker_id},
            )
        except Exception:
            pass
    return {
        "id": entry.get("id"),
        "broker_id": broker_id,
        "phone_number": entry.get("phone_number"),
        "display_label": entry.get("display_label") or "",
        "is_active": bool(entry.get("is_active", True)),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
    }


@router.patch("/api/orgs/{org_id}/phone-directory/{entry_id}")
async def patch_directory_entry(
    org_id: str,
    entry_id: str,
    body: DirectoryPatchRequest,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    await _require_org_permission(user, org_id, "manage_whatsapp")
    requested_org = await _request_organization_id(user, tenant_id)
    if requested_org != org_id and not await asyncio.to_thread(storage.is_super_admin, user["id"]):
        raise HTTPException(403, "Workspace mismatch")
    entry = await _scoped_directory_entry(entry_id, org_id)
    updates: dict = {}
    if body.display_label is not None:
        updates["display_label"] = str(body.display_label).strip()[:100]
    if body.is_active is not None:
        updates["is_active"] = bool(body.is_active)
    if body.phone_number is not None:
        normalized_phone = _validate_phone_string(body.phone_number)
        updates["phone_number"] = str(body.phone_number).strip()
    if not updates:
        return entry
    try:
        updated = await asyncio.to_thread(
            storage.update_org_whatsapp_phone_directory, entry_id, updates
        )
    except Exception as exc:
        message = str(exc).lower()
        if "unique" in message or "duplicate" in message:
            raise HTTPException(409, "Phone number already added to this workspace") from exc
        raise
    if not updated:
        raise HTTPException(500, "Failed to update directory entry")
    if "display_label" in updates:
        broker_id = updated.get("broker_id") or entry.get("broker_id")
        if broker_id:
            await asyncio.to_thread(
                storage.update_org_whatsapp_connection_by_broker_id,
                broker_id,
                {
                    "instance_name": updates["display_label"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
    return {
        "id": updated.get("id"),
        "broker_id": updated.get("broker_id"),
        "phone_number": updated.get("phone_number"),
        "display_label": updated.get("display_label") or "",
        "is_active": bool(updated.get("is_active", True)),
        "created_at": updated.get("created_at"),
        "updated_at": updated.get("updated_at"),
    }


@router.delete("/api/orgs/{org_id}/phone-directory/{entry_id}")
async def delete_directory_entry(
    org_id: str,
    entry_id: str,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    await _require_org_permission(user, org_id, "manage_whatsapp")
    requested_org = await _request_organization_id(user, tenant_id)
    if requested_org != org_id and not await asyncio.to_thread(storage.is_super_admin, user["id"]):
        raise HTTPException(403, "Workspace mismatch")
    entry = await _scoped_directory_entry(entry_id, org_id)
    broker_id = entry.get("broker_id", "")
    connection = None
    if broker_id:
        connection = await asyncio.to_thread(
            storage.get_org_whatsapp_connection_by_broker_id, broker_id
        )
    if connection:
        conn_id = int(connection.get("id") or 0)
        if conn_id:
            await asyncio.to_thread(storage.remove_org_whatsapp_connection, conn_id)
    if _first_ingestor_response is not None and broker_id:
        for verb, path in (("POST", "/disconnect"), ("POST", "/delete-session")):
            try:
                await _first_ingestor_response(
                    verb, path, timeout=10,
                    headers={"X-Broker-Id": broker_id},
                )
            except Exception:
                pass
    removed = await asyncio.to_thread(storage.remove_org_whatsapp_phone_directory, entry_id)
    if not removed:
        raise HTTPException(500, "Failed to remove directory entry")
    return {"ok": True, "id": entry_id}
