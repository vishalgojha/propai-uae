"""
Webhook tools for PropAI Business API (ElevenLabs WhatsApp agent).

Why this exists (not MCP):
ElevenLabs' MCP integration binds one OAuth session to one fixed broker_id
for the life of the connection. Since this WABA number serves MANY brokers,
an MCP session would silently attribute every broker's data to whichever
single identity authorized the MCP connection. These webhook tools are
stateless instead — every call carries the sender's WhatsApp phone number
fresh, so identity is resolved per-request, not per-session.

Wire-up on ElevenLabs' side:
  Tools tab -> Add tool -> Webhook
  URL: https://<your-api-host>/webhooks/business-api/save-listing
  URL: https://<your-api-host>/webhooks/business-api/create-requirement
  URL: https://<your-api-host>/webhooks/business-api/onboard-broker
  Header: X-Business-Api-Webhook-Secret: <BUSINESS_API_WEBHOOK_SECRET>
  Body params should map to the Pydantic fields below. Pass the broker's
  WhatsApp number via the {{system__caller_id}} dynamic variable into the
  `phone` field.

Onboarding flow (explicit consent, not silent):
  save-listing / create-requirement is called for an unknown phone
  -> returns HTTP 404 with needs_onboarding=true and an onboarding_prompt
  -> The agent relays that prompt to the broker and asks for their name
     + explicit consent to create a PropAI account.
  -> On "yes" + name, the agent calls POST /onboard-broker.
  -> The agent retries the original save-listing / create-requirement call,
     which now succeeds since the broker exists.

  System prompt guidance to add to the agent:
    "If a save tool call fails with needs_onboarding=true, do not claim the
    data was saved. Tell the broker in one line that they're not registered
    yet, ask for their name, and ask 'Should I set up your PropAI account so
    I can save this?' Only call onboard-broker after they say yes."
"""

import os
import re
import json
import asyncio
import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from storage import RawMessage, SupabaseStorage

router = APIRouter(prefix="/webhooks/business-api", tags=["business-api"])

# Set this in your environment and mirror it in ElevenLabs' webhook tool config.
BUSINESS_API_WEBHOOK_SECRET = os.environ.get("BUSINESS_API_WEBHOOK_SECRET", "")

_storage: SupabaseStorage | None = None


def get_storage() -> SupabaseStorage:
    """Create storage only when a webhook is invoked.

    Keeping this lazy means mounting these optional ElevenLabs endpoints does
    not make importing the FastAPI app depend on production environment vars.
    """
    global _storage
    if _storage is None:
        _storage = SupabaseStorage()
    return _storage

ONBOARDING_PROMPT = (
    "This number isn't registered with PropAI yet. Ask for the broker's name "
    "and explicit consent, then call /onboard-broker before retrying this save."
)


def _check_secret(x_business_api_webhook_secret: str | None):
    if not BUSINESS_API_WEBHOOK_SECRET:
        # Fail closed: refuse to run wide open in production.
        raise HTTPException(500, "BUSINESS_API_WEBHOOK_SECRET is not configured on the server")
    if x_business_api_webhook_secret != BUSINESS_API_WEBHOOK_SECRET:
        raise HTTPException(401, "Invalid webhook secret")


def _phone_identity_key(phone: str) -> str | None:
    digits = re.sub(r"\D+", "", phone or "")
    if len(digits) >= 9:
        return f"phone:{digits[-9:]}"
    return None


def _normalize_phone(phone: str) -> str:
    """Normalize phone to 9-digit UAE mobile format.

    Strips all non-digits, handles +971 / 00971 / 0 prefix, and validates
    the UAE mobile prefix (5). Returns empty string for invalid numbers.
    """
    raw = (phone or "").strip()
    if not raw or re.search(r"[xX*•]", raw):
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("971"):
        digits = digits[3:]
    elif len(digits) == 14 and digits.startswith("00971"):
        digits = digits[5:]
    elif len(digits) == 10 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 9 and re.match(r"^5\d{8}$", digits):
        return digits
    return ""


def resolve_broker(phone: str) -> dict:
    """Resolve an authenticated WhatsApp identity to its workspace.

    A QR-linked organization connection is the authorization record.  The
    inferred brokers table is used only to enrich that identity, never as the
    authority which grants workspace access.
    """
    norm_phone = _normalize_phone(phone)
    if not norm_phone:
        raise HTTPException(400, "A valid phone number is required to resolve broker identity")

    storage = get_storage()
    connection = storage.get_active_org_whatsapp_connection_by_phone(norm_phone)
    if connection and connection.get("organization_id"):
        existing = storage.find_broker(phone=norm_phone) or {}
        return {
            "id": existing.get("id") or connection.get("broker_id") or connection.get("id"),
            "canonical_name": existing.get("canonical_name") or connection.get("instance_name") or norm_phone,
            "tenant_id": connection["organization_id"],
            "connection_id": connection.get("id"),
        }

    existing = storage.find_broker(phone=norm_phone)
    if existing:
        # A market-inferred broker can be displayed but cannot authorize a
        # Business API write until their number is QR-linked to a workspace.
        raise HTTPException(403, {
            "needs_qr_connection": True,
            "message": "This number appears in market data but is not connected to a PropAI workspace. Link it by QR first.",
        })

    raise HTTPException(
        404,
        {"needs_onboarding": True, "message": ONBOARDING_PROMPT},
    )


class OnboardBrokerRequest(BaseModel):
    phone: str
    name: str
    consent: bool    # must be explicitly true — the broker said yes


@router.post("/onboard-broker")
async def onboard_broker_webhook(
    body: OnboardBrokerRequest,
    x_business_api_webhook_secret: str | None = Header(default=None),
):
    _check_secret(x_business_api_webhook_secret)
    if not body.consent:
        raise HTTPException(400, "Cannot onboard a broker without explicit consent=true")

    norm_phone = _normalize_phone(body.phone)
    if not norm_phone:
        raise HTTPException(400, "A valid phone number is required")

    storage = get_storage()
    existing = storage.find_broker(phone=norm_phone)
    if existing:
        return {"status": "already_onboarded", "broker_id": str(existing["id"])}

    key = _phone_identity_key(norm_phone)
    created = storage.save_broker({
        "identity_key": key,
        "primary_phone": norm_phone,
        "canonical_name": body.name,
        "observation_count": 0,
    })
    return {"status": "onboarded", "broker_id": str(created["id"])}


class SaveListingRequest(BaseModel):
    phone: str          # broker's WhatsApp number -> {{system__caller_id}}
    raw_text: str
    name: str | None = None
    bhk: str | None = None
    location: str | None = None
    price: str | None = None
    carpet_area: str | None = None
    furnishing: str | None = None
    contact_number: str | None = None
    source_message_id: str | None = None


class CreateRequirementRequest(BaseModel):
    phone: str          # broker's WhatsApp number -> {{system__caller_id}}
    raw_text: str
    lead_name: str | None = None
    lead_phone: str | None = None    # the buyer/tenant's phone, distinct from the broker's
    budget: str | None = None
    location_pref: str | None = None
    timeline: str | None = None
    bhk_preference: str | None = None
    property_type: str | None = None
    source_message_id: str | None = None
    confirmed: bool = False


def _to_price_m_aed(price: str | None) -> float | None:
    """Convert a human-readable price string to millions of AED.

    "1.5m" -> 1.5, "850k" -> 0.85, "AED 2500000" -> 2.5.
    Returns None if unparseable or if the text looks like a non-price query.
    """
    if not price:
        return None
    text = price.strip().lower()
    # Extract the numeric part
    digits = re.sub(r"[^\d.]", "", text)
    if not digits:
        return None
    try:
        val = float(digits)
    except ValueError:
        return None
    # Guard: if the text contains words that clearly indicate this isn't a
    # price (e.g. "2 BR in JVC"), bail out early.
    if re.search(r"\b(bhk|br\b|bed|room|floor|flat|apt|sq\s?ft|sqft)\b", text):
        return None
    # Unit-aware normalization — check the original text for unit keywords.
    # Use [^a-z] boundaries instead of \b for single-letter units like "M"
    # because \b doesn't trigger between a digit and a letter (both are \w).
    if re.search(r"(?:^|[^a-z])(?:mn|million|m)(?:[^a-z]|$)", text):
        return val          # already in millions
    if re.search(r"(?:^|[^a-z])(?:k|thousand)(?:[^a-z]|$)", text):
        return val / 1000   # thousand -> million
    # No unit found — assume the value is already absolute AED (most common
    # case from structured extraction which normalizes to dirhams).
    return val / 1_000_000


def _tenant_id_for(broker: dict) -> str:
    # An authenticated broker already belongs to a workspace.  Never use the
    # broker entity id as tenant_id: that silently hid listings from Inbox.
    return str(
        broker.get("tenant_id")
        or os.environ.get("BUSINESS_API_TENANT_ID", "").strip()
        or "00000000-0000-0000-0000-000000000010"
    )


def _agent_message_uid(body: SaveListingRequest, phone: str) -> str:
    supplied = (body.source_message_id or "").strip()
    if supplied:
        return f"waba-agent:{supplied[:180]}"
    # Gives retry-safe behaviour when the provider has not supplied a message
    # ID, without inventing a second listing for the exact same submission.
    digest = hashlib.sha256(f"{phone}\n{body.raw_text.strip()}".encode()).hexdigest()[:32]
    return f"waba-agent:body:{digest}"


@router.post("/save-listing")
async def save_listing_webhook(
    body: SaveListingRequest,
    x_business_api_webhook_secret: str | None = Header(default=None),
):
    _check_secret(x_business_api_webhook_secret)
    broker = resolve_broker(body.phone)
    storage = get_storage()
    broker_phone = _normalize_phone(body.phone)
    tenant_id = _tenant_id_for(broker)
    now = datetime.now(timezone.utc).isoformat()
    message_uid = _agent_message_uid(body, broker_phone)
    existing = storage.get_raw_by_uid(message_uid)
    if existing and existing.processed:
        raw_id = existing.id
        parsed_ids = [parsed.id for parsed in storage.get_parsed_by_message(raw_id)]
        listing_ids = []
        for parsed_id in parsed_ids:
            listing_id = storage.upsert_listing_from_parsed(parsed_id)
            if listing_id:
                listing_ids.append(listing_id)
        result = {"raw_id": raw_id, "parsed_ids": parsed_ids, "listing_ids": listing_ids}
    else:
        raw_id = existing.id if existing else 0
        if not raw_id:
            raw_id = storage.save_raw_message(RawMessage(
                group_name=f"WABA agent · {broker.get('canonical_name') or broker_phone}",
                sender=broker.get("canonical_name") or body.name or broker_phone,
                sender_jid=f"{broker_phone}@s.whatsapp.net",
                sender_phone=broker_phone,
                message=body.raw_text.strip(),
                message_type="text",
                timestamp=now,
                source="WABA_AGENT",
                raw_payload=json.dumps({
                    "source": "waba_business_api",
                    "source_message_id": body.source_message_id,
                    "submitted_fields": {
                        "bhk": body.bhk,
                        "location": body.location,
                        "price": body.price,
                        "carpet_area": body.carpet_area,
                        "furnishing": body.furnishing,
                    },
                }),
                message_uid=message_uid,
                is_group=False,
                tenant_id=tenant_id,
            ))
        # The raw message is the source of truth.  Running it through the normal
        # pipeline creates parsed_output, broker evidence and one listing per
        # option; it deliberately does not copy "location" into micro_market.
        from extraction import process_raw_message
        try:
            result = await asyncio.to_thread(process_raw_message, raw_id, {
                "sender_name": broker.get("canonical_name") or body.name or broker_phone,
                "push_name": broker.get("canonical_name") or body.name or broker_phone,
                "sender_jid": f"{broker_phone}@s.whatsapp.net",
                "sender_phone": broker_phone,
                "group": f"waba-agent:{broker_phone}",
                "group_name": f"WABA agent · {broker.get('canonical_name') or broker_phone}",
                "msg_text": body.raw_text.strip(),
                "instance": "waba-agent",
                "is_dm": True,
                "message_uid": message_uid,
                "message_id": body.source_message_id or message_uid,
                "msg": {},
                "tenant_id": tenant_id,
            }) or {}
        except Exception as exc:
            print(f"[business-api] extraction unavailable for listing raw_id={raw_id}: {exc}", flush=True)
            raise HTTPException(503, {
                "saved": False,
                "message": "Extraction is temporarily unavailable. The broker can retry.",
                "raw_id": raw_id,
            })
    listing_ids = result.get("listing_ids") or []
    if not listing_ids:
        raise HTTPException(422, {
            "saved": False,
            "message": "The message was received but no property listing could be verified. Do not tell the broker it was saved.",
            "raw_id": raw_id,
        })

    return {
        "status": "saved",
        "raw_id": raw_id,
        "parsed_ids": result.get("parsed_ids") or [],
        "listing_ids": listing_ids,
        "listing_urls": [
            f"{os.environ.get('PUBLIC_WWW_URL', 'https://ae.propai.live').rstrip('/')}/listings/{listing_id}/{listing_id}"
            for listing_id in listing_ids
        ],
        "broker_id": str(broker["id"]),
        "tenant_id": tenant_id,
        "source": "waba_agent_pipeline",
    }


@router.post("/create-requirement")
async def create_requirement_webhook(
    body: CreateRequirementRequest,
    x_business_api_webhook_secret: str | None = Header(default=None),
):
    _check_secret(x_business_api_webhook_secret)
    if not body.confirmed:
        raise HTTPException(409, {
            "saved": False,
            "message": "A requirement is only saved after the broker explicitly confirms it. For search-only, do not call this endpoint.",
        })
    broker = resolve_broker(body.phone)
    storage = get_storage()
    broker_phone = _normalize_phone(body.phone)
    tenant_id = _tenant_id_for(broker)
    now = datetime.now(timezone.utc).isoformat()
    source_message_id = (body.source_message_id or "").strip()
    digest = hashlib.sha256(f"{broker_phone}\nrequirement\n{body.raw_text.strip()}".encode()).hexdigest()[:32]
    message_uid = f"waba-agent:{source_message_id[:180]}" if source_message_id else f"waba-agent:requirement:{digest}"
    existing = storage.get_raw_by_uid(message_uid)
    raw_id = existing.id if existing else storage.save_raw_message(RawMessage(
        group_name=f"WABA agent · {broker.get('canonical_name') or broker_phone}",
        sender=broker.get("canonical_name") or broker_phone,
        sender_jid=f"{broker_phone}@s.whatsapp.net",
        sender_phone=broker_phone,
        message=body.raw_text.strip(),
        message_type="text",
        timestamp=now,
        source="WABA_AGENT",
        raw_payload=json.dumps({
            "source": "waba_business_api",
            "kind": "requirement",
            "source_message_id": source_message_id or None,
        }),
        message_uid=message_uid,
        is_group=False,
        tenant_id=tenant_id,
    ))
    if existing and existing.processed:
        parsed_ids = [parsed.id for parsed in storage.get_parsed_by_message(raw_id)]
    else:
        from extraction import process_raw_message
        try:
            result = await asyncio.to_thread(process_raw_message, raw_id, {
                "sender_name": broker.get("canonical_name") or body.name or broker_phone,
                "push_name": broker.get("canonical_name") or body.name or broker_phone,
                "sender_jid": f"{broker_phone}@s.whatsapp.net",
                "sender_phone": broker_phone,
                "group": f"waba-agent:{broker_phone}",
                "group_name": f"WABA agent · {broker.get('canonical_name') or broker_phone}",
                "msg_text": body.raw_text.strip(),
                "instance": "waba-agent",
                "is_dm": True,
                "message_uid": message_uid,
                "message_id": source_message_id or message_uid,
                "msg": {},
                "tenant_id": tenant_id,
            }) or {}
        except Exception as exc:
            print(f"[business-api] extraction unavailable for requirement raw_id={raw_id}: {exc}", flush=True)
            raise HTTPException(503, {
                "saved": False,
                "message": "Extraction is temporarily unavailable. The broker can retry.",
                "raw_id": raw_id,
            })
        parsed_ids = result.get("parsed_ids") or []
    if not parsed_ids:
        raise HTTPException(422, {
            "saved": False,
            "message": "The requirement was received but could not be structured. Do not say it was saved.",
            "raw_id": raw_id,
        })

    return {
        "status": "saved",
        "raw_id": raw_id,
        "parsed_ids": parsed_ids,
        "broker_id": str(broker["id"]),
        "tenant_id": tenant_id,
        "source": "waba_agent_pipeline",
    }
