"""Supabase implementation of the Storage interface."""

import contextvars
import hashlib
import json
import logging
import math
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

_logger = logging.getLogger(__name__)

_tenant_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("tenant_id", default=None)


def _normalize_requirement_urgency(value: Any) -> str:
    """Map provider wording to the enum enforced by requirement tables."""
    text = str(value or "").strip().lower()
    if text in {"urgent", "normal", "flexible"}:
        return text
    if any(token in text for token in ("urgent", "immediate", "asap", "right away", "today", "now")):
        return "urgent"
    if any(token in text for token in ("flexible", "no hurry", "whenever", "open to")):
        return "flexible"
    return "normal"

def get_tenant_id() -> Optional[str]:
    return _tenant_id_var.get()

def set_tenant_id(tid: Optional[str]):
    _tenant_id_var.set(tid)

from lab.storage.base import (
    Storage, RawMessage, ParsedObservation, Listing,
    ResolverDecision, Evaluation, SyncJob, SyncCheckpoint,
    AISuggestion, LLMProvider, WorkspaceAISettings,
    AgentBrowserSession, AgentBrowserStep, AgentAuditLog,
    dict_to_dataclass,
)
from lab.inventory import listing_fingerprint, listing_label
from location import canonical_micro_market_slug
from price_normalization import canonical_commercial_rental_price_rupees, canonical_price_rupees, canonical_rental_price_rupees, rent_price_needs_review
from building_quality import is_valid_building_candidate, normalize_building_name
from extraction_quality import building_name_problem, canonical_locality_alias


_EMOJI_ICON_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u200d"
    "\u20e3"
    "\u231a-\u23ff"
    "\u25a0-\u25ff"
    "\u2600-\u27bf"
    "\u2934-\u2935"
    "\u2b05-\u2b55"
    "\u3030"
    "\u303d"
    "\u3297"
    "\u3299"
    "\ufe00-\ufe0f"
    "]+",
    flags=re.UNICODE,
)


# These tables intentionally have different price columns.  Sending one
# cross-table PostgREST projection caused every building job to fail with a
# 400 before the external provider was even called.
_BUILDING_EVIDENCE_SELECTS = {
    "residential_sale_listings": "locality_raw,micro_market,building_name,summary_title,raw_payload,broker_id,bhk,total_asking_price,transaction_type,created_at",
    "residential_rent_listings": "locality_raw,micro_market,building_name,summary_title,raw_payload,broker_id,bhk,monthly_rent,transaction_type,created_at",
    "commercial_sale_listings": "locality_raw,micro_market,building_name,summary_title,raw_payload,broker_id,total_asking_price,transaction_type,created_at",
    "commercial_rent_listings": "locality_raw,micro_market,building_name,summary_title,raw_payload,broker_id,monthly_rent,transaction_type,created_at",
    "residential_sale_requirements": "locality_raw,micro_market,building_name,summary_title,raw_payload,broker_id,bhk_options,budget_min,budget_max,transaction_type,created_at",
    "residential_rent_requirements": "locality_raw,micro_market,building_name,summary_title,raw_payload,broker_id,bhk_options,budget_min,budget_max,transaction_type,created_at",
    "commercial_sale_requirements": "locality_raw,micro_market,building_name,summary_title,raw_payload,broker_id,budget_min,budget_max,transaction_type,created_at",
    "commercial_rent_requirements": "locality_raw,micro_market,building_name,summary_title,raw_payload,broker_id,budget_min,budget_max,transaction_type,created_at",
}


def _strip_icons(value: str = "") -> str:
    clean = _EMOJI_ICON_RE.sub("", value or "")
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r" *\n *", "\n", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean.strip()


def _sanitize_parsed_payload(value: Any) -> Any:
    if isinstance(value, str):
        clean = _strip_icons(value).strip()
        return clean if clean else None
    if isinstance(value, list):
        return [_sanitize_parsed_payload(item) for item in value]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            sanitized = _sanitize_parsed_payload(item)
            if sanitized is not None:
                clean[key] = sanitized
        return clean
    return value


# These fields are PostgreSQL booleans in the typed extraction tables.  The
# extraction providers use the human-readable sentinel "Unknown" for an
# unobserved fact; that sentinel must become SQL NULL at the persistence
# boundary, never a string sent to PostgREST.
_TYPED_BOOLEAN_FIELDS = {
    "balcony_present", "bank_loan_eligible", "bar_facility",
    "by_lanes_accepted", "cam_applicable", "can_sell_separately",
    "ceo_cabin_present", "client_profile_required", "co_brokered",
    "deposit_applicable", "development_charges_applicable",
    "fee_sharing_required", "floor_rise_applicable", "glass_facade_required",
    "gst_applicable", "has_central_ac", "has_lift", "has_mezzanine",
    "has_power_backup", "has_staircase", "heritage_space",
    "is_combination_unit", "is_converted_unit", "loan_preapproved",
    "media_requested", "needs_attached_washroom", "needs_central_ac",
    "needs_conference_room", "needs_lift", "needs_mezzanine", "needs_pantry",
    "needs_power_backup", "needs_review", "needs_server_room", "needs_washroom",
    "oc_required", "parking_charges_applicable", "parking_required",
    "plc_applicable", "plus_one_deal", "premium_building_required",
    "project_inventory", "property_tax_applicable", "reception_area",
    "registration_applicable", "rera_registered", "residential_cum_commercial_ok",
    "server_room", "short_term_allowed", "sit_out_present",
    "society_maintenance_applicable", "society_transfer_applicable",
    "stamp_duty_applicable", "storage_area", "sub_leasing_allowed",
}


def _coerce_typed_boolean(field: str, value: Any) -> Any:
    """Convert provider boolean/sentinel values to PostgreSQL-safe values."""
    if field not in _TYPED_BOOLEAN_FIELDS or value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "t", "yes", "y", "1", "available", "present"}:
        return True
    if text in {"false", "f", "no", "n", "0", "unavailable", "absent"}:
        return False
    return None


def _clean_person_name(name: str = "") -> str:
    clean = (name or "").strip()
    if re.fullmatch(r"^[+0-9 ()-]{7,15}$", clean) and re.search(r"\d", clean):
        return ""
    if re.search(r"@(s\.whatsapp\.net|lid|g\.us)$", clean, re.I):
        return ""
    clean = re.sub(r"\s*\([^)]*(?:\+?\d|X{2,})[^)]*\)\s*", " ", clean, flags=re.I)
    clean = re.sub(r"\s*\+?\d[\d\s().-]{7,}\s*", " ", clean)
    clean = re.sub(r"\s{2,}", " ", clean).strip(" -")
    return clean


_BROKER_IDENTITY_NOISE_RE = re.compile(
    r"\b(?:please\s+share|suitable\s+options|contact\s+redacted|see\s+agent|"
    r"contact|call|whatsapp|inspection|details|visit)\b",
    re.IGNORECASE,
)


def _effective_broker_name(*, source_name: str = "", profile_name: str = "",
                           sender_name: str = "", display_name: str = "") -> str:
    """Choose broker identity from source evidence before WhatsApp display data.

    WhatsApp display names are useful fallbacks, but they are not evidence that
    the sender is the broker named in a listing. In particular, using display
    data first made the same inventory flip between names such as ``Kapsy`` and
    the signed source name. CTA/footer text is never an identity.
    """
    for candidate in (source_name, profile_name, sender_name, display_name):
        clean = _clean_person_name(candidate)
        if clean and not _BROKER_IDENTITY_NOISE_RE.search(clean):
            return clean
    return ""


def _clean_market_building_name(row: dict) -> str:
    """Return a building label only when the source supports one.

    Parsers occasionally promote a locality (``khar(w)``) or a broker's
    signature (``Galaxy Estate Consultancy``) into ``building_name``. Those
    values must remain location/broker metadata, never inventory identity.
    """
    value = str(row.get("building_name") or "").strip()
    if not value:
        return ""
    market = str(row.get("micro_market") or row.get("locality_resolved") or "").strip()

    # Older typed rows can still contain an AI-wired field even though newer
    # extraction paths quarantine it. Apply the same source-grounded guard at
    # read time so polluted values never become building-intelligence links.
    if building_name_problem(value, locality=market):
        return ""

    def compact(text: object) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(text or "").casefold())

    value_key = compact(value)
    market_key = compact(market)
    payload = row.get("raw_payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            payload = {}
    if isinstance(payload, dict):
        source_slice = str(payload.get("slice_text") or payload.get("source_slice_text") or "").strip()
        if source_slice:
            source_tokens = {
                token for token in re.findall(r"[a-z0-9]+", source_slice.casefold())
                if len(token) >= 3
            }
            building_tokens = {
                token for token in re.findall(r"[a-z0-9]+", value.casefold())
                if len(token) >= 3 and token not in {"bldg", "building"}
            }
            if building_tokens and building_tokens.isdisjoint(source_tokens):
                return ""
    locality_aliases = {
        "marina": "dubaimarina", "dubaimarina": "dubaimarina",
        "hills": "dubaihillsestate", "dubaihills": "dubaihillsestate",
        "dubaihillsestate": "dubaihillsestate",
        "bbay": "businessbay", "businessbay": "businessbay",
        "furjan": "alfurjan", "alfurjan": "alfurjan",
        "springs": "thesprings", "thesprings": "thesprings",
        "meadows": "themeadows", "themeadows": "themeadows",
        "lakes": "thelakes", "thelakes": "thelakes",
        "greens": "thegreens", "thegreens": "thegreens",
        "views": "theviews", "theviews": "theviews",
        "jbr": "jbr", "difc": "difc", "jvc": "jvc", "jvt": "jvt",
        "jlt": "jlt",
    }
    if value_key in locality_aliases and locality_aliases[value_key] == locality_aliases.get(market_key, market_key):
        return ""
    if value_key.startswith("location") or value_key.startswith("address"):
        return ""

    broker_values = {
        compact(row.get("broker_name")),
        compact(row.get("broker_company")),
    } - {""}
    payload = row.get("raw_payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    source = str(payload.get("slice_text") or payload.get("full_text") or "") if isinstance(payload, dict) else ""
    has_property_signal = bool(re.search(
        r"\b(?:bhk|br|rk|flat|apartment|building|project|rent|lease|sale|sell|buy|price|aed|dhs|cheque|chq|yearly|carpet|sq\.?\s*ft)\b",
        source,
        re.I,
    ))
    if value_key in broker_values and not has_property_signal:
        return ""
    return value


def _jid_phone(value: str = "") -> str:
    """Extract a displayable phone from a user JID; never return a LID."""
    raw = str(value or "").strip()
    match = re.match(r"\+?(\d+)@s\.whatsapp\.net$", raw, re.I)
    return match.group(1) if match else ""


def _locality_fields(data: dict) -> tuple[str | None, str | None]:
    location = data.get("location")
    raw = data.get("location_raw") or data.get("area")
    resolved = data.get("locality_resolved")
    if isinstance(location, dict):
        raw = raw or location.get("raw_mention") or location.get("raw") or location.get("label")
        resolved = resolved or location.get("resolved_locality") or location.get("canonical") or location.get("name")
    elif isinstance(location, str):
        resolved = resolved or location
    return (str(raw).strip() if raw else None, str(resolved).strip() if resolved else None)


def _market_name_key(name: str = "") -> str:
    """Return the conservative comparison key used for market identities."""
    clean = _clean_person_name(name)
    # WhatsApp/device labels can append a numeric suffix to an otherwise
    # identical agency name ("Agency Mumbai" vs "Agency Mumbai-50"). Keep
    # that transport noise out of name-only broker identity resolution.
    clean = re.sub(r"\s*-\s*\d+\s*$", "", clean)
    return re.sub(r"[\W_]+", " ", clean.casefold(), flags=re.UNICODE).strip()


_AMBIGUOUS_NAME_WORDS: frozenset[str] = frozenset({
    "broker", "agent", "dealer", "builder", "owner", "company", "firm",
    "realty", "realtor", "property", "realestate", "real", "estate",
    "group", "associates", "consultancy", "consultant", "solutions",
    "services", "enterprises", "corporation", "corp", "inc", "ltd",
    "private", "limited", "projects", "developers", "infra", "housing",
    "homes", "constructions", "interior", "design", "decor",
})


def _resolve_market_identity(
    phone: str,
    name: str,
    phones_by_name: dict[str, set[str]],
) -> tuple[str, str]:
    """Link a name-only row only when that name has one unambiguous phone."""
    normalized_phone = _normalize_india_phone(phone)
    name_key = _market_name_key(name)
    if not normalized_phone and name_key:
        candidates = phones_by_name.get(name_key, set())
        if len(candidates) == 1:
            normalized_phone = next(iter(candidates))
        elif not candidates:
            name_words = {w for w in name_key.split() if w not in _AMBIGUOUS_NAME_WORDS}
            if name_words:
                for known_name, known_phones in phones_by_name.items():
                    known_words = {w for w in known_name.split() if w not in _AMBIGUOUS_NAME_WORDS}
                    if name_words & known_words:
                        normalized_phone = next(iter(known_phones))
                        break
    return normalized_phone or f"name:{name_key}", normalized_phone


def _normalize_india_phone(value: str = "") -> str:
    raw = (value or "").strip()
    if not raw or re.search(r"[xX*•]", raw):
        return ""
    # Strip WhatsApp JID suffixes: @s.whatsapp.net, :26 linked-device suffix
    raw = raw.split("@")[0].split(":")[0]
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[-10:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[-10:]
    if len(digits) == 10 and re.match(r"^[6-9]\d{9}$", digits):
        return digits
    return ""


def _raw_payload_from_me(payload: object) -> bool:
    """Read WhatsMeow's outgoing marker from either supported payload shape."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
    if not isinstance(payload, dict):
        return False
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    key = data.get("key") if isinstance(data.get("key"), dict) else {}
    value = key.get("fromMe", key.get("from_me", False))
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def _raw_payload_remote_jid(payload: object) -> str:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    key = data.get("key") if isinstance(data.get("key"), dict) else {}
    return str(key.get("remoteJid") or key.get("remote_jid") or "").strip()


def _raw_message_owned_by_user(
    raw: dict,
    *,
    owner_phones: set[str],
    allowed_broker_ids: set[str],
    user_id: str = "",
) -> bool:
    """Return true only when raw evidence proves this is the user's deal.

    A workspace/group receiving a message does not make that message the
    connected broker's deal. Extracted broker fields are also only advertised
    contact details, so ownership comes from WhatsMeow direction/identity or
    an explicit creator id on a non-WhatsApp source.
    """
    source = str(raw.get("source") or "").strip().upper()
    payload = raw.get("raw_payload") or {}
    message_uid = str(raw.get("message_uid") or "")
    origin_broker_id = message_uid.split(":", 1)[0] if ":" in message_uid else ""
    origin_allowed = bool(
        allowed_broker_ids
        and (not origin_broker_id or origin_broker_id in allowed_broker_ids)
    )

    sender_phone = _normalize_india_phone(
        str(raw.get("sender_phone") or raw.get("sender_jid") or "")
    )
    sender_is_owner = bool(sender_phone and sender_phone in owner_phones)

    if source == "WHATSAPP":
        if not origin_allowed:
            return False
        if _raw_payload_from_me(payload) or sender_is_owner:
            return True
        # A phone-sent self-chat can arrive at WhatsMeow with fromMe=false.
        # Its remote chat JID is still the connected account's own number.
        remote_phone = _normalize_india_phone(_raw_payload_remote_jid(payload))
        return bool(remote_phone and remote_phone in owner_phones)

    if source.startswith("WABA"):
        return sender_is_owner

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
    if isinstance(payload, dict) and user_id:
        creator = str(
            payload.get("owner_user_id")
            or payload.get("created_by_user_id")
            or payload.get("user_id")
            or ""
        ).strip()
        if creator:
            return creator == str(user_id).strip()
        # AI Chat saves created before owner_user_id was persisted have no
        # durable creator field. In a single-connection workspace, the
        # connected phone is the only safe legacy ownership signal available.
        if source == "AI_CHAT" and sender_is_owner:
            return True
        return False
    return False


def _normalize_listing_price(price: object, price_unit: object) -> tuple[float | None, str | None]:
    """Normalise to AED with K/M display units (Dubai convention)."""
    if price in (None, ""):
        return None, None
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None, None

    unit = str(price_unit or "").strip().lower()
    if unit == "m":
        if value >= 1_000_000:
            value = value / 1_000_000
        return value, "M"
    if unit == "k":
        if value >= 1_000:
            value = value / 1_000
        return value, "K"
    if unit in {"abs", "absolute", "aed", "dhs", "dirham", "dirhams", "", "none", "null"}:
        if value >= 1_000_000:
            return round(value / 1_000_000, 2), "M"
        if value >= 100_000:
            return round(value / 1_000, 0), "K"
        return value, "abs"
    return value, price_unit if price_unit is None or isinstance(price_unit, str) else str(price_unit)


_MARKET_REQUIREMENT_INTENTS = frozenset({
    "BUY", "BUYER", "REQUIREMENT", "RENTAL_SEEKER", "TENANT", "DEMAND",
})


def _is_market_requirement(parsed: dict) -> bool:
    message_type = str(parsed.get("message_type") or "").strip().upper()
    intent = str(parsed.get("intent") or "").strip().upper()
    return message_type in {"REQUIREMENT", "BUY"} or intent in _MARKET_REQUIREMENT_INTENTS


_TYPED_LISTING_TABLES = {
    ("residential", "sale"): "residential_sale_listings",
    ("residential", "rent"): "residential_rent_listings",
    ("commercial", "sale"): "commercial_sale_listings",
    ("commercial", "rent"): "commercial_rent_listings",
}
_TYPED_REQUIREMENT_TABLES = {
    ("residential", "sale"): "residential_sale_requirements",
    ("residential", "rent"): "residential_rent_requirements",
    ("commercial", "sale"): "commercial_sale_requirements",
    ("commercial", "rent"): "commercial_rent_requirements",
}

_ALL_TYPED_TABLES = tuple(_TYPED_LISTING_TABLES.values()) + tuple(_TYPED_REQUIREMENT_TABLES.values())
_TYPED_LISTING_TABLE_NAMES = tuple(_TYPED_LISTING_TABLES.values())
_TYPED_REQUIREMENT_TABLE_NAMES = tuple(_TYPED_REQUIREMENT_TABLES.values())

# PostgREST expects PostgreSQL text[] columns as JSON arrays. LLM output and
# older extraction paths sometimes send a single string instead.
_TYPED_ARRAY_FIELDS = frozenset({
    "deal_tags", "building_amenities", "unit_amenities", "society_restrictions",
    "contacts", "locality_options", "micro_market_options", "bhk_options",
    "configuration_preference", "building_preferences", "view_preference",
    "amenity_requirements", "permitted_use_types", "ideal_for",
    "commercial_use_type",
})
_REQUIREMENT_ONLY_FIELDS = frozenset({
    "bhk_options", "locality_options", "micro_market_options",
    "configuration_preference", "building_preferences", "view_preference",
    "amenity_requirements", "furnishing_preference", "possession_preference",
    "age_preference", "buyer_type", "nationality", "loan_preapproved",
    "brokerage_willingness", "is_flexible", "urgency", "status", "intent",
    "budget_min", "budget_max", "budget_currency", "area_min_sqft", "area_max_sqft",
})


def _coerce_text_array(value: Any) -> list[str]:
    """Convert LLM scalar/list output into a Postgres-compatible text[]."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item).strip() for item in values if item is not None and str(item).strip()]


def _coerce_numeric_array(value: Any) -> list[float]:
    """Convert BHK options into the numeric[] shape used by typed tables."""
    result: list[float] = []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    for item in values:
        match = re.search(r"\d+(?:\.\d+)?", str(item or ""))
        if match:
            result.append(float(match.group(0)))
    return result


def _coerce_sql_date(value: Any) -> str | None:
    """Accept only ISO dates for date columns; preserve other text in evidence."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return None

# Feed/audit reads do not need the extraction evidence blobs.  Keeping these
# columns explicit prevents a normal inbox refresh from transferring
# raw_payload and ai_extraction for hundreds of rows from each typed table.
_TYPED_COMMON_READ_COLUMNS = (
    "id,raw_message_id,tenant_id,listing_index,source_fingerprint,asset_type,"
    "transaction_type,building_name,locality_raw,locality_resolved,micro_market,"
    "visibility,source_scope,"
    "locality_confidence,landmark_name,street_name,broker_id,broker_name,"
    "broker_phone,group_name,summary_title,deal_tags,"
    "validation_flags,needs_review,extraction_confidence,corrected_fields,"
    "extraction_confidence_score,"
    "correction_confidence,corrected_at,created_at,updated_at,last_seen_at,expires_at,legacy_source_id,opportunity_key"
)
_TYPED_READ_COLUMNS_BY_TABLE = {
    "residential_sale_listings": "bhk,configuration_type,bathroom_count,carpet_area_sqft,built_up_area_sqft,super_built_up_area_sqft,area_raw_text,total_asking_price,price_per_sqft,price_basis,price_raw_text,price_qualifier,furnishing_status,possession_status,possession_date,car_parking_count,parking_type,floor_range,building_amenities,unit_amenities,amenities_unverified_claim,property_view,orientation,developer_name,broker_company,contacts,showing_instructions,contact_instructions,availability_status,brokerage_context,co_brokered,wing,floor_min,floor_max,floor_label,original_bhk,current_bhk,is_converted_unit,is_combination_unit,configuration_details,can_sell_separately,balcony_area_sqft,balcony_area_raw_text,terrace_area_sqft,covered_terrace_area_sqft,terrace_area_raw_text,sellable_area_sqft,computed_total_asking_price,computed_price_confidence,price_math,unit_condition,vastu_compliant,view_description,parking_details,society_restrictions,society_restrictions_raw,unstructured_facts,broker_rera_number",
    "residential_rent_listings": "bhk,configuration_type,bathroom_count,carpet_area_sqft,built_up_area_sqft,area_raw_text,monthly_rent,rent_per_sqft,price_basis,price_raw_text,price_qualifier,deposit_amount,deposit_months,deposit_applicable,deposit_raw_text,furnishing_status,possession_status,available_from,availability_date_raw,availability_status,car_parking_count,parking_type,parking_details,floor_range,floor_min,floor_max,floor_label,wing,has_lift,building_amenities,unit_amenities,amenities_unverified_claim,pet_policy,tenant_type_preference,sharing_allowed,tenant_nationality_preference,company_lease_criteria,food_preference,lease_term_type,lease_term_min_months,lease_term_max_months,lease_term_raw_text,property_view,view_description,brokerage_type,brokerage_context,brokerage_terms_raw,plus_one_deal,fee_sharing_required,client_profile_required,original_bhk,current_bhk,configuration_details,is_converted_unit,is_combination_unit,balcony_present,balcony_area_sqft,balcony_area_raw_text,terrace_area_sqft,covered_terrace_area_sqft,terrace_area_raw_text,sit_out_present,unit_condition,society_restrictions_raw,broker_company,contacts,showing_instructions,contact_instructions,unstructured_facts,broker_rera_number",
    "commercial_sale_listings": "commercial_use_type,carpet_area_sqft,built_up_area_sqft,chargeable_area_sqft,super_built_up_area_sqft,mezzanine_area_sqft,saleable_area_sqft,area_raw_text,total_asking_price,price_per_sqft,price_basis,price_raw_text,price_qualifier,fitout_status,ceiling_height,floor_level,floor_range,floor_count,has_mezzanine,has_staircase,car_parking_count,parking_type,building_amenities,has_central_ac,has_power_backup,has_lift,brokerage_type,developer_name,broker_rera_number,terrace_area_sqft,covered_terrace_area_sqft,terrace_area_raw_text,frontage_ft,entrance_count,permitted_use_types,ideal_for,project_inventory,area_min_sqft,area_max_sqft,floor_plate_sqft,project_status,director_cabin_count,ceo_cabin_present,cubicle_count,conference_room_capacity,meeting_room_capacity,reception_area,server_room,storage_area,inspection_notice_minutes,price_math",
    "commercial_rent_listings": "commercial_use_type,carpet_area_sqft,built_up_area_sqft,chargeable_area_sqft,mezzanine_area_sqft,area_raw_text,monthly_rent,rent_per_sqft,price_basis,price_raw_text,price_qualifier,deposit_amount,deposit_months,deposit_applicable,deposit_raw_text,fitout_status,ceiling_height,floor_level,floor_range,floor_count,has_mezzanine,car_parking_count,parking_type,building_amenities,has_central_ac,has_power_backup,has_lift,lease_term_type,brokerage_type,broker_rera_number,terrace_area_sqft,covered_terrace_area_sqft,terrace_area_raw_text,frontage_ft,entrance_count,otla_area_sqft,otla_area_raw_text,heritage_space,permitted_use_types,ideal_for,automatic_shutter_count,room_count,suite_count,banquet_hall_count,restaurant_count,bar_facility,operational_status,rent_inclusions,license_type,short_term_allowed,inspection_notice_minutes,director_cabin_count,ceo_cabin_present,cubicle_count,conference_room_capacity,meeting_room_capacity,training_room_capacity,cafeteria_seat_count,accounts_area,lounge_area,price_math",
    "residential_sale_requirements": "bhk,configuration_type,bathroom_count,carpet_area_sqft,built_up_area_sqft,super_built_up_area_sqft,area_raw_text,furnishing_status,possession_status,possession_date,car_parking_count,parking_type,floor_range,building_amenities,unit_amenities,amenities_unverified_claim,property_view,orientation,developer_name,budget_min,budget_max,budget_currency,area_min_sqft,area_max_sqft,locality_options,is_flexible,urgency,status,bhk_options,configuration_preference,carpet_area_min_sqft,carpet_area_max_sqft,built_up_area_min_sqft,built_up_area_max_sqft,budget_per_sqft_max,furnishing_preference,possession_preference,age_preference,micro_market_options,building_preferences,car_parking_min,floor_preference,view_preference,amenity_requirements,buyer_type,nationality,loan_preapproved,brokerage_willingness",
    "residential_rent_requirements": "bhk,configuration_type,bathroom_count,carpet_area_sqft,built_up_area_sqft,super_built_up_area_sqft,area_raw_text,furnishing_status,possession_status,possession_date,car_parking_count,parking_type,floor_range,building_amenities,unit_amenities,amenities_unverified_claim,property_view,orientation,developer_name,budget_min,budget_max,budget_currency,area_min_sqft,area_max_sqft,locality_options,is_flexible,urgency,status,bhk_options,configuration_preference,carpet_area_min_sqft,carpet_area_max_sqft,built_up_area_min_sqft,built_up_area_max_sqft,budget_per_sqft_max,furnishing_preference,possession_preference,age_preference,micro_market_options,building_preferences,car_parking_min,floor_preference,view_preference,amenity_requirements,buyer_type,nationality,loan_preapproved,brokerage_willingness,deposit_budget_max,tenant_type,has_pets,sharing_acceptable,food_preference,lease_term_preference",
    "commercial_sale_requirements": "carpet_area_sqft,built_up_area_sqft,super_built_up_area_sqft,area_raw_text,furnishing_status,possession_status,possession_date,car_parking_count,parking_type,floor_range,building_amenities,unit_amenities,amenities_unverified_claim,property_view,orientation,developer_name,budget_min,budget_max,budget_currency,area_min_sqft,area_max_sqft,locality_options,is_flexible,urgency,status,carpet_area_min_sqft,carpet_area_max_sqft,built_up_area_min_sqft,built_up_area_max_sqft,budget_per_sqft_max,furnishing_preference,possession_preference,micro_market_options,building_preferences,car_parking_min,floor_preference,view_preference,amenity_requirements,buyer_type,loan_preapproved,brokerage_willingness,commercial_use_type,chargeable_area_max_sqft,fitout_preference,min_ceiling_height,needs_mezzanine,needs_lift,needs_power_backup,needs_central_ac,min_power_load_kw,oc_required",
    "commercial_rent_requirements": "carpet_area_sqft,built_up_area_sqft,super_built_up_area_sqft,area_raw_text,furnishing_status,possession_status,possession_date,car_parking_count,parking_type,floor_range,building_amenities,unit_amenities,amenities_unverified_claim,property_view,orientation,developer_name,budget_min,budget_max,budget_currency,area_min_sqft,area_max_sqft,locality_options,is_flexible,urgency,status,carpet_area_min_sqft,carpet_area_max_sqft,built_up_area_min_sqft,built_up_area_max_sqft,budget_per_sqft_max,furnishing_preference,possession_preference,micro_market_options,building_preferences,car_parking_min,floor_preference,view_preference,amenity_requirements,buyer_type,loan_preapproved,brokerage_willingness,commercial_use_type,chargeable_area_max_sqft,fitout_preference,min_ceiling_height,needs_mezzanine,needs_lift,needs_power_backup,needs_central_ac,min_power_load_kw,oc_required,deposit_budget_max,lease_term_preference,intended_use_details,area_basis_preference,location_flexibility,floor_min,floor_max,floor_count_max,consecutive_floors_required,parking_required,needs_attached_washroom,needs_washroom,needs_pantry,power_requirements,premium_building_required,glass_facade_required,residential_cum_commercial_ok,by_lanes_accepted,entrance_requirement,signage_required,loading_access_required,budget_includes_maintenance,media_requested,min_cabin_count,min_workstation_count,needs_conference_room,brokerage_context,brokerage_terms_raw,contacts,min_washroom_count",
}

# The inbox card does not need extraction evidence or the long tail of typed
# fields. Keep this projection deliberately small; the detail route fetches
# the complete row only after the user expands a card.
_MARKET_CARD_COMMON_COLUMNS = (
    "id,raw_message_id,tenant_id,listing_index,asset_type,transaction_type,"
    "visibility,source_scope,"
    "building_name,locality_raw,locality_resolved,micro_market,landmark_name,"
    "broker_name,broker_phone,group_name,summary_title,created_at,updated_at,last_seen_at,expires_at,"
    "legacy_source_id,source_fingerprint,opportunity_key"
)

def _market_card_columns(table: str) -> str:
    """Return a lightweight projection containing only columns this table owns.

    The typed schemas deliberately differ: commercial inventory has no ``bhk``
    and requirements have no listing price fields.  A single shared projection
    makes PostgREST reject the entire table query, which previously caused six
    of the eight feed sources to disappear behind the fan-out's best-effort
    exception handling.
    """
    available = {
        column.strip()
        for column in _TYPED_READ_COLUMNS_BY_TABLE.get(table, "").split(",")
        if column.strip()
    }
    card_candidates = (
        "bhk", "bhk_options", "configuration_type", "configuration_preference",
        "commercial_use_type", "carpet_area_sqft", "built_up_area_sqft", "chargeable_area_sqft", "area_raw_text",
        "area_min_sqft", "area_max_sqft", "total_asking_price", "monthly_rent",
        "budget_max", "price_per_sqft", "rent_per_sqft", "price_raw_text",
        "furnishing_status", "furnishing_preference", "fitout_status",
        "fitout_preference", "floor_range", "floor_preference",
        "availability_status", "possession_status",
    )
    table_fields = [column for column in card_candidates if column in available]
    duplicate = ["duplicate_status", "duplicate_group_id", "possible_duplicate_source_table", "possible_duplicate_source_id", "possible_duplicate_similarity", "repost_count", "last_posted_at"]
    return ",".join([_MARKET_CARD_COMMON_COLUMNS, *duplicate, *table_fields])


def _typed_read_columns(
    table: str,
    *,
    include_evidence: bool = False,
    include_normalized_message: bool = False,
    include_raw_payload: bool = False,
) -> str:
    evidence_fields = []
    if include_raw_payload:
        evidence_fields.append("raw_payload")
    if include_normalized_message:
        evidence_fields.append("normalized_message")
    if include_evidence:
        evidence_fields.append("ai_extraction")
    evidence = "," + ",".join(evidence_fields) if evidence_fields else ""
    duplicate = ",duplicate_status,duplicate_group_id,possible_duplicate_source_table,possible_duplicate_source_id,possible_duplicate_similarity,repost_count,last_posted_at"
    return f"{_TYPED_COMMON_READ_COLUMNS}{duplicate}{evidence},{_TYPED_READ_COLUMNS_BY_TABLE.get(table, '')}"


def _typed_route(parsed: dict) -> tuple[str, str, str]:
    """Return (destination table, asset type, transaction type).

    This mirrors the classification functions used by the migration, but is
    deliberately kept in application code so new ingestion never depends on
    a compatibility trigger or an old flat table.
    """
    body = " ".join(str(parsed.get(key) or "") for key in (
        "normalized_message", "location_raw", "property_type", "commercial_use_type"
    )).lower()
    asset = str(parsed.get("asset_type") or "").strip().lower()
    if asset not in {"residential", "commercial"}:
        asset = "commercial" if re.search(
            r"office|shop|showroom|warehouse|godown|industrial|commercial|retail|bare.?shell|warm.?shell|chargeable area|ceiling height|mezzanine|cabin|workstation|conference room|\bpsf\b|\bcam\b|lease deed|power load|\bkw\b|food court",
            body,
        ) else "residential"
    tx = str(parsed.get("transaction_type") or "").strip().lower()
    # The classifier has historically emitted `sale` for some demand text
    # that explicitly says lease/rent.  For requirements, a strong rental
    # phrase must win unless the same message contains an unambiguous sale
    # phrase (for example, "outright requirement").
    full_text = " ".join(str(parsed.get(key) or "") for key in (
        "normalized_message", "intent", "message_type"
    ))
    rental_signal = re.search(
        r"\b(?:rent|rental|lease|monthly|per\s+month|deposit|tenancy|lock.?in|notice\s+period)\b",
        full_text, re.I,
    )
    sale_signal = re.search(
        r"\b(?:sale|sell|outright|outrate|for\s+sale|asking\s+price)\b",
        full_text, re.I,
    )
    if _is_market_requirement(parsed) and rental_signal and not sale_signal:
        tx = "rent"
    if tx not in {"rent", "sale"}:
        tx = "rent" if re.search(
            r"rent|rental|lease|monthly|per month|deposit|tenancy|lock.?in|notice period|lease out",
            " ".join(str(parsed.get(key) or "") for key in ("intent", "message_type", "normalized_message")),
            re.I,
        ) else "sale"
    tables = _TYPED_REQUIREMENT_TABLES if _is_market_requirement(parsed) else _TYPED_LISTING_TABLES
    return tables[(asset, tx)], asset, tx


def _typed_source_id(parsed: dict) -> int:
    """Stable cross-table observation id used by resolver_decisions."""
    if int(parsed.get("id") or 0) > 0:
        return int(parsed["id"])
    raw_id = int(parsed.get("raw_message_id") or 0)
    index = int(parsed.get("listing_index") or 0)
    if raw_id > 0:
        return raw_id * 1000 + index + 1
    digest = hashlib.sha256(json.dumps(parsed, sort_keys=True, default=str).encode()).hexdigest()
    return int(digest[:15], 16)


_MARKET_CONTACT_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")


def _redact_market_source_text(value: object) -> str:
    text = str(value or "")
    return _MARKET_CONTACT_RE.sub("[Contact redacted — see agent]", text)


def _preferred_market_source_text(
    raw_message: object,
    normalized_message: object,
    slice_text: object,
) -> str:
    """Choose display evidence in source-of-truth order.

    ``slice_text`` is an extraction boundary, not necessarily the complete
    WhatsApp post. A short header slice must never hide an available raw
    message. It is only a last resort when the raw and normalized fields are
    unavailable.
    """
    raw = str(raw_message or "").strip()
    if raw:
        return raw
    normalized = str(normalized_message or "").strip()
    if normalized:
        return normalized
    return str(slice_text or "").strip()


def _format_bhk_label(value: object) -> str:
    """Keep numeric BHK storage queryable while exposing a human label."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        number = float(raw)
        rendered = str(int(number)) if number.is_integer() else f"{number:g}"
        return f"{rendered} BHK"
    if re.fullmatch(r"\d+(?:\.\d+)?\s*(?:BHK|RK)", raw, re.I):
        return re.sub(r"\s+", " ", raw).upper()
    return raw


def _source_evidence_for_typed_row(typed: dict, raw: dict, fallback: object) -> str:
    """Return evidence that agrees with the row's stored configuration.

    Typed tables intentionally store BHK as a numeric dimension for filtering.
    A stale or mis-sliced normalized message must never be shown as evidence
    for a different configuration, so recover a matching block from the raw
    WhatsApp message when possible and otherwise return no misleading text.
    """
    source = str(fallback or "").strip()
    bhk = typed.get("bhk")
    if bhk is None and isinstance(typed.get("bhk_options"), (list, tuple)) and typed["bhk_options"]:
        bhk = typed["bhk_options"][0]
    expected = _format_bhk_label(typed.get("configuration_type") or bhk)
    if not expected or not re.search(r"\b(?:BHK|RK)\b", expected, re.I):
        return source
    number = re.match(r"^(\d+(?:\.\d+)?)\s*(BHK|RK)", expected, re.I)
    if not number:
        return source
    marker = re.compile(rf"\b{re.escape(number.group(1))}\s*(?:BHK|RK)\b", re.I)
    if marker.search(source) and len(re.findall(r"\b\d+(?:\.\d+)?\s*(?:BHK|RK)\b", source, re.I)) <= 1:
        return source

    raw_text = str(raw.get("message") or "").strip()
    if not raw_text:
        return source if marker.search(source) else ""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", raw_text) if part.strip()]
    candidates = lines if len(lines) > 1 else paragraphs or lines
    for candidate in candidates:
        if marker.search(candidate):
            return candidate
    for line in raw_text.splitlines():
        if marker.search(line):
            return line.strip()
    return ""


def _relevant_market_source_slice(source: object, building_name: object) -> str:
    """Return one listing block from a broadcast source when possible.

    Older typed rows sometimes stored the entire broadcast in slice_text.
    Keep the raw source intact in storage, but show the block belonging to the
    current building in the market feed. If the structure is ambiguous, the
    caller keeps the original source rather than inventing boundaries.
    """
    text = str(source or "").strip()
    building = re.sub(r"\s+", " ", str(building_name or "").strip()).lower()
    if not text or not building or len(text) < 500:
        return text
    lines = text.splitlines()
    bold_heading = re.compile(r"^\s*[*_]\s*[^*_\n]{2,120}?\s*[*_]\s*$")
    numbered_heading = re.compile(r"^\s*\d{1,3}[.)-]\s+\S+")
    boundaries = [
        idx for idx, line in enumerate(lines)
        if bold_heading.match(line) or numbered_heading.match(line)
    ]
    if len(boundaries) < 2:
        return text
    blocks = []
    for index, start in enumerate(boundaries):
        end = boundaries[index + 1] if index + 1 < len(boundaries) else len(lines)
        block_start = 0 if index == 0 else start
        block = "\n".join(lines[block_start:end]).strip()
        if block:
            blocks.append(block)
    if not blocks:
        return text

    def normalized(value: object) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    target = normalized(building)
    target_words = [word for word in target.split() if len(word) >= 4]
    ranked = []
    for index, block in enumerate(blocks):
        candidate = normalized(block)
        score = 100 if target and target in candidate else sum(
            1 for word in target_words if word in candidate
        )
        ranked.append((score, index, block))
    score, _, block = max(ranked, key=lambda item: (item[0], -item[1]))
    return block if score > 0 and len(block) >= 30 else text


def _price_to_rupees(value: object, unit: object) -> float | None:
    return canonical_price_rupees(value, unit)


def _is_market_group_name(group_name: str = "") -> bool:
    gn = (group_name or "").strip()
    if not gn or gn in ("seed", "seed-bot", "status@broadcast", "broadcast"):
        return False
    return not (
        gn.endswith("@s.whatsapp.net")
        or gn.endswith("@lid")
        or gn.endswith("@newsletter")
        or gn.endswith("@broadcast")
    )


def _is_market_group_row(row: dict) -> bool:
    """Return true only when the raw message is provably from a group chat.

    Older rows sometimes stored the group *title* in ``group_name`` and direct
    chats stored the contact name there as well.  Treating every non-JID title
    as a group leaked personal conversations into Market Inbox.  Prefer the
    canonical WhatsApp remote JID (raw payload/message_uid), and only fall back
    to an explicit @g.us group_name.
    """
    group_name = (row.get("group_name") or "").strip()
    if not group_name or group_name in ("seed", "seed-bot", "status@broadcast", "broadcast"):
        return False
    if group_name.endswith("@g.us"):
        return True
    payload = row.get("raw_payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    payload = payload if isinstance(payload, dict) else {}
    key = payload.get("key") if isinstance(payload.get("key"), dict) else {}
    remote = key.get("remoteJid") or payload.get("remoteJid") or ""
    if isinstance(payload.get("data"), dict):
        dkey = payload["data"].get("key") if isinstance(payload["data"].get("key"), dict) else {}
        remote = remote or dkey.get("remoteJid") or payload["data"].get("remoteJid") or ""
    uid = (row.get("message_uid") or "").split(":", 1)[0]
    return str(remote).endswith("@g.us") or uid.endswith("@g.us")


def _observation_fingerprint(row: dict, *, include_broker: bool = True) -> str:
    """Return a stable, broker-scoped identity for a market opportunity.

    Raw/parsed row IDs deliberately do not participate: WhatsApp reposts get
    new IDs even when the underlying listing or requirement is unchanged.
    Conversely, fields that distinguish real units (notably floor, area and
    price) remain part of the identity so multi-listing posts stay split.
    """
    payload = {
        "observation_type": row.get("observation_type") or "",
        "intent": row.get("intent") or "",
        "transaction_type": row.get("transaction_type") or "",
        "asset_type": row.get("asset_type") or "",
        "property_type": row.get("property_type") or "",
        "bhk": row.get("bhk") or row.get("configuration") or "",
        "price": row.get("price") or row.get("monthly_rent") or row.get("total_asking_price") or "",
        "area_sqft": row.get("area_sqft") or "",
        "furnishing": row.get("furnishing") or row.get("furnishing_canonical") or "",
        "building_name": row.get("building_name") or "",
        "landmark_name": row.get("landmark_name") or "",
        "micro_market": row.get("micro_market") or "",
        "location_raw": row.get("location_raw") or "",
        "floor_range": row.get("floor_range") or "",
        "wing": row.get("wing") or "",
        "flat_number": row.get("flat_number") or "",
        "commercial_use_type": row.get("commercial_use_type") or "",
        "occupancy_type": row.get("occupancy_type") or "",
    }
    if include_broker:
        # Broker identity is part of the listing identity. Without it, two
        # brokers advertising the same building/unit were collapsed together.
        payload["broker_identity"] = (
            row.get("broker_id")
            or row.get("broker_phone")
            or row.get("broker_name")
            or row.get("profile_name")
            or ""
        )
    normalized = {
        key: re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
        for key, value in payload.items()
    }

    # Weak parses without a property/location/size/price anchor are unsafe to
    # merge broadly.  Their item-specific text still collapses exact reposts
    # while avoiding one broker's unrelated generic posts becoming one item.
    anchors = (
        normalized["bhk"], normalized["price"], normalized["area_sqft"],
        normalized["building_name"], normalized["micro_market"],
        normalized["location_raw"], normalized["landmark_name"],
    )
    if not any(anchors):
        source = (
            row.get("source_message")
            or row.get("normalized_message")
            or row.get("summary_title")
            or row.get("raw_message")
            or row.get("message")
            or ""
        )
        normalized["source_fallback"] = re.sub(
            r"[^a-z0-9]+", " ", str(source).lower()
        ).strip()

    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _merge_observation_rows(rows: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []
    seen_weak_identity: dict[str, str] = {}
    for row in rows:
        # A repost that has been explicitly merged is evidence, not a second
        # market opportunity. Every consumer using this projection must obey
        # that invariant, including legacy listing/search paths.
        if str(row.get("duplicate_status") or "").lower() == "merged":
            continue
        key = _observation_fingerprint(row)
        existing = merged.get(key)
        if not existing:
            # A repost can lose broker_phone in one extraction branch while
            # retaining the same unit/property fields. Allow that one-sided
            # identity gap to collapse, but never collapse two known phone
            # identities merely because their property fields match.
            weak_key = _observation_fingerprint(row, include_broker=False)
            prior_key = seen_weak_identity.get(weak_key)
            prior = merged.get(prior_key) if prior_key else None
            current_phone = re.sub(r"\D+", "", str(row.get("broker_phone") or ""))[-10:]
            prior_phone = re.sub(r"\D+", "", str(prior.get("broker_phone") or ""))[-10:] if prior else ""
            if prior and (not current_phone or not prior_phone):
                existing = prior
                key = prior_key
        if not existing:
            copy = dict(row)
            copy["fingerprint"] = key
            copy["times_seen"] = int(copy.get("times_seen") or 1)
            evidence = copy.get("evidence_list")
            if isinstance(evidence, list):
                copy["evidence_list"] = list(evidence)
            elif evidence:
                copy["evidence_list"] = [evidence]
            else:
                copy["evidence_list"] = []
            merged[key] = copy
            order.append(key)
            seen_weak_identity[_observation_fingerprint(row, include_broker=False)] = key
            continue

        existing["times_seen"] = int(existing.get("times_seen") or 1) + int(row.get("times_seen") or 1)

        existing_ts = str(existing.get("last_seen") or existing.get("created_at") or "")
        row_ts = str(row.get("last_seen") or row.get("created_at") or "")
        if row_ts and row_ts >= existing_ts:
            for field in (
                "id",
                "latest_raw_message_id",
                "latest_parsed_id",
                "raw_message_id",
                "summary_title",
                "observation_type",
                "intent",
                "asset_type",
                "property_type",
                "transaction_type",
                "bhk",
                "configuration",
                "price",
                "price_unit",
                "price_model",
                "price_per_sqft",
                "monthly_rent",
                "total_asking_price",
                "budget_min",
                "budget_max",
                "budget_currency",
                "area_sqft",
                "furnishing",
                "furnishing_canonical",
                "building_name",
                "micro_market",
                "landmark_name",
                "location_raw",
                "commercial_use_type",
                "fitout_status",
                "occupancy_type",
                "floor_range",
                "availability_status",
                "possession_status",
                "possession_date",
                "available_from",
                "ready_by",
                "construction_stage",
                "launch_timeline",
                "expected_possession",
                "listing_index",
                "first_seen",
                "last_seen",
                "last_seen_at",
                "expires_at",
                "lifecycle_status",
                "raw_message",
                "normalized_message",
                "source_message",
                "raw_sender",
                "broker_name",
                "broker_phone",
            ):
                if row.get(field) not in (None, ""):
                    existing[field] = row[field]

        if row.get("evidence_list"):
            evidence = existing.setdefault("evidence_list", [])
            for item in row.get("evidence_list") or []:
                if item not in evidence:
                    evidence.append(item)

        if row.get("first_seen") and (not existing.get("first_seen") or str(row["first_seen"]) < str(existing["first_seen"])):
            existing["first_seen"] = row["first_seen"]
        if row.get("last_seen") and (not existing.get("last_seen") or str(row["last_seen"]) > str(existing["last_seen"])):
            existing["last_seen"] = row["last_seen"]
    result = [merged[key] for key in order]

    # Sale and rent are separate opportunities, but a broker often posts the
    # same unit in both modes. Surface that relationship without merging the
    # rows (price and transaction type remain distinct facts).
    availability: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for row in result:
        if row.get("observation_type") == "REQUIREMENT":
            continue
        building = re.sub(r"[^a-z0-9]+", " ", str(
            row.get("building_name") or row.get("micro_market") or row.get("location_raw") or ""
        ).lower()).strip()
        broker = re.sub(r"[^a-z0-9]+", " ", str(
            row.get("broker_phone") or row.get("broker_name") or ""
        ).lower()).strip()
        anchors = (
            building,
            str(row.get("bhk") or row.get("configuration") or "").strip().lower(),
            str(row.get("area_sqft") or "").strip().lower(),
            str(row.get("floor_range") or "").strip().lower(),
        )
        if building and broker and any(anchors[1:]):
            availability[(broker, *anchors)].add(str(row.get("intent") or "").upper())
    for row in result:
        if row.get("observation_type") == "REQUIREMENT":
            continue
        building = re.sub(r"[^a-z0-9]+", " ", str(
            row.get("building_name") or row.get("micro_market") or row.get("location_raw") or ""
        ).lower()).strip()
        broker = re.sub(r"[^a-z0-9]+", " ", str(
            row.get("broker_phone") or row.get("broker_name") or ""
        ).lower()).strip()
        anchors = (
            building,
            str(row.get("bhk") or row.get("configuration") or "").strip().lower(),
            str(row.get("area_sqft") or "").strip().lower(),
            str(row.get("floor_range") or "").strip().lower(),
        )
        intents = availability.get((broker, *anchors), set())
        current = str(row.get("intent") or "").upper()
        alternate = (intents - {current}).pop() if len(intents - {current}) == 1 else ""
        if alternate:
            row["alternate_intent"] = alternate
    return result


@dataclass
class _APIResponse:
    data: list[dict]
    count: Optional[int] = None
    status_code: int = 200
    error: Optional[str] = None


class _SupabaseRow(dict):
    """Row wrapper that behaves like both a dict and a tuple-like row."""

    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self._keys = list(data.keys())

    def __getitem__(self, key):
        if isinstance(key, int):
            return super().__getitem__(self._keys[key])
        return super().__getitem__(key)


class _SupabaseResult:
    def __init__(self, rows: list[dict[str, Any]], rowcount: int = 0):
        self._rows = [_SupabaseRow(row) for row in rows]
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _SupabaseDatabaseAdapter:
    def __init__(self, client: "_RestClient"):
        self._client = client
        self.row_factory = None

    @staticmethod
    def _translate_sql(sql: str, params: tuple[Any, ...] | list[Any] | None) -> tuple[str, list[Any]]:
        text = (sql or "").strip().rstrip(";")
        if not params:
            params = []

        translated: list[str] = []
        idx = 0
        for ch in text:
            if ch == "?":
                idx += 1
                translated.append(f"${idx}")
            else:
                translated.append(ch)

        # Apply SQLite-to-Postgres function translations for RPC compatibility
        translated_sql = "".join(translated)
        # INSTR(haystack, needle) -> POSITION(needle IN haystack) or split_part for common '@' case
        # Handle INSTR(sender_jid, '@') pattern
        translated_sql = re.sub(
            r'INSTR\s*\(\s*(\w+)\s*,\s*[\'"]([^\'"]+)[\'"]\s*\)',
            r'POSITION(\2 IN \1)',
            translated_sql,
            flags=re.IGNORECASE,
        )
        # General INSTR(haystack, needle) -> POSITION(needle IN haystack)
        translated_sql = re.sub(
            r'INSTR\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)',
            r'POSITION(\2 IN \1)',
            translated_sql,
            flags=re.IGNORECASE,
        )
        # SUBSTR(str, start, length) -> SUBSTRING(str FROM start FOR length)
        translated_sql = re.sub(
            r'SUBSTR\s*\(\s*([^,]+)\s*,\s*([^,]+)\s*,\s*([^)]+)\s*\)',
            r'SUBSTRING(\1 FROM \2 FOR \3)',
            translated_sql,
            flags=re.IGNORECASE,
        )
        # SUBSTR(str, start) -> SUBSTRING(str FROM start)
        translated_sql = re.sub(
            r'SUBSTR\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)',
            r'SUBSTRING(\1 FROM \2)',
            translated_sql,
            flags=re.IGNORECASE,
        )
        # IFNULL(a, b) -> COALESCE(a, b)
        translated_sql = re.sub(
            r'IFNULL\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)',
            r'COALESCE(\1, \2)',
            translated_sql,
            flags=re.IGNORECASE,
        )
        # datetime(expr) -> (expr)::timestamptz  (Postgres has no datetime() function)
        translated_sql = re.sub(
            r"datetime\(([^)]+)\)",
            r"(\1)::timestamptz",
            translated_sql,
            flags=re.IGNORECASE,
        )
        # date(expr) -> (expr)::date  (normalize to Postgres cast)
        # Special case: DATE('now', '-N days') -> (now() - interval 'N days')::date
        translated_sql = re.sub(
            r"(?<![:\w])date\('now',\s*'-?(\d+)\s*(day|days|month|months|year|years)'\)",
            r"(now() - interval '\1 \2')::date",
            translated_sql,
            flags=re.IGNORECASE,
        )
        # DATE('now') -> CURRENT_DATE
        translated_sql = re.sub(
            r"(?<![:\w])date\('now'\)",
            "CURRENT_DATE",
            translated_sql,
            flags=re.IGNORECASE,
        )
        # Generic: date(expr) -> (expr)::date
        translated_sql = re.sub(
            r"(?<![:\w])date\(([^)]+)\)",
            r"(\1)::date",
            translated_sql,
            flags=re.IGNORECASE,
        )
        # Boolean literals
        translated_sql = translated_sql.replace("TRUE", "true").replace("FALSE", "false")

        return translated_sql, list(params)

    @staticmethod
    def _is_query(sql: str) -> bool:
        head = re.sub(r"^\s*(?:--.*?\n|/\*.*?\*/\s*)*", "", sql, flags=re.S).lstrip().lower()
        # INSERT/UPDATE/DELETE with RETURNING should use propai_query_sql
        if head.startswith(("insert", "update", "delete")):
            return " returning " in head
        return head.startswith(("select", "with", "show", "values", "explain"))

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None):
        rendered_sql, rendered_params = self._translate_sql(sql, params)
        import logging
        logging.info(f"propai_query_sql RPC - sql: {rendered_sql[:200]}... params: {rendered_params}")
        if self._is_query(rendered_sql):
            data = self._client.rpc(
                "propai_query_sql",
                {"sql": rendered_sql, "params": rendered_params},
            )
            rows = data if isinstance(data, list) else []
            return _SupabaseResult(rows, rowcount=len(rows))

        data = self._client.rpc(
            "propai_run_sql",
            {"sql": rendered_sql, "params": rendered_params},
        )
        rowcount = 0
        if isinstance(data, dict):
            try:
                rowcount = int(data.get("row_count", 0) or 0)
            except (TypeError, ValueError):
                rowcount = 0
        return _SupabaseResult([], rowcount=rowcount)

    def commit(self):
        return None

    def close(self):
        return None


class _NotFilterBuilder:
    def __init__(self, query: "_QueryBuilder"):
        self._query = query

    def is_(self, column: str, value: str):
        self._query._filters.append((column, "not.is", value))
        return self._query


class _QueryBuilder:
    def __init__(self, client: "_RestClient", table: str):
        self._client = client
        self._table = table
        self._op = "select"
        self._payload: Any = None
        self._select = "*"
        self._count = None
        self._order: list[tuple[str, bool]] = []
        self._limit: Optional[int] = None
        self._offset: Optional[int] = None
        self._filters: list[tuple[str, str, Any]] = []
        self._or: Optional[str] = None
        self._on_conflict: Optional[str] = None

    @property
    def not_(self):
        return _NotFilterBuilder(self)

    def select(self, columns: str = "*", count: str | None = None):
        self._op = "select"
        self._select = columns
        self._count = count
        return self

    def order(self, column: str, desc: bool = False):
        self._order.append((column, desc))
        return self

    def limit(self, value: int):
        self._limit = value
        return self

    def offset(self, value: int):
        self._offset = value
        return self

    def eq(self, column: str, value: Any):
        self._filters.append((column, "eq", value))
        return self

    def neq(self, column: str, value: Any):
        self._filters.append((column, "neq", value))
        return self

    def gte(self, column: str, value: Any):
        self._filters.append((column, "gte", value))
        return self

    def lt(self, column: str, value: Any):
        """Add a PostgREST strict less-than filter."""
        self._filters.append((column, "lt", value))
        return self

    def lte(self, column: str, value: Any):
        """Add a PostgREST less-than-or-equal filter."""
        self._filters.append((column, "lte", value))
        return self

    def ilike(self, column: str, value: Any):
        self._filters.append((column, "ilike", value))
        return self

    def like(self, column: str, value: Any):
        """Add a PostgREST case-sensitive LIKE filter."""
        self._filters.append((column, "like", value))
        return self

    def in_(self, column: str, values: list[Any]):
        self._filters.append((column, "in", values))
        return self

    def is_(self, column: str, value: Any):
        """Add a PostgREST IS filter (typically ``null``/``true``)."""
        self._filters.append((column, "is", value))
        return self

    def filter(self, column: str, operator: str, value: Any):
        """Add a raw PostgREST filter for supabase-py compatibility."""
        self._filters.append((column, str(operator), value))
        return self

    def or_(self, expression: str):
        self._or = expression
        return self

    def insert(self, payload: Any):
        self._op = "insert"
        self._payload = payload
        return self

    def upsert(self, payload: Any, on_conflict: str | None = None):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def update(self, payload: dict[str, Any]):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def execute(self):
        return self._client._execute(self)


class _RestClient:
    def __init__(self, url: str, key: str):
        self._base_url = url.rstrip("/")
        self._headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        timeout_seconds = float(os.getenv("SUPABASE_HTTP_TIMEOUT_SECONDS", "30"))
        max_connections = max(4, int(os.getenv("SUPABASE_HTTP_MAX_CONNECTIONS", "20")))
        max_keepalive = max(2, min(max_connections, int(os.getenv("SUPABASE_HTTP_MAX_KEEPALIVE", "8"))))
        self._http = httpx.Client(
            timeout=httpx.Timeout(
                timeout_seconds,
                connect=min(5.0, timeout_seconds),
                pool=min(30.0, timeout_seconds),
            ),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive,
            ),
            headers=self._headers,
        )

    def table(self, name: str):
        return _QueryBuilder(self, name)

    def rpc(self, name: str, params: dict[str, Any] | None = None):
        import logging
        url = f"{self._base_url}/rest/v1/rpc/{name}"
        try:
            res = self._http.post(url, content=json.dumps(params or {}))
            res.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:500] if e.response else str(e)
            stale_extraction_claim = "not available for extraction" in detail
            log = logging.warning if stale_extraction_claim else logging.error
            log("Supabase RPC '%s' failed (HTTP %s): %s", name, e.response.status_code if e.response else "?", detail)
            if stale_extraction_claim:
                raise RuntimeError("raw message is not available for extraction") from e
            raise RuntimeError(f"Database query failed") from e
        if not res.text:
            return []
        return res.json()

    def close(self):
        self._http.close()

    def _execute(self, query: _QueryBuilder):
        url = f"{self._base_url}/rest/v1/{query._table}"
        params: list[tuple[str, Any]] = []

        if query._op == "select":
            params.append(("select", query._select))
        if query._or:
            params.append(("or", f"({query._or})"))
        for column, op, value in query._filters:
            if op == "in":
                rendered = ",".join(str(v) for v in value)
                params.append((column, f"in.({rendered})"))
            elif op == "not.is":
                params.append((column, f"not.is.{value}"))
            else:
                params.append((column, f"{op}.{value}"))
        for column, desc in query._order:
            params.append(("order", f"{column}.{ 'desc' if desc else 'asc' }"))
        if query._limit is not None:
            params.append(("limit", query._limit))
        if query._offset is not None:
            params.append(("offset", query._offset))
        request_headers = {"Prefer": "count=exact"} if query._count == "exact" else None

        if query._op == "select":
            res = self._http.get(url, params=params, headers=request_headers)
        elif query._op in {"insert", "upsert"}:
            headers = {"Prefer": "return=representation"}
            if query._op == "upsert":
                headers["Prefer"] = "resolution=merge-duplicates,return=representation"
                if query._on_conflict:
                    params.append(("on_conflict", query._on_conflict))
            res = self._http.post(url, params=params, content=json.dumps(query._payload), headers=headers)
        elif query._op == "update":
            res = self._http.patch(url, params=params, content=json.dumps(query._payload), headers={"Prefer": "return=representation"})
        elif query._op == "delete":
            res = self._http.delete(url, params=params, headers={"Prefer": "return=representation"})
        else:
            raise ValueError(f"Unsupported operation: {query._op}")

        if res.is_error:
            # PostgREST includes the actionable constraint/schema detail in
            # the response body; retain it in server logs for diagnosis.
            detail = res.text[:1000] if res.text else ""
            import logging
            # Insert-first typed persistence intentionally uses a 409 on an
            # existing source_fingerprint as its idempotent update signal.
            # The caller handles that response immediately; do not present
            # an expected duplicate replay as a production error.
            duplicate_replay = (
                query._op == "insert"
                and res.status_code == 409
                and "source_fingerprint" in detail
            )
            log = logging.warning if duplicate_replay else logging.error
            log(
                "Supabase REST %s %s failed (HTTP %s): %s",
                query._op, query._table, res.status_code, detail,
            )
        res.raise_for_status()
        data = res.json() if res.text else []
        if isinstance(data, dict):
            data = [data]
        count = None
        if query._count == "exact":
            content_range = res.headers.get("content-range", "")
            if "/" in content_range:
                try:
                    count = int(content_range.rsplit("/", 1)[1])
                except ValueError:
                    count = None
        return _APIResponse(data=data, count=count, status_code=res.status_code)


Client = _RestClient


def create_client(url: str, key: str) -> Client:
    return _RestClient(url, key)


class SupabaseStorage(Storage):
    """Postgres/Supabase backend implementing the Storage interface."""

    def __init__(self, url: str = "", key: str = ""):
        url = url or os.getenv("SUPABASE_URL", "")
        key = key or os.getenv("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY required")
        self._client: Client = create_client(url, key)
        self._db = _SupabaseDatabaseAdapter(self._client)
        self.__tenant_id_fallback: str | None = None
        self._stats_cache: dict[str, tuple[float, dict[str, int]]] = {}
        # The semantic status RPC includes bounded vector-quality checks. Keep
        # its result briefly so the dashboard's refresh loop does not queue
        # overlapping expensive database calls, and retain the last real
        # snapshot during a transient timeout.
        self._semantic_status_cache: tuple[float, dict[str, Any]] | None = None
        # A freshly saved observation already knows its typed destination.
        # Keep that mapping so the ingestion hot path does not have to scan
        # the compatibility UNION view to find the row it just wrote.
        self._typed_table_by_source_id: dict[int, str] = {}
        # Role checks happen on nearly every authenticated request.  Retain
        # a successful super-admin result briefly so a transient Supabase
        # statement timeout does not hide platform navigation.  Server-side
        # permission checks still run normally when the database is healthy.
        self._super_admin_cache: dict[str, float] = {}
        # The map enriches at most 100 listings, but the reference tables can
        # contain thousands of rows.  Re-fetching both tables on every map
        # refresh was a large, avoidable source of latency and seq scans.
        self._market_reference_cache: tuple[float, list[dict], list[dict]] | None = None

    @property
    def client(self) -> Client:
        return self._client

    @property
    def db(self):
        return self._db

    @property
    def tenant_id(self) -> str | None:
        return get_tenant_id() or self.__tenant_id_fallback

    @tenant_id.setter
    def tenant_id(self, value: str | None):
        set_tenant_id(value)

    @property
    def _tenant_id(self) -> str | None:
        """Compatibility for legacy methods that still read this attribute."""
        return self.tenant_id

    @_tenant_id.setter
    def _tenant_id(self, value: str | None):
        self.__tenant_id_fallback = value

    def close(self):
        pass

    # ── User Profiles / Onboarding ─────────────────────────────────

    def _normalize_phone(self, phone: str) -> str:
        """Normalize phone to 10-digit format (last 10 digits)."""
        digits = "".join(ch for ch in phone if ch.isdigit())
        return digits[-10:] if len(digits) >= 10 else digits

    def get_user_profile(self, phone: str = "", auth_user_id: str = "", tenant_id: str | None = None) -> dict | None:
        tid = tenant_id or self._tenant_id
        if auth_user_id:
            try:
                q = self.client.table("user_profiles").select("*").eq("auth_user_id", auth_user_id)
                if tid:
                    q = q.eq("tenant_id", tid)
                q = q.limit(1)
                res = q.execute()
                if res.data:
                    return res.data[0]
            except Exception:
                pass
            # auth_user_id is globally unique in user_profiles. Older profiles
            # retain the tenant where they were first created, so an owner who
            # switches workspaces must still see the same personal profile.
            if tid:
                try:
                    res = self.client.table("user_profiles").select("*")\
                        .eq("auth_user_id", auth_user_id).limit(1).execute()
                    if res.data:
                        return res.data[0]
                except Exception:
                    pass
        if not phone:
            return None
        norm = self._normalize_phone(phone)
        try:
            q = self.client.table("user_profiles").select("*").eq("phone", norm)
            if tid:
                q = q.eq("tenant_id", tid)
            q = q.limit(1)
            res = q.execute()
            return res.data[0] if res.data else None
        except Exception:
            return None

    def save_user_profile(self, phone: str, data: dict, auth_user_id: str = "", tenant_id: str | None = None) -> dict | None:
        tid = tenant_id or self._tenant_id
        norm = self._normalize_phone(phone)
        payload = {
            "phone": norm,
            "first_name": data.get("first_name", ""),
            "last_name": data.get("last_name", ""),
            "email": data.get("email", ""),
            "city": data.get("city", ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if auth_user_id:
            payload["auth_user_id"] = auth_user_id
        if tid:
            payload["tenant_id"] = tid
        existing = None
        if auth_user_id:
            existing = self.get_user_profile(auth_user_id=auth_user_id)
        if not existing and norm:
            existing = self.get_user_profile(phone=norm)
        if existing:
            # Auth providers do not always expose a phone number. Never erase a
            # previously saved identity (or collide on the unique empty value)
            # merely because this login has no auth phone.
            if not norm:
                payload["phone"] = existing.get("phone", "")
            # A profile belongs to the auth user; its legacy tenant_id records
            # where it was created and must not jump whenever an admin switches
            # active workspaces.
            if existing.get("tenant_id"):
                payload["tenant_id"] = existing["tenant_id"]
            update_where = ("auth_user_id", existing.get("auth_user_id")) if existing.get("auth_user_id") else ("phone", existing.get("phone", norm))
            uq = self.client.table("user_profiles").update(payload).eq(update_where[0], update_where[1])
            res = uq.execute()
        else:
            if not norm and auth_user_id:
                payload["phone"] = f"auth_{auth_user_id[:8]}"
            res = self.client.table("user_profiles").insert(payload).execute()
        return res.data[0] if res and res.data else None

    def get_sound_preferences(self, auth_user_id: str, tenant_id: str | None = None) -> dict:
        profile = self.get_user_profile(auth_user_id=auth_user_id, tenant_id=tenant_id)
        value = profile.get("sound_preferences") if profile else None
        return value if isinstance(value, dict) else {}

    def save_sound_preferences(self, auth_user_id: str, phone: str, preferences: dict, tenant_id: str | None = None) -> dict:
        tid = tenant_id or self._tenant_id
        profile = self.get_user_profile(auth_user_id=auth_user_id, tenant_id=tid)
        if not profile:
            profile = self.save_user_profile(
                phone,
                {"first_name": "", "last_name": "", "email": "", "city": ""},
                auth_user_id=auth_user_id,
                tenant_id=tid,
            )
        if not profile:
            raise RuntimeError("User profile could not be created")
        query = self.client.table("user_profiles").update({
            "sound_preferences": preferences,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", profile["id"])
        if tid:
            query = query.eq("tenant_id", tid)
        result = query.execute()
        return (result.data[0] if result.data else {"sound_preferences": preferences}).get("sound_preferences") or preferences

    def get_profile_photo(self, jid: str, tenant_id: str | None = None) -> dict | None:
        """Get cached profile photo for a WhatsApp JID (phone@s.whatsapp.net)."""
        tid = tenant_id or self._tenant_id
        phone = jid.split("@")[0].replace("+", "").strip() if "@" in jid else jid.replace("+", "").strip()
        if not phone:
            return None
        try:
            q = self.client.table("user_profiles").select("profile_photo_url, profile_photo_id, profile_photo_fetched_at").eq("phone", phone)
            if tid:
                q = q.eq("tenant_id", tid)
            res = q.limit(1).execute()
            return res.data[0] if res and res.data else None
        except Exception:
            return None

    def update_profile_photo(self, jid: str, url: str, photo_id: str = "", tenant_id: str | None = None) -> None:
        """Update cached profile photo URL for a WhatsApp JID."""
        tid = tenant_id or self._tenant_id
        phone = jid.split("@")[0].replace("+", "").strip() if "@" in jid else jid.replace("+", "").strip()
        if not phone or not url:
            return
        try:
            payload = {
                "profile_photo_url": url,
                "profile_photo_id": photo_id,
                "profile_photo_fetched_at": "now()",
            }
            # Use raw RPC-style update since now() needs to be SQL
            self.client.table("user_profiles").update({
                "profile_photo_url": url,
                "profile_photo_id": photo_id,
            }).eq("phone", phone).eq("tenant_id", tid).execute()
        except Exception:
            pass

    # ── Permission helpers ──────────────────────────────────────────

    PERMISSION_LABELS: list[tuple[str, str]] = [
        ("view_inbox", "View Market Inbox"),
        ("reply_whatsapp", "Reply from WhatsApp"),
        ("save_requirements", "Save Requirements"),
        ("save_listings", "Save Listings"),
        ("export_contacts", "Export Contacts"),
        ("view_broker_numbers", "View Broker Numbers"),
        ("add_team_members", "Add Team Members"),
        ("remove_team_members", "Remove Team Members"),
        ("delete_data", "Delete Data"),
        ("ai_actions", "AI Actions"),
        ("bulk_broadcast", "Bulk Broadcast"),
    ]

    def _perm_bitfield(self, keys: list[str]) -> int:
        labels = [k for k, _ in self.PERMISSION_LABELS]
        return sum(1 << i for i, k in enumerate(labels) if k in keys)

    def _perm_keys(self, bitfield: int) -> list[str]:
        labels = [k for k, _ in self.PERMISSION_LABELS]
        return [labels[i] for i in range(len(labels)) if bitfield & (1 << i)]

    # ── Team Members ───────────────────────────────────────────────

    def list_team_members(self, org_id: str | None = None) -> list[dict]:
        try:
            q = self.client.table("team_members").select("*")
            if org_id:
                q = q.eq("organization_id", org_id)
            res = q.order("role", desc=False).order("name", desc=False).execute()
            return res.data if res.data else []
        except Exception:
            return []

    def get_team_member(self, member_id: int) -> dict | None:
        try:
            res = self.client.table("team_members").select("*").eq("id", member_id).limit(1).execute()
            return res.data[0] if res.data else None
        except Exception:
            return None

    def get_team_member_by_email(self, email: str, org_id: str | None = None) -> dict | None:
        email = (email or "").strip().lower()
        if not email:
            return None
        try:
            q = self.client.table("team_members").select("*").ilike("email", email)
            if org_id:
                q = q.eq("organization_id", org_id)
            res = q.limit(1).execute()
            return res.data[0] if res.data else None
        except Exception:
            return None

    def create_team_member(self, name: str, email: str = "", phone: str = "",
                           role: str = "member", permission_keys: list[str] | None = None,
                           linked_broker_phone: str | None = None,
                           organization_id: str | None = None) -> dict:
        permissions = self._perm_bitfield(permission_keys or [])
        payload = {
            "name": name.strip(),
            "email": email.strip() or None,
            "phone": phone.strip() or None,
            "role": role,
            "permissions": permissions,
            "linked_broker_phone": linked_broker_phone,
            "organization_id": organization_id or self.tenant_id,
        }
        res = self.client.table("team_members").insert(payload).execute()
        return res.data[0] if res and res.data else {}

    def update_team_member(self, member_id: int, **kwargs) -> dict | None:
        member = self.get_team_member(member_id)
        if not member:
            return None
        fields = {}
        for k in ("name", "email", "phone", "role", "linked_broker_phone"):
            v = kwargs.get(k)
            if v is not None:
                fields[k] = v.strip() if isinstance(v, str) else v
        if "permission_keys" in kwargs:
            fields["permissions"] = self._perm_bitfield(kwargs["permission_keys"])
        if "is_active" in kwargs:
            fields["is_active"] = 1 if kwargs["is_active"] else 0
        if not fields:
            return member
        fields["updated_at"] = "now()"
        res = self.client.table("team_members").update(fields).eq("id", member_id).execute()
        return res.data[0] if res and res.data else None

    def deactivate_team_member(self, member_id: int) -> bool:
        try:
            self.client.table("team_members").update({
                "is_active": 0, "updated_at": "now()"
            }).eq("id", member_id).execute()
            return True
        except Exception:
            return False

    # ── Custom Roles ───────────────────────────────────────────────

    def list_team_roles(self) -> list[dict]:
        try:
            res = self.client.table("team_roles").select("*").order("is_system", desc=True).order("name", desc=False).execute()
            return res.data if res.data else []
        except Exception:
            return []

    def create_team_role(self, name: str, permission_keys: list[str]) -> dict | None:
        try:
            res = self.client.table("team_roles").insert({
                "name": name.strip(),
                "permission_keys": json.dumps(permission_keys),
                "is_system": False,
            }).execute()
            return res.data[0] if res and res.data else None
        except Exception:
            return None

    def update_team_role(self, role_id: int, name: str | None = None, permission_keys: list[str] | None = None) -> dict | None:
        fields = {}
        if name is not None:
            fields["name"] = name.strip()
        if permission_keys is not None:
            fields["permission_keys"] = json.dumps(permission_keys)
        if not fields:
            return self.get_team_role(role_id)
        try:
            res = self.client.table("team_roles").update(fields).eq("id", role_id).execute()
            return res.data[0] if res and res.data else None
        except Exception:
            return None

    def get_team_role(self, role_id: int) -> dict | None:
        try:
            res = self.client.table("team_roles").select("*").eq("id", role_id).limit(1).execute()
            return res.data[0] if res.data else None
        except Exception:
            return None

    def delete_team_role(self, role_id: int) -> bool:
        try:
            self.client.table("team_roles").delete().eq("id", role_id).execute()
            return True
        except Exception:
            return False

    def init_schema(self):
        pass

    # ── Multi-Tenant: Organizations ────────────────────────────────

    def get_organization_by_owner_user_id(self, user_id: str) -> dict | None:
        user_id = str(user_id or "").strip()
        if not user_id:
            return None
        res = self.client.table("organizations").select("*").eq(
            "owner_user_id", user_id
        ).limit(1).execute()
        return res.data[0] if res.data else None

    def get_organization_by_owner_phone(self, phone: str) -> dict | None:
        phone = str(phone or "").strip()
        if not phone:
            return None
        res = self.client.table("organizations").select("*").eq(
            "owner_phone", phone
        ).limit(1).execute()
        return res.data[0] if res.data else None

    def claim_organization_owner(
        self, org_id: str, owner_user_id: str | None, owner_phone: str | None
    ) -> dict | None:
        payload = {
            "owner_user_id": owner_user_id or None,
            "owner_phone": owner_phone or None,
        }
        try:
            res = self.client.table("organizations").update(payload).eq(
                "id", org_id
            ).is_("owner_user_id", "null").execute()
            return res.data[0] if res.data else self.get_organization(org_id)
        except Exception:
            return None

    def create_organization(
        self,
        name: str,
        slug: str,
        owner_user_id: str | None = None,
        owner_phone: str | None = None,
    ) -> dict | None:
        payload = {
            "name": name,
            "slug": slug,
            "owner_user_id": owner_user_id or None,
            "owner_phone": owner_phone or None,
        }
        try:
            res = self.client.table("organizations").insert(payload).execute()
            return res.data[0] if res.data else None
        except Exception:
            # A concurrent retry may have won the owner uniqueness race.
            # Return that row instead of creating a suffixed organization.
            if owner_user_id:
                existing = self.get_organization_by_owner_user_id(owner_user_id)
                if existing:
                    return existing
            if owner_phone:
                existing = self.get_organization_by_owner_phone(owner_phone)
                if existing:
                    return existing
            # Do not return an unrelated organization merely because its slug
            # collided. The caller will either retry with a new slug or report
            # the provisioning failure.
            return None

    def get_organization(self, org_id: str) -> dict | None:
        res = self.client.table("organizations").select("*").eq("id", org_id).limit(1).execute()
        return res.data[0] if res.data else None

    def get_organization_by_slug(self, slug: str) -> dict | None:
        res = self.client.table("organizations").select("*").eq("slug", slug).limit(1).execute()
        return res.data[0] if res.data else None

    def list_organizations(self, limit: int = 100, offset: int = 0) -> list[dict]:
        res = self.client.table("organizations").select("*")\
            .order("created_at", desc=True).limit(limit).offset(offset).execute()
        return res.data or []

    def update_organization(self, org_id: str, **updates) -> bool:
        res = self.client.table("organizations").update(updates).eq("id", org_id).execute()
        return bool(res.data)

    def add_organization_member(self, org_id: str, user_id: str, role_id: int | None = None) -> dict | None:
        res = self.client.table("organization_members").upsert({
            "organization_id": org_id, "user_id": user_id, "role_id": role_id
        }, on_conflict="organization_id,user_id").execute()
        return res.data[0] if res.data else None

    def remove_organization_member(self, org_id: str, user_id: str) -> bool:
        res = self.client.table("organization_members").delete()\
            .eq("organization_id", org_id).eq("user_id", user_id).execute()
        return bool(res.data)

    def list_organization_members(self, org_id: str) -> list[dict]:
        res = self.client.table("organization_members").select("*, auth.users(email, phone)")\
            .eq("organization_id", org_id).execute()
        return res.data or []

    def get_user_organizations(self, user_id: str) -> list[dict]:
        res = self.client.table("organization_members").select("*, organizations(*)")\
            .eq("user_id", user_id).eq("is_active", True).execute()
        return [m["organizations"] for m in (res.data or []) if m.get("organizations")]

    def get_user_organization_membership(self, user_id: str, org_id: str) -> dict | None:
        res = self.client.table("organization_members").select("*")\
            .eq("user_id", user_id).eq("organization_id", org_id)\
            .eq("is_active", True).limit(1).execute()
        return res.data[0] if res.data else None

    def get_system_role(self, slug: str) -> dict | None:
        res = self.client.table("roles").select("*").eq("slug", slug)\
            .is_("organization_id", "null").limit(1).execute()
        return res.data[0] if res.data else None

    def user_has_org_permission(self, user_id: str, org_id: str, permission_key: str) -> bool:
        membership = self.get_user_organization_membership(user_id, org_id)
        role_id = membership.get("role_id") if membership else None
        if not role_id:
            return False
        return permission_key in self.get_role_permissions(int(role_id))

    # ── Multi-Tenant: Roles & Permissions ─────────────────────────

    def list_roles(self, org_id: str | None = None) -> list[dict]:
        q = self.client.table("roles").select("*")
        if org_id:
            q = q.eq("organization_id", org_id)
        else:
            q = q.is_("organization_id", "null")
        return q.order("name", asc=True).execute().data or []

    def get_role(self, role_id: int) -> dict | None:
        res = self.client.table("roles").select("*").eq("id", role_id).limit(1).execute()
        return res.data[0] if res.data else None

    def create_role(self, org_id: str | None, name: str, slug: str, description: str = "") -> dict | None:
        res = self.client.table("roles").insert({
            "organization_id": org_id, "name": name, "slug": slug, "description": description
        }).execute()
        return res.data[0] if res.data else None

    def list_permissions(self) -> list[dict]:
        res = self.client.table("permissions").select("*").order("category", asc=True).order("label", asc=True).execute()
        return res.data or []

    def get_role_permissions(self, role_id: int) -> list[str]:
        res = self.client.table("role_permissions").select("permissions(key)")\
            .eq("role_id", role_id).execute()
        return [rp["permissions"]["key"] for rp in (res.data or []) if rp.get("permissions")]

    def set_role_permissions(self, role_id: int, permission_keys: list[str]):
        self.client.table("role_permissions").delete().eq("role_id", role_id).execute()
        perms = self.client.table("permissions").select("id").in_("key", permission_keys).execute()
        if perms.data:
            rows = [{"role_id": role_id, "permission_id": p["id"]} for p in perms.data]
            self.client.table("role_permissions").insert(rows).execute()

    def update_member_role(self, org_id: str, user_id: str, role_id: int) -> bool:
        res = self.client.table("organization_members").update({"role_id": role_id})\
            .eq("organization_id", org_id).eq("user_id", user_id).execute()
        return bool(res.data)

    # ── Multi-Tenant: Super Admin ─────────────────────────────────

    def is_super_admin(self, user_id: str) -> bool:
        now = time.monotonic()
        cached_at = self._super_admin_cache.get(str(user_id))
        if cached_at is not None and now - cached_at < 60:
            return True
        try:
            res = self.client.table("super_admins").select("id").eq("user_id", user_id).limit(1).execute()
            allowed = bool(res.data)
            if allowed:
                self._super_admin_cache[str(user_id)] = now
            else:
                self._super_admin_cache.pop(str(user_id), None)
            return allowed
        except Exception:
            # A cached positive role is only for a previously verified user;
            # tenant-scoped data queries still run independently.
            if cached_at is not None and now - cached_at < 600:
                return True
            raise

    def organization_has_super_admin(self, org_id: str) -> bool:
        """Return whether an active organization member is a platform admin."""
        rows = self.client.table("organization_members").select("user_id")\
            .eq("organization_id", org_id).eq("is_active", True).execute().data or []
        return any(
            row.get("user_id") and self.is_super_admin(str(row["user_id"]))
            for row in rows
        )

    def list_super_admins(self) -> list[dict]:
        res = self.client.table("super_admins").select("*").execute()
        return res.data or []

    def add_super_admin(self, user_id: str, phone: str = "") -> dict | None:
        res = self.client.table("super_admins").insert({"user_id": user_id, "phone": phone}).execute()
        return res.data[0] if res.data else None

    def remove_super_admin(self, user_id: str) -> bool:
        res = self.client.table("super_admins").delete().eq("user_id", user_id).execute()
        return bool(res.data)

    # ── Multi-Tenant: WhatsApp Connections ────────────────────────

    def list_org_whatsapp_connections(self, org_id: str) -> list[dict]:
        res = self.client.table("org_whatsapp_connections").select("*")\
            .eq("organization_id", org_id).order("created_at", desc=False).execute()
        return res.data or []

    def list_all_whatsapp_connections(self) -> list[dict]:
        res = self.client.table("org_whatsapp_connections")\
            .select("*, organizations(id,name,slug,is_active)")\
            .order("created_at", desc=False).execute()
        return res.data or []

    def get_org_placeholder_whatsapp_connection(self, org_id: str) -> dict | None:
        for row in self.list_org_whatsapp_connections(org_id):
            phone_number = str(row.get("phone_number") or "").strip()
            digits = "".join(ch for ch in phone_number if ch.isdigit())
            if (
                not phone_number
                or phone_number.startswith("Unpaired")
                or (len(digits) == 10 and digits.startswith("0"))
            ):
                return row
        return None

    def add_org_whatsapp_connection(self, org_id: str, phone_number: str, instance_name: str = "", broker_id: str = "") -> dict | None:
        data = {
            "organization_id": org_id,
            "phone_number": phone_number,
            "instance_name": instance_name,
            "extraction_status": "stopped",
        }
        if broker_id:
            data["broker_id"] = broker_id
        res = self.client.table("org_whatsapp_connections").insert(data).execute()
        return res.data[0] if res.data else None

    def update_org_whatsapp_connection(self, conn_id: int, updates: dict) -> dict | None:
        payload = {k: v for k, v in updates.items() if v is not None}
        if not payload:
            return self.get_org_whatsapp_connection(conn_id)
        res = self.client.table("org_whatsapp_connections").update(payload).eq("id", conn_id).execute()
        return res.data[0] if res.data else None

    def update_org_whatsapp_connection_by_broker_id(self, broker_id: str, updates: dict) -> dict | None:
        broker_id = (broker_id or "").strip()
        payload = {k: v for k, v in updates.items() if v is not None}
        if not broker_id or not payload:
            return None
        res = self.client.table("org_whatsapp_connections").update(payload).eq("broker_id", broker_id).execute()
        return res.data[0] if res.data else None

    def remove_org_whatsapp_connection(self, conn_id: int) -> bool:
        res = self.client.table("org_whatsapp_connections").delete().eq("id", conn_id).execute()
        # PostgREST may legitimately return an empty body for DELETE even when
        # the row was removed.  Verify absence instead of treating the response
        # representation as the success signal.
        return bool(res.data) or self.get_whatsapp_connection_unscoped(conn_id) is None

    def list_org_whatsapp_phone_directory(self, org_id: str) -> list[dict]:
        res = self.client.table("org_whatsapp_phone_directory").select("*")\
            .eq("organization_id", org_id).order("created_at", desc=False).execute()
        return res.data or []

    def get_org_whatsapp_phone_directory(self, entry_id: str) -> dict | None:
        res = self.client.table("org_whatsapp_phone_directory").select("*")\
            .eq("id", entry_id).limit(1).execute()
        if not res.data:
            return None
        if self._tenant_id and str(res.data[0].get("organization_id") or "") != str(self._tenant_id):
            return None
        return res.data[0]

    def get_org_whatsapp_phone_directory_by_broker_id(self, broker_id: str) -> dict | None:
        broker_id = (broker_id or "").strip()
        if not broker_id:
            return None
        res = self.client.table("org_whatsapp_phone_directory").select("*")\
            .eq("broker_id", broker_id).limit(1).execute()
        return res.data[0] if res.data else None

    def add_org_whatsapp_phone_directory(
        self,
        org_id: str,
        broker_id: str,
        phone_number: str,
        display_label: str = "",
        is_active: bool = True,
    ) -> dict | None:
        data = {
            "organization_id": org_id,
            "broker_id": broker_id,
            "phone_number": phone_number,
            "display_label": display_label or "",
            "is_active": bool(is_active),
        }
        res = self.client.table("org_whatsapp_phone_directory").insert(data).execute()
        return res.data[0] if res.data else None

    def update_org_whatsapp_phone_directory(self, entry_id: str, updates: dict) -> dict | None:
        payload = {k: v for k, v in (updates or {}).items() if v is not None}
        if not payload:
            return self.get_org_whatsapp_phone_directory(entry_id)
        payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        res = self.client.table("org_whatsapp_phone_directory")\
            .update(payload).eq("id", entry_id).execute()
        return res.data[0] if res.data else None

    def remove_org_whatsapp_phone_directory(self, entry_id: str) -> bool:
        res = self.client.table("org_whatsapp_phone_directory")\
            .delete().eq("id", entry_id).execute()
        return bool(res.data) or self.get_org_whatsapp_phone_directory(entry_id) is None

    def update_org_whatsapp_phone_directory_by_broker_id(self, broker_id: str, updates: dict) -> dict | None:
        broker_id = (broker_id or "").strip()
        payload = {k: v for k, v in (updates or {}).items() if v is not None}
        if not broker_id or not payload:
            return None
        payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        res = self.client.table("org_whatsapp_phone_directory")\
            .update(payload).eq("broker_id", broker_id).execute()
        return res.data[0] if res.data else None

    def get_org_whatsapp_connection(self, conn_id: int) -> dict | None:
        query = self.client.table("org_whatsapp_connections").select("*").eq("id", conn_id).limit(1)
        if self._tenant_id:
            query = query.eq("organization_id", self._tenant_id)
        res = query.execute()
        return res.data[0] if res.data else None

    def get_whatsapp_connection_unscoped(self, conn_id: int) -> dict | None:
        res = self.client.table("org_whatsapp_connections").select("*")\
            .eq("id", conn_id).limit(1).execute()
        return res.data[0] if res.data else None

    def get_org_whatsapp_connection_by_broker_id(self, broker_id: str) -> dict | None:
        """Lookup phone connection by broker_id. No tenant scoping — used by webhook to resolve tenant."""
        res = self.client.table("org_whatsapp_connections").select("organization_id, broker_id, phone_number, instance_name, is_active, self_chat_enabled, extraction_status").eq("broker_id", broker_id).limit(1).execute()
        return res.data[0] if res.data else None

    def get_running_extraction_tenant_ids(self) -> list[str] | None:
        """Return tenants allowed to consume raw messages.

        ``None`` means the control-plane lookup failed and the worker should
        fail open for compatibility. An empty list means all extraction is
        intentionally paused/stopped.
        """
        try:
            response = self.client.table("org_whatsapp_connections").select(
                "organization_id,extraction_status,is_active"
            ).execute()
            return sorted({
                str(row["organization_id"])
                for row in (response.data or [])
                if row.get("organization_id")
                and row.get("is_active", True)
                and row.get("extraction_status", "running") == "running"
            })
        except Exception:
            return None

    def set_raw_group_extraction_suppressed(self, tenant_id: str, group_jid: str, suppressed: bool) -> None:
        """Pause/resume queued extraction for one opted-out group.

        Raw messages are intentionally retained. This only changes whether
        the extraction queue may consume unprocessed rows for the group.
        """
        if not tenant_id or not group_jid:
            return
        self.client.table("raw_messages").update({
            "extraction_suppressed": bool(suppressed),
        }).eq("tenant_id", tenant_id).eq("group_name", group_jid).eq("processed", False).execute()

    def set_raw_message_extraction_suppressed(self, raw_id: int, suppressed: bool) -> None:
        if raw_id:
            self.client.table("raw_messages").update({
                "extraction_suppressed": bool(suppressed),
            }).eq("id", raw_id).eq("processed", False).execute()

    def reenable_selected_extraction_rows(self, limit: int = 500) -> int:
        """Boundedly release rows from groups that are selected for extraction.

        Older worker cycles marked non-selected rows suppressed. When a broker
        later selects a group, those rows must become eligible again without
        performing an unbounded raw_messages update in the control-plane API.
        """
        remaining = max(1, min(int(limit), 2000))
        selected = self.client.table("organization_group_connections").select(
            "organization_id,group_jid"
        ).eq("is_active", True).eq("opted_out", False).limit(500).execute().data or []
        reenabled = 0
        for group in selected:
            if remaining <= 0:
                break
            tenant_id = str(group.get("organization_id") or "").strip()
            group_jid = str(group.get("group_jid") or "").strip()
            if not tenant_id or not group_jid:
                continue
            rows = self.client.table("raw_messages").select("id").eq(
                "tenant_id", tenant_id
            ).eq("group_name", group_jid).eq("processed", False).eq(
                "extraction_suppressed", True
            ).order("id", desc=False).limit(min(remaining, 100)).execute().data or []
            ids = [int(row["id"]) for row in rows if row.get("id") is not None]
            if not ids:
                continue
            self.client.table("raw_messages").update({
                "extraction_suppressed": False,
            }).in_("id", ids).eq("processed", False).execute()
            reenabled += len(ids)
            remaining -= len(ids)
        return reenabled

    def get_opted_out_extraction_groups(self) -> set[tuple[str, str]] | None:
        """Return (tenant_id, group_jid) pairs excluded from extraction."""
        try:
            res = self.client.table("organization_group_connections").select(
                "organization_id,group_jid"
            ).eq("opted_out", True).execute()
            return {
                (str(row["organization_id"]), str(row["group_jid"]))
                for row in (res.data or [])
                if row.get("organization_id") and row.get("group_jid")
            }
        except Exception:
            return None

    def get_active_org_whatsapp_connection_by_phone(self, phone: str) -> dict | None:
        """Resolve a QR-linked WhatsApp number to its owning workspace."""
        target = re.sub(r"\D+", "", phone or "")[-10:]
        if len(target) != 10:
            return None
        res = self.client.table("org_whatsapp_connections").select(
            "id,organization_id,broker_id,phone_number,instance_name,is_active,self_chat_enabled"
        ).eq("is_active", True).execute()
        for row in res.data or []:
            candidate = re.sub(r"\D+", "", str(row.get("phone_number") or ""))[-10:]
            if candidate == target:
                return row
        return None

    def get_org_waba_connection(self, org_id: str) -> dict | None:
        """Return the server-only Cloud API connection for one workspace."""
        res = self.client.table("org_waba_connections").select("*")\
            .eq("organization_id", org_id).limit(1).execute()
        return res.data[0] if res.data else None

    def get_org_waba_connection_by_phone_number_id(self, phone_number_id: str) -> dict | None:
        """Resolve an incoming Meta webhook to its owning workspace."""
        phone_number_id = str(phone_number_id or "").strip()
        if not phone_number_id:
            return None
        res = self.client.table("org_waba_connections").select("*")\
            .eq("phone_number_id", phone_number_id).eq("is_active", True).limit(1).execute()
        return res.data[0] if res.data else None

    def upsert_org_waba_connection(self, org_id: str, values: dict) -> dict | None:
        """Create or update a workspace Cloud API connection without crossing tenants."""
        payload = {
            "organization_id": org_id,
            **{key: value for key, value in values.items() if value is not None},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        res = self.client.table("org_waba_connections").upsert(
            payload, on_conflict="organization_id"
        ).execute()
        return res.data[0] if res.data else None

    def list_whatsapp_access(self, org_id: str) -> list[dict]:
        members = self.list_team_members(org_id=org_id)
        phones = self.list_org_whatsapp_connections(org_id)
        member_ids = [member["id"] for member in members if member.get("id") is not None]
        explicit: dict[tuple[int, str], dict] = {}
        if member_ids:
            res = self.client.table("team_member_whatsapp_access").select("*")\
                .in_("team_member_id", member_ids).execute()
            explicit = {
                (int(row["team_member_id"]), str(row.get("whatsapp_number") or "")): row
                for row in (res.data or [])
            }

        matrix: list[dict] = []
        for member in members:
            permission_keys = self._perm_keys(int(member.get("permissions") or 0))
            for phone in phones:
                number = str(phone.get("phone_number") or "")
                row = explicit.get((int(member["id"]), number))
                matrix.append({
                    "id": row.get("id") if row else None,
                    "team_member_id": member["id"],
                    "member_name": member.get("name") or member.get("email") or "Team member",
                    "member_email": member.get("email") or "",
                    "whatsapp_connection_id": phone.get("id"),
                    "whatsapp_number": number,
                    "instance_name": phone.get("instance_name") or "",
                    "broker_id": phone.get("broker_id") or "",
                    "can_send": bool(row.get("can_send")) if row else "reply_whatsapp" in permission_keys,
                    "can_view_messages": bool(row.get("can_view_messages")) if row else "view_inbox" in permission_keys,
                    "is_explicit": row is not None,
                })
        return matrix

    def set_whatsapp_access(self, team_member_id: int, whatsapp_number: str,
                            can_send: bool = False, can_view_messages: bool = True,
                            org_id: str | None = None) -> dict | None:
        member = self.get_team_member(team_member_id)
        if not member or (org_id and str(member.get("organization_id")) != str(org_id)):
            return None
        if org_id:
            valid_numbers = {
                str(phone.get("phone_number") or "")
                for phone in self.list_org_whatsapp_connections(org_id)
            }
            if whatsapp_number not in valid_numbers:
                return None
        payload = {
            "team_member_id": team_member_id,
            "whatsapp_number": whatsapp_number,
            "can_send": bool(can_send),
            "can_view_messages": bool(can_view_messages),
        }
        res = self.client.table("team_member_whatsapp_access").upsert(
            payload, on_conflict="team_member_id,whatsapp_number"
        ).execute()
        return res.data[0] if res.data else None

    def get_member_whatsapp_access(self, team_member_id: int, org_id: str) -> list[dict]:
        numbers = {
            str(phone.get("phone_number") or "")
            for phone in self.list_org_whatsapp_connections(org_id)
        }
        if not numbers:
            return []
        res = self.client.table("team_member_whatsapp_access").select("*")\
            .eq("team_member_id", team_member_id).in_("whatsapp_number", list(numbers)).execute()
        return res.data or []

    def get_phone_broker_id(self, conn_id: int) -> str | None:
        row = self.get_org_whatsapp_connection(conn_id)
        return row.get("broker_id") if row else None

    def count_org_phones(self, org_id: str) -> int:
        res = self.client.table("org_whatsapp_connections").select("id", count="exact")\
            .eq("organization_id", org_id).execute()
        return res.count if hasattr(res, "count") else 0

    # ── Raw Messages ─────────────────────────────────────────────

    RAW_MESSAGE_COLUMNS = {
        "group_name", "sender", "sender_jid", "sender_phone",
        "message", "message_hash", "author_content_fingerprint", "repeat_of_raw_message_id",
        "message_type", "attachments", "reply_context",
        "timestamp", "source", "raw_payload", "message_uid",
        "is_group", "processed", "processed_at", "tenant_id",
        "parent_message_id", "split_index",
        "extraction_superseded",
        "created_at",
    }
    RAW_MESSAGE_SELECT_COLUMNS = (
        "id,group_name,sender,sender_jid,sender_phone,message,message_hash,author_content_fingerprint,repeat_of_raw_message_id,message_type,"
        "attachments,reply_context,timestamp,source,raw_payload,message_uid,is_group,"
        "pipeline_version,synced_at,event_id,processed,processed_at,tenant_id,"
        "parent_message_id,split_index,extraction_superseded,created_at,extraction_attempts,extraction_last_error,"
        "extraction_outcome"
    )

    def save_raw_message(self, msg: RawMessage) -> int:
        data = {k: v for k, v in msg.__dict__.items()
                if v is not None and k in self.RAW_MESSAGE_COLUMNS}
        data.pop("id", None)
        if not data.get("tenant_id") and self._tenant_id:
            data["tenant_id"] = self._tenant_id
        if isinstance(data.get("attachments"), str):
            try:
                data["attachments"] = json.loads(data["attachments"])
            except (json.JSONDecodeError, TypeError):
                data["attachments"] = []
        if isinstance(data.get("reply_context"), str):
            try:
                data["reply_context"] = json.loads(data["reply_context"])
            except (json.JSONDecodeError, TypeError):
                data["reply_context"] = {}
        if isinstance(data.get("raw_payload"), str):
            try:
                data["raw_payload"] = json.loads(data["raw_payload"])
            except (json.JSONDecodeError, TypeError):
                data["raw_payload"] = {}
        if "created_at" in data and not data["created_at"]:
            del data["created_at"]
        res = self.client.table("raw_messages").insert(data).execute()
        return res.data[0]["id"] if res.data else 0

    def get_raw_messages(self, limit: int = 50, offset: int = 0,
                          group_name: str = "", sender: str = "",
                          sender_phone: str = "", sender_jid: str = "",
                          source: str = "") -> list[RawMessage]:
        # Enforce a storage-level ceiling as a final guard even when an
        # internal caller forgets to clamp pagination at the route boundary.
        limit = max(1, min(int(limit or 1), int(os.getenv("PROPAI_MAX_PAGE_SIZE", "100"))))
        offset = max(0, min(int(offset or 0), int(os.getenv("PROPAI_MAX_OFFSET", "10000"))))
        # Select only columns needed for RawMessage dataclass to avoid full row fetch
        cols = (
            "id, group_name, sender, sender_jid, sender_phone, message, message_hash, author_content_fingerprint, repeat_of_raw_message_id, message_type, "
            "attachments, reply_context, timestamp, source, raw_payload, message_uid, "
            "is_group, pipeline_version, synced_at, event_id, processed, processed_at, tenant_id, parent_message_id, split_index, extraction_superseded, created_at"
        )
        query = self.client.table("raw_messages").select(cols).order("timestamp", desc=True).limit(limit).offset(offset)
        if group_name:
            query = query.eq("group_name", group_name)
        if sender:
            query = query.eq("sender", sender)
        if sender_phone:
            query = query.eq("sender_phone", sender_phone)
        if sender_jid:
            query = query.eq("sender_jid", sender_jid)
        if source:
            query = query.eq("source", source)
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        return [dict_to_dataclass(RawMessage, d) for d in res.data]

    def get_raw_message(self, msg_id: int) -> RawMessage | None:
        res = self.client.table("raw_messages").select("*").eq("id", msg_id).limit(1).execute()
        if res.data:
            return dict_to_dataclass(RawMessage, res.data[0])
        return None

    def get_raw_message_by_hash(
        self,
        message_hash: str,
        *,
        tenant_id: str | None = None,
        processed: bool | None = None,
        exclude_raw_id: int | None = None,
        with_parsed: bool = False,
    ) -> dict | None:
        digest = (message_hash or "").strip()
        if not digest:
            return None
        query = self.client.table("raw_messages").select("*").eq("message_hash", digest).order("id", desc=False).limit(20)
        tid = tenant_id or self._tenant_id
        if tid:
            query = query.eq("tenant_id", tid)
        if processed is not None:
            query = query.eq("processed", bool(processed))
        if exclude_raw_id is not None:
            query = query.neq("id", int(exclude_raw_id))
        res = query.execute()
        rows = res.data or []
        if not rows:
            return None
        if with_parsed:
            for row in rows:
                parsed = self.get_parsed_by_message(int(row.get("id") or 0))
                if parsed:
                    return {"raw": row, "parsed": [dict(item.__dict__) for item in parsed]}
            return None
        return rows[0]

    def find_author_content_repeat(
        self,
        fingerprint: str,
        *,
        tenant_id: str | None = None,
        exclude_raw_id: int | None = None,
        sender_phone: str = "",
        sender_jid: str = "",
        message: str = "",
    ) -> dict | None:
        """Find an earlier processed message from the same author and copy."""
        digest = str(fingerprint or "").strip()
        if not digest:
            return None
        query = self.client.table("raw_messages").select(
            "id,tenant_id,sender_jid,sender_phone,group_name,timestamp"
        ).eq("author_content_fingerprint", digest).eq("processed", True).order("id", desc=False).limit(50)
        tid = tenant_id or self._tenant_id
        if tid:
            query = query.eq("tenant_id", tid)
        if exclude_raw_id is not None:
            query = query.neq("id", int(exclude_raw_id))
        rows = query.execute().data or []
        # Older processed rows predate this column.  Look back only within the
        # same resolved author, compute the new fingerprint in Python, and
        # backfill the matching row so this change also converges existing
        # inventory instead of waiting for a new baseline post.
        if not rows and message and (sender_phone or sender_jid):
            from message_identity import author_content_fingerprint
            identity_query = self.client.table("raw_messages").select(
                "id,tenant_id,sender_jid,sender_phone,group_name,timestamp,message"
            ).eq("processed", True).order("id", desc=False).limit(100)
            if sender_phone:
                identity_query = identity_query.eq("sender_phone", sender_phone)
            else:
                identity_query = identity_query.eq("sender_jid", sender_jid)
            if tid:
                identity_query = identity_query.eq("tenant_id", tid)
            if exclude_raw_id is not None:
                identity_query = identity_query.neq("id", int(exclude_raw_id))
            for candidate in identity_query.execute().data or []:
                candidate_fingerprint = author_content_fingerprint(
                    sender_phone=str(candidate.get("sender_phone") or ""),
                    sender_jid=str(candidate.get("sender_jid") or ""),
                    message=str(candidate.get("message") or ""),
                )
                if candidate_fingerprint == digest:
                    rows = [candidate]
                    self.set_raw_author_content_fingerprint(
                        int(candidate.get("id") or 0), candidate_fingerprint
                    )
                    break
        for row in rows:
            parsed = self.get_parsed_by_message(int(row.get("id") or 0))
            # Split broadcasts keep typed observations on their child raw
            # messages. Resolve those children before declaring that an exact
            # repost has no canonical rows; otherwise the full repost is sent
            # through splitting/LLM again.
            if not parsed:
                child_query = self.client.table("raw_messages").select("id").eq(
                    "parent_message_id", int(row.get("id") or 0)
                ).eq("processed", True).order("split_index", desc=False).limit(500)
                if tid:
                    child_query = child_query.eq("tenant_id", tid)
                children = child_query.execute().data or []
                for child in children:
                    parsed.extend(self.get_parsed_by_message(int(child.get("id") or 0)))
            if parsed:
                return {"raw": row, "parsed": parsed}
        return None

    def record_repeat_observation(
        self,
        raw_id: int,
        repeat_of_raw_id: int,
        parsed_rows: list[ParsedObservation],
        *,
        observed_at: str | None = None,
    ) -> list[int]:
        """Keep the raw repost, refresh canonical rows, and mark it skipped."""
        seen_at = observed_at or datetime.now(timezone.utc).isoformat()
        try:
            seen_dt = datetime.fromisoformat(str(seen_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            seen_dt = datetime.now(timezone.utc)
        expires_at = (seen_dt + timedelta(days=30)).isoformat()
        touched: list[int] = []
        for parsed in parsed_rows:
            table = str(getattr(parsed, "source_schema", "") or "")
            parsed_id = int(getattr(parsed, "id", 0) or 0)
            if table not in _ALL_TYPED_TABLES or not parsed_id:
                continue
            query = self.client.table(table).update({
                "last_seen_at": seen_dt.isoformat(),
                "expires_at": expires_at,
            }).eq("id", parsed_id)
            if self._tenant_id:
                query = query.eq("tenant_id", self._tenant_id)
            result = query.execute()
            if result.data:
                touched.append(parsed_id)
        self.client.table("raw_messages").update({
            "processed": True,
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "repeat_of_raw_message_id": int(repeat_of_raw_id),
            "extraction_outcome": "repeat_observation",
        }).eq("id", int(raw_id)).execute()
        return touched

    def set_raw_message_hash(self, raw_id: int, message_hash: str) -> None:
        if not raw_id or not message_hash:
            return
        self.client.table("raw_messages").update({
            "message_hash": message_hash,
        }).eq("id", raw_id).execute()

    def set_raw_author_content_fingerprint(self, raw_id: int, fingerprint: str) -> None:
        if not raw_id or not fingerprint:
            return
        self.client.table("raw_messages").update({
            "author_content_fingerprint": fingerprint,
        }).eq("id", raw_id).execute()

    def get_raw_by_uid(self, message_uid: str) -> Optional[RawMessage]:
        res = self.client.table("raw_messages").select("*").eq("message_uid", message_uid).limit(1).execute()
        if res.data:
            return dict_to_dataclass(RawMessage, res.data[0])
        return None

    def get_all_raw_for_replay(self, tenant_id: str | None = None) -> list[RawMessage]:
        query = self.client.table("raw_messages").select("*")
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        res = query.order("timestamp", desc=True).limit(1000).execute()
        return [dict_to_dataclass(RawMessage, d) for d in res.data]

    def get_unprocessed_raw_messages(self, limit: int = 100) -> list[RawMessage]:
        res = self.client.table("raw_messages").select(self.RAW_MESSAGE_SELECT_COLUMNS)\
            .eq("processed", False)\
            .eq("is_group", True)\
            .eq("extraction_suppressed", False)\
            .order("id", desc=False).limit(limit).execute()
        return [dict_to_dataclass(RawMessage, d) for d in res.data]

    def get_unprocessed_raw_messages_since(self, cutoff: str, limit: int = 100, tenant_ids: list[str] | None = None) -> list[RawMessage]:
        """Return the unprocessed recent lane in message-time FIFO order.

        Keep the ordering aligned with the partial timestamp index.  Ordering
        by id here made PostgREST scan the entire pending FIFO index while
        filtering out old backlog rows, which could time out as a 500 response
        once the queue became large.
        """
        # A combined IN (...) query has to satisfy one global timestamp
        # ordering across tenants. Postgres then prefers the older global
        # timestamp index and filters tenant_id row-by-row. Fetch each tenant
        # through the tenant-leading partial index instead, then merge the
        # small bounded result sets locally.
        def fetch_one(tenant_id: str | None = None) -> list[dict]:
            # Probe the FIFO using narrow index-friendly rows first. Reading
            # raw_payload during the ordered scan makes Postgres perform wide
            # heap I/O before it knows which 25 rows survive the filters.
            keys = self.client.table("raw_messages").select("id,timestamp") \
                .eq("processed", False) \
                .eq("is_group", True) \
                .eq("extraction_suppressed", False) \
                .gte("timestamp", cutoff)
            if tenant_id is not None:
                keys = keys.eq("tenant_id", tenant_id)
            key_rows = keys.order("timestamp", desc=False).order("id", desc=False).limit(limit).execute().data or []
            ids = [int(item["id"]) for item in key_rows if item.get("id") is not None]
            if not ids:
                return []
            details = self.client.table("raw_messages").select(self.RAW_MESSAGE_SELECT_COLUMNS).in_("id", ids).execute().data or []
            by_id = {int(item["id"]): item for item in details}
            return [by_id[item_id] for item_id in ids if item_id in by_id]

        if tenant_ids is not None:
            rows = [item for tenant_id in tenant_ids for item in fetch_one(tenant_id)]
            rows.sort(key=lambda item: (item.get("timestamp") or "", int(item.get("id") or 0)))
            return [dict_to_dataclass(RawMessage, d) for d in rows[:limit]]
        return [dict_to_dataclass(RawMessage, d) for d in fetch_one()]

    def get_unprocessed_raw_messages_before(self, cutoff: str, limit: int = 100, tenant_ids: list[str] | None = None) -> list[RawMessage]:
        """Return the unprocessed historical lane in message-time FIFO order.

        Legacy rows with a null timestamp are included in the backlog so they
        cannot be stranded by the two-lane cutoff.
        """
        def fetch_one(tenant_id: str | None = None) -> list[dict]:
            keys = self.client.table("raw_messages").select("id,timestamp") \
                .eq("processed", False) \
                .eq("is_group", True) \
                .eq("extraction_suppressed", False) \
                .or_(f"timestamp.lt.{cutoff},timestamp.is.null")
            if tenant_id is not None:
                keys = keys.eq("tenant_id", tenant_id)
            key_rows = keys.order("id", desc=False).limit(limit).execute().data or []
            ids = [int(item["id"]) for item in key_rows if item.get("id") is not None]
            if not ids:
                return []
            details = self.client.table("raw_messages").select(self.RAW_MESSAGE_SELECT_COLUMNS).in_("id", ids).execute().data or []
            by_id = {int(item["id"]): item for item in details}
            return [by_id[item_id] for item_id in ids if item_id in by_id]

        if tenant_ids is not None:
            rows = [item for tenant_id in tenant_ids for item in fetch_one(tenant_id)]
            rows.sort(key=lambda item: int(item.get("id") or 0))
            return [dict_to_dataclass(RawMessage, d) for d in rows[:limit]]
        return [dict_to_dataclass(RawMessage, d) for d in fetch_one()]

    def mark_raw_processed(self, raw_id: int):
        now = datetime.now(timezone.utc).isoformat()
        self.client.table("raw_messages").update({
            "processed": True,
            "processed_at": now,
        }).eq("id", raw_id).execute()
        child = self.client.table("raw_messages").select("parent_message_id").eq("id", int(raw_id)).limit(1).execute().data or []
        parent_id = child[0].get("parent_message_id") if child else None
        if parent_id is not None:
            repair = self.client.table("extraction_repair_jobs").select("id").eq("parent_raw_id", int(parent_id)).limit(1).execute().data or []
            if repair:
                children = self.client.table("raw_messages").select("processed").eq("parent_message_id", int(parent_id)).execute().data or []
                if children and all(bool(row.get("processed")) for row in children):
                    self.update_extraction_repair_job(
                        int(repair[0]["id"]),
                        status="completed",
                        completed_at=now,
                    )

    def mark_raw_extraction_superseded(self, raw_id: int, job_id: int | None = None) -> bool:
        payload = {"extraction_superseded": True}
        result = self.client.table("raw_messages").update(payload).eq("id", int(raw_id)).execute()
        return bool(result.data is not None)

    def get_extraction_repair_jobs(self, *, limit: int = 50) -> list[dict]:
        return self.client.table("extraction_repair_jobs").select("*").order("created_at", desc=True).limit(max(1, min(int(limit), 200))).execute().data or []

    def get_extraction_repair_status(self) -> dict:
        jobs = self.get_extraction_repair_jobs(limit=200)
        counts = {status: 0 for status in ("queued", "running", "completed", "no_split", "failed")}
        rows = self.client.table("extraction_repair_jobs").select("status").limit(10000).execute().data or []
        for row in rows:
            status = str(row.get("status") or "")
            if status in counts:
                counts[status] += 1
        return {"counts": counts, "recent_jobs": jobs}

    def list_extraction_repair_candidates(self, *, limit: int = 100) -> list[dict]:
        query = self.client.table("raw_messages").select(
            "id,group_name,sender,sender_jid,sender_phone,message,message_uid,message_type,"
            "attachments,reply_context,timestamp,source,raw_payload,is_group,pipeline_version,"
            "synced_at,event_id,processed,processed_at,tenant_id,parent_message_id,split_index,"
            "extraction_superseded,created_at"
        ).eq("processed", True).eq("extraction_superseded", False).is_("parent_message_id", "null").order("id", desc=False).limit(max(1, min(int(limit), 500)))
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        return query.execute().data or []

    def upsert_extraction_repair_job(self, parent_raw_id: int, *, tenant_id: str | None = None, requested_by: str | None = None, existing_parsed_count: int = 0) -> dict:
        payload = {
            "parent_raw_id": int(parent_raw_id),
            "tenant_id": tenant_id or self._tenant_id,
            "requested_by": requested_by,
            "existing_parsed_count": int(existing_parsed_count or 0),
            "status": "queued",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        result = self.client.table("extraction_repair_jobs").upsert(payload, on_conflict="parent_raw_id").execute()
        return (result.data or [{}])[0]

    def update_extraction_repair_job(self, job_id: int, **updates) -> dict:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = self.client.table("extraction_repair_jobs").update(updates).eq("id", int(job_id)).execute()
        return (result.data or [{}])[0]

    def claim_extraction_repair_jobs(self, *, limit: int = 2) -> list[dict]:
        result = self.client.rpc("claim_extraction_repair_jobs", {"p_limit": max(1, min(int(limit), 10))})
        data = result if isinstance(result, list) else []
        return [dict(row) for row in data if isinstance(row, dict)]

    def begin_raw_extraction_attempt(self, raw_id: int, lane: str = "") -> dict:
        """Atomically increment and persist one extraction attempt."""
        result = self.client.rpc("begin_extraction_attempt", {
            "p_raw_message_id": int(raw_id),
            "p_lane": str(lane or "")[:20],
        })
        data = result or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        return dict(data) if isinstance(data, dict) else {}

    def finish_raw_extraction_attempt(self, attempt_id: int, status: str,
                                      reason: str = "", details: dict | None = None) -> bool:
        result = self.client.rpc("finish_extraction_attempt", {
            "p_attempt_id": int(attempt_id),
            "p_status": str(status or "failed")[:30],
            "p_reason": str(reason or "")[:500],
            "p_details": details or {},
        })
        return result is not False

    def dead_letter_raw_extraction(self, raw_id: int, reason: str,
                                   lane: str = "") -> bool:
        result = self.client.rpc("dead_letter_extraction", {
            "p_raw_message_id": int(raw_id),
            "p_reason": str(reason or "")[:500],
            "p_lane": str(lane or "")[:20],
        })
        return result is not False

    def count_unprocessed_raw(self) -> int:
        res = self.client.table("raw_messages").select("id", count="exact")\
            .eq("processed", False).execute()
        return res.count if hasattr(res, "count") else 0

    def has_unprocessed_raw(self) -> bool:
        """Return whether the extraction queue has at least one eligible row.

        Exact counts scan the full raw_messages queue and can hit the
        statement timeout once the backlog is large. The worker only needs an
        existence check before fetching its bounded lanes.
        """
        res = self.client.table("raw_messages").select("id") \
            .eq("processed", False) \
            .eq("extraction_suppressed", False) \
            .limit(1).execute()
        return bool(res.data)

    # ── Sender splitter cache ─────────────────────────────────────

    def get_sender_splitter_cache(
        self,
        sender_key: str,
        *,
        tenant_id: str | None = None,
    ) -> dict | None:
        sender_key = (sender_key or "").strip()
        if not sender_key:
            return None
        query = self.client.table("raw_message_splitter_cache").select("*").eq("sender_key", sender_key).limit(1)
        tid = tenant_id or self._tenant_id
        if tid:
            query = query.eq("tenant_id", tid)
        res = query.execute()
        return res.data[0] if res.data else None

    def upsert_sender_splitter_cache(
        self,
        *,
        sender_key: str,
        pattern_id: str,
        tenant_id: str | None = None,
        sender_phone: str | None = None,
        sender_jid: str | None = None,
        message_hash: str | None = None,
        revalidated: bool = False,
    ) -> dict | None:
        sender_key = (sender_key or "").strip()
        pattern_id = (pattern_id or "").strip()
        if not sender_key or not pattern_id:
            return None
        payload = {
            "tenant_id": tenant_id or self._tenant_id,
            "sender_key": sender_key,
            "sender_phone": sender_phone or None,
            "sender_jid": sender_jid or None,
            "pattern_id": pattern_id,
            "last_message_hash": message_hash or None,
            "last_validated_at": datetime.now(timezone.utc).isoformat(),
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            existing = self.get_sender_splitter_cache(sender_key, tenant_id=payload["tenant_id"])
            if existing and existing.get("id"):
                updates = {
                    "pattern_id": pattern_id,
                    "last_message_hash": payload["last_message_hash"],
                    "last_validated_at": payload["last_validated_at"] if revalidated or not existing.get("last_validated_at") else existing.get("last_validated_at"),
                    "last_seen_at": payload["last_seen_at"],
                    "message_count": int(existing.get("message_count") or 0) + 1,
                    "validated_count": int(existing.get("validated_count") or 0) + (1 if revalidated else 0),
                }
                res = self.client.table("raw_message_splitter_cache").update(updates).eq("id", existing["id"]).execute()
                return res.data[0] if res.data else existing
            payload["message_count"] = 1
            payload["validated_count"] = 1 if revalidated else 0
            res = self.client.table("raw_message_splitter_cache").insert(payload).execute()
            return res.data[0] if res.data else None
        except Exception:
            return None

    @staticmethod
    def _payload_dict(value) -> dict:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value:
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    @staticmethod
    def _jid_phone(jid: str = "") -> str:
        head = (jid or "").split("@", 1)[0]
        digits = re.sub(r"\D", "", head)
        return digits or head

    @staticmethod
    def _is_group_jid(value: str = "") -> bool:
        return (value or "").endswith("@g.us")

    @staticmethod
    def _is_direct_jid(value: str = "") -> bool:
        return (value or "").endswith("@s.whatsapp.net") or (value or "").endswith("@lid")

    def _source_group_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        try:
            res = self.client.table("source_sync_jobs")\
                .select("group_id,group_name")\
                .neq("group_id", "")\
                .execute()
        except Exception:
            return {}, {}
        id_to_name: dict[str, str] = {}
        name_to_id: dict[str, str] = {}
        for row in (res.data or []):
            gid = (row.get("group_id") or "").strip()
            name = (row.get("group_name") or "").strip()
            if gid and name:
                id_to_name[gid] = name
                name_to_id.setdefault(name, gid)
        return id_to_name, name_to_id

    def _raw_chat_identity(
        self,
        row: dict,
        id_to_name: dict[str, str] | None = None,
        name_to_id: dict[str, str] | None = None,
    ) -> tuple[str, str, str]:
        id_to_name = id_to_name or {}
        name_to_id = name_to_id or {}
        payload = self._payload_dict(row.get("raw_payload"))
        key = payload.get("key") if isinstance(payload.get("key"), dict) else {}
        if not key and isinstance(payload.get("data"), dict):
            key = payload["data"].get("key") if isinstance(payload["data"].get("key"), dict) else {}
        chat_id = (
            key.get("remoteJid")
            or payload.get("remoteJid")
            or payload.get("from")
            or ""
        )
        message_uid = (row.get("message_uid") or "").strip()
        if not chat_id and ":" in message_uid:
            uid_parts = message_uid.split(":")
            # Current ingestor UIDs are broker_id:remoteJid:message_id.
            # Prefer the remote JID now that get_chats intentionally omits the
            # large raw_payload column.
            chat_id = uid_parts[1] if len(uid_parts) >= 3 else uid_parts[0]

        group_name = (row.get("group_name") or "").strip()
        sender_jid = (row.get("sender_jid") or "").strip()
        sender_phone = (row.get("sender_phone") or "").strip()
        sender = (row.get("sender") or "").strip()

        if not chat_id and group_name in name_to_id:
            chat_id = name_to_id[group_name]
        if not chat_id and self._is_group_jid(group_name):
            chat_id = group_name
        if not chat_id and self._is_direct_jid(group_name):
            chat_id = group_name
        if not chat_id:
            chat_id = sender_jid or sender_phone or group_name or sender or "unknown"

        chat_type = "group" if self._is_group_jid(chat_id) or (group_name and not self._is_direct_jid(group_name) and group_name not in ("seed", "seed-bot")) else "direct"
        if chat_type == "group":
            chat_name = id_to_name.get(chat_id) or (group_name if not self._is_group_jid(group_name) else "") or chat_id
        else:
            chat_name = _clean_person_name(sender) or sender_phone or self._jid_phone(chat_id) or "Direct Message"
        return chat_id, chat_type, chat_name

    @staticmethod
    def _decorate_chat_row(row: dict, chat_id: str, chat_type: str, chat_name: str, count: int | None = None) -> dict:
        decorated = dict(row)
        decorated["chat_id"] = chat_id
        decorated["chat_type"] = chat_type
        decorated["chat_name"] = chat_name
        decorated["conversation_key"] = chat_id
        decorated["conversation_type"] = "group" if chat_type == "group" else "direct"
        decorated["conversation_name"] = chat_name
        decorated["latest_message_at"] = decorated.get("timestamp") or decorated.get("created_at") or ""
        if count is not None:
            decorated["message_count"] = count
        return decorated

    def get_chats(self, limit: int = 500, offset: int = 0, tenant_id: str | None = None) -> list[dict]:
        query = self.client.table("raw_messages").select(
            "id,group_name,sender,sender_jid,sender_phone,message_type,"
            "timestamp,source,message_uid,created_at,tenant_id"
        )\
            .order("timestamp", desc=True)\
            .limit(min(2000, max(500, limit + offset)))
        tid = tenant_id or self._tenant_id
        if tid:
            query = query.eq("tenant_id", tid)
        res = query.execute()
        rows = res.data or []
        if not rows:
            return []

        id_to_name, name_to_id = self._source_group_maps()
        grouped: dict[str, dict] = {}
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            group_name = (row.get("group_name") or "").strip()
            sender_jid = (row.get("sender_jid") or "").strip()
            if group_name in ("status@broadcast", "broadcast") or group_name.endswith("@broadcast"):
                continue
            if group_name.endswith("@newsletter") or sender_jid.endswith("@newsletter"):
                continue
            chat_id, chat_type, chat_name = self._raw_chat_identity(row, id_to_name, name_to_id)
            if chat_id in ("seed", "seed-bot", "status@broadcast", "broadcast"):
                continue
            counts[chat_id] += 1
            if chat_id not in grouped:
                grouped[chat_id] = self._decorate_chat_row(row, chat_id, chat_type, chat_name)
        chats = []
        for chat_id, latest in grouped.items():
            latest["message_count"] = counts.get(chat_id, 0)
            chats.append(latest)
        chats.sort(key=lambda t: (t.get("timestamp") or t.get("created_at") or "", t.get("id") or 0), reverse=True)
        return chats[offset:offset + limit]

    def get_chat_messages(self, chat_id: str, limit: int = 200, offset: int = 0, tenant_id: str | None = None) -> list[RawMessage]:
        chat_id = (chat_id or "").strip()
        if not chat_id:
            return []
        id_to_name, _ = self._source_group_maps()
        names = [chat_id]
        if chat_id in id_to_name:
            names.append(id_to_name[chat_id])
        digits = self._jid_phone(chat_id)
        tid = tenant_id or self._tenant_id
        collected: dict[int, dict] = {}

        def add_query(field: str, value: str, op: str = "eq"):
            if not value:
                return
            try:
                q = self.client.table("raw_messages").select(
                    "id,group_name,sender,sender_phone,sender_jid,timestamp,created_at,message_uid,message"
                ).order("timestamp", desc=True).limit(limit + offset)
                if tid:
                    q = q.eq("tenant_id", tid)
                if op == "like":
                    q = q.like(field, value)
                else:
                    q = q.eq(field, value)
                for row in (q.execute().data or []):
                    rid = row.get("id")
                    if rid is not None:
                        collected[int(rid)] = row
            except Exception:
                return

        # Current Whatsmeow events use
        #   <broker_id>:<remoteJid>:<message_id>
        # so a chat ID is in the middle of the UID, never at its beginning.
        # This covers existing history as well as live messages without
        # depending on a mutable WhatsApp group name.
        # PostgREST's `like` operator uses `*` (not SQL's `%`) as its
        # URL-level wildcard.
        add_query("message_uid", f"*:{chat_id}:*", "like")
        for name in names:
            add_query("group_name", name)
        add_query("sender_jid", chat_id)
        if digits:
            add_query("sender_phone", digits)

        rows = list(collected.values())
        rows.sort(key=lambda r: (r.get("timestamp") or r.get("created_at") or "", r.get("id") or 0), reverse=True)
        return [dict_to_dataclass(RawMessage, row) for row in rows[offset:offset + limit]]

    def get_inbox_threads(self, limit: int = 500, offset: int = 0, tenant_id: str | None = None) -> list[dict]:
        # Market Inbox is a parsed broker feed.  The raw message is joined by
        # _get_parsed_market_threads only so the UI can open source evidence;
        # raw_messages must never determine which broker posts appear here.
        return self._get_parsed_market_threads(limit, offset, tenant_id=tenant_id)

    def _get_parsed_market_threads(self, limit: int, offset: int, tenant_id: str | None = None) -> list[dict]:
        tid = tenant_id or self._tenant_id
        parsed_rows, raw_map = self._fetch_recent_market_typed_rows(
            tenant_id=tid,
            limit=limit,
            offset=offset,
        )
        parsed_rows = [self._typed_row_to_legacy(row) for row in parsed_rows]
        parsed_rows.sort(
            key=lambda row: str((raw_map.get(int(row.get("raw_message_id") or 0)) or {}).get("timestamp") or row.get("created_at") or ""),
            reverse=True,
        )
        parsed_rows = parsed_rows[:min(4000, max(500, limit + offset))]
        if not parsed_rows:
            return []
        raw_ids = list({p["raw_message_id"] for p in parsed_rows if p.get("raw_message_id")})
        dropped = len(raw_ids) - len(raw_map)
        if dropped > 0:
            _logger.warning(
                "_get_parsed_market_threads: %d raw_message_ids had no matching raw_messages row",
                dropped,
            )

        market_rows: list[tuple[dict, dict, str, str]] = []
        phones_by_name: dict[str, set[str]] = defaultdict(set)
        for parsed in parsed_rows:
            # The typed market projection deliberately omits raw evidence for
            # the list path. Parsed identity is still authoritative enough for
            # directory/search cards; the detail route resolves raw evidence
            # later when a user opens a record.
            raw = raw_map.get(parsed.get("raw_message_id")) or {}
            phone = (
                _normalize_india_phone(parsed.get("broker_phone") or "")
                or _normalize_india_phone(raw.get("sender_phone") or "")
                or _normalize_india_phone((raw.get("sender_jid") or "").split("@")[0])
            )
            name = (
                _clean_person_name(parsed.get("broker_name") or "")
                or _clean_person_name(parsed.get("profile_name") or "")
                or _clean_person_name(raw.get("sender") or "")
            )
            if not phone and not name:
                continue

            market_rows.append((parsed, raw, phone, name))
            name_key = _market_name_key(name)
            if phone and name_key:
                phones_by_name[name_key].add(phone)

        grouped: dict[str, dict] = {}
        for parsed, raw, phone, name in market_rows:
            identity, resolved_phone = _resolve_market_identity(phone, name, phones_by_name)
            ts = raw.get("timestamp") or parsed.get("created_at") or raw.get("created_at") or ""
            bucket = grouped.setdefault(identity, {
                "latest": None,
                "opportunity_keys": set(),
                "listing_keys": set(),
                "requirement_keys": set(),
                "source_group_names": set(),
                "specialty_localities": {},
                "specialty_property_types": {},
                "latest_ts": "",
            })

            intent = (parsed.get("intent") or "").upper()
            observation_type = (
                "REQUIREMENT"
                if (parsed.get("message_type") or "").upper() == "REQUIREMENT"
                or intent in {"BUY", "BUYER", "REQUIREMENT", "RENTAL_SEEKER", "WANTED"}
                else "LISTING"
            )
            opportunity_key = _observation_fingerprint({
                **parsed,
                "observation_type": observation_type,
                "broker_phone": resolved_phone,
                "broker_name": name,
                "source_message": parsed.get("normalized_message") or raw.get("message") or "",
            })
            is_new_opportunity = opportunity_key not in bucket["opportunity_keys"]
            bucket["opportunity_keys"].add(opportunity_key)
            if observation_type == "REQUIREMENT":
                bucket["requirement_keys"].add(opportunity_key)
            else:
                bucket["listing_keys"].add(opportunity_key)
            if raw.get("group_name"):
                bucket["source_group_names"].add(raw.get("group_name"))

            locality = (parsed.get("micro_market") or parsed.get("location_raw") or "").strip()
            if locality and is_new_opportunity:
                bucket["specialty_localities"][locality] = bucket["specialty_localities"].get(locality, 0) + 1
            property_type = (parsed.get("property_type") or parsed.get("asset_type") or "").strip()
            if property_type and is_new_opportunity:
                bucket["specialty_property_types"][property_type] = bucket["specialty_property_types"].get(property_type, 0) + 1

            if not bucket["latest"] or str(ts) > str(bucket["latest_ts"]):
                bucket["latest"] = (parsed, raw, resolved_phone, name, identity)
                bucket["latest_ts"] = ts

        threads: list[dict] = []
        for bucket in grouped.values():
            latest = bucket.get("latest")
            if not latest:
                continue
            parsed, raw, phone, name, identity = latest
            conv_name = name or (phone and phone) or "Unknown broker"
            chat_id = phone or identity
            raw_group = (raw or {}).get("group_name") or ""
            is_group = bool(raw_group) or "@g.us" in (raw_group or "") or "_broadcast" in (raw_group or "")
            raw_row = dict(raw)
            raw_row.update({
                "chat_id": chat_id,
                "chat_type": "group" if is_group else "direct",
                "chat_name": conv_name,
                "conversation_key": identity,
                "conversation_type": "group" if is_group else "direct",
                "conversation_name": conv_name,
                "message_count": len(bucket["opportunity_keys"]),
                "opportunity_count": len(bucket["opportunity_keys"]),
                "listing_count": len(bucket["listing_keys"]),
                "requirement_count": len(bucket["requirement_keys"]),
                "latest_message_at": bucket["latest_ts"],
                "broker_name": name,
                "broker_phone": phone,
                "parsed_intent": parsed.get("intent"),
                "intent": parsed.get("intent"),
                "building_name": parsed.get("building_name"),
                "micro_market": parsed.get("micro_market"),
                "landmark_name": parsed.get("landmark_name"),
                "location_raw": parsed.get("location_raw"),
                "summary_title": parsed.get("summary_title"),
                "source_group_names": sorted(bucket["source_group_names"]),
                "specialty_localities": [
                    value for value, _count in sorted(
                        bucket["specialty_localities"].items(),
                        key=lambda item: (-item[1], item[0].lower()),
                    )[:3]
                ],
                "specialty_property_types": [
                    value for value, _count in sorted(
                        bucket["specialty_property_types"].items(),
                        key=lambda item: (-item[1], item[0].lower()),
                    )[:2]
                ],
                "market_scope": "workspace",
            })
            threads.append(raw_row)

        threads.sort(key=lambda t: (t.get("latest_message_at") or t.get("timestamp") or t.get("created_at") or ""), reverse=True)
        return threads[offset:offset + limit]

    def count_raw_messages(self, group_name: str = "") -> int:
        query = self.client.table("raw_messages").select("id", count="exact")
        if group_name:
            query = query.eq("group_name", group_name)
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        return res.count or 0

    # ── Broker Identity Resolution ──────────────────────────────

    def _canonical_broker_name(self, broker_id: object) -> str:
        """Return the authoritative profile name for a resolved broker."""
        try:
            broker_id = int(broker_id)
        except (TypeError, ValueError):
            return ""
        try:
            rows = self.client.table("brokers").select("canonical_name").eq(
                "id", broker_id
            ).limit(1).execute().data or []
        except Exception:
            return ""
        return _effective_broker_name(source_name=(rows[0].get("canonical_name") if rows else ""))

    def _resolve_whatsapp_display_name(self, jid: str = "", phone: str = "") -> str:
        """Best-effort contact lookup; missing contact tables never break ingest."""
        candidates = [value for value in (jid, phone, _jid_phone(jid)) if value]
        for lookup in candidates:
            for column in ("their_jid", "our_jid", "redacted_phone"):
                try:
                    result = self.client.table("whatsmeow_contacts").select("*").eq(column, lookup).limit(1).execute()
                    if result.data:
                        row = result.data[0]
                        for key in ("push_name", "full_name", "first_name", "business_name"):
                            name = _clean_person_name(row.get(key) or "")
                            if name:
                                return name
                except Exception:
                    continue
        return ""

    def resolve_broker(self, broker_phone: str = "", sender_phone: str = "",
                       sender_jid: str = "", broker_name: str = "",
                       profile_name: str = "", sender: str = "") -> Optional[int]:
        """Upsert broker identity and return broker_id.

        Computes the canonical identity the same way the SQL backfill does:
          effective_phone = market_normalize_phone(broker_phone, sender_phone, sender_jid)
          effective_name  = market_clean_person_name(broker_name, profile_name, sender)
          identity_key    = 'phone:' + phone  OR  'name:' + name_key

        Returns the brokers.id (bigint) or None if no identity could be resolved.
        """
        effective_phone = (
            _normalize_india_phone(broker_phone)
            or _normalize_india_phone(sender_phone)
            or _normalize_india_phone(sender_jid)
        )

        effective_name = _effective_broker_name(
            source_name=broker_name,
            profile_name=profile_name,
            sender_name=sender,
            display_name=self._resolve_whatsapp_display_name(sender_jid, effective_phone),
        )
        effective_phone = effective_phone or _jid_phone(broker_phone) or _jid_phone(sender_jid)

        if not effective_phone and not effective_name:
            return None

        if effective_phone:
            identity_key = f"phone:{effective_phone}"
        else:
            identity_key = f"name:{_market_name_key(effective_name)}"

        canonical = effective_name or effective_phone or None
        if not canonical:
            return None
        tenant_id = self._tenant_id

        row = {
            "identity_key": identity_key,
            "primary_phone": effective_phone or None,
            "canonical_name": canonical,
        }
        if tenant_id:
            row["tenant_id"] = tenant_id

        # Upsert — avoids TOCTOU race between concurrent extractions.
        # On conflict (identity_key), update last_seen_at and return the id.
        try:
            res = self.client.table("brokers").upsert(
                row, on_conflict="identity_key"
            ).execute()
            if res.data:
                return res.data[0]["id"]
        except Exception:
            pass

        # Fallback: select if upsert failed (e.g. constraint details differ)
        q = self.client.table("brokers").select("id").eq("identity_key", identity_key)
        if tenant_id:
            q = q.eq("tenant_id", tenant_id)
        res = q.execute()
        if res.data:
            return res.data[0]["id"]
        return None

    # ── Parsed Output ────────────────────────────────────────────

    # Input fields accepted from the legacy ParsedObservation adapter. The
    # adapter still exists for old callers, but persistence is typed below.
    TYPED_OBSERVATION_INPUT_COLUMNS = {
        "raw_message_id", "message_type", "intent", "principal",
        "bhk", "configuration", "price", "price_unit", "price_model",
        "price_per_sqft", "monthly_rent", "total_asking_price",
        "area_sqft", "furnishing", "furnishing_canonical",
        "location", "building_name", "building_id", "landmark_name",
        "street_name", "area", "micro_market", "developer",
        "broker_name", "broker_phone", "profile_name", "listing_index",
        "broker_rera_number",
        "forwarded", "confidence", "raw_payload", "created_at",
        "extraction_confidence_score",
        "summary_title", "reparsed_at", "event_id", "tenant_id",
        "normalized_message",
        "asset_type", "property_type", "transaction_type",
        "commercial_use_type", "fitout_status", "occupancy_type",
        "floor_range", "rent_per_sqft",
        "availability_status", "possession_status", "possession_date",
        "available_from", "availability_date_raw", "ready_by", "construction_stage",
        "launch_timeline", "expected_possession",
        "ai_extraction",
        "deal_tags", "additional_charges", "validation_flags", "needs_review",
        # v2 schema — physical / deal attributes
        "carpet_area_sqft", "built_up_area_sqft",
        "bathroom_count", "car_parking_count", "parking_type",
        "has_lift", "has_power_backup", "power_load_kw",
        "deposit_amount", "oc_status", "interior_value",
        "ceiling_height", "price_basis", "brokerage_type",
        "configuration_type", "configuration_details", "lease_term_type",
        "lease_term_min_months", "lease_term_max_months", "lease_term_raw_text",
        "original_bhk", "current_bhk", "is_converted_unit", "is_combination_unit",
        "balcony_present", "balcony_area_sqft", "balcony_area_raw_text",
        "terrace_area_sqft", "covered_terrace_area_sqft", "terrace_area_raw_text",
        "sit_out_present", "unit_condition", "wing", "floor_min", "floor_max", "floor_label",
        "has_lift", "view_description", "parking_details", "society_restrictions_raw",
        "broker_company", "contacts", "showing_instructions", "contact_instructions",
        "brokerage_context", "brokerage_terms_raw", "plus_one_deal", "fee_sharing_required",
        "client_profile_required", "unstructured_facts",
        # v2 schema — amenities
        "amenities", "amenities_unverified_claim", "building_amenities",
        # v2 schema — rental / tenancy policy
        "pet_policy", "tenant_type_preference", "sharing_allowed",
        "company_lease_criteria", "tenant_nationality_preference",
        "intended_use_details", "area_basis_preference", "location_flexibility",
        "floor_preference", "parking_required", "needs_attached_washroom",
        "needs_washroom", "needs_pantry", "premium_building_required",
        "glass_facade_required", "residential_cum_commercial_ok", "by_lanes_accepted",
        "media_requested", "min_cabin_count", "min_workstation_count",
        "needs_conference_room", "min_washroom_count",
        "chargeable_area_sqft", "floor_level", "floor_count", "price_math",
        "frontage_ft", "entrance_count", "otla_area_sqft", "otla_area_raw_text",
        "heritage_space", "permitted_use_types", "ideal_for", "automatic_shutter_count",
        "room_count", "suite_count", "banquet_hall_count", "restaurant_count",
        "bar_facility", "operational_status", "rent_inclusions", "license_type",
        "short_term_allowed", "inspection_notice_minutes", "director_cabin_count",
        "ceo_cabin_present", "cubicle_count", "conference_room_capacity",
        "meeting_room_capacity", "training_room_capacity", "cafeteria_seat_count",
        "accounts_area", "lounge_area",
        "broker_id",
        "group_name",
    }

    def save_typed_observation(self, parsed: ParsedObservation) -> int:
        """Adapt a legacy observation object into one typed-table row.

        The object shape is retained for callers that still construct
        ``ParsedObservation`` instances, but persistence is routed directly
        to one of the eight typed tables by ``_typed_route``.
        """
        data = {k: v for k, v in parsed.__dict__.items()
                if v is not None and k in self.TYPED_OBSERVATION_INPUT_COLUMNS}
        data.pop("id", None)
        data.pop("embedding", None)
        # Don't send created_at - let DB handle it (NOT NULL with default now())
        data.pop("created_at", None)
        # Fix boolean fields
        if "forwarded" in data and isinstance(data["forwarded"], int):
            data["forwarded"] = bool(data["forwarded"])
        for field in ("raw_payload", "location"):
            if isinstance(data.get(field), str):
                try:
                    data[field] = json.loads(data[field])
                except (json.JSONDecodeError, TypeError):
                    data[field] = {} if field == "raw_payload" else None
        data = _sanitize_parsed_payload(data)
        if not data.get("tenant_id") and self._tenant_id:
            data["tenant_id"] = self._tenant_id
        source_id = _typed_source_id(data)
        table, asset_type, transaction_type = _typed_route(data)
        raw_id = int(data.get("raw_message_id") or 0)
        listing_index = int(data.get("listing_index") or 0)
        raw_payload = data.get("raw_payload") or {}
        ai = data.get("ai_extraction") or {}
        if isinstance(ai, str):
            try:
                ai = json.loads(ai)
            except (TypeError, json.JSONDecodeError):
                ai = {}
        price_obj = ai.get("price") if isinstance(ai, dict) else {}
        if not isinstance(price_obj, dict):
            price_obj = {}
        price_value = data.get("price")
        price_unit = data.get("price_unit") or price_obj.get("unit")
        raw_price_text = price_obj.get("raw_price_text") or data.get("price_raw_text")
        price_rupees = (
            (canonical_commercial_rental_price_rupees if asset_type == "commercial" else canonical_rental_price_rupees)(price_value, price_unit, raw_price_text)
            if transaction_type == "rent"
            else canonical_price_rupees(price_value, price_unit, raw_price_text)
        )
        area_sqft = data.get("carpet_area_sqft") or data.get("area_sqft")
        bhk_value = data.get("bhk")
        if isinstance(bhk_value, str):
            match = re.search(r"\d+(?:\.\d+)?", bhk_value)
            bhk_value = float(match.group(0)) if match else None
        price_unit_key = str(price_unit or "").lower()
        is_per_sqft_price = price_unit_key in {"per_sqft", "psf"}
        source_total_price = price_rupees if transaction_type == "sale" and not is_per_sqft_price else None
        source_rent_price = price_rupees if transaction_type == "rent" and not is_per_sqft_price else None
        total_asking_price = (
            source_total_price
            if source_total_price is not None
            else (data.get("total_asking_price") if transaction_type == "sale" else None)
        )
        monthly_rent = (
            source_rent_price
            if source_rent_price is not None
            else (data.get("monthly_rent") if transaction_type == "rent" else None)
        )
        price_basis_text = str(data.get("price_basis") or "").lower()
        if transaction_type == "rent" and is_per_sqft_price and asset_type == "commercial":
            pricing_area = data.get("chargeable_area_sqft") if "chargeable" in price_basis_text else area_sqft
            if pricing_area and price_rupees is not None:
                monthly_rent = price_rupees * pricing_area

        # The typed tables deliberately retain the complete LLM payload while
        # promoting only fields that belong to the selected schema.
        locality_raw, locality_resolved = _locality_fields(data)
        furnishing_value = data.get("furnishing_canonical") or data.get("furnishing")
        furnishing_value = {
            "ff": "fully_furnished",
            "furnished": "fully_furnished",
            "sf": "semi_furnished",
            "pf": "semi_furnished",
            "none": "unfurnished",
        }.get(str(furnishing_value or "").strip().lower(), furnishing_value)
        possession_value = data.get("possession_status")
        possession_value = {
            "immediate": "ready_to_move",
            "ready": "ready_to_move",
            "ready to move": "ready_to_move",
            "oc avlb": "oc_received",
            "oc available": "oc_received",
        }.get(str(possession_value or "").strip().lower(), possession_value)
        confidence_score = data.get("extraction_confidence_score")
        try:
            confidence_score = float(confidence_score) if confidence_score is not None else 0.0
        except (TypeError, ValueError):
            confidence_score = 0.0
        if confidence_score <= 0:
            confidence_score = {
                "high": 0.9,
                "medium": 0.7,
                "low": 0.4,
            }.get(str(ai.get("extraction_confidence") or "").lower(), confidence_score)
        broker_name = _effective_broker_name(
            source_name=data.get("broker_name") or "",
            profile_name=data.get("profile_name") or "",
            sender_name=data.get("sender") or "",
            display_name=self._resolve_whatsapp_display_name(
                data.get("sender_jid") or "", data.get("broker_phone") or ""
            ),
        ) or None
        broker_name = self._canonical_broker_name(data.get("broker_id")) or broker_name
        common = {
            # Let the typed table's identity column generate its primary key.
            # ``source_fingerprint`` is the retry/idempotency key; the stable
            # legacy observation id is retained separately for old callers.
            "raw_message_id": raw_id,
            "tenant_id": data.get("tenant_id") or self._tenant_id,
            "listing_index": listing_index,
            "source_fingerprint": hashlib.sha256(
                f"typed-observation:{raw_id}:{listing_index}".encode()
            ).hexdigest(),
            # A repost is new evidence, but the same market opportunity.
            "opportunity_key": None,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            "legacy_source_id": source_id,
            "asset_type": asset_type,
            "transaction_type": transaction_type,
            "building_name": data.get("building_name"),
            "building_id": data.get("building_id"),
            "locality_raw": locality_raw,
            "locality_resolved": locality_resolved,
            "micro_market": data.get("micro_market") or locality_resolved or locality_raw,
            "landmark_name": data.get("landmark_name"),
            "street_name": data.get("street_name"),
            "broker_id": data.get("broker_id"),
            "broker_name": broker_name,
            "broker_phone": data.get("broker_phone"),
            "broker_rera_number": data.get("broker_rera_number"),
            "group_name": data.get("group_name"),
            "summary_title": data.get("summary_title"),
            "normalized_message": data.get("normalized_message"),
            "raw_payload": raw_payload,
            "ai_extraction": ai or None,
            "deal_tags": data.get("deal_tags") or [],
            "additional_charges": data.get("additional_charges") or [],
            "validation_flags": data.get("validation_flags") or [],
            "needs_review": bool(data.get("needs_review")),
            "extraction_confidence": (
                str(data.get("extraction_confidence") or ai.get("extraction_confidence") or "").lower()
                if str(data.get("extraction_confidence") or ai.get("extraction_confidence") or "").lower() in {"high", "medium", "low"}
                else ("high" if confidence_score >= .85 else ("medium" if confidence_score >= .6 else "low"))
            ),
            "extraction_confidence_score": max(0.0, min(1.0, confidence_score)),
            "corrected_fields": data.get("corrected_fields") or [],
            "correction_confidence": data.get("correction_confidence"),
            "corrected_at": data.get("corrected_at"),
        }
        typed = dict(common)
        typed.update({
            "bhk": bhk_value, "carpet_area_sqft": area_sqft,
            "built_up_area_sqft": data.get("built_up_area_sqft"),
            "super_built_up_area_sqft": data.get("super_built_up_area_sqft"),
            "area_raw_text": data.get("area") or data.get("location_raw"),
            "bathroom_count": data.get("bathroom_count"),
            "car_parking_count": data.get("car_parking_count"),
            "parking_type": data.get("parking_type"),
            "has_lift": data.get("has_lift"),
            "has_power_backup": data.get("has_power_backup"),
            "power_load_kw": data.get("power_load_kw"),
            "floor_range": data.get("floor_range"),
            "configuration_type": data.get("configuration_type"),
            "furnishing_status": furnishing_value,
            "fitout_status": data.get("fitout_status") or furnishing_value,
            "possession_status": possession_value,
            "possession_date": _coerce_sql_date(data.get("possession_date")),
            "available_from": _coerce_sql_date(data.get("available_from")),
            "availability_date_raw": data.get("availability_date_raw"),
            "oc_status": data.get("oc_status"),
            "ceiling_height": data.get("ceiling_height"),
            "commercial_use_type": data.get("commercial_use_type") or data.get("property_type"),
            "chargeable_area_sqft": data.get("chargeable_area_sqft"),
            "mezzanine_area_sqft": data.get("mezzanine_area_sqft"),
            "floor_level": data.get("floor_level"),
            "floor_count": data.get("floor_count"),
            "frontage_ft": data.get("frontage_ft"),
            "entrance_count": data.get("entrance_count"),
            "otla_area_sqft": data.get("otla_area_sqft"),
            "otla_area_raw_text": data.get("otla_area_raw_text"),
            "terrace_area_sqft": data.get("terrace_area_sqft"),
            "covered_terrace_area_sqft": data.get("covered_terrace_area_sqft"),
            "terrace_area_raw_text": data.get("terrace_area_raw_text"),
            "heritage_space": data.get("heritage_space"),
            "permitted_use_types": data.get("permitted_use_types") or [],
            "ideal_for": data.get("ideal_for"),
            "automatic_shutter_count": data.get("automatic_shutter_count"),
            "room_count": data.get("room_count"),
            "suite_count": data.get("suite_count"),
            "banquet_hall_count": data.get("banquet_hall_count"),
            "restaurant_count": data.get("restaurant_count"),
            "bar_facility": data.get("bar_facility"),
            "operational_status": data.get("operational_status"),
            "rent_inclusions": data.get("rent_inclusions"),
            "license_type": data.get("license_type"),
            "short_term_allowed": data.get("short_term_allowed"),
            "inspection_notice_minutes": data.get("inspection_notice_minutes"),
            "director_cabin_count": data.get("director_cabin_count"),
            "ceo_cabin_present": data.get("ceo_cabin_present"),
            "cubicle_count": data.get("cubicle_count"),
            "conference_room_capacity": data.get("conference_room_capacity"),
            "meeting_room_capacity": data.get("meeting_room_capacity"),
            "training_room_capacity": data.get("training_room_capacity"),
            "cafeteria_seat_count": data.get("cafeteria_seat_count"),
            "accounts_area": data.get("accounts_area"),
            "lounge_area": data.get("lounge_area"),
            "occupancy_status": data.get("occupancy_type"),
            "developer_name": data.get("developer"),
            "price_raw_text": raw_price_text or (str(price_value) if price_value is not None else None),
            "price_basis": data.get("price_basis"),
            "price_math": data.get("price_math") or ({
                "rate": price_rupees,
                "basis": "chargeable_area_sqft" if "chargeable" in price_basis_text else "carpet_area_sqft",
                "area_sqft": data.get("chargeable_area_sqft") if "chargeable" in price_basis_text else area_sqft,
                "computed_monthly_rent": monthly_rent,
            } if transaction_type == "rent" and is_per_sqft_price and monthly_rent is not None else {}),
            "price_qualifier": "plus_plus" if "++" in str(price_obj.get("raw_price_text") or "") else None,
            "total_asking_price": total_asking_price,
            "monthly_rent": monthly_rent,
            "price_per_sqft": (
                data.get("price_per_sqft")
                if data.get("price_per_sqft") is not None
                and transaction_type == "sale"
                and is_per_sqft_price
                else (price_value if transaction_type == "sale" and is_per_sqft_price else None)
            ),
            "rent_per_sqft": (
                data.get("rent_per_sqft")
                if data.get("rent_per_sqft") is not None
                and transaction_type == "rent"
                and is_per_sqft_price
                else (price_value if transaction_type == "rent" and is_per_sqft_price else None)
            ),
            "deposit_amount": data.get("deposit_amount"),
            "deposit_months": data.get("deposit_months"),
            "deposit_applicable": data.get("deposit_amount") is not None,
            "deposit_raw_text": data.get("deposit_raw_text") or price_obj.get("deposit_raw_text"),
            "lease_term_type": data.get("lease_term_type"),
            "lease_term_min_months": data.get("lease_term_min_months"),
            "lease_term_max_months": data.get("lease_term_max_months"),
            "lease_term_raw_text": data.get("lease_term_raw_text"),
            "brokerage_type": data.get("brokerage_type"),
            "brokerage_context": data.get("brokerage_context"),
            "brokerage_terms_raw": data.get("brokerage_terms_raw"),
            "plus_one_deal": data.get("plus_one_deal"),
            "fee_sharing_required": data.get("fee_sharing_required"),
            "client_profile_required": data.get("client_profile_required"),
            "pet_policy": data.get("pet_policy"),
            "tenant_type_preference": data.get("tenant_type_preference"),
            "sharing_allowed": data.get("sharing_allowed"),
            "company_lease_criteria": data.get("company_lease_criteria"),
            "tenant_nationality_preference": data.get("tenant_nationality_preference"),
            "broker_company": data.get("broker_company"),
            "contacts": data.get("contacts") or [],
            "showing_instructions": data.get("showing_instructions"),
            "contact_instructions": data.get("contact_instructions"),
            "unit_condition": data.get("unit_condition"),
            "availability_status": data.get("availability_status"),
            "wing": data.get("wing"),
            "floor_min": data.get("floor_min"),
            "floor_max": data.get("floor_max"),
            "floor_label": data.get("floor_label"),
            "original_bhk": data.get("original_bhk"),
            "current_bhk": data.get("current_bhk"),
            "is_converted_unit": data.get("is_converted_unit"),
            "is_combination_unit": data.get("is_combination_unit"),
            "configuration_details": data.get("configuration_details"),
            "balcony_present": data.get("balcony_present"),
            "balcony_area_sqft": data.get("balcony_area_sqft"),
            "balcony_area_raw_text": data.get("balcony_area_raw_text"),
            "terrace_area_sqft": data.get("terrace_area_sqft"),
            "covered_terrace_area_sqft": data.get("covered_terrace_area_sqft"),
            "terrace_area_raw_text": data.get("terrace_area_raw_text"),
            "sit_out_present": data.get("sit_out_present"),
            "has_lift": data.get("has_lift"),
            "view_description": data.get("view_description"),
            "parking_details": data.get("parking_details") or {},
            "society_restrictions_raw": data.get("society_restrictions_raw"),
            "unstructured_facts": data.get("unstructured_facts") or {},
            "building_amenities": data.get("building_amenities") or [],
            "unit_amenities": data.get("amenities") or [],
            "amenities_unverified_claim": data.get("amenities_unverified_claim"),
            "deal_tags": data.get("deal_tags") or [],
            "broker_company": data.get("broker_company"),
            "contacts": data.get("contacts") or [],
            "showing_instructions": data.get("showing_instructions"),
            "contact_instructions": data.get("contact_instructions"),
            "availability_status": data.get("availability_status"),
            "brokerage_context": data.get("brokerage_context"),
            "co_brokered": data.get("co_brokered"),
            "wing": data.get("wing"),
            "floor_min": data.get("floor_min"),
            "floor_max": data.get("floor_max"),
            "floor_label": data.get("floor_label"),
            "original_bhk": data.get("original_bhk"),
            "current_bhk": data.get("current_bhk"),
            "is_converted_unit": data.get("is_converted_unit"),
            "is_combination_unit": data.get("is_combination_unit"),
            "configuration_details": data.get("configuration_details"),
            "can_sell_separately": data.get("can_sell_separately"),
            "balcony_area_sqft": data.get("balcony_area_sqft"),
            "balcony_area_raw_text": data.get("balcony_area_raw_text"),
            "terrace_area_sqft": data.get("terrace_area_sqft"),
            "covered_terrace_area_sqft": data.get("covered_terrace_area_sqft"),
            "terrace_area_raw_text": data.get("terrace_area_raw_text"),
            "sellable_area_sqft": data.get("sellable_area_sqft"),
            "computed_total_asking_price": data.get("computed_total_asking_price"),
            "computed_price_confidence": data.get("computed_price_confidence"),
            "price_math": data.get("price_math") or {},
            "unit_condition": data.get("unit_condition"),
            "vastu_compliant": data.get("vastu_compliant"),
            "view_description": data.get("view_description"),
            "parking_details": data.get("parking_details") or {},
            "society_restrictions": data.get("society_restrictions") or [],
            "society_restrictions_raw": data.get("society_restrictions_raw"),
            "unstructured_facts": data.get("unstructured_facts") or {},
        })
        if transaction_type == "rent" and rent_price_needs_review(typed.get("monthly_rent"), raw_price_text):
            typed["needs_review"] = True
            typed["extraction_confidence"] = "low"
        if table.endswith("requirements"):
            req = ai if isinstance(ai, dict) else {}
            typed.update({
                "intent": "RENT" if transaction_type == "rent" else "BUY",
                "budget_min": data.get("price_min") or data.get("budget_min") or req.get("budget_min"),
                "budget_max": data.get("price_max") or data.get("budget_max") or req.get("budget_max") or price_rupees,
                "budget_currency": "AED",
                "area_min_sqft": data.get("area_min_sqft") or req.get("area_min_sqft") or area_sqft,
                "area_max_sqft": data.get("area_max_sqft") or req.get("area_max_sqft") or area_sqft,
                "locality_options": _coerce_text_array(req.get("locality_options") or [x for x in [data.get("micro_market"), data.get("location_raw")] if x]),
                "status": "active",
                "is_flexible": req.get("is_flexible") if req.get("is_flexible") is not None else False,
                "urgency": {"high": "urgent", "low": "flexible"}.get(str(req.get("urgency") or "").lower(), req.get("urgency")) or ("urgent" if "urgent" in str(data.get("normalized_message") or "").lower() else "normal"),
                "bhk_options": _coerce_numeric_array(req.get("bhk_options") or ([bhk_value] if bhk_value is not None else [])),
                "configuration_preference": _coerce_text_array(req.get("configuration_preference") or ([data.get("configuration_type")] if data.get("configuration_type") else [])),
                "furnishing_preference": data.get("furnishing_canonical") or data.get("furnishing") or req.get("furnishing_preference"),
                "possession_preference": data.get("possession_preference") or req.get("possession_preference"),
                "building_preferences": _coerce_text_array(data.get("building_preferences") or req.get("building_preferences") or []),
                "age_preference": data.get("age_preference") or req.get("age_preference"),
                "floor_preference": data.get("floor_preference") or req.get("floor_preference"),
                "view_preference": _coerce_text_array(data.get("view_preference") or req.get("view_preference") or []),
                "tenant_type": data.get("tenant_type") or req.get("tenant_type"),
                "nationality": data.get("nationality") or req.get("nationality"),
                "has_pets": data.get("has_pets") if data.get("has_pets") is not None else req.get("has_pets"),
                "sharing_acceptable": data.get("sharing_acceptable") if data.get("sharing_acceptable") is not None else req.get("sharing_acceptable"),
                "food_preference": data.get("food_preference") or req.get("food_preference"),
                "lease_term_preference": data.get("lease_term_preference") or req.get("lease_term_preference"),
                "deposit_budget_max": data.get("deposit_budget_max") or req.get("deposit_budget_max"),
                "car_parking_min": data.get("car_parking_min") or req.get("car_parking_min"),
                "amenity_requirements": _coerce_text_array(data.get("amenity_requirements") or req.get("amenity_requirements") or []),
                "brokerage_willingness": data.get("brokerage_willingness") or req.get("brokerage_willingness"),
            })
            if table.startswith("commercial_"):
                typed["commercial_use_type"] = [
                    data.get("commercial_use_type") or data.get("property_type")
                ] if (data.get("commercial_use_type") or data.get("property_type")) else []
        typed["opportunity_key"] = _observation_fingerprint(typed)
        allowed_by_table = {
            "residential_sale_listings": {"bhk","configuration_type","bathroom_count","carpet_area_sqft","built_up_area_sqft","super_built_up_area_sqft","area_raw_text","total_asking_price","price_per_sqft","price_basis","price_raw_text","price_qualifier","furnishing_status","possession_status","possession_date","car_parking_count","parking_type","floor_range","building_amenities","unit_amenities","amenities_unverified_claim","oc_status","brokerage_type","developer_name","broker_company","contacts","showing_instructions","contact_instructions","availability_status","brokerage_context","co_brokered","wing","floor_min","floor_max","floor_label","original_bhk","current_bhk","is_converted_unit","is_combination_unit","configuration_details","can_sell_separately","balcony_area_sqft","balcony_area_raw_text","terrace_area_sqft","covered_terrace_area_sqft","terrace_area_raw_text","sellable_area_sqft","computed_total_asking_price","computed_price_confidence","price_math","unit_condition","vastu_compliant","view_description","parking_details","society_restrictions","society_restrictions_raw","unstructured_facts"},
            "residential_rent_listings": {"bhk","configuration_type","bathroom_count","carpet_area_sqft","built_up_area_sqft","area_raw_text","monthly_rent","rent_per_sqft","price_basis","price_raw_text","price_qualifier","deposit_amount","deposit_months","deposit_applicable","deposit_raw_text","furnishing_status","possession_status","available_from","availability_date_raw","availability_status","car_parking_count","parking_type","parking_details","floor_range","floor_min","floor_max","floor_label","wing","has_lift","building_amenities","unit_amenities","amenities_unverified_claim","pet_policy","tenant_type_preference","sharing_allowed","company_lease_criteria","tenant_nationality_preference","lease_term_type","lease_term_min_months","lease_term_max_months","lease_term_raw_text","brokerage_type","brokerage_context","brokerage_terms_raw","plus_one_deal","fee_sharing_required","client_profile_required","original_bhk","current_bhk","configuration_details","is_converted_unit","is_combination_unit","balcony_present","balcony_area_sqft","balcony_area_raw_text","terrace_area_sqft","covered_terrace_area_sqft","terrace_area_raw_text","sit_out_present","unit_condition","view_description","society_restrictions_raw","broker_company","contacts","showing_instructions","contact_instructions","unstructured_facts"},
            "commercial_sale_listings": {"commercial_use_type","carpet_area_sqft","built_up_area_sqft","chargeable_area_sqft","super_built_up_area_sqft","saleable_area_sqft","area_raw_text","total_asking_price","price_per_sqft","price_basis","price_raw_text","price_qualifier","fitout_status","ceiling_height","floor_level","floor_count","floor_range","car_parking_count","parking_type","oc_status","building_amenities","has_central_ac","has_power_backup","has_lift","power_load_kw","brokerage_type","developer_name","broker_rera_number","terrace_area_sqft","covered_terrace_area_sqft","terrace_area_raw_text","frontage_ft","entrance_count","permitted_use_types","ideal_for","project_inventory","area_min_sqft","area_max_sqft","floor_plate_sqft","project_status","director_cabin_count","ceo_cabin_present","cubicle_count","conference_room_capacity","meeting_room_capacity","reception_area","server_room","storage_area","inspection_notice_minutes","price_math"},
            "commercial_rent_listings": {"commercial_use_type","carpet_area_sqft","built_up_area_sqft","chargeable_area_sqft","area_raw_text","monthly_rent","rent_per_sqft","price_basis","price_raw_text","price_qualifier","deposit_amount","deposit_months","deposit_applicable","deposit_raw_text","fitout_status","ceiling_height","floor_level","floor_count","floor_range","car_parking_count","parking_type","building_amenities","has_central_ac","has_power_backup","has_lift","power_load_kw","lease_term_type","brokerage_type","broker_rera_number","terrace_area_sqft","covered_terrace_area_sqft","terrace_area_raw_text","frontage_ft","entrance_count","otla_area_sqft","otla_area_raw_text","heritage_space","permitted_use_types","ideal_for","automatic_shutter_count","room_count","suite_count","banquet_hall_count","restaurant_count","bar_facility","operational_status","rent_inclusions","license_type","short_term_allowed","inspection_notice_minutes","director_cabin_count","ceo_cabin_present","cubicle_count","conference_room_capacity","meeting_room_capacity","training_room_capacity","cafeteria_seat_count","accounts_area","lounge_area","price_math"},
        }
        # ``loft`` is stored in the existing commercial mezzanine column.
        allowed_by_table["commercial_rent_listings"].add("mezzanine_area_sqft")
        if table.endswith("requirements"):
            # Residential requirement tables intentionally do not have
            # commercial_use_type.  Keeping this allow-list per table avoids
            # PostgREST rejecting otherwise valid residential requirements.
            allowed_by_table = {
                "residential_sale_requirements": {
                    "bhk_options", "configuration_preference",
                    "carpet_area_min_sqft", "carpet_area_max_sqft",
                    "built_up_area_min_sqft", "built_up_area_max_sqft",
                    "budget_min", "budget_max", "budget_currency",
                    "area_min_sqft", "area_max_sqft", "locality_options",
                    "is_flexible", "urgency", "status",
                    "furnishing_preference", "possession_preference",
                    "car_parking_min", "buyer_type", "brokerage_willingness",
                    "amenity_requirements",
                },
                "residential_rent_requirements": {
                    "bhk_options", "configuration_preference",
                    "carpet_area_min_sqft", "carpet_area_max_sqft",
                    "budget_min", "budget_max", "budget_currency",
                    "area_min_sqft", "area_max_sqft", "locality_options",
                    "is_flexible", "urgency", "status",
                    "furnishing_preference", "possession_preference",
                    "tenant_type", "has_pets", "sharing_acceptable",
                    "lease_term_preference", "deposit_budget_max", "nationality",
                    "food_preference", "floor_preference", "view_preference",
                    "building_preferences", "age_preference", "car_parking_min",
                    "brokerage_willingness",
                    "amenity_requirements",
                },
                "commercial_sale_requirements": {
                    "commercial_use_type", "carpet_area_min_sqft",
                    "carpet_area_max_sqft", "chargeable_area_max_sqft",
                    "budget_min", "budget_max", "budget_currency",
                    "area_min_sqft", "area_max_sqft", "locality_options",
                    "is_flexible", "urgency", "status", "fitout_preference",
                    "car_parking_min", "needs_mezzanine", "needs_lift",
                    "needs_power_backup", "needs_central_ac",
                    "min_power_load_kw", "buyer_type", "brokerage_willingness",
                },
                "commercial_rent_requirements": {
                    "commercial_use_type", "carpet_area_min_sqft",
                    "carpet_area_max_sqft", "chargeable_area_max_sqft",
                    "budget_min", "budget_max", "budget_currency",
                    "area_min_sqft", "area_max_sqft", "locality_options",
                    "is_flexible", "urgency", "status", "fitout_preference",
                    "car_parking_min", "needs_mezzanine", "needs_lift",
                    "needs_power_backup", "needs_central_ac",
                    "min_power_load_kw", "lease_term_preference",
                    "deposit_budget_max", "brokerage_willingness",
                    "intended_use_details", "area_basis_preference", "location_flexibility",
                    "floor_min", "floor_max", "floor_count_max", "floor_preference",
                    "consecutive_floors_required", "parking_required",
                    "needs_attached_washroom", "needs_washroom", "needs_pantry",
                    "power_requirements", "entrance_requirement", "signage_required",
                    "loading_access_required", "budget_includes_maintenance",
                    "premium_building_required", "glass_facade_required",
                    "residential_cum_commercial_ok", "by_lanes_accepted", "media_requested",
                    "min_cabin_count", "min_workstation_count", "needs_conference_room",
                    "brokerage_context", "brokerage_terms_raw", "contacts", "min_washroom_count",
                },
            }
            allowed = allowed_by_table[table]
        else:
            allowed = allowed_by_table[table]
        allowed = set(allowed)
        allowed.add("extraction_confidence_score")
        typed = {k: v for k, v in typed.items() if v is not None and k in (set(common) | allowed)}
        try:
            return self.save_typed_listing(table, typed, _already_filtered=True, _source_id=source_id)
        except Exception as exc:
            print(f"[storage] typed observation insert failed: {exc}", flush=True)
            try:
                print(
                    "[storage] typed observation payload="
                    + json.dumps(data, default=str, ensure_ascii=False, sort_keys=True),
                    flush=True,
                )
            except Exception:
                print(f"[storage] save_parsed payload={data!r}", flush=True)
            raise

    def save_parsed(self, parsed: ParsedObservation) -> int:
        """Deprecated compatibility wrapper; writes to a typed table."""
        return self.save_typed_observation(parsed)

    def link_typed_observation_to_building(
        self, parsed_id: int, building_db_id: int, observation: dict
    ) -> bool:
        """Persist the resolver decision on the typed row after discovery."""
        table, _asset, _transaction = _typed_route(observation)
        payload = {"building_id": int(building_db_id)}
        building = self.get_building(building_db_id=building_db_id)
        if (
            building
            and building.get("micro_market")
            and building.get("geocode_source") == "google_places_text_search"
            and float(building.get("geocode_confidence") or 0) >= 0.9
            and not observation.get("micro_market")
        ):
            payload.update({
                "locality_resolved": building["micro_market"],
                "micro_market": building["micro_market"],
            })
        result = self.client.table(table).update(payload).eq("id", int(parsed_id)).execute()
        return bool(result.data)

    def save_typed_listing(
        self,
        table_name: str,
        data: dict,
        *,
        _already_filtered: bool = False,
        _source_id: int | None = None,
    ) -> int:
        """Persist one row directly to one of the eight typed tables.

        The extraction bridge owns routing and field selection.  This method
        is the final safety boundary: identity/timestamp fields are never
        supplied by the application, and only allow-listed typed tables are
        accepted.  ``source_fingerprint`` is the idempotency key.
        """
        valid_tables = set(_TYPED_LISTING_TABLES.values()) | set(_TYPED_REQUIREMENT_TABLES.values())
        if table_name not in valid_tables:
            raise ValueError(f"unsupported typed extraction table: {table_name}")
        row = dict(data or {})
        for key in ("id", "created_at", "updated_at", "embedding"):
            row.pop(key, None)
        # Freshness belongs to the WhatsApp event, not to the extraction run.
        # A queued June message can be parsed in August; using ``now()`` here
        # would make an old listing look freshly seen and extend its public
        # lifetime incorrectly.
        surfaced_at = datetime.now(timezone.utc)
        if _source_id:
            try:
                source_rows = self.client.table("raw_messages").select(
                    "timestamp"
                ).eq("id", int(_source_id)).limit(1).execute().data or []
                source_timestamp = source_rows[0].get("timestamp") if source_rows else None
                if source_timestamp:
                    parsed_timestamp = datetime.fromisoformat(
                        str(source_timestamp).replace("Z", "+00:00")
                    )
                    surfaced_at = (
                        parsed_timestamp
                        if parsed_timestamp.tzinfo
                        else parsed_timestamp.replace(tzinfo=timezone.utc)
                    )
            except (TypeError, ValueError, IndexError, KeyError):
                pass
        row["last_seen_at"] = surfaced_at.isoformat()
        row["expires_at"] = (surfaced_at + timedelta(days=30)).isoformat()
        if not row.get("tenant_id") and self._tenant_id:
            row["tenant_id"] = self._tenant_id
        if isinstance(row.get("forwarded"), int):
            row["forwarded"] = bool(row["forwarded"])
        for field in _TYPED_BOOLEAN_FIELDS:
            if field in row:
                row[field] = _coerce_typed_boolean(field, row[field])
        if not table_name.endswith("_requirements"):
            for field in _REQUIREMENT_ONLY_FIELDS:
                row.pop(field, None)
            if isinstance(row.get("commercial_use_type"), (list, tuple)):
                row["commercial_use_type"] = row["commercial_use_type"][0] if row["commercial_use_type"] else None
        elif "urgency" in row:
            row["urgency"] = _normalize_requirement_urgency(row.get("urgency"))
        for field in ("raw_payload", "ai_extraction", "additional_charges", "validation_flags", "company_lease_criteria"):
            if isinstance(row.get(field), str):
                try:
                    row[field] = json.loads(row[field])
                except (TypeError, json.JSONDecodeError):
                    row[field] = {} if field in {"raw_payload", "ai_extraction", "company_lease_criteria"} else []
        for field in _TYPED_ARRAY_FIELDS:
            if field == "commercial_use_type" and not table_name.endswith("_requirements"):
                continue
            value = row.get(field)
            if field == "bhk_options":
                row[field] = _coerce_numeric_array(value)
                continue
            if isinstance(value, str):
                row[field] = [value]
            elif value is not None and not isinstance(value, (list, tuple)):
                row[field] = [value]
            elif isinstance(value, tuple):
                row[field] = list(value)
        # The bridge already filters columns.  Keep a second filter here for
        # callers that use this storage method directly.
        if not _already_filtered:
            row = {k: v for k, v in row.items() if v is not None}
        else:
            row = {k: v for k, v in row.items() if v is not None}

        # Final schema boundary: bhk_options belongs only to requirement
        # tables. Keep this immediately before any REST write so no later
        # transformation or direct caller can leak it into listing tables.
        if (not table_name.endswith("_requirements") or table_name.startswith("commercial_")) and "bhk_options" in row:
            # BHK is residential-only. Keep this final boundary silent: old
            # callers may still send the generic key, but it must never spam
            # extraction logs or reach a commercial table.
            row.pop("bhk_options", None)

        # A corrected extraction can change route (rent <-> sale, or
        # residential <-> commercial).  Because each route has its own typed
        # table, an upsert in the new table alone leaves the old projection
        # alive and the public UNION view shows the stale extraction again.
        # Remove only the same raw message/listing index in the other typed
        # listing tables; requirements are intentionally excluded.
        raw_message_id = row.get("raw_message_id")
        listing_index = row.get("listing_index", 0)
        if raw_message_id and not table_name.endswith("_requirements"):
            for stale_table in _TYPED_LISTING_TABLE_NAMES:
                if stale_table == table_name:
                    continue
                try:
                    stale_query = self.client.table(stale_table).select("id").eq(
                        "raw_message_id", raw_message_id
                    ).eq("listing_index", listing_index)
                    if row.get("tenant_id"):
                        stale_query = stale_query.eq("tenant_id", row["tenant_id"])
                    stale_rows = stale_query.execute().data or []
                    for stale_row in stale_rows:
                        self.client.table(stale_table).delete().eq(
                            "id", stale_row["id"]
                        ).execute()
                except Exception as exc:
                    print(f"[storage] stale typed route cleanup failed for {stale_table}: {exc}", flush=True)
        # Idempotently persist the source fingerprint. Reprocessing the same
        # WhatsApp message must update the typed row instead of turning a
        # duplicate-key response into a failed extraction attempt.
        try:
            result = self.client.table(table_name).upsert(
                row, on_conflict="source_fingerprint"
            ).execute()
        except Exception:
            existing = self.client.table(table_name).select("id,last_seen_at,expires_at").eq(
                "source_fingerprint", row["source_fingerprint"]
            ).limit(1).execute().data or []
            if not existing:
                raise
            typed_id = int(existing[0]["id"])
            # Never move freshness backwards when a historical source is
            # reprocessed after a newer repost already updated the row.
            existing_seen = existing[0].get("last_seen_at")
            if existing_seen:
                try:
                    existing_dt = datetime.fromisoformat(str(existing_seen).replace("Z", "+00:00"))
                    row_dt = datetime.fromisoformat(str(row["last_seen_at"]).replace("Z", "+00:00"))
                    if existing_dt > row_dt:
                        row["last_seen_at"] = existing_seen
                        row["expires_at"] = existing[0].get("expires_at") or row["expires_at"]
                except (TypeError, ValueError):
                    pass
            update_row = {k: v for k, v in row.items() if k != "source_fingerprint"}
            result = self.client.table(table_name).update(update_row).eq(
                "id", typed_id
            ).execute()
            if not result.data:
                result.data = [{"id": typed_id}]
        if not result.data:
            return 0
        typed_id = int(result.data[0].get("id") or 0)
        source_id = _source_id or int(row.get("legacy_source_id") or 0)
        if source_id:
            if not hasattr(self, "_typed_table_by_source_id"):
                self._typed_table_by_source_id = {}
            self._typed_table_by_source_id[source_id] = table_name
        return typed_id

    def _get_typed_observation(self, parsed_id: int) -> tuple[str, dict] | None:
        """Read an observation from its typed table when its route is known.

        New observations have a route recorded by ``save_parsed``; after a
        restart we probe the eight source tables by ``legacy_source_id``.
        """
        table = getattr(self, "_typed_table_by_source_id", {}).get(int(parsed_id))
        tables = [table] if table else []
        if not tables:
            tables = list(_TYPED_LISTING_TABLES.values()) + list(_TYPED_REQUIREMENT_TABLES.values())

        for candidate in dict.fromkeys(tables):
            try:
                query = self.client.table(candidate).select("*").eq("legacy_source_id", parsed_id)
                if self._tenant_id:
                    query = query.eq("tenant_id", self._tenant_id)
                query = query.limit(1)
                rows = query.execute().data or []
                if rows:
                    return candidate, rows[0]
                if table:
                    query = self.client.table(candidate).select("*").eq("id", parsed_id)
                    if self._tenant_id:
                        query = query.eq("tenant_id", self._tenant_id)
                    rows = (query.limit(1).execute().data or [])
                    if rows:
                        return candidate, rows[0]
            except Exception:
                continue

        return None

    def _fetch_typed_rows(
        self,
        *,
        requirements: bool | None = None,
        tenant_id: str | None = None,
        raw_message_id: int | None = None,
        limit_per_table: int = 500,
        all_tenants: bool = False,
        include_normalized_message: bool = False,
        include_raw_payload: bool = False,
        card_only: bool = False,
        broker_key: str = "",
        transaction_type: str = "",
        search_text: str = "",
    ) -> list[dict]:
        """Fetch rows from the typed source tables.

        This is deliberately a small fan-out rather than a compatibility
        view.  It keeps the source-of-truth table explicit and lets callers
        apply their existing legacy-shaped presentation logic locally.
        """
        if requirements is True:
            tables = _TYPED_REQUIREMENT_TABLE_NAMES
        elif requirements is False:
            tables = _TYPED_LISTING_TABLE_NAMES
        else:
            tables = _ALL_TYPED_TABLES
        # Public market reads intentionally use the shared network and must
        # not inherit the request workspace's tenant scope.
        tid = None if all_tenants else (tenant_id or self._tenant_id)
        def fetch_table(table: str) -> list[dict]:
            try:
                requested_columns = _market_card_columns(table) if card_only else _typed_read_columns(
                        table,
                        include_normalized_message=include_normalized_message,
                        include_raw_payload=include_raw_payload,
                    )
                query = self.client.table(table).select(requested_columns).order("created_at", desc=True).limit(limit_per_table)
                if tid:
                    query = query.eq("tenant_id", tid)
                if raw_message_id is not None:
                    query = query.eq("raw_message_id", raw_message_id)
                if transaction_type in {"sale", "rent"}:
                    query = query.eq("transaction_type", transaction_type)
                if search_text.strip():
                    # Keep the palette query database-side.  The values are
                    # sanitized for PostgREST's OR expression; the final
                    # Python filter still verifies the complete match.
                    search_value = re.sub(r"[%,*(),]", " ", search_text).strip()[:120]
                    if search_value:
                        query = query.or_(
                            ",".join(
                                f"{column}.ilike.*{search_value}*"
                                for column in (
                                    "building_name",
                                    "micro_market",
                                    "locality_raw",
                                    "broker_name",
                                    "group_name",
                                    "summary_title",
                                )
                            )
                        )
                if broker_key:
                    phone = _normalize_india_phone(broker_key)
                    variants = [phone, f"91{phone}", f"+91{phone}"] if phone else []
                    if variants:
                        query = query.or_("".join(f"broker_phone.eq.{value}," for value in variants).rstrip(","))
                    elif broker_key.lower().startswith("name:"):
                        query = query.ilike("broker_name", f"*{broker_key[5:].strip()}*")
                result: list[dict] = []
                for row in query.execute().data or []:
                    row["_typed_table"] = table
                    result.append(row)
                return result
            except Exception:
                _logger.debug("typed row fetch failed for %s", table, exc_info=True)
                return []

        # The eight typed tables are independent. Do not make the first card
        # wait for eight network round trips in series.
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(8, len(tables))) as executor:
            futures = [executor.submit(fetch_table, table) for table in tables]
            for future in as_completed(futures):
                rows.extend(future.result())
        # Shared market tables are intentionally read across tenants, but a
        # workspace may suppress a noisy broker without deleting source data.
        # The active tenant context is used for this presentation-level
        # filter; unauthenticated public reads have no workspace context.
        rows = self._filter_workspace_blocked_rows(rows)
        # A repaired parent remains immutable evidence, but its old typed
        # children must disappear from all market cards and search results.
        raw_ids = {int(row.get("raw_message_id")) for row in rows if row.get("raw_message_id")}
        if raw_ids:
            superseded: set[int] = set()
            ids = list(raw_ids)
            for start in range(0, len(ids), 200):
                found = self.client.table("raw_messages").select("id").eq("extraction_superseded", True).in_("id", ids[start:start + 200]).execute().data or []
                superseded.update(int(item["id"]) for item in found if item.get("id") is not None)
            rows = [row for row in rows if int(row.get("raw_message_id") or 0) not in superseded]
        return rows

    def get_shared_market_listings(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        intent: str = "",
        bhk: str = "",
        building: str = "",
        micro_market: str = "",
        q: str = "",
        price_max: float = 0,
        price_min: float = 0,
        furnishing: str = "",
        broker: str = "",
    ) -> dict:
        """Read the shared market from the typed listing tables.

        This is the source used by the public/internal market map. It must
        remain independent of the deprecated SQLite ``listings_unified``
        compatibility view so every connected workspace sees the same shared
        inventory.
        """
        limit = max(1, min(int(limit or 100), 100))
        offset = max(0, int(offset or 0))
        rows = self._fetch_typed_rows(
            requirements=False,
            all_tenants=True,
            limit_per_table=max(250, limit + offset + 100),
        )
        intent = str(intent or "").strip().upper()
        bhk = str(bhk or "").strip().lower().replace(" bhk", "")
        building = str(building or q or "").strip().lower()
        micro_market = str(micro_market or "").strip().lower()
        furnishing = str(furnishing or "").strip().lower()
        broker = str(broker or "").strip().lower()
        filtered: list[dict] = []
        for typed in rows:
            if str(typed.get("duplicate_status") or "").lower() == "merged":
                continue
            # MCP and future ingestion paths can explicitly mark a row as
            # workspace-private. Legacy WhatsApp rows with NULL retain their
            # existing visibility behavior until they are backfilled.
            if str(typed.get("visibility") or "").strip().lower() == "workspace_private":
                continue
            legacy = self._typed_row_to_legacy(typed)
            legacy["_typed_table"] = typed.get("_typed_table")
            text = " ".join(
                str(legacy.get(key) or "")
                for key in ("building_name", "micro_market", "locality_raw", "locality_resolved", "landmark_name", "broker_name")
            ).lower()
            if intent and intent not in {"ANY", "ALL"} and str(legacy.get("intent") or "").upper() != intent:
                continue
            if bhk and bhk not in str(legacy.get("bhk") or "").lower().replace(" bhk", ""):
                continue
            if building and building not in text:
                continue
            if micro_market and micro_market not in " ".join(str(legacy.get(key) or "").lower() for key in ("micro_market", "locality_raw", "locality_resolved")):
                continue
            if furnishing and furnishing not in str(legacy.get("furnishing") or "").lower():
                continue
            if broker and broker not in str(legacy.get("broker_name") or "").lower():
                continue
            try:
                price = float(legacy.get("price") or 0)
            except (TypeError, ValueError):
                # One malformed legacy-shaped price must not make the entire
                # shared market endpoint return HTTP 500.
                price = 0.0
            if price_min and price < float(price_min):
                continue
            if price_max and price > float(price_max):
                continue
            filtered.append(legacy)

        # The shared map/search route must use the same canonical opportunity
        # projection as inbox and get_listings. Otherwise every repost remains
        # independently visible outside the inbox.
        filtered = _merge_observation_rows(filtered)
        filtered.sort(key=lambda row: str(row.get("last_seen") or row.get("created_at") or ""), reverse=True)
        page = filtered[offset:offset + limit]

        # Coordinates enrich listing pins only; the map never presents these
        # as a building directory. Missing coordinates are valid and remain
        # visible in the listing side panel.
        try:
            now = time.monotonic()
            cached_refs = self._market_reference_cache
            if cached_refs and now - cached_refs[0] < max(
                30.0, float(os.getenv("MARKET_REFERENCE_CACHE_TTL_SECONDS", "300"))
            ):
                building_rows, alias_rows = cached_refs[1], cached_refs[2]
            else:
                building_rows = self.client.table("buildings").select(
                    "id,canonical_name,address,micro_market,latitude,longitude"
                ).limit(10000).execute().data or []
                alias_rows = self.client.table("building_name_aliases").select(
                    "building_id,alias,canonical_name"
                ).limit(20000).execute().data or []
                self._market_reference_cache = (now, building_rows, alias_rows)
        except Exception:
            building_rows = []
            alias_rows = []

        def location_key(value: object) -> str:
            key = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
            return key.replace("lakshmi", "laxmi")

        aliases_by_building: dict[int, list[str]] = defaultdict(list)
        for alias in alias_rows:
            try:
                building_id = int(alias.get("building_id"))
            except (TypeError, ValueError):
                continue
            if alias.get("alias"):
                aliases_by_building[building_id].append(str(alias["alias"]))

        def resolve_building(row: dict) -> dict:
            listing_name = location_key(row.get("building_name"))
            listing_market = location_key(row.get("micro_market") or row.get("locality_resolved"))
            if not listing_name:
                return {}
            best: tuple[int, dict] | None = None
            for building_row in building_rows:
                building_id = building_row.get("id")
                names = [building_row.get("canonical_name") or ""]
                if building_id is not None:
                    names.extend(aliases_by_building.get(int(building_id), []))
                score = 0
                for candidate in names:
                    candidate_key = location_key(candidate)
                    if candidate_key == listing_name:
                        score = max(score, 100)
                    elif candidate_key.startswith(listing_name) and listing_market and listing_market in candidate_key:
                        # Prefer a locality-qualified canonical record over a
                        # shorter duplicate with a merely similar address.
                        score = max(score, 130)
                if not score:
                    continue
                canonical_key = location_key(building_row.get("canonical_name"))
                address_key = location_key(building_row.get("address"))
                if listing_market and listing_market in canonical_key:
                    score += 20
                elif listing_market and listing_market == address_key:
                    score += 10
                elif listing_market and address_key and (listing_market in address_key or address_key in listing_market):
                    score += 5
                if best is None or score > best[0]:
                    best = (score, building_row)
            return best[1] if best else {}

        results = []
        for row in page:
            # Never fall back to another listing/building's coordinates. If
            # the resolved building has no verified coordinates, return null;
            # the UI keeps that listing in the side list without a pin.
            building_row = resolve_building(row)
            price = row.get("price")
            results.append({
                "listing_id": row.get("id"),
                "source_schema": row.get("_typed_table"),
                "fingerprint": row.get("source_fingerprint"),
                "market_scope": "shared",
                "intent": row.get("intent"),
                "transaction_type": row.get("transaction_type"),
                "asset_type": row.get("asset_type"),
                "property_type": row.get("property_type"),
                "bhk": row.get("bhk"),
                "price": price,
                "price_formatted": row.get("price_raw_text") or (f"AED {price:,.0f}" if price else "Price on request"),
                "price_unit": row.get("price_unit"),
                "area_sqft": row.get("area_sqft"),
                "furnishing": row.get("furnishing"),
                "location_label": row.get("micro_market") or row.get("locality_resolved") or row.get("locality_raw"),
                "street_name": row.get("street_name"),
                # Present the canonical building label when the resolver has
                # matched one; raw broker wording remains in the detail view.
                "building_name": building_row.get("canonical_name") or row.get("building_name") or "On Request",
                "building_address": building_row.get("address"),
                "landmark_name": row.get("landmark_name"),
                "micro_market": row.get("micro_market"),
                "locality_raw": row.get("locality_raw"),
                "locality_resolved": row.get("locality_resolved"),
                "broker_name": row.get("broker_name"),
                "broker_phone": row.get("broker_phone"),
                "source_group": row.get("group_name"),
                "first_seen": row.get("created_at"),
                "last_seen": row.get("updated_at") or row.get("created_at"),
                "observation_count": int(row.get("times_seen") or 1),
                "group_count": max(1, len({str(item.get("source_group") or "") for item in row.get("evidence_list") or [] if item})),
                "raw_message_id": row.get("raw_message_id"),
                "latitude": building_row.get("latitude"),
                "longitude": building_row.get("longitude"),
                "match_reasons": [],
            })
        return {
            "type": "listing_results",
            "total": len(filtered),
            "results": results,
            "grouped": {},
            "showing": len(results),
            "offset": offset,
            "has_more": offset + len(results) < len(filtered),
            "remaining": max(0, len(filtered) - offset - len(results)),
        }

    def _fetch_recent_market_typed_rows(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
        intent: str = "",
        result_type: str = "all",
    ) -> tuple[list[dict], dict[int, dict]]:
        """Return recent typed market rows with lightweight evidence metadata.

        The list path deliberately avoids a raw-message join. Raw evidence is
        fetched by the detail route using ``raw_message_id`` so a slow or large
        evidence lookup cannot make the parsed market appear empty.
        """
        tid = tenant_id or self._tenant_id
        target = max(100, limit + offset)
        # Start from the typed source rows rather than scanning recent raw
        # messages and trying to infer group identity from optional payload
        # fields. This is both faster and guarantees that valid parsed rows
        # remain visible when a raw message has an old/incomplete group flag.
        requirement_filter = (
            True if result_type == "requirements"
            else False if result_type == "listings"
            else None
        )
        typed_rows = self._fetch_typed_rows(
            requirements=requirement_filter,
            all_tenants=True,
            limit_per_table=max(25, limit + offset),
            card_only=True,
            transaction_type={"SELL": "sale", "RENT": "rent"}.get(str(intent or "").upper(), ""),
        )
        # Shared feeds may include an owner's private workspace rows for that
        # owner, but must not leak them to other tenants. Public callers pass
        # no tenant_id and therefore receive only shared/legacy rows.
        typed_rows = [
            row for row in typed_rows
            if str(row.get("visibility") or "").strip().lower() != "workspace_private"
            or (tenant_id and str(row.get("tenant_id") or "") == str(tenant_id))
        ]
        # Source evidence is intentionally absent from this projection. The
        # detail route fetches it after an explicit card expansion.
        raw_map: dict[int, dict] = {}

        typed_rows.sort(
            key=lambda row: (
                str((raw_map.get(int(row.get("raw_message_id") or 0)) or {}).get("timestamp") or ""),
                str((raw_map.get(int(row.get("raw_message_id") or 0)) or {}).get("created_at") or ""),
                str(row.get("created_at") or ""),
                int(row.get("raw_message_id") or 0),
                int(row.get("listing_index") or 0),
            ),
            reverse=True,
        )
        return typed_rows, raw_map

    @staticmethod
    def _typed_row_to_legacy(row: dict) -> dict:
        """Normalize one typed row for older dataclass/UI consumers."""
        table = row.get("_typed_table") or ""
        requirement = table.endswith("_requirements")
        transaction = row.get("transaction_type") or ("rent" if "_rent_" in table else "sale")
        asset = row.get("asset_type") or ("commercial" if table.startswith("commercial_") else "residential")
        corrected_monthly_rent = None
        if asset == "commercial" and transaction == "rent" and not requirement:
            # A commercial PSF quote is authoritative when both operands are
            # present. Recompute it on reads as well as writes so previously
            # persisted rows cannot display a stale unit-scaled total.
            try:
                rate = float(row.get("rent_per_sqft"))
                area = next(
                    float(row.get(field))
                    for field in ("chargeable_area_sqft", "carpet_area_sqft", "built_up_area_sqft")
                    if row.get(field) not in (None, "") and float(row.get(field)) > 0
                )
                if rate > 0 and area > 0:
                    corrected_monthly_rent = rate * area
            except (TypeError, ValueError, StopIteration):
                corrected_monthly_rent = None
        price = (
            row.get("budget_max") if requirement
            else (corrected_monthly_rent or row.get("monthly_rent") if transaction == "rent" else row.get("total_asking_price"))
        )
        price_model = None
        if price is None:
            price = row.get("rent_per_sqft") if transaction == "rent" else row.get("price_per_sqft")
            price_model = "psf" if price is not None else None
        bhk = row.get("bhk")
        bhk_options = row.get("bhk_options")
        if bhk is None and isinstance(bhk_options, (list, tuple)) and bhk_options:
            bhk = bhk_options[0]
        furnishing = (
            row.get("furnishing_status")
            or row.get("fitout_status")
            or row.get("furnishing_preference")
            or row.get("fitout_preference")
        )
        area_min = row.get("area_min_sqft") or row.get("carpet_area_min_sqft")
        area_max = row.get("area_max_sqft") or row.get("carpet_area_max_sqft")
        price_per_sqft = row.get("rent_per_sqft") if transaction == "rent" else row.get("price_per_sqft")
        building_name = _clean_market_building_name(row)
        broker_name = re.sub(r"^[\W_]+|[\W_]+$", "", _clean_person_name(str(row.get("broker_name") or ""))).strip()
        summary_title = row.get("summary_title")
        if corrected_monthly_rent is not None:
            area_label = f"{int(area):,}" if float(area).is_integer() else f"{area:,.1f}"
            use_type = row.get("commercial_use_type")
            if isinstance(use_type, list):
                use_type = next((str(item) for item in use_type if item), None)
            use_type = str(use_type or "commercial space").strip()
            place_label = building_name or row.get("micro_market") or row.get("locality_raw")
            summary_title = f"{use_type.title()} with {area_label} sqft for rent"
            if place_label:
                summary_title += f" at {place_label}"
        return {
            **row,
            "source_schema": table,
            "message_type": "requirement" if requirement else "listing",
            "intent": ("RENT" if transaction == "rent" else "BUY") if requirement else ("RENT" if transaction == "rent" else "SELL"),
            "asset_type": asset,
            "transaction_type": transaction,
            "building_name": building_name or None,
            "broker_name": broker_name or None,
            "property_type": (
                ", ".join(str(item) for item in row.get("commercial_use_type") if item)
                if isinstance(row.get("commercial_use_type"), list)
                else row.get("commercial_use_type") or row.get("property_type")
            ),
            "configuration": row.get("configuration_type"),
            "bhk": bhk,
            "bhk_label": _format_bhk_label(row.get("configuration_type") or bhk),
            "price": price,
            "monthly_rent": corrected_monthly_rent or row.get("monthly_rent"),
            "summary_title": summary_title,
            "price_unit": "per_sqft" if price_model == "psf" else "abs",
            "price_model": "budget" if requirement and price is not None else price_model,
            "price_per_sqft": price_per_sqft,
            "area_sqft": row.get("carpet_area_sqft") or row.get("area_min_sqft"),
            "area_min_sqft": area_min,
            "area_max_sqft": area_max,
            "furnishing": furnishing,
            "furnishing_canonical": furnishing,
            "location_raw": row.get("locality_raw"),
            "profile_name": row.get("broker_name"),
            "confidence": row.get("extraction_confidence"),
            "budget_max": row.get("budget_max"),
            "last_seen": row.get("last_seen_at") or row.get("updated_at") or row.get("created_at"),
            "expires_at": row.get("expires_at"),
            "lifecycle_status": (
                "expired"
                if row.get("expires_at") and str(row.get("expires_at")) <= datetime.now(timezone.utc).isoformat()
                else "active"
            ),
        }

    def update_parsed_fields(self, row_id: int, updates: dict[str, Any], source_schema: str | None = None) -> bool:
        """Apply correction-layer updates to the typed source row.

        ``row_id`` is the stable observation id exposed by the normalized read
        view, not the identity sequence of any one typed table.
        """
        row = None
        table = None
        candidates = [source_schema] if source_schema in _ALL_TYPED_TABLES else list(_ALL_TYPED_TABLES)
        for candidate in candidates:
            try:
                query = self.client.table(candidate).select(
                    "id,raw_message_id,listing_index,asset_type,transaction_type,tenant_id,legacy_source_id,summary_title,normalized_message,raw_payload"
                )
                if self._tenant_id:
                    query = query.eq("tenant_id", self._tenant_id)
                rows = query.eq("id", row_id).limit(1).execute().data or []
                if rows:
                    row, table = rows[0], candidate
                    break
            except Exception:
                continue
        if not row:
            return False
        _, _, tx = _typed_route(row)
        mapping = {
            "price": "monthly_rent" if tx == "rent" else "total_asking_price",
            "price_per_sqft": "rent_per_sqft" if tx == "rent" else "price_per_sqft",
            "area_sqft": "carpet_area_sqft",
            "furnishing": "fitout_status" if table.startswith("commercial_") else "furnishing_status",
            "furnishing_canonical": "fitout_status" if table.startswith("commercial_") else "furnishing_status",
            "location_raw": "locality_raw",
            "profile_name": "broker_name",
            "bhk": "bhk",
        }
        shared_fields = {
            "building_name", "landmark_name", "street_name", "micro_market",
            "broker_name", "broker_phone", "group_name", "summary_title",
            "normalized_message", "ai_extraction", "deal_tags",
            "additional_charges", "validation_flags", "needs_review",
            "extraction_confidence", "corrected_fields", "correction_confidence",
            "corrected_at", "correction_hash", "locality_raw", "locality_resolved",
        }
        table_fields = {
            "bhk", "carpet_area_sqft", "built_up_area_sqft", "area_raw_text",
            "total_asking_price", "monthly_rent", "price_per_sqft", "rent_per_sqft",
            "price_raw_text", "price_basis", "price_qualifier", "furnishing_status",
            "fitout_status", "possession_status", "possession_date", "available_from",
            "car_parking_count", "parking_type", "floor_range", "commercial_use_type",
            "deposit_amount", "deposit_months", "deposit_applicable", "deposit_raw_text",
            "pet_policy", "tenant_type_preference", "sharing_allowed", "lease_term_type",
            "brokerage_type", "developer_name", "oc_status", "ceiling_height",
            "building_amenities", "unit_amenities", "amenities_unverified_claim",
            "intent", "budget_min", "budget_max", "budget_currency", "area_min_sqft",
            "area_max_sqft", "carpet_area_min_sqft", "carpet_area_max_sqft",
            "locality_options", "is_flexible", "urgency", "status",
            "furnishing_preference", "possession_preference", "car_parking_min",
            "bhk_options", "micro_market_options", "building_preferences",
            "buyer_type", "brokerage_willingness", "tenant_type", "has_pets",
            "sharing_acceptable", "lease_term_preference", "deposit_budget_max",
            "fitout_preference", "needs_mezzanine", "needs_lift", "needs_power_backup",
            "needs_central_ac", "min_power_load_kw", "commercial_use_type",
            "availability_status",
        }
        typed = {}
        for key, value in updates.items():
            target = mapping.get(key, key)
            if target in {"id", "raw_message_id", "tenant_id", "listing_index", "source_fingerprint", "legacy_source_id"}:
                continue
            if target in shared_fields or target in table_fields:
                if target == "bhk_options" and isinstance(value, str):
                    value = [float(item.strip()) for item in value.split(",") if item.strip()]
                elif target in {"micro_market_options", "building_preferences"} and isinstance(value, str):
                    value = [item.strip() for item in value.split(",") if item.strip()]
                typed[target] = value
        if not typed:
            return False
        typed["updated_at"] = datetime.now(timezone.utc).isoformat()
        # The admin/UI id is the typed table primary key. Do not switch to
        # legacy_source_id here: that id belongs to the deprecated source row
        # and can update the wrong record after the typed-table cutover.
        existing_corrections = row.get("corrected_fields") or {}
        if isinstance(existing_corrections, str):
            try:
                existing_corrections = json.loads(existing_corrections)
            except (TypeError, json.JSONDecodeError):
                existing_corrections = {}
        if not isinstance(existing_corrections, dict):
            existing_corrections = {}
        existing_corrections.update({key: value for key, value in updates.items() if key in shared_fields or key in table_fields})
        typed["corrected_fields"] = existing_corrections
        typed["corrected_at"] = datetime.now(timezone.utc).isoformat()
        result = self.client.table(table).update(typed).eq("id", row_id).execute()
        if result.data:
            self._record_extraction_learning_examples(
                row=row,
                table=table,
                updates=updates,
            )
        return bool(result.data)

    def _record_extraction_learning_examples(
        self,
        *,
        row: dict,
        table: str,
        updates: dict[str, Any],
    ) -> None:
        """Persist explicit user corrections as tenant-scoped prompt examples."""
        tenant_id = row.get("tenant_id") or self._tenant_id
        if not tenant_id:
            return
        payload = row.get("raw_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                payload = {}
        payload = payload if isinstance(payload, dict) else {}
        source_text = str(
            payload.get("slice_text")
            or payload.get("full_text")
            or row.get("normalized_message")
            or row.get("summary_title")
            or ""
        ).strip()
        if not source_text:
            return
        mapping = {
            "price": "monthly_rent" if row.get("transaction_type") == "rent" else "total_asking_price",
            "price_per_sqft": "rent_per_sqft" if row.get("transaction_type") == "rent" else "price_per_sqft",
            "area_sqft": "carpet_area_sqft",
            "furnishing": "fitout_status" if table.startswith("commercial_") else "furnishing_status",
            "furnishing_canonical": "fitout_status" if table.startswith("commercial_") else "furnishing_status",
            "location_raw": "locality_raw",
            "profile_name": "broker_name",
            "bhk": "bhk",
        }
        for field_name, corrected_value in updates.items():
            target = mapping.get(field_name, field_name)
            original_value = row.get(target)
            try:
                self.client.table("extraction_learning_examples").upsert({
                    "tenant_id": tenant_id,
                    "raw_message_id": row.get("raw_message_id"),
                    "source_schema": table,
                    "source_text": source_text[:12000],
                    "field_name": field_name,
                    "original_value": original_value,
                    "corrected_value": corrected_value,
                    "status": "approved",
                    "approved_at": datetime.now(timezone.utc).isoformat(),
                }, on_conflict="tenant_id,raw_message_id,source_schema,field_name,corrected_value").execute()
            except Exception:
                # Learning is an optimisation. Never make a valid correction
                # fail because the optional example write is unavailable.
                _logger.debug("could not record extraction learning example", exc_info=True)

    def get_extraction_learning_examples(
        self,
        source_text: str,
        *,
        tenant_id: str | None = None,
        limit: int = 3,
    ) -> list[dict]:
        """Return the most relevant approved corrections for one tenant."""
        tid = tenant_id or self._tenant_id
        if not tid or not source_text:
            return []
        try:
            rows = self.client.table("extraction_learning_examples").select(
                "source_text,field_name,corrected_value"
            ).eq("tenant_id", tid).eq("status", "approved").order(
                "created_at", desc=True
            ).limit(100).execute().data or []
        except Exception:
            _logger.debug("could not load extraction learning examples", exc_info=True)
            return []
        incoming = set(re.findall(r"[a-z0-9]{3,}", source_text.lower()))
        scored = []
        for row in rows:
            example_text = str(row.get("source_text") or "")
            tokens = set(re.findall(r"[a-z0-9]{3,}", example_text.lower()))
            score = len(incoming & tokens)
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in scored[:max(1, min(limit, 5))]]

    def _team_member_group_scope(self, tenant_id: str, team_member_id: int | None) -> set[str] | None:
        """Return the WhatsApp groups visible to one team member.

        Tenant membership is deliberately not enough here: a workspace can
        contain several connected WhatsApp numbers.  My Deals must follow the
        member's connected-number access and the group-to-connection registry,
        otherwise one broker's CRM leaks another broker's inventory.
        """
        try:
            connections = self.list_org_whatsapp_connections(tenant_id)
            by_id = {
                int(row["id"]): row for row in connections
                if row.get("id") is not None and row.get("is_active", True)
            }
            # A missing team-member id is used only after the route has
            # explicitly verified a platform admin. In that case the selected
            # workspace, not an unrelated team_members row, defines scope.
            allowed_ids: set[int] = set(by_id) if team_member_id is None else set()
            if team_member_id is not None:
                access = self.client.table("team_member_whatsapp_access").select(
                    "whatsapp_number,can_view_messages"
                ).eq("team_member_id", team_member_id).execute().data or []
                allowed_numbers = {
                    re.sub(r"\D+", "", str(row.get("whatsapp_number") or ""))[-10:]
                    for row in access
                    if row.get("can_view_messages", True)
                }
                allowed_ids = {
                    connection_id for connection_id, row in by_id.items()
                    if re.sub(r"\D+", "", str(row.get("phone_number") or ""))[-10:] in allowed_numbers
                }

                # Access rows are optional.  In their absence the existing
                # dashboard semantics grant a member access to every active
                # connection in the workspace.  Once explicit rows exist,
                # only the selected numbers are in scope.
                if not access:
                    allowed_ids = set(by_id)

            if not allowed_ids and len(by_id) == 1:
                allowed_ids = set(by_id)
            if not allowed_ids:
                return set()
            groups = self.client.table("organization_group_connections").select(
                "group_jid"
            ).eq("organization_id", tenant_id).eq("is_active", True).eq(
                "opted_out", False
            ).in_("whatsapp_connection_id", list(allowed_ids)).execute().data or []
            return {str(row.get("group_jid") or "") for row in groups if row.get("group_jid")}
        except Exception:
            _logger.debug("Could not resolve team member WhatsApp group scope", exc_info=True)
            return None

    def _my_deals_owner_scope(
        self,
        tenant_id: str,
        team_member_id: int | None,
        user_id: str = "",
    ) -> tuple[set[str], set[str]]:
        """Resolve connection ids and phone identities that prove ownership."""
        all_connections = self.list_org_whatsapp_connections(tenant_id)
        connections = [
            row for row in all_connections
            if row.get("is_active", True)
        ]
        if not connections:
            return set(), set()

        personal_phones: set[str] = set()
        allowed_connection_ids: set[int] = set()

        if team_member_id is not None:
            member = self.get_team_member(team_member_id) or {}
            for value in (member.get("linked_broker_phone"), member.get("phone")):
                phone = _normalize_india_phone(str(value or ""))
                if phone:
                    personal_phones.add(phone)

            access = self.get_member_whatsapp_access(team_member_id, tenant_id)
            access_phones = {
                _normalize_india_phone(str(row.get("whatsapp_number") or ""))
                for row in access
                if row.get("can_view_messages", True)
            } - {""}
            matched_personal_connections = {
                int(row["id"])
                for row in connections
                if row.get("id") is not None
                and _normalize_india_phone(str(row.get("phone_number") or "")) in personal_phones
            }
            if matched_personal_connections:
                allowed_connection_ids = matched_personal_connections
            elif len(access_phones) == 1:
                allowed_connection_ids = {
                    int(row["id"])
                    for row in connections
                    if row.get("id") is not None
                    and _normalize_india_phone(str(row.get("phone_number") or "")) in access_phones
                }
            elif len(connections) == 1 and connections[0].get("id") is not None:
                allowed_connection_ids = {int(connections[0]["id"])}
        else:
            # Platform admins may open a selected workspace without a duplicate
            # team_members row. Only the actual workspace owner inherits its
            # phone identity; inspecting somebody else's workspace is not a
            # claim that their deals belong to the admin.
            organization = self.get_organization(tenant_id) or {}
            if user_id and str(organization.get("owner_user_id") or "") == str(user_id):
                owner_phone = _normalize_india_phone(str(organization.get("owner_phone") or ""))
                if owner_phone:
                    personal_phones.add(owner_phone)
                allowed_connection_ids = {
                    int(row["id"])
                    for row in connections
                    if row.get("id") is not None
                    and _normalize_india_phone(str(row.get("phone_number") or "")) in personal_phones
                }
                if not allowed_connection_ids and len(connections) == 1 and connections[0].get("id") is not None:
                    allowed_connection_ids = {int(connections[0]["id"])}
            elif len(connections) == 1 and connections[0].get("id") is not None:
                # Legacy workspaces can predate owner_user_id/owner_phone.
                # A single connection is unambiguous; never extend this
                # fallback to a multi-phone workspace.
                allowed_connection_ids = {int(connections[0]["id"])}

        allowed_connections = [
            row for row in connections
            if row.get("id") is not None and int(row["id"]) in allowed_connection_ids
        ]
        connection_phones = {
            _normalize_india_phone(str(row.get("phone_number") or ""))
            for row in allowed_connections
        } - {""}
        broker_ids = {
            str(row.get("broker_id") or "").strip()
            for row in all_connections
            if _normalize_india_phone(str(row.get("phone_number") or "")) in connection_phones
        } - {""}
        return broker_ids, personal_phones | connection_phones

    def parsed_owned_by_connected_phone(self, row_id: int, tenant_id: str, source_schema: str | None = None, team_member_id: int | None = None) -> bool:
        """Allow edits only for typed records from this member's groups."""
        owned_groups = self._team_member_group_scope(tenant_id, team_member_id) or set()
        candidates = [source_schema] if source_schema in _ALL_TYPED_TABLES else list(_ALL_TYPED_TABLES)
        for candidate in candidates:
            try:
                result = self.client.table(candidate).select("id,group_name").eq(
                    "id", row_id
                ).eq("tenant_id", tenant_id).limit(1).execute()
                if result.data:
                    group_name = str(result.data[0].get("group_name") or "")
                    return not group_name.endswith("@g.us") or group_name in owned_groups
            except Exception:
                continue
        return False

    def get_parsed_by_raw(self, raw_id: int) -> Optional[ParsedObservation]:
        rows = self.get_parsed_by_message(raw_id)
        if rows:
            return rows[0]
        return None

    def get_parsed(self, limit: int = 50, offset: int = 0, intent: str = "", classified_only: bool = False, asset_type: str = "", kind: str = "") -> list[dict]:
        # Merge all eight typed schemas globally. Per-table pagination causes
        # unstable pages and allows the same source item to appear twice.
        limit = max(1, min(int(limit or 1), 100))
        offset = max(0, min(int(offset or 0), 10000))
        fetch_limit = min(250, max(limit + offset, limit))
        rows = []
        rows_with_display_name = []
        rows_without_display_name = []
        for table in list(_TYPED_LISTING_TABLES.values()) + list(_TYPED_REQUIREMENT_TABLES.values()):
            try:
                query = self.client.table(table).select(
                    _typed_read_columns(table, include_evidence=True)
                ).order("created_at", desc=True).limit(fetch_limit)
                if self._tenant_id:
                    query = query.eq("tenant_id", self._tenant_id)
                result = query.execute()
                for row in result.data or []:
                    row["_typed_table"] = table
                    rows.append(row)
            except Exception:
                continue
        normalized_rows = []
        for row in rows:
            try:
                normalized_rows.append(self._typed_row_to_legacy(row))
            except (IndexError, KeyError, TypeError, ValueError) as exc:
                _logger.warning(
                    "Skipping malformed typed extraction row table=%s id=%s: %s",
                    row.get("_typed_table"), row.get("id"), exc,
                )
        rows = normalized_rows
        if intent:
            rows = [row for row in rows if str(row.get("intent") or "").upper() == str(intent).upper()]
        if asset_type:
            rows = [row for row in rows if str(row.get("asset_type") or "").lower() == asset_type.lower()]
        if kind == "listing":
            rows = [row for row in rows if row.get("message_type") == "listing"]
        elif kind == "requirement":
            rows = [row for row in rows if row.get("message_type") == "requirement"]
        if classified_only:
            rows = [row for row in rows if row.get("extraction_confidence") and row.get("needs_review") is not True]

        # Do not present an extraction without the WhatsApp evidence it is
        # supposed to represent. This also removes old malformed rows where
        # the typed record survived but the source text was empty/deleted.
        raw_ids = sorted({int(row.get("raw_message_id") or 0) for row in rows if int(row.get("raw_message_id") or 0) > 0})
        if raw_ids:
            valid_raw_ids: set[int] = set()
            for start in range(0, len(raw_ids), 100):
                try:
                    raw_query = self.client.table("raw_messages").select("id,message,is_group").in_("id", raw_ids[start:start + 100])
                    if self._tenant_id:
                        raw_query = raw_query.eq("tenant_id", self._tenant_id)
                    for raw in raw_query.execute().data or []:
                        if raw.get("is_group") is True and str(raw.get("message") or "").strip():
                            valid_raw_ids.add(int(raw["id"]))
                except Exception:
                    # Keep rows if evidence lookup is temporarily unavailable;
                    # a database timeout must not erase the audit page.
                    valid_raw_ids = set(raw_ids)
                    break
            rows = [row for row in rows if int(row.get("raw_message_id") or 0) in valid_raw_ids]
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)

        # Tables have independent unique indexes. Remove exact source-item
        # duplicates first, then reposts from the same broker/property within
        # the 24-hour deduplication window described in DATA_QUALITY.md.
        deduped: list[dict] = []
        seen_source_items: set[tuple[Any, ...]] = set()
        seen_raw_slices: dict[tuple[Any, ...], int] = {}
        seen_identity: dict[tuple[Any, ...], list[datetime]] = defaultdict(list)

        def norm(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "").strip().lower())

        def parse_created(value: Any) -> datetime | None:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                return None

        def source_slice(row: dict) -> str:
            payload = row.get("raw_payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    payload = {}
            if isinstance(payload, dict):
                value = payload.get("slice_text") or payload.get("full_text")
                if value:
                    return norm(str(value))
            return norm(row.get("normalized_message"))

        def row_quality(row: dict) -> int:
            fields = (
                "building_name", "micro_market", "broker_name", "broker_phone",
                "price", "price_per_sqft", "area_sqft", "area_min_sqft",
                "area_max_sqft", "furnishing", "floor_range", "car_parking_count",
            )
            return sum(1 for field in fields if row.get(field) not in (None, "", []))

        for row in rows:
            raw_id = row.get("raw_message_id")
            if raw_id is not None and int(raw_id or 0) > 0:
                source_key = (str(row.get("tenant_id") or ""), int(raw_id), int(row.get("listing_index") or 0))
                if source_key in seen_source_items:
                    continue
                seen_source_items.add(source_key)

                # Retries from the old flat/typed bridge could write the same
                # raw slice under different listing indexes. Keep legitimate
                # multi-listing slices separate, but collapse an identical
                # source slice and retain the more complete extraction.
                slice_text = source_slice(row)
                if slice_text:
                    raw_slice_key = (source_key[0], int(raw_id), slice_text)
                    prior_index = seen_raw_slices.get(raw_slice_key)
                    if prior_index is not None and 0 <= prior_index < len(deduped):
                        prior = deduped[prior_index]
                        if row_quality(row) > row_quality(prior):
                            deduped[prior_index] = row
                        continue
                    seen_raw_slices[raw_slice_key] = len(deduped)

            broker = norm(row.get("broker_phone")) or norm(row.get("broker_name"))
            location = norm(row.get("building_name")) or norm(row.get("landmark_name")) or norm(row.get("micro_market")) or norm(row.get("location_raw"))
            transaction = norm(row.get("transaction_type") or row.get("intent"))
            unit = norm(row.get("bhk")) or norm(row.get("configuration"))
            area = row.get("area_sqft") or row.get("carpet_area_sqft")
            floor = norm(row.get("floor_range"))
            price = row.get("price")
            created = parse_created(row.get("created_at"))
            if broker and location and (unit or area or floor or price is not None):
                identity = (broker, location, transaction, unit, str(area or ""), floor, str(price or ""))
                if created and any(abs((created - prior).total_seconds()) <= 24 * 3600 for prior in seen_identity[identity]):
                    continue
                if created:
                    seen_identity[identity].append(created)
            deduped.append(row)

        return [dict_to_dataclass(ParsedObservation, row) for row in deduped[offset:offset + limit]]

    def get_parsed_by_message(self, raw_message_id: int) -> list[ParsedObservation]:
        rows = []
        for table in list(_TYPED_LISTING_TABLES.values()) + list(_TYPED_REQUIREMENT_TABLES.values()):
            try:
                query = self.client.table(table).select("*").eq("raw_message_id", raw_message_id)
                if self._tenant_id:
                    query = query.eq("tenant_id", self._tenant_id)
                for row in query.execute().data or []:
                    row["_typed_table"] = table
                    rows.append(row)
            except Exception:
                continue
        rows.sort(key=lambda row: (row.get("listing_index") or 0, str(row.get("created_at") or "")))
        return [dict_to_dataclass(ParsedObservation, self._typed_row_to_legacy({**row, "_typed_table": row.get("_typed_table")})) for row in rows]

    @staticmethod
    def _duplicate_text(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    @staticmethod
    def _duplicate_number(value: Any) -> float | None:
        try:
            return float(value) if value is not None and str(value).strip() else None
        except (TypeError, ValueError):
            return None

    def find_near_duplicate_candidate(
        self,
        message_text: str,
        *,
        tenant_id: str,
        broker_phone: str = "",
        raw_timestamp: str | None = None,
    ) -> dict | None:
        """Return a recent same-broker semantic candidate, never a decision.

        Embeddings are deliberately used only to narrow the search.  The
        caller still extracts the message and ``record_listing_repost`` makes
        the structured-field decision afterwards.
        """
        if not message_text or not tenant_id:
            return None
        try:
            from semantic_embeddings import semantic_search
            hits = semantic_search(
                self,
                message_text,
                entity_types=["listing"],
                tenant_id=tenant_id,
                limit=20,
                min_similarity=0.86,
            ) or []
        except Exception as exc:
            _logger.debug("near-duplicate semantic lookup skipped: %s", exc)
            return None
        phone = re.sub(r"\D+", "", broker_phone or "")[-10:]
        cutoff = None
        if raw_timestamp:
            try:
                cutoff = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00")) - timedelta(days=7)
            except ValueError:
                cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        tables = set(_TYPED_LISTING_TABLE_NAMES)
        for hit in hits:
            table = str(hit.get("source_table") or "")
            if table not in tables:
                continue
            try:
                row = (self.client.table(table).select(
                    "id,tenant_id,raw_message_id,broker_id,broker_phone,building_name,"
                    "transaction_type,total_asking_price,monthly_rent,bhk,carpet_area_sqft,"
                    "floor_range,source_fingerprint,created_at"
                ).eq("id", int(hit.get("source_id"))).eq("tenant_id", tenant_id).limit(1).execute().data or [None])[0]
            except Exception:
                continue
            if not row:
                continue
            candidate_phone = re.sub(r"\D+", "", str(row.get("broker_phone") or ""))[-10:]
            if phone and candidate_phone and phone != candidate_phone:
                continue
            try:
                created = datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00"))
                if created < cutoff:
                    continue
            except ValueError:
                continue
            return {
                "source_table": table,
                "source_id": int(row["id"]),
                "similarity": float(hit.get("similarity") or 0),
                "row": row,
            }
        return None

    def record_listing_repost(
        self,
        parsed_id: int,
        candidate: dict | None,
        *,
        raw_timestamp: str | None = None,
        broker_id: int | None = None,
    ) -> dict:
        """Classify a saved listing using deterministic structured evidence."""
        current = self._get_typed_observation(parsed_id)
        if not current or current[0].endswith("_requirements"):
            return {"status": "distinct"}
        table, row = current
        posted_at = raw_timestamp or row.get("created_at")
        try:
            posted_dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            posted_dt = datetime.now(timezone.utc)
        base = {
            "duplicate_status": "distinct",
            "repost_count": 1,
            "last_posted_at": posted_dt.isoformat(),
        }
        if not candidate or not candidate.get("row"):
            self.client.table(table).update(base).eq("id", int(row["id"])).execute()
            return base
        old = candidate["row"]
        same_broker = (
            broker_id is not None and old.get("broker_id") is not None and int(old["broker_id"]) == int(broker_id)
        ) or (
            self._duplicate_text(row.get("broker_phone"))
            and self._duplicate_text(row.get("broker_phone")) == self._duplicate_text(old.get("broker_phone"))
        )
        same_building = self._duplicate_text(row.get("building_name")) == self._duplicate_text(old.get("building_name"))
        price_key = "monthly_rent" if row.get("transaction_type") == "rent" else "total_asking_price"
        current_price = self._duplicate_number(row.get(price_key))
        old_price = self._duplicate_number(old.get(price_key))
        current_area = self._duplicate_number(row.get("carpet_area_sqft"))
        old_area = self._duplicate_number(old.get("carpet_area_sqft"))
        current_bhk = self._duplicate_number(row.get("bhk"))
        old_bhk = self._duplicate_number(old.get("bhk"))
        same_bhk = current_bhk is not None and old_bhk is not None and current_bhk == old_bhk
        current_floor = self._duplicate_text(row.get("floor_range") or row.get("floor_label"))
        old_floor = self._duplicate_text(old.get("floor_range") or old.get("floor_label"))
        current_wing = self._duplicate_text(row.get("wing"))
        old_wing = self._duplicate_text(old.get("wing"))
        same_known_unit = not (
            current_floor and old_floor and current_floor != old_floor
            or current_wing and old_wing and current_wing != old_wing
        )
        same_price = current_price is not None and old_price is not None and current_price == old_price
        same_area = current_area is not None and old_area is not None and current_area == old_area
        same_source = bool(
            row.get("source_fingerprint")
            and old.get("source_fingerprint")
            and str(row.get("source_fingerprint")) == str(old.get("source_fingerprint"))
        )
        try:
            old_dt = datetime.fromisoformat(str(old.get("created_at") or "").replace("Z", "+00:00"))
            same_day = posted_dt.date() == old_dt.date()
        except (TypeError, ValueError):
            same_day = False
        similarity = float(candidate.get("similarity") or 0)
        exact = same_broker and same_building and same_day and (
            same_source or (same_price and same_area and same_bhk)
        )
        close = (
            same_broker and same_building and similarity >= 0.86 and
            ((current_price is not None and old_price is not None and abs(current_price - old_price) / max(old_price, 1) <= .10) or same_price) and
            ((current_area is not None and old_area is not None and abs(current_area - old_area) / max(old_area, 1) <= .10) or same_area)
        )
        # A same-day repost from the same broker for the same named building,
        # transaction and configuration is a likely duplicate even when the
        # broker changes the asking price. Keep it reviewable and out of
        # duplicate-looking public projections; different known floors/wings
        # remain distinct.
        likely_repost = same_broker and same_building and same_day and same_bhk and same_known_unit
        status = "merged" if exact else ("flagged" if close or likely_repost else "distinct")
        group_id = old.get("duplicate_group_id") or (str(uuid.uuid4()) if status == "merged" else None)
        if status == "merged":
            old_update = {
                "duplicate_status": "distinct", "duplicate_group_id": group_id,
                "repost_count": int(old.get("repost_count") or 1) + 1,
                "last_posted_at": posted_dt.isoformat(),
            }
            self.client.table(candidate["source_table"]).update(old_update).eq("id", int(candidate["source_id"])).execute()
        update = {
            **base,
            "duplicate_status": status,
            "duplicate_group_id": group_id,
            "possible_duplicate_source_table": candidate.get("source_table"),
            "possible_duplicate_source_id": int(candidate.get("source_id") or 0),
            "possible_duplicate_similarity": similarity,
        }
        self.client.table(table).update(update).eq("id", int(row["id"])).execute()
        return update

    @staticmethod
    def _requirement_duplicate_key(row: dict) -> tuple[str, ...]:
        """Build a conservative identity for repeated demand posts.

        Requirements are only candidates here. Missing fields stay empty so a
        generic "office wanted" post cannot collapse every other office need.
        """
        def norm(value: Any) -> str:
            if isinstance(value, (list, tuple)):
                value = ",".join(sorted(str(item) for item in value if item))
            return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

        return (
            norm(row.get("asset_type")),
            norm(row.get("transaction_type")),
            norm(row.get("commercial_use_type") or row.get("property_type")),
            norm(row.get("bhk_options") or row.get("configuration_type") or row.get("bhk")),
            norm(row.get("locality_options") or row.get("micro_market_options") or row.get("micro_market")),
            norm(row.get("building_preferences") or row.get("building_name")),
            norm(row.get("budget_min")),
            norm(row.get("budget_max")),
            norm(row.get("area_min_sqft") or row.get("carpet_area_min_sqft")),
            norm(row.get("area_max_sqft") or row.get("carpet_area_max_sqft")),
            norm(row.get("furnishing_preference") or row.get("furnishing_status")),
        )

    def record_requirement_duplicate(
        self,
        parsed_id: int,
        *,
        table: str,
        tenant_id: str,
    ) -> dict:
        """Flag an exact structured demand repeat for broker review.

        This deliberately never merges. The source and candidate remain
        independently traceable until the broker confirms the merge in My
        Deals.
        """
        if table not in _TYPED_REQUIREMENT_TABLE_NAMES or not tenant_id:
            return {"status": "distinct"}
        try:
            current = (self.client.table(table).select(
                "*"
            ).eq("id", int(parsed_id)).eq("tenant_id", tenant_id).limit(1).execute().data or [None])[0]
            if not current:
                return {"status": "distinct"}
            current_key = self._requirement_duplicate_key(current)
            if not any(current_key) or not current_key[1]:
                return {"status": "distinct"}
            candidates = self.client.table(table).select(
                "id,tenant_id,raw_message_id,created_at,duplicate_group_id,repost_count"
            ).eq("tenant_id", tenant_id).neq("id", int(parsed_id)).order(
                "created_at", desc=True
            ).limit(250).execute().data or []
            candidate_ids = [int(row["id"]) for row in candidates if row.get("id")]
            if not candidate_ids:
                return {"status": "distinct"}
            full_candidates = self.client.table(table).select("*").eq(
                "tenant_id", tenant_id
            ).in_("id", candidate_ids).execute().data or []
            candidate = next(
                (row for row in full_candidates if self._requirement_duplicate_key(row) == current_key),
                None,
            )
            if not candidate:
                return {"status": "distinct"}
            group_id = candidate.get("duplicate_group_id") or str(uuid.uuid4())
            self.client.table(table).update({
                "duplicate_status": "flagged",
                "duplicate_group_id": group_id,
                "possible_duplicate_source_table": table,
                "possible_duplicate_source_id": int(current["id"]),
                "possible_duplicate_similarity": 1.0,
                "repost_count": int(candidate.get("repost_count") or 1) + 1,
            }).eq("id", int(candidate["id"])).eq("tenant_id", tenant_id).execute()
            update = {
                "duplicate_status": "flagged",
                "duplicate_group_id": group_id,
                "possible_duplicate_source_table": table,
                "possible_duplicate_source_id": int(candidate["id"]),
                "possible_duplicate_similarity": 1.0,
                "repost_count": 1,
            }
            self.client.table(table).update(update).eq("id", int(current["id"])).eq("tenant_id", tenant_id).execute()
            return update
        except Exception:
            _logger.debug("requirement duplicate review skipped", exc_info=True)
            return {"status": "distinct"}

    def merge_my_deal_listing(
        self, source_schema: str, source_id: int, target_schema: str, target_id: int, *, tenant_id: str
    ) -> bool:
        allowed = set(_ALL_TYPED_TABLES)
        if source_schema not in allowed or target_schema not in allowed or source_schema == target_schema and source_id == target_id:
            return False
        source = (self.client.table(source_schema).select("*").eq("id", int(source_id)).eq("tenant_id", tenant_id).limit(1).execute().data or [None])[0]
        target = (self.client.table(target_schema).select("*").eq("id", int(target_id)).eq("tenant_id", tenant_id).limit(1).execute().data or [None])[0]
        if not source or not target:
            return False
        group_id = target.get("duplicate_group_id") or source.get("duplicate_group_id") or str(uuid.uuid4())
        latest = max(str(source.get("last_posted_at") or source.get("created_at") or ""), str(target.get("last_posted_at") or target.get("created_at") or ""))
        self.client.table(target_schema).update({
            "duplicate_status": "distinct", "duplicate_group_id": group_id,
            "repost_count": int(target.get("repost_count") or 1) + int(source.get("repost_count") or 1),
            "last_posted_at": latest or datetime.now(timezone.utc).isoformat(),
        }).eq("id", int(target_id)).execute()
        self.client.table(source_schema).update({"duplicate_status": "merged", "duplicate_group_id": group_id}).eq("id", int(source_id)).execute()
        return True

    def get_my_deals(
        self,
        limit: int = 200,
        tenant_id: str | None = None,
        team_member_id: int | None = None,
        user_id: str = "",
    ) -> list[dict]:
        """Return deals whose raw evidence proves they belong to this user."""
        tenant_id = tenant_id or self._tenant_id
        if not tenant_id:
            return []
        requested = max(1, min(int(limit or 200), 500))
        # There are eight typed source tables. Fetching ``requested`` rows
        # from every table made a normal CRM refresh transfer up to 4,000
        # records (plus evidence blobs) before sorting them locally. Bound
        # each branch so the fan-out stays proportional to the requested
        # result size while still giving every schema a fair share.
        per_table_limit = max(50, min(150, math.ceil(requested / 2)))
        typed_rows = self._fetch_typed_rows(
            tenant_id=tenant_id,
            limit_per_table=per_table_limit,
            include_normalized_message=True,
        )
        allowed_broker_ids, owner_phones = self._my_deals_owner_scope(
            tenant_id, team_member_id, user_id
        )
        raw_ids = sorted({
            int(row.get("raw_message_id") or 0)
            for row in typed_rows
            if int(row.get("raw_message_id") or 0) > 0
        })
        raw_by_id: dict[int, dict] = {}
        raw_batches = [raw_ids[start:start + 100] for start in range(0, len(raw_ids), 100)]

        def fetch_raw_batch(batch: list[int]) -> list[dict]:
            raw_query = self.client.table("raw_messages").select(
                "id,sender_jid,sender_phone,source,raw_payload,message_uid,group_name,message,timestamp"
            ).eq("tenant_id", tenant_id).in_("id", batch)
            return raw_query.execute().data or []

        if raw_batches:
            with ThreadPoolExecutor(max_workers=min(8, len(raw_batches))) as executor:
                futures = [executor.submit(fetch_raw_batch, batch) for batch in raw_batches]
                for future in futures:
                    for raw in future.result():
                        raw_by_id[int(raw["id"])] = raw

        owned: list[dict] = []
        seen: set[tuple[int, int, str]] = set()
        for typed in typed_rows:
            if typed.get("duplicate_status") == "merged":
                continue
            raw_id = int(typed.get("raw_message_id") or 0)
            raw = raw_by_id.get(raw_id)
            if not raw or not _raw_message_owned_by_user(
                raw,
                owner_phones=owner_phones,
                allowed_broker_ids=allowed_broker_ids,
                user_id=user_id,
            ):
                continue
            candidate_phone = str(typed.get("broker_phone") or "")
            row = self._typed_row_to_legacy(typed)
            row["crm_owner_phone"] = re.sub(r"\D+", "", candidate_phone)[-10:] if candidate_phone else None
            source_message = _source_evidence_for_typed_row(
                typed,
                raw,
                typed.get("normalized_message") or row.get("summary_title") or "",
            )
            # Keep the complete authenticated evidence available when the
            # normalized slice is empty or was rejected as inconsistent.
            row["raw_message"] = raw.get("message") or ""
            if not source_message or re.fullmatch(r"(?:\[unstructured\]\s*)?(?:unknown|listing)", source_message.strip(), re.IGNORECASE):
                source_message = row["raw_message"]
            row["source_message"] = source_message
            row["source_group"] = typed.get("group_name") or raw.get("group_name")
            row["source_sender"] = typed.get("broker_name")
            row["source_timestamp"] = (
                raw.get("timestamp")
                or typed.get("last_posted_at")
                or typed.get("created_at")
            )
            row["source"] = raw.get("source") or row.get("source")
            raw_payload = raw.get("raw_payload") or {}
            row["source_scope"] = typed.get("source_scope") or raw_payload.get("source_scope")
            row["visibility"] = typed.get("visibility") or raw_payload.get("visibility")
            key = (
                int(typed.get("id") or 0),
                int(typed.get("raw_message_id") or 0),
                str(typed.get("_typed_table") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            owned.append(row)

        # Existing requirements predate the duplicate-review columns. Group
        # exact structured repeats at read time as well, so the broker sees a
        # review prompt immediately after deployment without a destructive
        # historical rewrite. The explicit merge action persists the broker's
        # decision for the selected pair.
        requirement_groups: dict[tuple[str, ...], list[dict]] = defaultdict(list)
        for row in owned:
            if row.get("message_type") != "requirement":
                continue
            duplicate_key = self._requirement_duplicate_key(row)
            if any(duplicate_key) and duplicate_key[1]:
                requirement_groups[duplicate_key].append(row)
        for group in requirement_groups.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
            primary = group[0]
            group_id = primary.get("duplicate_group_id") or str(uuid.uuid4())
            for row in group:
                if row is primary:
                    candidate = group[1]
                else:
                    candidate = primary
                row.update({
                    "duplicate_status": "flagged",
                    "duplicate_group_id": group_id,
                    "possible_duplicate_source_table": candidate.get("source_schema"),
                    "possible_duplicate_source_id": int(candidate.get("id") or 0),
                    "possible_duplicate_similarity": 1.0,
                    "repost_count": len(group),
                })

        # Exact repeated supply evidence should occupy one CRM card while
        # older source rows remain available for audit/history. Use the raw
        # WhatsApp text when extraction is incomplete, and do not require the
        # same broker/group: the same listing is commonly reposted across
        # several broker groups. Exact evidence is safe; building-name-only
        # similarity is deliberately not.
        listing_groups: dict[tuple[str, ...], list[dict]] = defaultdict(list)
        for row in owned:
            if row.get("message_type") == "requirement":
                continue
            evidence = re.sub(r"\s+", " ", str(row.get("source_message") or row.get("raw_message") or "").strip().lower())
            evidence = re.sub(r"^\d+\s*[.)\]:-]\s*", "", evidence)
            if len(evidence) < 20 or evidence in {"[unstructured] unknown", "unknown", "listing"}:
                continue
            # The normalized raw text is the cross-group identity. Parsed
            # building/transaction fields are intentionally not part of this
            # key because those are precisely the fields that may be missing
            # or wrong on an incomplete extraction.
            listing_groups[(evidence,)].append(row)
        for group in listing_groups.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
            primary = group[0]
            group_id = primary.get("duplicate_group_id") or str(uuid.uuid4())
            primary["repost_count"] = len(group)
            primary["last_posted_at"] = primary.get("source_timestamp") or primary.get("created_at")
            primary["repost_source_groups"] = sorted({
                str(item.get("source_group") or "").strip()
                for item in group
                if str(item.get("source_group") or "").strip()
            })
            for duplicate in group[1:]:
                duplicate.update({
                    "duplicate_status": "flagged",
                    "duplicate_group_id": group_id,
                    "possible_duplicate_source_table": primary.get("source_schema"),
                    "possible_duplicate_source_id": int(primary.get("id") or 0),
                    "possible_duplicate_similarity": 1.0,
                    "repost_count": len(group),
                })

        owned.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return owned[:requested]

    # ── Listings ─────────────────────────────────────────────────

    def _market_requirement_payload(
        self,
        obs: dict,
        broker_id: int | None = None,
    ) -> dict | None:
        """Build the demand-side projection from one parsed observation.

        Price bounds are stored in absolute AED so requirement matching can
        compare them to listings without mixing K/M units. A single
        explicit budget is represented as equal min/max bounds.
        """
        if not _is_market_requirement(obs):
            return None
        tenant_id = obs.get("tenant_id") or self._tenant_id
        raw_id = obs.get("raw_message_id")
        parsed_id = obs.get("id")
        if not tenant_id or raw_id is None or parsed_id is None:
            return None

        raw_payload = obs.get("raw_payload") or {}
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                raw_payload = {}
        price_unit = obs.get("price_unit")
        default_price = obs.get("price")
        price_min = _price_to_rupees(
            raw_payload.get("price_min", default_price) if isinstance(raw_payload, dict) else default_price,
            raw_payload.get("price_unit", price_unit) if isinstance(raw_payload, dict) else price_unit,
        )
        price_max = _price_to_rupees(
            raw_payload.get("price_max", default_price) if isinstance(raw_payload, dict) else default_price,
            raw_payload.get("price_unit", price_unit) if isinstance(raw_payload, dict) else price_unit,
        )
        if price_min is not None and price_max is not None and price_min > price_max:
            price_min, price_max = price_max, price_min

        fingerprint = f"requirement:{tenant_id}:{parsed_id}"
        return {
            "fingerprint": fingerprint,
            "intent": obs.get("intent") or "BUY",
            "transaction_type": obs.get("transaction_type"),
            "bhk": obs.get("bhk"),
            "price_min": price_min,
            "price_max": price_max,
            "price_unit": "abs",
            "area_sqft": obs.get("area_sqft"),
            "location_label": obs.get("location_raw") or obs.get("micro_market"),
            "building_name": obs.get("building_name"),
            "landmark_name": obs.get("landmark_name"),
            "micro_market": obs.get("micro_market"),
            "broker_id": broker_id if broker_id is not None else obs.get("broker_id"),
            "broker_name": obs.get("broker_name") or obs.get("profile_name"),
            "broker_phone": obs.get("broker_phone"),
            "raw_message_id": raw_id,
            "confidence": obs.get("confidence") or 0,
            "first_seen": obs.get("created_at") or None,
            "last_seen": obs.get("created_at") or None,
            "tenant_id": tenant_id,
        }

    def _market_requirement_broker_id(self, obs: dict, tenant_id: str | None = None) -> int | None:
        if obs.get("broker_id") is not None:
            try:
                return int(obs["broker_id"])
            except (TypeError, ValueError):
                pass
        phone = _normalize_india_phone(obs.get("broker_phone") or "")
        if not phone:
            return None
        query = self.client.table("brokers").select("id").eq("primary_phone", phone).limit(1)
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        try:
            rows = query.execute().data or []
            return int(rows[0]["id"]) if rows else None
        except Exception:
            return None

    def upsert_market_requirement_from_parsed(self, parsed_id: int) -> int:
        """Return the already-promoted typed requirement observation.

        Requirements are promoted by ``save_parsed`` itself.  This method is
        retained as a compatibility hook for callers in the extraction loop,
        but it must not write through the deprecated bridge or create a second
        copy of the same demand record.
        """
        try:
            found = self._get_typed_observation(parsed_id)
            if not found:
                return 0
            table, obs = found
            if not table.endswith("_requirements") and not _is_market_requirement(obs):
                return 0
            tenant_id = obs.get("tenant_id") or self._tenant_id
            return int(obs.get("id") or 0)
        except Exception as exc:
            print(f"[upsert_market_requirement_from_parsed] parsed {parsed_id}: {exc}", flush=True)
            return 0

    def rebuild_market_requirements(self, limit: int = 0, tenant_id: str | None = None) -> int:
        """Report typed requirements; new rows are written by ``save_parsed``."""
        tid = tenant_id or self._tenant_id
        rows = self._fetch_typed_rows(requirements=True, tenant_id=tid, limit_per_table=10000)
        return min(limit, len(rows)) if limit else len(rows)

    def _listing_from_parsed(
        self, obs: dict, resolver: Optional[dict]
    ) -> Listing:
        """Build a Listing from a parsed_observation (+ optional resolver
        decision). The resolver's resolved building/micro_market win when the
        parse was unresolved, so listings inherit confirmed locality."""
        micro_market = (resolver or {}).get("micro_market") or obs.get("micro_market")
        building_name = (resolver or {}).get("building_name") or obs.get("building_name")

        # If we have price_per_sqft and area_sqft but no total price, compute it.
        price = obs.get("price")
        price_unit = obs.get("price_unit")
        area_sqft = obs.get("area_sqft")
        price_per_sqft = obs.get("price_per_sqft")
        total_asking_price = obs.get("total_asking_price")
        
        # If we have price_per_sqft and area but no total price, compute it
        if total_asking_price is None and price_per_sqft is not None and area_sqft is not None and area_sqft > 0:
            total_asking_price = price_per_sqft * area_sqft
            # If we're inferring total from psf, the unit is absolute rupees
            if price is None:
                price = total_asking_price
                price_unit = "abs"

        price, price_unit = _normalize_listing_price(price, price_unit)

        # ── Broker phone fallback: when the phone is missing from this
        # typed listing tables, look up a sibling observation from the same raw
        # message that did extract it.  This happens when multi-listing
        # extraction creates multiple parsed rows and only one captures the
        # broker identity.
        broker_phone = obs.get("broker_phone")
        broker_name = obs.get("broker_name")
        raw_msg_id = obs.get("raw_message_id")
        if not broker_phone and raw_msg_id:
            try:
                siblings = self._fetch_typed_rows(raw_message_id=int(raw_msg_id), limit_per_table=50)
                sibling = next((item for item in siblings if item.get("broker_phone")), None)
                if sibling:
                    broker_phone = sibling.get("broker_phone") or broker_phone
                    broker_name = broker_name or sibling.get("broker_name")
            except Exception:
                pass
        
        return Listing(
            intent=obs.get("intent"),
            asset_type=obs.get("asset_type"),
            property_type=obs.get("property_type"),
            transaction_type=obs.get("transaction_type"),
            commercial_use_type=obs.get("commercial_use_type"),
            fitout_status=obs.get("fitout_status"),
            occupancy_type=obs.get("occupancy_type"),
            bhk=obs.get("bhk"),
            price=price,
            price_unit=price_unit,
            price_model=obs.get("price_model"),
            price_per_sqft=obs.get("price_per_sqft"),
            area_sqft=obs.get("area_sqft"),
            furnishing=obs.get("furnishing"),
            location_label=micro_market or obs.get("location_raw"),
            building_name=building_name,
            landmark_name=obs.get("landmark_name"),
            micro_market=micro_market,
            canonical_micro_market_slug=canonical_micro_market_slug(micro_market),
            broker_name=broker_name,
            broker_phone=broker_phone,
            latest_raw_message_id=obs.get("raw_message_id"),
            representative_raw_message_id=obs.get("raw_message_id"),
            representative_listing_index=obs.get("listing_index"),
            last_seen=obs.get("created_at") or None,
            first_seen=obs.get("created_at") or None,
            observation_count=1,
            deal_tags=list(obs.get("deal_tags") or []),
            additional_charges=list(obs.get("additional_charges") or []),
            # v2 schema — physical / deal attributes
            carpet_area_sqft=obs.get("carpet_area_sqft"),
            built_up_area_sqft=obs.get("built_up_area_sqft"),
            bathroom_count=obs.get("bathroom_count"),
            car_parking_count=obs.get("car_parking_count"),
            parking_type=obs.get("parking_type"),
            deposit_amount=obs.get("deposit_amount"),
            possession_date=obs.get("possession_date"),
            possession_status=obs.get("possession_status"),
            oc_status=obs.get("oc_status"),
            interior_value=obs.get("interior_value"),
            ceiling_height=obs.get("ceiling_height"),
            price_basis=obs.get("price_basis"),
            brokerage_type=obs.get("brokerage_type"),
            configuration_type=obs.get("configuration_type"),
            lease_term_type=obs.get("lease_term_type"),
            # v2 schema — unit-level amenities
            amenities=list(obs.get("amenities") or []),
            amenities_unverified_claim=obs.get("amenities_unverified_claim"),
            # v2 schema — rental / tenancy policy
            pet_policy=obs.get("pet_policy"),
            tenant_type_preference=obs.get("tenant_type_preference"),
            sharing_allowed=obs.get("sharing_allowed"),
            company_lease_criteria=obs.get("company_lease_criteria"),
            # IMPORTANT: tenant_nationality_preference is INTERNAL/BROKER-FACING ONLY.
            # Must NEVER appear in any public-facing API response, search filter,
            # or badge on propai.live / consumer surfaces.
            tenant_nationality_preference=obs.get("tenant_nationality_preference"),
            validation_flags=obs.get("validation_flags") or [],
            needs_review=bool(obs.get("needs_review")),
        )

    def rebuild_listings(self, limit: int = 0):
        """Bridge parsed_observations (+ resolver_decisions) into the `listings`
        table. Idempotent: each observation upserts via save_listing's
        fingerprint dedup, so re-running only updates existing rows and never
        creates duplicates. Call without limit to rebuild everything, or pass a
        recent cutoff by feeding only new observations.

        This is the bridge between AI-produced parsed observations and the
        `listings` table that www reads.
        """
        processed = 0
        rows = self._fetch_typed_rows(requirements=False, limit_per_table=10000)
        rows.sort(key=lambda row: int(row.get("raw_message_id") or 0))
        for obs in rows[:limit] if limit else rows:
            try:
                resolver = None
                try:
                    r = self.get_resolver_by_parsed(obs["id"])
                    resolver = r.__dict__ if r else None
                except Exception:
                    resolver = None
                self.save_listing(self._listing_from_parsed(self._typed_row_to_legacy(obs), resolver))
                processed += 1
            except Exception as exc:
                print(f"[rebuild_listings] skip obs {obs.get('id')}: {exc}", flush=True)
        return processed

    def upsert_listing_from_parsed(self, parsed_id: int) -> int:
        """Incrementally push a single parsed_observation (+ its resolver
        decision) into `listings`. Safe to call on every new observation — the
        fingerprint upsert dedupes against existing rows.

        Enrichment job queuing is in a separate try block so a failure there
        cannot mask a successful save_listing() return value.
        """
        obs = None
        listing_id = 0
        try:
            found = self._get_typed_observation(parsed_id)
            if not found:
                return 0
            table, obs = found
            if table.endswith("_requirements") or _is_market_requirement(obs):
                return 0
            resolver = None
            try:
                r = self.get_resolver_by_parsed(parsed_id)
                resolver = r.__dict__ if r else None
            except Exception:
                resolver = None
            listing = self._listing_from_parsed(obs, resolver)

            # ── Locality validation (DB-aware, second pass) ──────
            try:
                from listing_validation import validate_listing_locality
                loc_flags = validate_listing_locality(obs, self)
                if loc_flags:
                    existing_flags = list(listing.validation_flags or [])
                    listing.validation_flags = existing_flags + loc_flags
            except Exception as lve:
                print(f"[upsert_listing_from_parsed] locality validation error: {lve}", flush=True)

            # ``save_parsed`` already wrote this observation to the selected
            # typed listing table. Do not project it a second time through a
            # legacy-shaped Listing payload.
            listing_id = int(obs.get("id") or 0)
        except Exception as exc:
            print(f"[upsert_listing_from_parsed] parsed {parsed_id}: {exc}", flush=True)
            return 0

        # ── Queue enrichment job (separate try: failure here must not mask
        # save_listing success above) ──
        if listing_id and obs:
            try:
                building_name = obs.get("building_name")
                raw_msg_id = obs.get("raw_message_id")
                if not self._building_resolved(building_name) and raw_msg_id:
                    self.create_enrichment_job(parsed_id, raw_msg_id,
                                               scheduled_after=datetime.now(timezone.utc).isoformat())
            except Exception as exc:
                print(f"[upsert_listing_from_parsed] enrichment job queue error: {exc}", flush=True)
        return listing_id

    def save_listing(self, listing: Listing) -> int:
        data = {k: v for k, v in listing.__dict__.items() if v is not None and v != ""}
        data.pop("id", None)
        if not data.get("fingerprint"):
            data["fingerprint"] = listing_fingerprint(data)
        if not data.get("location_label"):
            data["location_label"] = listing_label(data)
        if not data.get("tenant_id") and self._tenant_id:
            data["tenant_id"] = self._tenant_id
        # Write directly to the typed listing table.  The old flat `listings`
        # relation was only a compatibility bridge and is intentionally not a
        # destination for new observations.
        asset = str(data.get("asset_type") or "residential").lower()
        transaction = str(data.get("transaction_type") or "").lower()
        if transaction not in {"rent", "sale"}:
            transaction = "rent" if str(data.get("intent") or "").upper() == "RENT" else "sale"
        table = _TYPED_LISTING_TABLES.get((asset, transaction), "residential_sale_listings")
        locality_raw = data.get("location_raw") or data.get("location_label")
        locality_resolved = data.get("locality_resolved") or data.get("micro_market")
        typed = {
            "raw_message_id": data.get("latest_raw_message_id") or data.get("representative_raw_message_id") or 0,
            "tenant_id": data.get("tenant_id") or self._tenant_id,
            "listing_index": data.get("representative_listing_index") or 0,
            "source_fingerprint": data["fingerprint"],
            "opportunity_key": _observation_fingerprint(data),
            "legacy_source_id": data.get("latest_raw_message_id"),
            "asset_type": asset,
            "transaction_type": transaction,
            "building_name": data.get("building_name"),
            "locality_raw": locality_raw,
            "locality_resolved": locality_resolved,
            "micro_market": data.get("micro_market") or locality_resolved or locality_raw,
            "landmark_name": data.get("landmark_name"),
            "broker_id": data.get("broker_id"),
            # A phone belongs in broker_phone only. Do not use it as a display
            # name when the source message has no broker name.
            "broker_name": _clean_person_name(data.get("broker_name") or "") or None,
            "broker_phone": data.get("broker_phone"),
            "summary_title": data.get("summary_title"),
            "raw_payload": data.get("raw_payload") or {},
            "deal_tags": data.get("deal_tags") or [],
            "additional_charges": data.get("additional_charges") or [],
            "validation_flags": data.get("validation_flags") or [],
            "needs_review": bool(data.get("needs_review")),
            "extraction_confidence": "high" if not data.get("needs_review") else "low",
            "bhk": float(re.search(r"\d+(?:\.\d+)?", str(data.get("bhk"))).group(0)) if re.search(r"\d+(?:\.\d+)?", str(data.get("bhk"))) else None,
            "configuration_type": data.get("configuration_type") or data.get("configuration") or data.get("bhk"),
            "carpet_area_sqft": data.get("carpet_area_sqft") or data.get("area_sqft"),
            "built_up_area_sqft": data.get("built_up_area_sqft"),
            "mezzanine_area_sqft": data.get("mezzanine_area_sqft"),
            "area_raw_text": data.get("area_sqft"),
            "total_asking_price": _price_to_rupees(data.get("price"), data.get("price_unit")) if transaction == "sale" else None,
            "monthly_rent": _price_to_rupees(data.get("price"), data.get("price_unit")) if transaction == "rent" else None,
            "price_per_sqft": data.get("price_per_sqft") if transaction == "sale" else None,
            "rent_per_sqft": data.get("price_per_sqft") if transaction == "rent" else None,
            "price_raw_text": data.get("price"),
            "price_basis": data.get("price_basis"),
            "furnishing_status": data.get("furnishing"),
            "fitout_status": data.get("fitout_status") or data.get("furnishing"),
            "commercial_use_type": data.get("commercial_use_type") or data.get("property_type"),
            "possession_status": data.get("possession_status"),
            "possession_date": _coerce_sql_date(data.get("possession_date")),
            "available_from": _coerce_sql_date(data.get("available_from")),
            "floor_range": data.get("floor_range"),
            "car_parking_count": data.get("car_parking_count"),
            "parking_type": data.get("parking_type"),
            "deposit_amount": data.get("deposit_amount"),
            "deposit_applicable": data.get("deposit_amount") is not None,
            "lease_term_type": data.get("lease_term_type"),
            "oc_status": data.get("oc_status"),
            "ceiling_height": data.get("ceiling_height"),
            "has_lift": data.get("has_lift"),
            "has_power_backup": data.get("has_power_backup"),
            "power_load_kw": data.get("power_load_kw"),
            "brokerage_type": data.get("brokerage_type"),
            "building_amenities": data.get("building_amenities") or [],
            "unit_amenities": data.get("amenities") or [],
            "amenities_unverified_claim": data.get("amenities_unverified_claim"),
            "pet_policy": data.get("pet_policy"),
            "tenant_type_preference": data.get("tenant_type_preference"),
            "sharing_allowed": data.get("sharing_allowed"),
            "company_lease_criteria": data.get("company_lease_criteria"),
            "tenant_nationality_preference": data.get("tenant_nationality_preference"),
            "broker_company": data.get("broker_company"),
            "contacts": data.get("contacts") or [],
            "showing_instructions": data.get("showing_instructions"),
            "contact_instructions": data.get("contact_instructions"),
            "availability_status": data.get("availability_status"),
            "brokerage_context": data.get("brokerage_context"),
            "co_brokered": data.get("co_brokered"),
            "wing": data.get("wing"),
            "floor_min": data.get("floor_min"),
            "floor_max": data.get("floor_max"),
            "floor_label": data.get("floor_label"),
            "original_bhk": data.get("original_bhk"),
            "current_bhk": data.get("current_bhk"),
            "is_converted_unit": data.get("is_converted_unit"),
            "is_combination_unit": data.get("is_combination_unit"),
            "configuration_details": data.get("configuration_details"),
            "can_sell_separately": data.get("can_sell_separately"),
            "balcony_area_sqft": data.get("balcony_area_sqft"),
            "balcony_area_raw_text": data.get("balcony_area_raw_text"),
            "terrace_area_sqft": data.get("terrace_area_sqft"),
            "covered_terrace_area_sqft": data.get("covered_terrace_area_sqft"),
            "terrace_area_raw_text": data.get("terrace_area_raw_text"),
            "sellable_area_sqft": data.get("sellable_area_sqft"),
            "computed_total_asking_price": data.get("computed_total_asking_price"),
            "computed_price_confidence": data.get("computed_price_confidence"),
            "price_math": data.get("price_math") or {},
            "unit_condition": data.get("unit_condition"),
            "vastu_compliant": data.get("vastu_compliant"),
            "view_description": data.get("view_description"),
            "parking_details": data.get("parking_details") or {},
            "society_restrictions": data.get("society_restrictions") or [],
            "society_restrictions_raw": data.get("society_restrictions_raw"),
            "unstructured_facts": data.get("unstructured_facts") or {},
        }
        typed["broker_name"] = self._canonical_broker_name(data.get("broker_id")) or typed.get("broker_name")
        allowed = {
            "residential_sale_listings": {"raw_message_id","tenant_id","listing_index","source_fingerprint","legacy_source_id","asset_type","transaction_type","building_name","locality_raw","locality_resolved","micro_market","landmark_name","broker_id","broker_name","broker_phone","summary_title","raw_payload","deal_tags","additional_charges","validation_flags","needs_review","extraction_confidence","bhk","carpet_area_sqft","built_up_area_sqft","super_built_up_area_sqft","area_raw_text","total_asking_price","price_per_sqft","price_raw_text","price_basis","furnishing_status","possession_status","possession_date","floor_range","car_parking_count","parking_type","oc_status","brokerage_type","building_amenities","unit_amenities","amenities_unverified_claim","configuration_type","broker_company","contacts","showing_instructions","contact_instructions","availability_status","brokerage_context","co_brokered","wing","floor_min","floor_max","floor_label","original_bhk","current_bhk","is_converted_unit","is_combination_unit","configuration_details","can_sell_separately","balcony_area_sqft","balcony_area_raw_text","terrace_area_sqft","covered_terrace_area_sqft","terrace_area_raw_text","sellable_area_sqft","computed_total_asking_price","computed_price_confidence","price_math","unit_condition","vastu_compliant","view_description","parking_details","society_restrictions","society_restrictions_raw","unstructured_facts"},
            "residential_rent_listings": {"raw_message_id","tenant_id","listing_index","source_fingerprint","legacy_source_id","asset_type","transaction_type","building_name","locality_raw","locality_resolved","micro_market","landmark_name","broker_id","broker_name","broker_phone","summary_title","raw_payload","deal_tags","additional_charges","validation_flags","needs_review","extraction_confidence","bhk","carpet_area_sqft","built_up_area_sqft","area_raw_text","monthly_rent","rent_per_sqft","price_raw_text","price_basis","furnishing_status","possession_status","available_from","availability_date_raw","availability_status","floor_range","floor_min","floor_max","floor_label","wing","car_parking_count","parking_type","parking_details","has_lift","deposit_amount","deposit_applicable","lease_term_type","lease_term_min_months","lease_term_max_months","lease_term_raw_text","brokerage_type","brokerage_context","brokerage_terms_raw","plus_one_deal","fee_sharing_required","client_profile_required","original_bhk","current_bhk","configuration_details","is_converted_unit","is_combination_unit","balcony_present","balcony_area_sqft","balcony_area_raw_text","terrace_area_sqft","covered_terrace_area_sqft","terrace_area_raw_text","sit_out_present","unit_condition","view_description","society_restrictions_raw","broker_company","contacts","showing_instructions","contact_instructions","unstructured_facts","building_amenities","unit_amenities","amenities_unverified_claim","pet_policy","tenant_type_preference","sharing_allowed","company_lease_criteria","tenant_nationality_preference"},
            "commercial_sale_listings": {"raw_message_id","tenant_id","listing_index","source_fingerprint","legacy_source_id","asset_type","transaction_type","building_name","locality_raw","locality_resolved","micro_market","landmark_name","broker_id","broker_name","broker_phone","broker_rera_number","summary_title","raw_payload","deal_tags","additional_charges","validation_flags","needs_review","extraction_confidence","commercial_use_type","occupancy_status","carpet_area_sqft","built_up_area_sqft","chargeable_area_sqft","super_built_up_area_sqft","saleable_area_sqft","area_raw_text","total_asking_price","price_per_sqft","price_raw_text","price_basis","fitout_status","ceiling_height","floor_level","floor_count","floor_range","car_parking_count","parking_type","has_lift","has_power_backup","power_load_kw","oc_status","brokerage_type","building_amenities","terrace_area_sqft","covered_terrace_area_sqft","terrace_area_raw_text","frontage_ft","entrance_count","permitted_use_types","ideal_for","project_inventory","area_min_sqft","area_max_sqft","floor_plate_sqft","project_status","director_cabin_count","ceo_cabin_present","cubicle_count","conference_room_capacity","meeting_room_capacity","reception_area","server_room","storage_area","inspection_notice_minutes","price_math"},
            "commercial_rent_listings": {"raw_message_id","tenant_id","listing_index","source_fingerprint","legacy_source_id","asset_type","transaction_type","building_name","locality_raw","locality_resolved","micro_market","landmark_name","broker_id","broker_name","broker_phone","broker_rera_number","summary_title","raw_payload","deal_tags","additional_charges","validation_flags","needs_review","extraction_confidence","commercial_use_type","carpet_area_sqft","built_up_area_sqft","chargeable_area_sqft","area_raw_text","monthly_rent","rent_per_sqft","price_raw_text","price_basis","fitout_status","ceiling_height","floor_level","floor_count","floor_range","car_parking_count","parking_type","has_lift","has_power_backup","power_load_kw","deposit_amount","deposit_applicable","lease_term_type","brokerage_type","building_amenities","terrace_area_sqft","covered_terrace_area_sqft","terrace_area_raw_text","frontage_ft","entrance_count","otla_area_sqft","otla_area_raw_text","heritage_space","permitted_use_types","ideal_for","automatic_shutter_count","room_count","suite_count","banquet_hall_count","restaurant_count","bar_facility","operational_status","rent_inclusions","license_type","short_term_allowed","inspection_notice_minutes","director_cabin_count","ceo_cabin_present","cubicle_count","conference_room_capacity","meeting_room_capacity","training_room_capacity","cafeteria_seat_count","accounts_area","lounge_area","price_math"},
        }[table]
        if table == "commercial_rent_listings":
            allowed.add("mezzanine_area_sqft")
        # ``allowed`` is the selected table's column allow-list. These shared
        # audit/bookkeeping columns are writable on every typed table.
        allowed.add("broker_rera_number")
        allowed.add("extraction_confidence_score")
        allowed.update({"last_seen_at", "expires_at"})
        allowed.add("opportunity_key")
        typed = {k: v for k, v in typed.items() if v is not None and k in allowed}
        try:
            res = self.client.table(table).insert(typed).execute()
            if res.data:
                return res.data[0]["id"]
        except Exception:
            pass
        existing = self.get_listing_by_fingerprint(data["fingerprint"])
        if existing and existing.id:
            upd = {k: v for k, v in data.items() if k != "fingerprint"}
            try:
                self.client.table(table).update({k: v for k, v in typed.items() if k != "source_fingerprint"}).eq("id", existing.id).execute()
            except Exception:
                pass
            return existing.id
        res = self.client.table(table).insert(typed).execute()
        return res.data[0]["id"] if res.data else 0

    def merge_building_amenities(self, building_name: str, new_amenities: list[str], micro_market: str | None = None) -> bool:
        """Merge newly extracted building amenities into the buildings table.

        When a building amenity is mentioned in a new listing for a building
        that already exists in `buildings`, merge/reinforce rather than
        overwrite — increment confidence rather than replacing the array.
        Only updates if the building exists and new_amenities is non-empty.
        """
        if not building_name or not new_amenities or not micro_market:
            return False
        try:
            res = (
                self.client.table("buildings")
                .select("id,amenities,amenities_confidence")
                .ilike("canonical_name", building_name)
                .eq("canonical_micro_market_slug", canonical_micro_market_slug(micro_market))
                .limit(1)
                .execute()
            )
            if not res.data:
                return False
            row = res.data[0]
            existing = row.get("amenities") or []
            if not isinstance(existing, list):
                existing = []
            # Deduplicate: add only amenities not already present
            merged = list(existing)
            added = False
            for a in new_amenities:
                if a and a not in merged:
                    merged.append(a)
                    added = True
            if not added:
                return False  # nothing new to merge
            # Increment confidence (capped at 1.0)
            current_conf = float(row.get("amenities_confidence") or 0.0)
            new_conf = min(current_conf + 0.1, 1.0)
            self.client.table("buildings").update({
                "amenities": merged,
                "amenities_confidence": new_conf,
            }).eq("id", row["id"]).execute()
            return True
        except Exception as exc:
            print(f"[storage] merge_building_amenities({building_name}): {exc}", flush=True)
            return False

    def _building_resolved(self, building_name: str) -> bool:
        """Check if a building_name is already known (canonical or alias).

        Used to decide whether to queue an enrichment job — if the building
        is already known, no resolution work is needed.
        """
        if not building_name:
            return False
        try:
            res = self.client.table("buildings").select("id").ilike("canonical_name", building_name).limit(1).execute()
            if res.data:
                return True
            res = self.client.table("building_aliases").select("canonical").ilike("alias", building_name).limit(1).execute()
            if res.data:
                return True
            res = self.client.table("building_name_aliases").select("building_id").ilike("alias", building_name).limit(1).execute()
            if res.data:
                return True
        except Exception:
            pass
        return False

    def get_listings(self, limit: int = 50, offset: int = 0,
                      intent: str = "", bhk: str = "",
                      building: str = "", micro_market: str = "",
                      broker: str = "", sort_by: str = "last_seen") -> list[Listing]:
        rows = [self._typed_row_to_legacy(row) for row in self._fetch_typed_rows(requirements=False, limit_per_table=max(500, limit + offset))]
        if intent:
            rows = [row for row in rows if str(row.get("intent") or "").upper() == str(intent).upper()]
        if bhk:
            needle = str(bhk).lower().replace(" ", "")
            rows = [row for row in rows if str(row.get("bhk") or "").lower().replace(" ", "") in {needle, needle.replace("bhk", "")}]
        if building:
            rows = [row for row in rows if building.lower() in str(row.get("building_name") or "").lower()]
        if micro_market:
            rows = [row for row in rows if micro_market.lower() in str(row.get("micro_market") or "").lower()]
        if broker:
            needle = broker.lower()
            rows = [row for row in rows if needle in str(row.get("broker_name") or "").lower() or needle in str(row.get("broker_phone") or "").lower()]
        # Keep the listing projection consistent with the parsed/inbox feed:
        # typed tables are independent, so an identical repost can otherwise
        # appear once per schema or once per raw message.
        rows = _merge_observation_rows(rows)
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return [dict_to_dataclass(Listing, row) for row in rows[offset:offset + limit]]

    # ── Listing media / self-chat drafts ──────────────────────────────

    def get_listing_photos(self, listing_id: int, tenant_id: str | None = None) -> list[dict]:
        query = self.client.table("listing_photos").select(
            "id,listing_id,pic_token,media_id,filename,filepath,storage_path,mime_type,"
            "caption,sender_phone,sender_name,source_message_id,created_at"
        ).eq("listing_id", listing_id).order("created_at", desc=True).limit(50)
        tid = tenant_id or self._tenant_id
        if tid:
            query = query.eq("tenant_id", tid)
        return query.execute().data or []

    def save_listing_photo(self, listing_id: int, pic_token: str = "", media_id: str = "",
                           filename: str = "", filepath: str = "", mime_type: str = "image/jpeg",
                           caption: str = "", sender_phone: str = "", sender_name: str = "",
                           storage_path: str = "", source_message_id: str = "",
                           tenant_id: str | None = None) -> int:
        data = {
            "listing_id": listing_id,
            "pic_token": pic_token,
            "media_id": media_id,
            "filename": filename or media_id or "property-image",
            "filepath": filepath,
            "storage_path": storage_path,
            "mime_type": mime_type or "image/jpeg",
            "caption": caption or "",
            "sender_phone": sender_phone or "",
            "sender_name": sender_name or "",
            "source_message_id": source_message_id or "",
        }
        tid = tenant_id or self._tenant_id
        if tid:
            data["tenant_id"] = tid
        result = self.client.table("listing_photos").insert(data).execute()
        return int(result.data[0]["id"]) if result.data else 0

    def get_or_create_listing_media_draft(self, tenant_id: str, broker_phone: str,
                                          session_key: str) -> dict | None:
        query = self.client.table("listing_media_drafts").select("*")
        row = query.eq("tenant_id", tenant_id).eq("broker_phone", broker_phone).eq(
            "session_key", session_key
        ).limit(1).execute().data
        if row:
            return row[0]
        result = self.client.table("listing_media_drafts").insert({
            "tenant_id": tenant_id,
            "broker_phone": broker_phone,
            "session_key": session_key,
            "status": "collecting",
            "details": {},
        }).execute()
        return result.data[0] if result.data else None

    def add_listing_media_draft_item(self, draft_id: int, tenant_id: str, storage_path: str,
                                     media_id: str = "", filename: str = "",
                                     mime_type: str = "image/jpeg", file_length: int | None = None,
                                     caption: str = "", source_message_id: str = "") -> dict | None:
        data = {
            "draft_id": draft_id, "tenant_id": tenant_id, "storage_path": storage_path,
            "media_id": media_id or "", "filename": filename or "",
            "mime_type": mime_type or "image/jpeg", "caption": caption or "",
            "source_message_id": source_message_id or "",
        }
        if file_length is not None:
            data["file_length"] = file_length
        result = self.client.table("listing_media_draft_items").upsert(
            data, on_conflict="draft_id,storage_path"
        ).execute()
        return result.data[0] if result.data else None

    def reset_listing_media_draft(self, draft_id: int, tenant_id: str) -> None:
        self.client.table("listing_media_draft_items").delete().eq(
            "draft_id", draft_id
        ).eq("tenant_id", tenant_id).execute()
        self.client.table("listing_media_drafts").update({
            "status": "collecting", "listing_id": None, "details": {},
        }).eq("id", draft_id).eq("tenant_id", tenant_id).execute()

    def get_listing_media_draft_items(self, draft_id: int, tenant_id: str) -> list[dict]:
        return self.client.table("listing_media_draft_items").select("*").eq(
            "draft_id", draft_id
        ).eq("tenant_id", tenant_id).order("created_at", desc=False).limit(20).execute().data or []

    def update_listing_media_draft(self, draft_id: int, tenant_id: str, **values) -> dict | None:
        allowed = {key: value for key, value in values.items() if key in {"status", "listing_id", "details"}}
        if not allowed:
            return None
        result = self.client.table("listing_media_drafts").update(allowed).eq(
            "id", draft_id
        ).eq("tenant_id", tenant_id).execute()
        return result.data[0] if result.data else None

    def attach_listing_media_draft(self, draft_id: int, tenant_id: str) -> int:
        """Attach a draft's uploaded images to its parsed listing, if ready."""
        draft_rows = self.client.table("listing_media_drafts").select("*").eq(
            "id", draft_id
        ).eq("tenant_id", tenant_id).limit(1).execute().data or []
        if not draft_rows:
            return 0
        draft = draft_rows[0]
        if draft.get("listing_id"):
            listing_id = int(draft["listing_id"])
        else:
            items = self.get_listing_media_draft_items(draft_id, tenant_id)
            raw_ids: set[int] = set()
            for item in items:
                message_id = str(item.get("source_message_id") or "").strip()
                if not message_id:
                    continue
                raw = self.client.table("raw_messages").select("id").eq(
                    "tenant_id", tenant_id
                ).like("message_uid", f"%:{message_id}").limit(1).execute().data or []
                if raw:
                    raw_ids.add(int(raw[0]["id"]))
            if not raw_ids:
                return 0
            listings = self._fetch_typed_rows(
                requirements=False, tenant_id=tenant_id, limit_per_table=1000
            )
            listings = [row for row in listings if row.get("raw_message_id") in raw_ids]
            if not listings:
                return 0
            listing_id = int(listings[0]["id"])
            self.client.table("listing_media_drafts").update({"listing_id": listing_id}).eq(
                "id", draft_id
            ).eq("tenant_id", tenant_id).execute()

        attached = 0
        for item in self.get_listing_media_draft_items(draft_id, tenant_id):
            exists = self.client.table("listing_photos").select("id").eq(
                "listing_id", listing_id
            ).eq("storage_path", item.get("storage_path") or "").eq(
                "tenant_id", tenant_id
            ).limit(1).execute().data or []
            if exists:
                continue
            if self.save_listing_photo(
                listing_id=listing_id, media_id=item.get("media_id", ""),
                filename=item.get("filename", ""), mime_type=item.get("mime_type", "image/jpeg"),
                caption=item.get("caption", ""), storage_path=item.get("storage_path", ""),
                source_message_id=item.get("source_message_id", ""), tenant_id=tenant_id,
            ):
                attached += 1
        return attached

    def get_listing_by_fingerprint(self, fingerprint: str) -> Listing | None:
        for table in _TYPED_LISTING_TABLE_NAMES:
            query = self.client.table(table).select("*").eq("source_fingerprint", fingerprint).limit(1)
            if self._tenant_id:
                query = query.eq("tenant_id", self._tenant_id)
            rows = query.execute().data or []
            if rows:
                return dict_to_dataclass(Listing, self._typed_row_to_legacy({**rows[0], "_typed_table": table}))
        return None

    def get_typed_listing_detail(self, listing_id: int, tenant_id: str | None = None) -> dict | None:
        """Return the canonical typed listing row used by the market map."""
        for table in _TYPED_LISTING_TABLE_NAMES:
            query = self.client.table(table).select(
                _typed_read_columns(table, include_raw_payload=True)
            ).eq("id", int(listing_id)).limit(1)
            tid = tenant_id or self._tenant_id
            if tid:
                query = query.eq("tenant_id", tid)
            rows = query.execute().data or []
            if rows:
                typed_row = {**rows[0], "_typed_table": table}
                payload = typed_row.get("raw_payload")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (TypeError, ValueError):
                        payload = {}
                payload = payload if isinstance(payload, dict) else {}
                row = self._typed_row_to_legacy(typed_row)
                # The extraction pipeline stores the exact per-listing slice
                # here. Keep it separate from the complete raw WhatsApp
                # message so the UI can show the evidence for this listing.
                row["source_slice_text"] = str(
                    payload.get("full_text")
                    or typed_row.get("normalized_message")
                    or payload.get("slice_text")
                    or ""
                )
                raw_id = row.get("raw_message_id")
                row["latest_raw_message_id"] = raw_id
                row["representative_raw_message_id"] = raw_id
                return row
        return None

    # ── Clients ──────────────────────────────────────────────────

    def save_client(self, data: dict) -> dict:
        res = self.client.table("clients").insert(data).execute()
        return res.data[0] if res.data else {}

    def get_clients(self, search: str = "") -> list[dict]:
        query = self.client.table("clients").select("*").order("created_at", desc=True)
        if search:
            query = query.or_(f"name.ilike.%{search}%,phone.ilike.%{search}%")
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        return res.data

    def get_client(self, client_id: int) -> dict | None:
        query = self.client.table("clients").select("*").eq("id", client_id).limit(1)
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        return res.data[0] if res.data else None

    def create_client(self, name: str, phone: str = None, email: str = None, notes: str = "") -> int:
        data = {"name": name}
        if phone:
            data["phone"] = phone
        if email:
            data["email"] = email
        if notes:
            data["notes"] = notes
        if self._tenant_id:
            data["tenant_id"] = self._tenant_id
        res = self.client.table("clients").insert(data).execute()
        return res.data[0]["id"] if res.data else 0

    # ── Brokers ──────────────────────────────────────────────────

    def save_broker(self, data: dict) -> dict:
        if not data.get("tenant_id") and self._tenant_id:
            data["tenant_id"] = self._tenant_id
        if data.get("identity_key"):
            # Try insert first — faster in the common (new broker) case.
            # If a concurrent request already inserted the same identity_key,
            # the UNIQUE constraint fires and we fall back to update.
            try:
                res = self.client.table("brokers").insert(data).execute()
                return res.data[0] if res.data else {}
            except Exception:
                # Race: another request inserted first, or row already exists.
                existing = self.client.table("brokers").select("id").eq("identity_key", data["identity_key"]).limit(1).execute()
                if existing.data:
                    self.client.table("brokers").update(data).eq("id", existing.data[0]["id"]).execute()
                    return existing.data[0]
                # Shouldn't happen, but if it does, return empty.
                return {}
        res = self.client.table("brokers").insert(data).execute()
        return res.data[0] if res.data else {}

    def get_brokers(self, search: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
        query = self.client.table("brokers").select("*").order("observation_count", desc=True).limit(limit).offset(offset)
        if search:
            query = query.or_(f"canonical_name.ilike.%{search}%,primary_phone.ilike.%{search}%")
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        return res.data

    def get_broker(self, broker_id: int) -> dict | None:
        query = self.client.table("brokers").select("*").eq("id", broker_id).limit(1)
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        if not res.data:
            return None
        broker = res.data[0]
        
        # Get aliases
        aliases_res = self.client.table("broker_aliases").select("*").eq("broker_id", broker_id).order("observation_count", desc=True).limit(20).execute()
        broker["aliases"] = aliases_res.data if aliases_res.data else []
        
        # Get phones
        phones_res = self.client.table("broker_phones").select("*").eq("broker_id", broker_id).order("observation_count", desc=True).limit(10).execute()
        broker["phones"] = phones_res.data if phones_res.data else []
        
        # Get market stats
        markets_res = self.client.table("broker_market_stats").select("*").eq("broker_id", broker_id).order("observation_count", desc=True).limit(20).execute()
        broker["markets"] = markets_res.data if markets_res.data else []
        
        # Get building stats
        buildings_res = self.client.table("broker_building_stats").select("*").eq("broker_id", broker_id).order("observation_count", desc=True).limit(20).execute()
        broker["buildings"] = buildings_res.data if buildings_res.data else []
        
        return broker

    def find_broker(self, name: str = "", phone: str = "") -> dict | None:
        q = self.client.table("brokers").select("*")
        if phone:
            q = q.eq("primary_phone", phone)
        if name:
            q = q.ilike("canonical_name", name)
        res = q.limit(1).execute()
        return res.data[0] if res.data else None

    # ── Buildings ────────────────────────────────────────────────

    def save_building(self, data: dict) -> dict:
        if not data.get("tenant_id") and self._tenant_id:
            data["tenant_id"] = self._tenant_id
        if data.get("building_id"):
            existing = self.client.table("buildings").select("id").eq("building_id", data["building_id"]).limit(1).execute()
            if existing.data:
                self.client.table("buildings").update(data).eq("id", existing.data[0]["id"]).execute()
                return existing.data[0]
        res = self.client.table("buildings").insert(data).execute()
        return res.data[0] if res.data else {}

    def get_buildings(self, search: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
        query = self.client.table("buildings").select("*").order("observed_listings", desc=True).limit(limit).offset(offset)
        if search:
            query = query.or_(f"canonical_name.ilike.%{search}%,micro_market.ilike.%{search}%")
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        return res.data

    def get_building(self, building_id: str | None = None, canonical_name: str | None = None,
                     building_db_id: int | str | None = None,
                     micro_market: str | None = None) -> dict | None:
        def base_query():
            query = self.client.table("buildings").select("*")
            if building_db_id is not None:
                return query.eq("id", int(building_db_id))
            if canonical_name:
                return query.eq("canonical_name", canonical_name)
            if building_id:
                return query.eq("building_id", building_id)
            return None

        query = base_query()
        if query is None:
            return None
        if micro_market and building_db_id is None:
            query = query.eq("canonical_micro_market_slug", canonical_micro_market_slug(micro_market))
        if self._tenant_id:
            tenant_result = query.eq("tenant_id", self._tenant_id).limit(1).execute()
            if tenant_result.data:
                return tenant_result.data[0]
            query = base_query()
            if query is None:
                return None
            if micro_market:
                query = query.eq("canonical_micro_market_slug", canonical_micro_market_slug(micro_market))
            global_result = query.is_("tenant_id", "null").limit(1).execute()
            return global_result.data[0] if global_result.data else None
        res = query.limit(1).execute()
        return res.data[0] if res.data else None

    def update_building_from_enrichment(
        self, building_db_id: int | str, fields: dict, provider: str, confidence: float
    ) -> dict | None:
        """Apply approved building enrichment fields, including geocoding metadata."""
        allowed = {
            "address", "developer", "pincode", "micro_market", "latitude", "longitude", "google_place_id",
            "rera_number", "amenities", "completion_status", "building_type",
            "nearby_metro", "nearby_landmarks", "nearby_roads",
            "plus_code", "geocode_query", "geocode_source", "geocode_confidence",
            "geocoded_at",
        }
        values = {key: value for key, value in (fields or {}).items() if key in allowed and value is not None}
        if not values:
            return self.get_building(building_db_id=building_db_id)
        if values.get("micro_market"):
            values["canonical_micro_market_slug"] = canonical_micro_market_slug(values["micro_market"])
        values["last_enriched"] = datetime.now(timezone.utc).isoformat()
        values["enrichment_confidence"] = max(float(confidence or 0), float(values.get("geocode_confidence") or 0))
        values["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = self.client.table("buildings").update(values).eq("id", int(building_db_id)).execute()
        return result.data[0] if result.data else self.get_building(building_db_id=building_db_id)

    def backfill_linked_listings_from_building(
        self, building_db_id: int | str, fields: dict, confidence: float
    ) -> int:
        """Propagate verified building locality to explicitly linked typed rows.

        Raw locality remains untouched: it is WhatsApp evidence.  Only the
        resolved/derived fields are filled, and only when the Places result is
        high confidence.  The building_id FK prevents same-name projects in
        different markets from contaminating each other.
        """
        micro_market = str((fields or {}).get("micro_market") or "").strip()
        if not micro_market or float(confidence or 0) < 0.9:
            return 0
        updated = 0
        for table in (
            "residential_sale_listings", "residential_rent_listings",
            "commercial_sale_listings", "commercial_rent_listings",
            "residential_sale_requirements", "residential_rent_requirements",
            "commercial_sale_requirements", "commercial_rent_requirements",
        ):
            result = (
                self.client.table(table)
                .update({"locality_resolved": micro_market, "micro_market": micro_market})
                .eq("building_id", int(building_db_id))
                # `filter` is supported across the deployed supabase-py /
                # postgrest versions; older deployed clients do not expose
                # the convenience `.is_()` method.
                .filter("locality_resolved", "is", "null")
                .execute()
            )
            updated += len(result.data or [])
        return updated

    def get_building_resolution_evidence(self, building_db_id: int | str) -> dict:
        """Collect bounded source context for safe building resolution.

        Enrichment must see the listing slice that produced the building row.
        Keep this packet bounded and redact phones: it is search context, not
        a second copy of the private WhatsApp message.
        """
        rows: list[dict] = []
        for table, select_columns in _BUILDING_EVIDENCE_SELECTS.items():
            result = (
                self.client.table(table)
                .select(select_columns)
                .eq("building_id", int(building_db_id))
                .order("created_at", desc=True)
                .limit(100)
                .execute()
            )
            rows.extend(result.data or [])

        source_localities: dict[str, int] = {}
        broker_ids: set[int] = set()
        source_contexts: list[dict] = []
        for row in rows:
            locality = canonical_locality_alias(row.get("locality_raw") or row.get("micro_market"))
            if locality:
                source_localities[locality] = source_localities.get(locality, 0) + 1
            if row.get("broker_id"):
                broker_ids.add(int(row["broker_id"]))
            payload = row.get("raw_payload") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    payload = {}
            slice_text = str(
                payload.get("slice_text") or payload.get("source_slice_text")
                or payload.get("full_text") or ""
            ).strip() if isinstance(payload, dict) else ""
            slice_text = re.sub(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)", "[contact]", slice_text)
            if slice_text:
                source_contexts.append({
                    "building_name": str(row.get("building_name") or "").strip(),
                    "locality": locality,
                    "summary_title": str(row.get("summary_title") or "").strip(),
                    "source_slice": slice_text[:1200],
                })

        broker_markets: dict[str, int] = {}
        if broker_ids:
            stats = (
                self.client.table("broker_market_stats")
                .select("micro_market,observation_count")
                .in_("broker_id", list(broker_ids))
                .order("observation_count", desc=True)
                .limit(100)
                .execute()
            )
            for row in stats.data or []:
                locality = str(row.get("micro_market") or "").strip()
                if locality:
                    broker_markets[locality] = broker_markets.get(locality, 0) + int(row.get("observation_count") or 0)

        latest = rows[0] if rows else {}
        price = latest.get("monthly_rent") or latest.get("total_asking_price")
        price_bands: dict[str, dict] = {}
        if latest.get("bhk") is not None and latest.get("transaction_type"):
            stats = (
                self.client.table("price_stats")
                .select("micro_market,p5,p25,median,p75,p95")
                .eq("bhk", str(latest["bhk"]))
                .eq("intent", "RENT" if latest["transaction_type"] == "rent" else "SELL")
                .limit(250)
                .execute()
            )
            price_bands = {
                str(row["micro_market"]): row for row in (stats.data or []) if row.get("micro_market")
            }
        return {
            "source_localities": source_localities,
            "broker_markets": broker_markets,
            "source_contexts": source_contexts[:12],
            "price": price,
            "price_bands": price_bands,
        }

    def create_building(self, canonical_name: str, micro_market: str | None = None,
                        tenant_id: str | None = None) -> dict | None:
        """Create or return a canonical building for an observed name.

        Typed extraction can discover a building before any external geocoder
        has data for it.  Keep that discovery deterministic and idempotent;
        enrichment is a separate, queued step.
        """
        name = normalize_building_name(canonical_name)
        if not is_valid_building_candidate(name) or building_name_problem(name, locality=micro_market):
            return None
        target_tenant = tenant_id or self._tenant_id
        locality = str(micro_market or "").strip()
        locality_slug = canonical_micro_market_slug(locality) if locality else ""
        existing_query = self.client.table("buildings").select("*").ilike("canonical_name", name)
        if target_tenant:
            existing_query = existing_query.eq("tenant_id", target_tenant)
        else:
            existing_query = existing_query.is_("tenant_id", "null")
        if locality_slug:
            existing_query = existing_query.eq("canonical_micro_market_slug", locality_slug)
        existing = existing_query.limit(1).execute()
        if existing.data:
            building = existing.data[0]
            if building.get("canonical_name") != name:
                updated = self.client.table("buildings").update({
                    "canonical_name": name,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", building["id"]).execute()
                if updated.data:
                    building = updated.data[0]
            if micro_market and not building.get("micro_market"):
                updated = self.client.table("buildings").update({
                    "micro_market": micro_market,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", building["id"]).execute()
                if updated.data:
                    building = updated.data[0]
            return building

        # The historical IDs are human-readable, but the table has no
        # sequence for building_id.  A stable hash keeps retries idempotent
        # and avoids a max(id)+1 race between workers.
        digest = hashlib.sha1(f"{target_tenant or 'global'}:{name.casefold()}:{locality_slug}".encode("utf-8")).hexdigest()[:12].upper()
        stable_building_id = f"BLD-{digest}"
        existing = self.client.table("buildings").select("*").eq(
            "building_id", stable_building_id
        ).limit(1).execute()
        if existing.data:
            building = existing.data[0]
            if micro_market and not building.get("micro_market"):
                updated = self.client.table("buildings").update({
                    "micro_market": micro_market,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", building["id"]).execute()
                if updated.data:
                    building = updated.data[0]
            return building
        payload = {
            "building_id": stable_building_id,
            "canonical_name": name,
            "micro_market": micro_market,
            "status": "discovered",
        }
        if locality_slug:
            payload["canonical_micro_market_slug"] = locality_slug
        if target_tenant:
            payload["tenant_id"] = target_tenant
        try:
            result = self.client.table("buildings").upsert(
                payload, on_conflict="building_id"
            ).execute()
            return result.data[0] if result.data else None
        except Exception:
            # A concurrent insert is a normal race; return the winner.
            retry = self.client.table("buildings").select("*").eq(
                "building_id", stable_building_id
            ).limit(1).execute()
            if not retry.data:
                retry_query = self.client.table("buildings").select("*").ilike("canonical_name", name)
                if locality_slug:
                    retry_query = retry_query.eq("canonical_micro_market_slug", locality_slug)
                retry = retry_query.limit(1).execute()
            return retry.data[0] if retry.data else None

    def ensure_building_from_observation(self, canonical_name: str,
                                         micro_market: str | None = None,
                                         tenant_id: str | None = None) -> dict | None:
        """Persist a discovered building and queue its first enrichment job."""
        building = self.create_building(canonical_name, micro_market, tenant_id)
        if not building:
            return None
        self.create_building_alias_for_building(
            int(building["id"]), canonical_name,
            building.get("canonical_name") or canonical_name,
            confidence=1.0,
            source="whatsapp",
        )
        # Automatic discovery owns one durable job per building. Terminal jobs
        # are retried explicitly, never multiplied by every later observation.
        if not str(micro_market or "").strip():
            # A building without locality context is not safe to enrich. Keep
            # the observation, but do not send a name-only geocoding job.
            return building
        existing_job = self.client.table("building_enrichment_jobs").select("id").eq(
            "building_id", building["id"]
        ).limit(1).execute()
        if not existing_job.data:
            job = {"building_id": building["id"], "status": "pending", "provider": "unassigned", "priority": 0}
            if tenant_id or self._tenant_id:
                job["tenant_id"] = tenant_id or self._tenant_id
            try:
                self.client.table("building_enrichment_jobs").insert(job).execute()
            except Exception as exc:
                # Multiple extraction threads can discover the same building
                # simultaneously. The unique active-job constraint makes this
                # an idempotent winner/loser race, not an enrichment failure.
                if "23505" not in str(exc) and "409" not in str(exc):
                    raise
        return building

    def create_building_alias_for_building(self, building_db_id: int, alias: str,
                                           canonical: str, confidence: float = 0.0,
                                           source: str = "whatsapp") -> bool:
        payload = {
            "building_id": int(building_db_id),
            "alias": " ".join(str(alias or "").split()).strip(),
            "canonical_name": canonical,
            "confidence": confidence,
            "source": source,
        }
        if not payload["alias"]:
            return False
        building = self.get_building(building_db_id=building_db_id)
        if building and building.get("tenant_id"):
            payload["tenant_id"] = building["tenant_id"]
        existing = self.client.table("building_name_aliases").select(
            "id,building_id"
        ).eq("alias", payload["alias"]).limit(1).execute()
        if existing.data:
            # Never silently move an established alias to another building.
            # Conflicts are resolver evidence, not an upsert opportunity.
            return int(existing.data[0]["building_id"]) == int(building_db_id)
        result = self.client.table("building_name_aliases").insert(payload).execute()
        return bool(result.data)

    def apply_web_building_alias(self, building_db_id: int, observed_name: str,
                                 canonical_name: str, confidence: float,
                                 source: str = "crawl4ai") -> dict:
        """Attach a verified web spelling without silently merging buildings."""
        current = self.get_building(building_db_id=building_db_id) or {}
        target = self.get_building(canonical_name=canonical_name)
        if target and int(target.get("id") or 0) != int(building_db_id):
            # A tenant-local target is not safe to link across tenants. A
            # global shared-market target is safe; otherwise retain evidence
            # for review rather than auto-merging two buildings.
            current_tenant = current.get("tenant_id")
            target_tenant = target.get("tenant_id")
            if target_tenant and target_tenant != current_tenant:
                return {"action": "needs_review", "target_id": target.get("id")}
            linked = self.create_building_alias_for_building(
                int(target["id"]), observed_name, target.get("canonical_name") or canonical_name,
                confidence=confidence, source=source,
            )
            return {"action": "linked_existing", "target_id": target.get("id"), "created": linked}

        created = self.create_building_alias_for_building(
            int(building_db_id), observed_name, canonical_name,
            confidence=confidence, source=source,
        )
        if not target and canonical_name and canonical_name != current.get("canonical_name"):
            self.client.table("buildings").update({"canonical_name": canonical_name}).eq(
                "id", int(building_db_id)
            ).execute()
        return {"action": "updated_current", "target_id": building_db_id, "created": created}

    # ── Building enrichment queue ───────────────────────────────

    def get_pending_building_jobs(self, limit: int = 10) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        result = (self.client.table("building_enrichment_jobs").select("*")
                  .eq("status", "pending")
                  .lte("scheduled_after", now)
                  .order("priority", desc=True).order("id")
                  .limit(limit).execute())
        return result.data or []

    def create_building_enrichment_job(self, building_db_id: int, provider: str,
                                       priority: int = 0) -> bool:
        existing = (self.client.table("building_enrichment_jobs").select("id,status")
                    .eq("building_id", int(building_db_id)).eq("provider", provider)
                    .order("id", desc=True).limit(1).execute())
        if existing.data:
            row = existing.data[0]
            if row.get("status") in {"pending", "running"}:
                return False
            result = self.client.table("building_enrichment_jobs").update({
                "priority": priority,
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "scheduled_after": datetime.now(timezone.utc).isoformat(),
                "started_at": None,
                "completed_at": None,
            }).eq("id", int(row["id"])).execute()
            return bool(result.data)
        result = self.client.table("building_enrichment_jobs").insert({
            "building_id": int(building_db_id),
            "provider": provider,
            "priority": priority,
            "status": "pending",
        }).execute()
        return bool(result.data)

    def claim_building_job(self, job_id: int, provider: str | None = None) -> bool:
        current = self.client.table("building_enrichment_jobs").select(
            "attempts,provider"
        ).eq("id", int(job_id)).eq("status", "pending").limit(1).execute()
        if not current.data:
            return False
        row = current.data[0]
        updates = {
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "attempts": int(row.get("attempts") or 0) + 1,
        }
        if provider:
            updates["provider"] = provider
        self.client.table("building_enrichment_jobs").update(updates).eq(
            "id", int(job_id)
        ).eq("status", "pending").execute()
        check = self.client.table("building_enrichment_jobs").select("status").eq(
            "id", int(job_id)
        ).limit(1).execute()
        return bool(check.data and check.data[0].get("status") == "running")

    def complete_building_job(self, job_id: int, success: bool, error: str = "") -> bool:
        updates = {
            "status": "completed" if success else "failed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "last_error": None if success else error,
        }
        result = self.client.table("building_enrichment_jobs").update(updates).eq(
            "id", int(job_id)
        ).execute()
        return bool(result.data)

    def retry_building_job(self, job_id: int, error: str,
                           max_attempts: int | None = None) -> str:
        """Schedule bounded exponential retry or terminally fail one job."""
        current = self.client.table("building_enrichment_jobs").select(
            "attempts,max_attempts"
        ).eq("id", int(job_id)).limit(1).execute()
        if not current.data:
            return "failed"
        row = current.data[0]
        attempts = int(row.get("attempts") or 0)
        configured = int(row.get("max_attempts") or 3)
        limit = min(configured, int(max_attempts)) if max_attempts is not None else configured
        now = datetime.now(timezone.utc)
        if attempts < max(1, limit):
            delay_seconds = min(60 * (2 ** max(0, attempts - 1)), 3600)
            updates = {
                "status": "pending",
                "last_error": str(error or "")[:1000],
                "scheduled_after": (now + timedelta(seconds=delay_seconds)).isoformat(),
                "started_at": None,
                "completed_at": None,
            }
            status = "pending"
        else:
            updates = {
                "status": "failed",
                "last_error": str(error or "")[:1000],
                "completed_at": now.isoformat(),
            }
            status = "failed"
        self.client.table("building_enrichment_jobs").update(updates).eq(
            "id", int(job_id)
        ).execute()
        return status

    def defer_building_job(self, job_id: int, scheduled_after: str, reason: str = "") -> bool:
        """Leave a claimed job pending until a bounded external budget resets."""
        result = self.client.table("building_enrichment_jobs").update({
            "status": "pending",
            "last_error": str(reason or "")[:1000] or None,
            "scheduled_after": scheduled_after,
            "started_at": None,
            "completed_at": None,
        }).eq("id", int(job_id)).execute()
        return bool(result.data)

    def release_budget_deferred_building_jobs(self, limit: int = 100) -> int:
        """Make a bounded batch of budget-deferred jobs runnable now.

        The worker originally deferred these jobs until the next UTC day. If
        an operator raises the provider limit during the same day, that old
        schedule would leave the jobs invisible until tomorrow. Only rows
        carrying the exact budget-defer reason are released; ordinary retries
        and operator-scheduled jobs are untouched.
        """
        bounded = max(1, min(int(limit), 1000))
        rows = (self.client.table("building_enrichment_jobs").select("id")
                .eq("status", "pending")
                .eq("last_error", "Crawl4AI daily budget reached")
                .limit(bounded).execute().data or [])
        ids = [int(row["id"]) for row in rows if row.get("id") is not None]
        if not ids:
            return 0
        result = (self.client.table("building_enrichment_jobs").update({
            "scheduled_after": datetime.now(timezone.utc).isoformat(),
        }).in_("id", ids).eq("status", "pending").execute())
        return len(result.data or [])

    def recover_stale_building_jobs(self, max_attempts: int | None = None,
                                    stale_minutes: int = 10) -> int:
        """Recover claims left running after a worker crash."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max(1, stale_minutes))).isoformat()
        rows = self.client.table("building_enrichment_jobs").select(
            "id"
        ).eq("status", "running").lt("started_at", cutoff).limit(1000).execute().data or []
        for row in rows:
            self.retry_building_job(
                int(row["id"]), "Recovered stale running claim", max_attempts=max_attempts
            )
        return len(rows)

    def add_enrichment_history(self, building_db_id: int, provider: str, action: str,
                               fields_updated: list[str] | None = None,
                               confidence: float = 0.0, details: dict | None = None,
                               job_id: int | None = None) -> bool:
        payload = {
            "building_id": int(building_db_id),
            "provider": provider,
            "action": action,
            "fields_updated": fields_updated or [],
            "confidence": confidence or 0.0,
            "details": details or {},
        }
        if job_id is not None:
            payload["job_id"] = int(job_id)
        result = self.client.table("building_enrichment_history").insert(payload).execute()
        return bool(result.data)

    def count_recent_enrichment_actions(self, provider: str, action: str) -> int:
        """Return today's durable count for bounded external enrichment calls."""
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        result = (self.client.table("building_enrichment_history")
                  .select("id", count="exact")
                  .eq("provider", provider)
                  .eq("action", action)
                  .gte("created_at", today)
                  .limit(1)
                  .execute())
        return int(getattr(result, "count", None) or 0)

    def record_enrichment_sources(self, building_db_id: int, provider: str,
                                  fields: dict, confidence: float,
                                  source_url: str = "", source_record_id: str = "") -> None:
        for field_name, value in (fields or {}).items():
            self.client.table("building_enrichment_sources").upsert({
                "building_id": int(building_db_id),
                "provider": provider,
                "field_name": field_name,
                "field_value": str(value),
                "confidence": confidence or 0.0,
                "source_url": source_url or None,
                "source_record_id": source_record_id or None,
                "enriched_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="building_id,provider,field_name").execute()

    def record_structured_enrichment_evidence(
        self, building_db_id: int, claims: dict, source_url: str = "", job_id: int | None = None
    ) -> None:
        """Persist explicit Crawl4AI claims without applying them to buildings."""
        for field_name, claim in (claims or {}).items():
            if not isinstance(claim, dict) or claim.get("value") in (None, "", []):
                continue
            value = claim.get("value")
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            self.client.table("building_enrichment_sources").upsert({
                "building_id": int(building_db_id),
                "provider": "crawl4ai_structured",
                "field_name": field_name,
                "field_value": str(value),
                "confidence": float(claim.get("confidence") or 0.0),
                "source_url": claim.get("source_url") or source_url or None,
                "source_record_id": str(job_id) if job_id is not None else None,
                "enriched_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="building_id,provider,field_name").execute()

    def mark_building_enriched(self, building_db_id: int, provider: str,
                               confidence: float) -> bool:
        result = self.client.table("buildings").update({
            "status": "enriched",
            "last_enriched": datetime.now(timezone.utc).isoformat(),
            "enrichment_confidence": confidence or 0.0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", int(building_db_id)).execute()
        return bool(result.data)

    def create_enrichment_review_suggestion(self, building: dict, result: Any,
                                            job_id: int) -> int:
        fields_summary = "\n".join(
            f"- {key}: {value}" for key, value in (result.fields or {}).items()
        )
        payload = {
            "agent": "building",
            "suggestion_type": "enrichment_review",
            "title": f"Review enrichment for {building.get('canonical_name', '')}",
            "description": (
                f"Provider: {result.provider}\nConfidence: {result.confidence:.0%}\n\n"
                f"Suggested fields:\n{fields_summary}\n\nSource: {result.source_url}"
            ),
            "source_data": {"building_db_id": building.get("id"), "job_id": job_id},
            "proposal_data": {"building_db_id": building.get("id"), "fields": result.fields or {}},
            "confidence": result.confidence or 0.0,
            "status": "pending",
        }
        inserted = self.client.table("ai_suggestions").insert(payload).execute()
        return int(inserted.data[0]["id"]) if inserted.data else 0

    # ── Resolver Decisions ───────────────────────────────────────

    def _legacy_parsed_exists(self, parsed_id: int) -> bool:
        """Whether an id belongs to the pre-typed parsed archive.

        The typed pipeline exposes stable synthetic observation ids, while
        resolver_decisions and enrichment_jobs still reference the historical
        parsed_output_legacy table.  Do not send typed-only ids into those
        legacy-FK tables: it creates noisy 409/500 errors and can obscure a
        successful extraction.
        """
        if not parsed_id:
            return False
        try:
            rows = (self.client.table("parsed_output_legacy")
                    .select("id").eq("id", int(parsed_id)).limit(1)
                    .execute().data or [])
            return bool(rows)
        except Exception:
            return False

    def save_resolver_decision(self, dec: ResolverDecision) -> int:
        if not self._legacy_parsed_exists(int(dec.parsed_id or 0)):
            return 0
        data = {k: v for k, v in dec.__dict__.items() if v is not None}
        data.pop("id", None)
        if not data.get("created_at"):
            data.pop("created_at", None)
        res = self.client.table("resolver_decisions").insert(data).execute()
        return res.data[0]["id"] if res.data else 0

    def get_resolver_by_parsed(self, parsed_id: int) -> Optional[ResolverDecision]:
        res = self.client.table("resolver_decisions").select("*").eq("parsed_id", parsed_id).limit(1).execute()
        if res.data:
            return dict_to_dataclass(ResolverDecision, res.data[0])
        return None

    def get_resolver_decisions(self, limit: int = 50, offset: int = 0,
                                method: str = "") -> list[dict]:
        query = self.client.table("resolver_decisions").select("*").order("id", desc=True).limit(limit).offset(offset)
        if method:
            query = query.eq("method", method)
        res = query.execute()
        return res.data

    def get_failed(self, limit: int = 50, offset: int = 0) -> list[dict]:
        res = self.client.table("resolver_decisions").select("*").eq("success", False).order("id", desc=True).limit(limit).offset(offset).execute()
        return res.data

    # ── Evaluations ──────────────────────────────────────────────

    def save_evaluation(self, ev: Evaluation) -> int:
        data = {k: v for k, v in ev.__dict__.items() if v is not None}
        data.pop("id", None)
        res = self.client.table("evaluations").insert(data).execute()
        return res.data[0]["id"] if res.data else 0

    def get_evaluation_by_raw(self, raw_id: int) -> Optional[Evaluation]:
        res = self.client.table("evaluations").select("*").eq("raw_message_id", raw_id).limit(1).execute()
        if res.data:
            return dict_to_dataclass(Evaluation, res.data[0])
        return None

    def get_evaluations(self, limit: int = 50, offset: int = 0) -> list[dict]:
        res = self.client.table("evaluations").select("*").order("id", desc=True).limit(limit).offset(offset).execute()
        return res.data

    # ── Sync Jobs ────────────────────────────────────────────────

    def create_sync_job(self, job: SyncJob) -> int:
        data = {k: v for k, v in job.__dict__.items() if v is not None}
        data.pop("id", None)
        res = self.client.table("sync_jobs").insert(data).execute()
        return res.data[0]["id"] if res.data else 0

    def update_sync_job(self, job_id: int, **updates):
        self.client.table("sync_jobs").update(updates).eq("id", job_id).execute()

    def get_sync_job(self, job_id: int) -> Optional[SyncJob]:
        res = self.client.table("sync_jobs").select("*").eq("id", job_id).limit(1).execute()
        if res.data:
            return dict_to_dataclass(SyncJob, res.data[0])
        return None

    def upsert_sync_job(self, source: str, instance: str = "",
                         group_id: str = "", group_name: str = "",
                         participants: int = 0,
                         status: str = "pending") -> int:
        existing = self.client.table("sync_jobs").select("id,status").eq("source", source).eq("group_id", group_id).limit(1).execute()
        if existing.data:
            existing_status = existing.data[0].get("status", "pending")
            terminal = {"complete", "failed", "cancelled"}
            new_status = status if existing_status not in terminal else existing_status
            self.client.table("sync_jobs").update({
                "status": new_status, "instance": instance,
                "group_name": group_name, "participants": participants,
            }).eq("id", existing.data[0]["id"]).execute()
            return existing.data[0]["id"]
        data = {"source": source, "instance": instance, "group_id": group_id,
                "group_name": group_name, "participants": participants, "status": status}
        res = self.client.table("sync_jobs").insert(data).execute()
        return res.data[0]["id"] if res.data else 0

    def prune_sync_jobs(self, source: str, instance: str,
                         keep_jids: set) -> int:
        all_jobs = self.client.table("sync_jobs").select("id,group_id").eq("source", source).eq("instance", instance).execute()
        removed = 0
        for job in all_jobs.data:
            if job["group_id"] not in keep_jids:
                self.client.table("sync_jobs").delete().eq("id", job["id"]).execute()
                removed += 1
        return removed

    def get_sync_jobs(self, limit: int = 200, offset: int = 0,
                       source: str = "", status: str = "") -> list[SyncJob]:
        query = self.client.table("sync_jobs").select("*").order("id", desc=True).limit(limit).offset(offset)
        if source:
            query = query.eq("source", source)
        if status:
            query = query.eq("status", status)
        res = query.execute()
        return [dict_to_dataclass(SyncJob, d) for d in res.data]

    def resolve_lid_from_group_members(self, member_jid: str, group_id: str = "") -> dict | None:
        """Look up a LID JID in group_members to find phone + display_name.

        Searches for the best match:
        1. Exact (group_id + member_jid) if group_id provided
        2. Any row with matching member_jid (most recent first)
        """
        if not member_jid:
            return None
        q = self.client.table("group_members").select(
            "member_phone,display_name,group_id"
        ).eq("member_jid", member_jid).order("last_seen_at", desc=True).limit(1)
        if group_id:
            q = q.eq("group_id", group_id)
        res = q.execute()
        if res.data:
            return res.data[0]
        if group_id:
            res = self.client.table("group_members").select(
                "member_phone,display_name,group_id"
            ).eq("member_jid", member_jid).order("last_seen_at", desc=True).limit(1).execute()
            if res.data:
                return res.data[0]
        return None

    def upsert_group_members(self, tenant_id: str, group_id: str, participants: list[dict]) -> int:
        if not tenant_id or not group_id or not participants:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        rows_with_display_name = []
        rows_without_display_name = []
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            member_jid = str(
                participant.get("member_jid")
                or participant.get("id")
                or participant.get("phone_jid")
                or participant.get("lid")
                or ""
            ).strip()
            if not member_jid:
                continue
            row = {
                "tenant_id": tenant_id,
                "group_id": group_id,
                "member_jid": member_jid,
                "member_phone": self._normalize_phone(str(participant.get("member_phone") or "")) or None,
                "is_admin": bool(participant.get("is_admin")),
                "last_seen_at": now,
            }
            # An empty participant display name is common in WhatsMeow group
            # snapshots. Omit it from the upsert so Postgres preserves an
            # existing name; the group_members contact-enrichment trigger
            # fills it for new rows from whatsmeow_contacts.
            display_name = str(participant.get("display_name") or "").strip()
            if display_name:
                row["display_name"] = display_name
            rows.append(row)
            (rows_with_display_name if display_name else rows_without_display_name).append(row)
        if not rows:
            return 0
        # PostgREST requires every object in one bulk upsert to have the same
        # keys. Keep the no-name batch separate so empty WhatsMeow names do not
        # erase an existing contact name.
        for batch in (rows_with_display_name, rows_without_display_name):
            if batch:
                self.client.table("group_members").upsert(
                    batch,
                    on_conflict="tenant_id,group_id,member_jid",
                ).execute()
        return len(rows)

    def group_ids_with_member_phone(self, tenant_id: str, phone: str) -> set[str]:
        """Return groups containing a normalized phone in group_members."""
        normalized = self._normalize_phone(phone)
        if not tenant_id or not normalized:
            return set()
        try:
            rows = self.client.table("group_members").select("group_id") \
                .eq("tenant_id", tenant_id).eq("member_phone", normalized).execute().data or []
            return {str(row.get("group_id") or "") for row in rows if row.get("group_id")}
        except Exception:
            return set()

    def prune_group_members(self, tenant_id: str, group_id: str, keep_member_jids: set) -> int:
        if not tenant_id or not group_id:
            return 0
        existing = self.client.table("group_members").select("id,member_jid").eq("tenant_id", tenant_id).eq("group_id", group_id).execute()
        removed = 0
        keep_member_jids = {str(jid).strip() for jid in (keep_member_jids or set()) if str(jid).strip()}
        for row in existing.data or []:
            member_jid = str(row.get("member_jid") or "").strip()
            if member_jid and member_jid not in keep_member_jids:
                self.client.table("group_members").delete().eq("id", row["id"]).execute()
                removed += 1
        return removed

    # ── Durable WhatsApp conversation directory ───────────────────

    def upsert_whatsapp_conversations(
        self,
        tenant_id: str,
        broker_id: str,
        instance: str,
        conversations: list[dict],
    ) -> int:
        """Persist WhatsApp's directory independently of raw/parsed messages."""
        if not tenant_id or not broker_id or not conversations:
            return 0
        existing_result = self.client.table("whatsapp_conversations").select(
            "conversation_jid,display_name,message_count,last_message_at"
        ).eq("tenant_id", tenant_id).eq("broker_id", broker_id).limit(1000).execute()
        existing_by_jid = {
            str(row.get("conversation_jid") or ""): row
            for row in (existing_result.data or [])
        }
        rows = []
        for conversation in conversations:
            jid = str(conversation.get("jid") or conversation.get("id") or "").strip()
            kind = str(conversation.get("type") or conversation.get("conversation_type") or "").strip()
            if not jid or kind not in {"group", "broadcast", "direct"}:
                continue
            row = {
                "tenant_id": tenant_id,
                "broker_id": broker_id,
                "instance": instance or "",
                "conversation_jid": jid,
                "conversation_type": kind,
                "display_name": str(conversation.get("name") or conversation.get("display_name") or jid).strip(),
                "unread_count": max(0, int(conversation.get("unread_count") or 0)),
                "message_count": max(0, int(conversation.get("message_count") or 0)),
                "last_message_at": conversation.get("last_message_at") or None,
                "last_seen_at": conversation.get("last_seen_at") or datetime.now(timezone.utc).isoformat(),
                "source": str(conversation.get("source") or "live"),
                "metadata": conversation.get("metadata") or {},
            }
            existing = existing_by_jid.get(jid)
            if existing:
                # Directory/history refreshes can know a conversation exists
                # without containing its complete message history. Never let
                # that shallow refresh erase activity we have already stored.
                row["message_count"] = max(
                    int(existing.get("message_count") or 0), row["message_count"],
                )
                row["last_message_at"] = (
                    max(str(existing.get("last_message_at") or ""), str(row["last_message_at"] or "")) or None
                )
                existing_name = str(existing.get("display_name") or "").strip()
                if existing_name and existing_name != jid and row["display_name"] == jid:
                    row["display_name"] = existing_name
            rows.append(row)
        if not rows:
            return 0
        # The composite key makes every refresh idempotent. Batch calls keep a
        # full WhatsApp directory from turning into hundreds of REST requests.
        for start in range(0, len(rows), 200):
            self.client.table("whatsapp_conversations").upsert(
                rows[start:start + 200],
                on_conflict="tenant_id,broker_id,conversation_jid",
            ).execute()
        return len(rows)

    def touch_whatsapp_conversation(
        self,
        tenant_id: str,
        broker_id: str,
        instance: str,
        conversation_jid: str,
        conversation_type: str,
        last_message_at: str | None = None,
    ) -> None:
        """Advance activity without erasing a name learned from WhatsApp."""
        if not tenant_id or not broker_id or not conversation_jid:
            return
        updates = {
            "instance": instance or "",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "source": "live",
        }
        if last_message_at:
            updates["last_message_at"] = last_message_at
        existing = self.client.table("whatsapp_conversations").select("id,message_count").eq(
            "tenant_id", tenant_id
        ).eq("broker_id", broker_id).eq("conversation_jid", conversation_jid).limit(1).execute()
        if existing.data:
            current = existing.data[0]
            updates["message_count"] = int(current.get("message_count") or 0) + 1
            self.client.table("whatsapp_conversations").update(updates).eq("id", current["id"]).execute()
            return
        self.upsert_whatsapp_conversations(tenant_id, broker_id, instance, [{
            "jid": conversation_jid,
            "type": conversation_type,
            "name": conversation_jid,
            "message_count": 1,
            "last_message_at": last_message_at,
            "source": "live",
        }])

    def prune_whatsapp_conversations(
        self,
        tenant_id: str,
        broker_id: str,
        keep_jids: set[str],
        conversation_types: set[str] | None = None,
    ) -> int:
        """Remove directory entries absent from a complete WhatsApp refresh.

        A joined-group directory is authoritative. Retaining rows after a
        complete refresh made groups that the user had left remain visible in
        the mirror indefinitely.
        """
        if not tenant_id or not broker_id:
            return 0
        query = self.client.table("whatsapp_conversations").select("id,conversation_jid").eq(
            "tenant_id", tenant_id
        ).eq("broker_id", broker_id)
        if conversation_types:
            query = query.in_("conversation_type", sorted(conversation_types))
        removed = 0
        keep = {str(jid).strip() for jid in keep_jids if str(jid).strip()}
        for row in query.execute().data or []:
            if str(row.get("conversation_jid") or "") not in keep:
                self.client.table("whatsapp_conversations").delete().eq("id", row["id"]).execute()
                removed += 1
        return removed

    def get_whatsapp_conversations(
        self,
        tenant_id: str,
        types: list[str] | None = None,
        limit: int = 500,
        search: str = "",
        relevant_only: bool = False,
    ) -> list[dict]:
        query = self.client.table("whatsapp_conversations").select(
            "id,tenant_id,broker_id,instance,conversation_jid,conversation_type,"
            "display_name,unread_count,message_count,last_message_at,last_seen_at,"
            "source,metadata,created_at,updated_at"
        ).eq("tenant_id", tenant_id)
        if types:
            query = query.in_("conversation_type", types)
        # The internal REST query builder only supports column + direction;
        # PostgREST already puts null timestamps after dated conversations for
        # this descending directory order.
        result = query.order("last_message_at", desc=True).limit(limit).execute()
        directory = result.data or []

        def filter_directory(rows: list[dict]) -> list[dict]:
            filtered_rows = rows
            # The Groups mirror searches both directory names and captured
            # post text. Searching only the group title made queries such as
            # "jvc 2 br" appear to do nothing because those words usually
            # occur in a message, not in the group name.
            search_terms = [term.lower() for term in re.findall(r"[\w]+", search or "") if len(term) > 1]
            if search_terms and filtered_rows:
                message_query = (
                    self.client.table("raw_messages")
                    .select("group_name")
                    .eq("tenant_id", tenant_id)
                    .limit(1000)
                )
                for term in search_terms:
                    message_query = message_query.ilike("message", f"%{term}%")
                evidence = message_query.execute().data or []
                matching_names = {str(row.get("group_name") or "").strip().lower() for row in evidence}
                phrase = " ".join(search_terms)
                filtered_rows = []
                for row in rows:
                    name = str(row.get("display_name") or row.get("conversation_name") or "").strip()
                    jid = str(row.get("conversation_jid") or "").strip()
                    name_match = phrase in name.lower() or all(term in name.lower() for term in search_terms)
                    content_match = name.lower() in matching_names or jid.lower() in matching_names
                    if name_match or content_match:
                        row["search_match"] = True
                        filtered_rows.append(row)

            if relevant_only and filtered_rows:
                # Keep the mirror focused on property-market groups without
                # hiding the complete directory from onboarding controls. A
                # group qualifies by title or by a recent captured post;
                # generic names such as "DIABETIC SOLUTION" therefore no
                # longer appear in the market-facing mirror unless they
                # actually carry property posts.
                property_signal = re.compile(
                    r"\b(?:real\s*estate|realty|property|properties|broker|rent|rental|sale|sell|lease|flat|apartment|house|villa|townhouse|plot|land|bhk|br|rk|showroom|office|shop|warehouse|commercial|carpet|sq\.?\s*ft|aed|dhs|ejari|brokerage|inventory|requirement)\b",
                    re.IGNORECASE,
                )
                title_matches = {
                    str(row.get("display_name") or row.get("conversation_name") or "").strip().lower()
                    for row in filtered_rows
                    if property_signal.search(str(row.get("display_name") or row.get("conversation_name") or ""))
                }
                recent = (
                    self.client.table("raw_messages")
                    .select("group_name,message")
                    .eq("tenant_id", tenant_id)
                    .order("created_at", desc=True)
                    .limit(1000)
                    .execute()
                    .data
                    or []
                )
                content_matches = {
                    str(row.get("group_name") or "").strip().lower()
                    for row in recent
                    if property_signal.search(str(row.get("message") or ""))
                }
                filtered_rows = [
                    row for row in filtered_rows
                    if str(row.get("display_name") or row.get("conversation_name") or "").strip().lower() in title_matches
                    or str(row.get("conversation_jid") or "").strip().lower() in content_matches
                    or str(row.get("display_name") or row.get("conversation_name") or "").strip().lower() in content_matches
                ]
            return filtered_rows

        # The durable directory is authoritative for membership, but it is not
        # authoritative for activity: GROUPS_REFRESHED events intentionally
        # carry no message timestamp, and old broker connection ids can leave
        # duplicate directory rows behind. Merge by WhatsApp JID and overlay
        # the newest captured raw message so the mirror is live without asking
        # the LLM or scanning parsed tables.
        if directory:
            merged: dict[str, dict] = {}
            for row in directory:
                jid = str(row.get("conversation_jid") or row.get("jid") or "").strip()
                if not jid:
                    continue
                current = merged.get(jid)
                if current is None:
                    merged[jid] = dict(row)
                    continue
                current["message_count"] = max(
                    int(current.get("message_count") or 0),
                    int(row.get("message_count") or 0),
                )
                current["last_message_at"] = max(
                    str(current.get("last_message_at") or ""),
                    str(row.get("last_message_at") or ""),
                ) or None
                current["last_seen_at"] = max(
                    str(current.get("last_seen_at") or ""),
                    str(row.get("last_seen_at") or ""),
                ) or None
                if str(current.get("display_name") or "").strip() in {"", jid}:
                    current["display_name"] = row.get("display_name") or current.get("display_name")
                metadata = current.get("metadata") or {}
                incoming_metadata = row.get("metadata") or {}
                if isinstance(metadata, dict) and isinstance(incoming_metadata, dict):
                    current["metadata"] = {**metadata, **incoming_metadata}

            # The directory is durable membership metadata, not the source of
            # truth for activity. The WhatsMeow ingestor writes raw_messages
            # directly and older directory rows can therefore have a stale
            # last_message_at (the group detail view may already show newer
            # messages). Overlay the newest captured raw activity before
            # sorting, while retaining the directory's participant metadata.
            try:
                raw_activity = self.client.table("raw_messages").select(
                    "group_name,timestamp,created_at"
                ).eq("tenant_id", tenant_id).order("timestamp", desc=True).limit(20000).execute().data or []
                latest_by_key: dict[str, str] = {}
                counts_by_key: dict[str, int] = {}
                for raw in raw_activity:
                    key = str(raw.get("group_name") or "").strip().lower()
                    if not key:
                        continue
                    timestamp = str(raw.get("timestamp") or raw.get("created_at") or "").strip()
                    if timestamp and timestamp > latest_by_key.get(key, ""):
                        latest_by_key[key] = timestamp
                    counts_by_key[key] = counts_by_key.get(key, 0) + 1
                for row in merged.values():
                    jid = str(row.get("conversation_jid") or "").strip().lower()
                    name = str(row.get("display_name") or row.get("conversation_name") or "").strip().lower()
                    candidates = [latest_by_key.get(jid, ""), latest_by_key.get(name, "")]
                    latest = max(candidates or [""])
                    if latest:
                        row["last_message_at"] = max(
                            str(row.get("last_message_at") or ""), latest
                        )
                    row["message_count"] = max(
                        int(row.get("message_count") or 0),
                        counts_by_key.get(jid, 0),
                        counts_by_key.get(name, 0),
                    )
                merged_rows = filter_directory(list(merged.values()))
                return sorted(
                    merged_rows,
                    key=lambda row: str(row.get("last_message_at") or ""),
                    reverse=True,
                )[:limit]
            except Exception:
                _logger.debug("raw activity overlay failed for WhatsApp directory", exc_info=True)
                return filter_directory(list(merged.values()))[:limit]

        # The Go WhatsMeow ingestor writes raw_messages directly for low-latency
        # delivery. Bootstrap the directory from that evidence only when the
        # durable directory is genuinely empty.
        try:
            raw_chats = self.get_chats(limit=min(limit, 500), offset=0, tenant_id=tenant_id)
            requested = set(types or [])
            by_id: dict[str, dict] = {}
            for chat in raw_chats:
                if requested and chat.get("chat_type") not in requested:
                    continue
                chat_id = str(chat.get("chat_id") or chat.get("conversation_key") or "")
                if not chat_id:
                    continue
                latest = chat.get("latest_message_at") or ""
                by_id[chat_id] = {
                    "conversation_jid": chat_id,
                    "conversation_name": chat.get("chat_name") or chat_id,
                    "display_name": chat.get("chat_name") or chat_id,
                    "conversation_type": chat.get("chat_type"),
                    "message_count": chat.get("message_count") or 0,
                    "last_message_at": latest,
                    "source": "raw_messages",
                }
            directory = list(by_id.values())
            directory.sort(key=lambda row: str(row.get("last_message_at") or ""), reverse=True)
            return filter_directory(directory)[:limit]
        except Exception:
            return directory

    def get_group_markets(self) -> dict[str, list[str]]:
        """Derived tags: aggregate distinct micro_markets per WhatsApp group
        from parsed_output joined to raw_messages by group_name.
        Returns {group_name: [market, ...]}."""
        try:
            rows = self._fetch_typed_rows(limit_per_table=1000)
            raw_ids = {row.get("raw_message_id") for row in rows if row.get("raw_message_id")}
            raw_rows = self.client.table("raw_messages").select("id,group_name").in_("id", list(raw_ids)).execute().data or []
            groups = {row.get("id"): row.get("group_name") for row in raw_rows}
            out: dict[str, set[str]] = {}
            for row in rows:
                gn = groups.get(row.get("raw_message_id"))
                mk = row.get("micro_market")
                if gn and mk:
                    out.setdefault(gn, set()).add(mk)
            return {k: sorted(v) for k, v in out.items()}
        except Exception:
            return {}

    # ── Sync Checkpoints ─────────────────────────────────────────

    def get_checkpoints(self, instance_name: str) -> list[SyncCheckpoint]:
        res = self.client.table("sync_checkpoints").select("*").eq("instance_name", instance_name).execute()
        return [dict_to_dataclass(SyncCheckpoint, d) for d in res.data]

    def save_checkpoint(self, cp: SyncCheckpoint):
        data = {k: v for k, v in cp.__dict__.items() if v is not None}
        data.pop("id", None)
        existing = self.client.table("sync_checkpoints").select("id").eq("instance_name", cp.instance_name).eq("group_jid", cp.group_jid).limit(1).execute()
        if existing.data:
            self.client.table("sync_checkpoints").update(data).eq("id", existing.data[0]["id"]).execute()
        else:
            self.client.table("sync_checkpoints").insert(data).execute()

    def get_checkpoint(self, instance_name: str,
                        group_jid: str) -> Optional[SyncCheckpoint]:
        res = self.client.table("sync_checkpoints").select("*").eq("instance_name", instance_name).eq("group_jid", group_jid).limit(1).execute()
        if res.data:
            return dict_to_dataclass(SyncCheckpoint, res.data[0])
        return None

    # ── Stats ────────────────────────────────────────────────────

    def get_stats(self, tenant_id: str | None = None) -> dict:
        # ContextVars do not reliably propagate through asyncio.to_thread;
        # callers serving a tenant must pass the scope explicitly.
        tenant_id = tenant_id or self.tenant_id
        cache_key = tenant_id or "__all__"
        cached = self._stats_cache.get(cache_key)
        now = time.monotonic()
        # Connections, WhatsWow and the sidebar ask for these same counts in
        # quick succession. Keep dashboard counts responsive without turning a
        # page load into repeated full-table count queries.
        # These are dashboard counters, not transactional state. A short
        # cache prevents every open admin tab from launching six exact-count
        # scans against the same large tables while keeping the UI current.
        if cached and now - cached[0] < 60:
            return {**cached[1], "stats_available": True}

        def count(table: str) -> int:
            query = self.client.table(table).select("id", count="exact")
            if tenant_id:
                query = query.eq("tenant_id", tenant_id)
            response = query.execute()
            return int(response.count or 0)

        keys = (
            "total_messages", "total_parsed", "total_listings",
            "total_requirements", "total_brokers", "total_buildings",
        )
        try:
            stats = {
                "total_messages": count("raw_messages"),
                "total_parsed": sum(count(table) for table in _ALL_TYPED_TABLES),
                "total_listings": sum(count(table) for table in _TYPED_LISTING_TABLE_NAMES),
                "total_requirements": sum(count(table) for table in _TYPED_REQUIREMENT_TABLE_NAMES),
                "total_brokers": count("brokers"),
                "total_buildings": count("buildings"),
            }
        except Exception as exc:
            # Stats are informational. Do not let a transient count failure
            # take down Connections or conceal the WhatsApp session state.
            import logging
            logging.warning("Supabase stats query failed: %s", exc)
            stale = dict(cached[1]) if cached else {key: 0 for key in keys}
            # A zero-filled fallback is not evidence that the tenant has no
            # data. Let clients distinguish an unavailable count query from a
            # genuine live zero while retaining last-known values.
            return {**stale, "stats_available": False}

        self._stats_cache[cache_key] = (now, stats)
        return {**stats, "stats_available": True}

    # ── Inbox Evidence Detail ───────────────────────────────────

    def get_inbox_evidence_detail(self, raw_message_id: int) -> dict:
        resolved_raw_id = int(raw_message_id or 0)
        typed_rows = self._fetch_typed_rows(raw_message_id=resolved_raw_id, limit_per_table=1000)
        if not typed_rows:
            return {}

        parsed_rows = _merge_observation_rows([self._typed_row_to_legacy(row) for row in typed_rows])
        first_parsed = parsed_rows[0] if parsed_rows else None

        # Get raw message
        raw_res = self.client.table("raw_messages").select("*").eq("id", resolved_raw_id).limit(1).execute()
        raw_dict = raw_res.data[0] if raw_res.data else {}

        # Get resolver decision for first parsed
        resolver_dict = {}
        if first_parsed:
            r_res = self.client.table("resolver_decisions").select("*").eq("parsed_id", first_parsed["id"]).order("id", desc=True).limit(1).execute()
            if r_res.data:
                resolver_dict = r_res.data[0]
                if isinstance(resolver_dict.get("candidates"), str):
                    try:
                        resolver_dict["candidates"] = json.loads(resolver_dict["candidates"])
                    except (json.JSONDecodeError, TypeError):
                        resolver_dict["candidates"] = []
        
        # Get evaluation
        eval_dict = {}
        eval_res = self.client.table("evaluations").select("*").eq("raw_message_id", resolved_raw_id).order("id", desc=True).limit(1).execute()
        if eval_res.data:
            eval_dict = eval_res.data[0]
        
        return {
            "raw": raw_dict,
            "parsed": first_parsed or {},
            "listings": parsed_rows,
            "resolver": resolver_dict,
            "evaluation": eval_dict,
        }

    def source_summary(self) -> dict:
        query = self.client.table("raw_messages").select("group_name", count="exact")
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        groups = query.execute()
        return {
            "total_groups": len(set(d.get("group_name") for d in groups.data)) if groups.data else 0,
            "total_messages": groups.count or 0,
        }

    # ── AI Layer ─────────────────────────────────────────────────

    def get_all_parsed_with_embeddings(self) -> list[dict]:
        return [row for row in self._fetch_typed_rows(limit_per_table=250)
                if row.get("embedding") is not None]

    def knn_search(self, query_embedding: bytes, k: int = 10) -> list[dict]:
        return []

    def get_observations_by_broker(self, broker_name: str) -> list[dict]:
        rows = self._fetch_typed_rows(limit_per_table=250)
        return [self._typed_row_to_legacy(row) for row in rows
                if str(row.get("broker_name") or "").casefold() == broker_name.casefold()][:100]

    def get_observations_by_building(self, building_name: str) -> list[dict]:
        rows = self._fetch_typed_rows(limit_per_table=250)
        return [self._typed_row_to_legacy(row) for row in rows
                if str(row.get("building_name") or "").casefold() == building_name.casefold()][:100]

    def get_top_brokers_today(self, today_prefix: str, limit: int = 10) -> list[dict]:
        query = self.client.table("brokers").select("*").order("observation_count", desc=True).limit(limit)
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        return res.data

    # ── Dashboard ────────────────────────────────────────────────

    def dashboard_activity(self, today_prefix: str) -> dict:
        query = self.client.table("raw_messages").select("id", count="exact").gte("created_at", today_prefix)
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        today = query.execute()
        return {
            "messages_today": today.count or 0,
            "message_types": {},
        }

    def log_activity(
        self,
        team_member_id: int,
        action: str,
        target_type: str = "",
        target_id: str = "",
        details: Any | None = None,
        ip_address: str = "",
    ) -> int:
        parsed_details: dict[str, Any]
        if isinstance(details, dict):
            parsed_details = details
        elif isinstance(details, str) and details:
            try:
                parsed_details = json.loads(details)
            except Exception:
                parsed_details = {"value": details}
        else:
            parsed_details = {}
        payload = {
            "team_member_id": team_member_id,
            "action": action,
            "target_type": target_type or "",
            "target_id": target_id or "",
            "details": parsed_details,
            "ip_address": ip_address or "",
        }
        res = self.client.table("activity_log").insert(payload).execute()
        if res.data:
            try:
                return int(res.data[0]["id"])
            except Exception:
                return 0
        return 0

    def list_activity(
        self,
        limit: int = 50,
        offset: int = 0,
        action: str = "",
        team_member_id: int | None = None,
    ) -> list[dict]:
        try:
            q = self.client.table("activity_log").select("*")
            if action:
                q = q.ilike("action", f"%{action}%")
            if team_member_id:
                q = q.eq("team_member_id", team_member_id)
            res = q.order("created_at", desc=True).limit(limit).offset(offset).execute()
            rows = res.data or []
            member_ids = {int(row.get("team_member_id")) for row in rows if row.get("team_member_id") is not None}
            member_map = {
                int(m.get("id")): m
                for m in self.list_team_members()
                if m.get("id") is not None and int(m.get("id")) in member_ids
            }
            out: list[dict] = []
            for row in rows:
                details = row.get("details")
                if isinstance(details, dict):
                    details_value = json.dumps(details)
                elif details is None:
                    details_value = ""
                else:
                    details_value = str(details)
                member = member_map.get(row.get("team_member_id")) or {}
                out.append({
                    **row,
                    "details": details_value,
                    "member_name": member.get("name") or "System",
                    "member_role": member.get("role") or "",
                })
            return out
        except Exception:
            return []

    def dashboard_feed(self, limit: int = 20) -> list[dict]:
        rows = self._fetch_typed_rows(limit_per_table=limit)
        rows = [self._typed_row_to_legacy(row) for row in rows]
        return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:limit]

    def dashboard_heatmap(self) -> list[dict]:
        return []

    def dashboard_listings(self, limit: int = 20) -> list[dict]:
        rows = [self._typed_row_to_legacy(row) for row in self._fetch_typed_rows(requirements=False, limit_per_table=limit)]
        return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:limit]

    def dashboard_requirements(self, limit: int = 20) -> list[dict]:
        rows = [self._typed_row_to_legacy(row) for row in self._fetch_typed_rows(requirements=True, limit_per_table=limit)]
        return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:limit]

    def dashboard_signals(self) -> list[dict]:
        return []

    def dashboard_message_types_today(self, today_prefix: str) -> list[dict]:
        return []

    def dashboard_obs_types_today(self, today_prefix: str) -> list[dict]:
        return []

    def dashboard_growth(self, today_prefix: str) -> dict:
        return {"messages_growth": 0, "brokers_growth": 0, "listings_growth": 0}

    # ── Enrichment Jobs ──────────────────────────────────────────

    def create_enrichment_job(self, parsed_id: int, raw_message_id: int,
                               scheduled_after: str) -> int:
        if not self._legacy_parsed_exists(int(parsed_id or 0)):
            return 0
        # Dedup: skip if a job already exists for this parsed_id (any status)
        existing = self.get_enrichment_job_by_parsed(parsed_id)
        if existing:
            return existing["id"]
        data = {"parsed_id": parsed_id, "raw_message_id": raw_message_id,
                "scheduled_after": scheduled_after, "status": "pending"}
        res = self.client.table("enrichment_jobs").insert(data).execute()
        return res.data[0]["id"] if res.data else 0

    def get_pending_enrichment_jobs(self, limit: int = 50) -> list[dict]:
        res = self.client.table("enrichment_jobs").select("*").eq("status", "pending").limit(limit).execute()
        return res.data

    def claim_enrichment_job(self, job_id: int) -> bool:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        # Use eq on status=pending + the specific id.  PostgREST may not
        # return updated rows from .update(), so we verify with a select.
        self.client.table("enrichment_jobs").update({
            "status": "in_progress",
            "started_at": now,
        }).eq("id", job_id).eq("status", "pending").execute()
        res = self.client.table("enrichment_jobs").select("status").eq("id", job_id).execute()
        if res.data and res.data[0].get("status") == "in_progress":
            return True
        return False

    def complete_enrichment_job(self, job_id: int, error: str = ""):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        updates = {"status": "done" if not error else "failed", "completed_at": now}
        if error:
            updates["last_error"] = error
        self.client.table("enrichment_jobs").update(updates).eq("id", job_id).execute()

    def recover_stale_enrichment_jobs(self, stale_seconds: int = 600) -> int:
        """Reset enrichment_jobs stuck in_progress for longer than stale_seconds.

        Handles both cases: jobs with old started_at AND jobs with NULL started_at
        (from before started_at tracking was added).
        This storage implementation uses Supabase REST, so do not assume a
        psycopg connection exists on the adapter.
        """
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
        stale = self.client.table("enrichment_jobs").select("id") \
            .eq("status", "in_progress") \
            .or_(f"started_at.lt.{cutoff},started_at.is.null") \
            .execute()
        ids = [row.get("id") for row in (stale.data or []) if row.get("id") is not None]
        if not ids:
            return 0
        updated = self.client.table("enrichment_jobs").update({
            "status": "pending", "started_at": None, "attempts": 0,
        }).eq("status", "in_progress").in_("id", ids).execute()
        return len(updated.data or ids)

    def get_enrichment_job_by_parsed(self, parsed_id: int) -> Optional[dict]:
        res = self.client.table("enrichment_jobs").select("*").eq("parsed_id", parsed_id).limit(1).execute()
        return res.data[0] if res.data else None

    # ── Knowledge Graph Aliases ──────────────────────────────────

    def create_location_alias(self, alias: str, canonical: str,
                               confidence: float = 0.0, source: str = "ai") -> bool:
        data = {"alias": alias, "canonical": canonical, "confidence": confidence, "source": source}
        res = self.client.table("location_aliases").upsert(data, on_conflict="alias").execute()
        return len(res.data) > 0

    def create_building_alias(self, alias: str, canonical: str,
                               confidence: float = 0.0, source: str = "ai") -> bool:
        data = {"alias": alias, "canonical": canonical, "confidence": confidence, "source": source}
        # Kept for the legacy knowledge-graph contract.  Building discovery
        # uses create_building_alias_for_building(), which carries the FK.
        res = self.client.table("building_aliases").upsert(data, on_conflict="alias").execute()
        return len(res.data) > 0

    def resolve_location(self, text: str) -> Optional[str]:
        res = self.client.table("location_aliases").select("canonical").eq("alias", text).limit(1).execute()
        if res.data:
            return res.data[0]["canonical"]
        return None

    def resolve_building(self, text: str) -> Optional[str]:
        res = self.client.table("building_name_aliases").select("canonical_name").eq("alias", text).limit(1).execute()
        if res.data:
            return res.data[0]["canonical_name"]
        return None

    # ── Price Stats ──────────────────────────────────────────────

    def recompute_price_stats(self):
        pass

    def get_price_stats(self, micro_market: str, bhk: str,
                         intent: str = "listing") -> Optional[dict]:
        rows = [self._typed_row_to_legacy(row) for row in self._fetch_typed_rows(requirements=False, limit_per_table=1000)]
        wanted_bhk = re.sub(r"\s*bhk\s*$", "", str(bhk), flags=re.I).strip()
        rows = [row for row in rows if str(row.get("micro_market") or "").casefold() == str(micro_market).casefold()
                and str(row.get("bhk") or "").replace(" BHK", "").strip() == wanted_bhk]
        if not rows:
            return None
        prices = [float(row.get("price")) for row in rows if row.get("price")]
        if not prices:
            return None
        return {
            "micro_market": micro_market,
            "bhk": bhk,
            "intent": intent,
            "count": len(prices),
            "min": min(prices),
            "max": max(prices),
            "avg": sum(prices) / len(prices),
            "median": sorted(prices)[len(prices) // 2],
        }

    # ── Counts ───────────────────────────────────────────────────

    def message_count_today(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        query = self.client.table("raw_messages").select("id", count="exact").gte("created_at", today)
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        return res.count or 0

    def broker_count(self) -> int:
        query = self.client.table("brokers").select("id", count="exact")
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        return res.count or 0

    def listing_count(self) -> int:
        total = 0
        for table in _TYPED_LISTING_TABLE_NAMES:
            query = self.client.table(table).select("id", count="exact")
            if self._tenant_id:
                query = query.eq("tenant_id", self._tenant_id)
            total += int(query.execute().count or 0)
        return total

    def building_count(self) -> int:
        query = self.client.table("buildings").select("id", count="exact")
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        return res.count or 0

    # ── Suggestions (AI) ─────────────────────────────────────────

    def get_suggestions(self, status: str = "pending", limit: int = 50, offset: int = 0) -> list[dict]:
        query = self.client.table("ai_suggestions").select("*").order("created_at", desc=True).limit(limit).offset(offset)
        if status != "all":
            query = query.eq("status", status)
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        return res.data or []

    def get_suggestion_counts(self) -> dict:
        counts = {"pending": 0, "approved": 0, "rejected": 0, "ignored": 0}
        query = self.client.table("ai_suggestions").select("status")
        if self._tenant_id:
            query = query.eq("tenant_id", self._tenant_id)
        res = query.execute()
        for row in res.data or []:
            status = row.get("status") or "pending"
            counts[status] = counts.get(status, 0) + 1
        return counts

    def create_suggestion(self, sug: AISuggestion) -> int:
        data = {k: v for k, v in sug.__dict__.items() if v is not None}
        data.pop("id", None)
        for skip in ("created_at", "updated_at"):
            if skip in data and not data[skip]:
                del data[skip]
        if not data.get("tenant_id") and self._tenant_id:
            data["tenant_id"] = self._tenant_id
        res = self.client.table("ai_suggestions").insert(data).execute()
        return res.data[0]["id"] if res.data else 0

    def apply_suggestion(self, sug_id: int) -> bool:
        res = self.client.table("ai_suggestions").update({"status": "applied"}).eq("id", sug_id).execute()
        return len(res.data) > 0

    def update_suggestion_status(self, sug_id: int, status: str, rejection_reason: str = ""):
        data = {"status": status}
        if rejection_reason:
            data["rejection_reason"] = rejection_reason
        self.client.table("ai_suggestions").update(data).eq("id", sug_id).execute()

    def batch_update_suggestions(self, ids: list[int], status: str, rejection_reason: str = ""):
        data = {"status": status}
        if rejection_reason:
            data["rejection_reason"] = rejection_reason
        self.client.table("ai_suggestions").update(data).in_("id", ids).execute()

    def get_ai_memory_stats(self) -> dict:
        return {"suggestions": 0, "memory_entries": 0}

    def get_ai_usage_stats(self, days: int = 1) -> dict:
        return {"requests": 0, "tokens": 0}

    # ── AI Chat Sessions ──────────────────────────────────────

    def list_chat_sessions(self, broker_phone: str, limit: int = 50, tenant_id: str | None = None) -> list[dict]:
        tid = tenant_id or self._tenant_id
        q = (
            self.client.table("ai_chat_sessions")
            .select("*")
            .eq("broker_phone", broker_phone)
        )
        if tid:
            q = q.eq("tenant_id", tid)
        q = q.order("updated_at", desc=True).limit(limit)
        res = q.execute()
        return res.data or []

    def adopt_chat_session_owners(
        self,
        owner_keys: list[str],
        canonical_owner: str,
        tenant_id: str | None = None,
    ) -> None:
        """Move legacy per-phone/per-UUID chat threads to one authenticated owner."""
        aliases = [str(key).strip() for key in owner_keys if str(key).strip() and str(key).strip() != canonical_owner]
        if not aliases:
            return
        tid = tenant_id or self._tenant_id
        q = (
            self.client.table("ai_chat_sessions")
            .update({"broker_phone": canonical_owner})
            .in_("broker_phone", aliases)
        )
        if tid:
            q = q.eq("tenant_id", tid)
        q.execute()

    def create_chat_session(self, broker_phone: str, title: str = "New chat", source: str = "parsed", tenant_id: str | None = None) -> dict | None:
        tid = tenant_id or self._tenant_id
        payload = {"broker_phone": broker_phone, "title": title, "source": source}
        if tid:
            payload["tenant_id"] = tid
        res = (
            self.client.table("ai_chat_sessions")
            .insert(payload)
            .execute()
        )
        return res.data[0] if res.data else None

    def get_or_create_chat_session(
        self,
        owner_key: str,
        title: str = "New chat",
        tenant_id: str | None = None,
    ) -> dict | None:
        sessions = self.list_chat_sessions(owner_key, limit=1, tenant_id=tenant_id)
        if sessions:
            return sessions[0]
        return self.create_chat_session(owner_key, title=title, tenant_id=tenant_id)

    def get_chat_session(self, session_id: str, tenant_id: str | None = None) -> dict | None:
        tid = tenant_id or self._tenant_id
        q = (
            self.client.table("ai_chat_sessions")
            .select("*")
            .eq("id", session_id)
        )
        if tid:
            q = q.eq("tenant_id", tid)
        q = q.limit(1)
        res = q.execute()
        return res.data[0] if res.data else None

    def delete_chat_session(self, session_id: str, tenant_id: str | None = None) -> bool:
        tid = tenant_id or self._tenant_id
        q = self.client.table("ai_chat_sessions").delete().eq("id", session_id)
        if tid:
            q = q.eq("tenant_id", tid)
        q.execute()
        return True

    def touch_chat_session(self, session_id: str, tenant_id: str | None = None) -> None:
        tid = tenant_id or self._tenant_id
        updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        q = self.client.table("ai_chat_sessions").update({"updated_at": updated_at}).eq("id", session_id)
        if tid:
            q = q.eq("tenant_id", tid)
        q.execute()

    def update_chat_session_title(self, session_id: str, title: str, tenant_id: str | None = None) -> None:
        tid = tenant_id or self._tenant_id
        q = self.client.table("ai_chat_sessions").update({"title": title}).eq("id", session_id)
        if tid:
            q = q.eq("tenant_id", tid)
        q.execute()

    def add_chat_message(self, session_id: str, role: str, content: str, tenant_id: str | None = None, blocks: list | None = None) -> dict | None:
        tid = tenant_id or self._tenant_id
        payload = {"session_id": session_id, "role": role, "content": content}
        if tid:
            payload["tenant_id"] = tid
        if blocks:
            payload["blocks"] = blocks
        res = (
            self.client.table("ai_chat_messages")
            .insert(payload)
            .execute()
        )
        return res.data[0] if res.data else None

    def add_chat_message_if_new(
        self,
        session_id: str,
        role: str,
        content: str,
        tenant_id: str | None = None,
        blocks: list | None = None,
    ) -> dict | None:
        """Persist a turn once even when a transport retries the same request."""
        clean = str(content or "").strip()
        if not clean:
            return None
        tid = tenant_id or self._tenant_id
        q = (
            self.client.table("ai_chat_messages")
            .select("id,role,content")
            .eq("session_id", session_id)
        )
        if tid:
            q = q.eq("tenant_id", tid)
        latest = q.order("created_at", desc=True).limit(1).execute().data or []
        if latest and latest[0].get("role") == role and str(latest[0].get("content") or "").strip() == clean:
            return latest[0]
        return self.add_chat_message(session_id, role, clean, tenant_id=tenant_id, blocks=blocks)

    def get_ai_chat_messages(self, session_id: str, limit: int = 200, tenant_id: str | None = None) -> list[dict]:
        """Return persisted AI-chat messages.

        This must not share a name with get_chat_messages(), which is the
        WhatsApp raw-message timeline used by /api/chats/{jid}/messages.
        Python keeps only the later method definition; the old collision made
        every WhatsApp group JID query ai_chat_messages instead.
        """
        tid = tenant_id or self._tenant_id
        q = (
            self.client.table("ai_chat_messages")
            .select("*")
            .eq("session_id", session_id)
        )
        if tid:
            q = q.eq("tenant_id", tid)
        q = q.order("created_at").limit(limit)
        res = q.execute()
        return res.data or []

    # ── LLM Providers ──────────────────────────────────────────

    def get_llm_providers(self, tenant_id: str | None = None) -> list[LLMProvider]:
        tid = tenant_id or self._tenant_id
        q = self.client.table("llm_providers").select("*")
        if tid:
            q = q.eq("tenant_id", tid)
        q = q.order("is_active", desc=True).order("provider_name")
        res = q.execute()
        return [dict_to_dataclass(LLMProvider, r) for r in res.data]

    def get_active_llm_provider(self, tenant_id: str | None = None) -> Optional[LLMProvider]:
        tid = tenant_id or self._tenant_id
        q = self.client.table("llm_providers").select("*").eq("is_active", 1)
        if tid:
            q = q.eq("tenant_id", tid)
        q = q.limit(1)
        res = q.execute()
        return dict_to_dataclass(LLMProvider, res.data[0]) if res.data else None

    def list_all_llm_providers(self) -> list[dict]:
        """Cross-tenant provider list — used by the probe loop and /admin/providers.

        Returns raw dicts (not dataclasses) so the probe loop and admin UI can
        read fields without dataclass round-tripping. Includes inactive
        providers so they still get probed (so you can see outage evidence for
        a provider that was deactivated during an incident).
        """
        res = self.client.table("llm_providers").select("*").execute()
        return list(res.data or [])

    def save_llm_provider(self, provider: LLMProvider, tenant_id: str | None = None) -> int:
        tid = tenant_id or self._tenant_id
        data = {k: v for k, v in provider.__dict__.items() if v is not None}
        data.pop("created_at", None)
        data.pop("updated_at", None)
        if tid:
            data["tenant_id"] = tid
        if provider.id:
            data.pop("id", None)
            if not provider.api_key or "****" in provider.api_key:
                eq = self.client.table("llm_providers").select("api_key").eq("id", provider.id)
                if tid:
                    eq = eq.eq("tenant_id", tid)
                existing = eq.execute()
                if existing.data and existing.data[0].get("api_key"):
                    data["api_key"] = existing.data[0]["api_key"]
            # NOTE: multiple providers may be active at once — this is what
            # feeds _workspace_provider_candidates()/_run_workspace_agent()'s
            # failover rotation. Do not force-deactivate other rows here; a
            # broker toggling one provider on must never silently turn off
            # their other configured keys.
            up = self.client.table("llm_providers").update(data).eq("id", provider.id)
            if tid:
                up = up.eq("tenant_id", tid)
            up.execute()
            return provider.id
        else:
            data.pop("id", None)
            # Same note as above — do not force-deactivate other rows.
            res = self.client.table("llm_providers").insert(data).execute()
            return res.data[0]["id"] if res.data else 0

    def delete_llm_provider(self, provider_id: int, tenant_id: str | None = None) -> bool:
        tid = tenant_id or self._tenant_id
        q = self.client.table("llm_providers").delete().eq("id", provider_id)
        if tid:
            q = q.eq("tenant_id", tid)
        res = q.execute()
        return len(res.data) > 0

    # ── Workspace AI Settings ───────────────────────────────────────────

    def get_workspace_ai_settings(self, tenant_id: str | None = None) -> Optional[WorkspaceAISettings]:
        tid = tenant_id or self._tenant_id
        if not tid:
            return None
        try:
            res = (
                self.client.table("workspace_ai_settings")
                .select("*")
                .eq("tenant_id", tid)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            import logging

            logging.warning(
                "workspace_ai_settings read failed for tenant %s; falling back to defaults: %s",
                tid,
                exc,
            )
            return None
        return dict_to_dataclass(WorkspaceAISettings, res.data[0]) if res.data else None

    def save_workspace_ai_settings(self, settings: WorkspaceAISettings, tenant_id: str | None = None) -> int:
        tid = tenant_id or self._tenant_id
        if not tid:
            raise ValueError("tenant_id is required to save workspace AI settings")
        data = {k: v for k, v in settings.__dict__.items() if v is not None}
        data.pop("id", None)
        data.pop("created_at", None)
        data.pop("updated_at", None)
        data["tenant_id"] = tid
        data["browser_provider"] = "agent-browser" if str(data.get("browser_provider") or "").lower() in {
            "", "browser-use", "browser-use-cli", "browser_use", "playwright"
        } else data.get("browser_provider")
        try:
            res = self.client.table("workspace_ai_settings").upsert(data, on_conflict="tenant_id").execute()
        except Exception:
            import logging
            logging.exception("workspace_ai_settings upsert failed for tenant %s", tid)
            raise
        if not res.data:
            raise RuntimeError(f"workspace_ai_settings upsert returned no row for tenant {tid}")
        return int(res.data[0]["id"])

    def get_workspace_market_preferences(self, tenant_id: str | None = None) -> dict | None:
        tid = tenant_id or self._tenant_id
        if not tid:
            return None
        try:
            res = self.client.table("workspace_market_preferences").select("*").eq("tenant_id", tid).limit(1).execute()
            return dict(res.data[0]) if res.data else None
        except Exception:
            logging.getLogger(__name__).warning("workspace market preferences unavailable", exc_info=True)
            return None

    def save_workspace_market_preferences(self, preferences: dict, tenant_id: str | None = None) -> dict:
        tid = tenant_id or self._tenant_id
        if not tid:
            raise ValueError("tenant_id is required to save market preferences")
        data = {
            "tenant_id": tid,
            "primary_localities": preferences.get("primary_localities") or [],
            "nearby_localities": preferences.get("nearby_localities") or [],
            "transaction_types": preferences.get("transaction_types") or ["sale", "rent"],
            "asset_types": preferences.get("asset_types") or ["residential", "commercial"],
            "onboarding_completed": True,
            "source": "explicit",
        }
        res = self.client.table("workspace_market_preferences").upsert(data, on_conflict="tenant_id").execute()
        if not res.data:
            raise RuntimeError("market preferences were not saved")
        return dict(res.data[0])

    # ── Agent Browser / Audit traces ─────────────────────────────────────

    def create_agent_browser_session(self, session: AgentBrowserSession, tenant_id: str | None = None) -> dict | None:
        tid = tenant_id or self._tenant_id
        if not tid:
            raise ValueError("tenant_id is required to create a browser session")
        data = {k: v for k, v in session.__dict__.items() if v is not None}
        data.pop("id", None)
        data.pop("started_at", None)
        data.pop("updated_at", None)
        data.pop("closed_at", None)
        data["tenant_id"] = tid
        if not data.get("context"):
            data["context"] = {}
        res = self.client.table("agent_browser_sessions").insert(data).execute()
        return dict(res.data[0]) if res.data else None

    def list_agent_browser_sessions(
        self,
        tenant_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        tid = tenant_id or self._tenant_id
        q = self.client.table("agent_browser_sessions").select("*")
        if tid:
            q = q.eq("tenant_id", tid)
        if session_id:
            q = q.eq("session_id", session_id)
        if user_id:
            q = q.eq("user_id", user_id)
        res = q.order("updated_at", desc=True).limit(max(1, min(limit, 200))).execute()
        return list(res.data or [])

    def update_agent_browser_session(self, browser_session_id: str, values: dict, tenant_id: str | None = None) -> dict | None:
        tid = tenant_id or self._tenant_id
        if not browser_session_id:
            return None
        data = {k: v for k, v in (values or {}).items() if v is not None}
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        q = self.client.table("agent_browser_sessions").update(data).eq("id", browser_session_id)
        if tid:
            q = q.eq("tenant_id", tid)
        res = q.execute()
        return dict(res.data[0]) if res.data else None

    def add_agent_browser_step(self, step: AgentBrowserStep, tenant_id: str | None = None) -> int:
        tid = tenant_id or self._tenant_id
        if not tid:
            raise ValueError("tenant_id is required to add a browser step")
        data = {k: v for k, v in step.__dict__.items() if v is not None}
        data.pop("id", None)
        data.pop("created_at", None)
        data["tenant_id"] = tid
        if not data.get("metadata"):
            data["metadata"] = {}
        res = self.client.table("agent_browser_steps").insert(data).execute()
        return int(res.data[0]["id"]) if res.data else 0

    def log_agent_audit_event(self, event: AgentAuditLog, tenant_id: str | None = None) -> int:
        tid = tenant_id or self._tenant_id
        if not tid:
            raise ValueError("tenant_id is required to log an agent audit event")
        data = {k: v for k, v in event.__dict__.items() if v is not None}
        data.pop("id", None)
        data.pop("created_at", None)
        data["tenant_id"] = tid
        if not data.get("metadata"):
            data["metadata"] = {}
        res = self.client.table("agent_audit_log").insert(data).execute()
        return int(res.data[0]["id"]) if res.data else 0

    # ── Provider Outage Log ────────────────────────────────────────────

    def insert_provider_outage_event(self, event) -> int:
        """Write one probe result. Returns the new row id.

        Skips None and empty-string fields so the DB column defaults
        (notably `ts timestamptz default now()`) kick in. Without this filter,
        the dataclass default `ts = ""` is sent to Supabase and rejected as
        400 (can't cast '' to timestamptz).
        """
        data = {
            k: v for k, v in event.__dict__.items()
            if v is not None and v != "" and k != "id"
        }
        res = self.client.table("provider_outage_log").insert(data).execute()
        return int(res.data[0]["id"]) if res.data else 0

    def list_provider_outage_events(
        self,
        since_minutes: int = 60,
        provider_name: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Recent probe results, newest first.

        Used by /admin/providers/health + /admin/providers/history.
        Returns raw dicts (not dataclasses) so timestamps survive the round-trip
        without timezone parsing in tests.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        ).isoformat()
        q = (
            self.client.table("provider_outage_log")
            .select("*")
            .gte("ts", cutoff)
        )
        if provider_name:
            q = q.eq("provider_name", provider_name)
        q = q.order("ts", desc=True).limit(limit)
        res = q.execute()
        return list(res.data or [])

    def list_provider_outage_events_by_provider_id(
        self,
        since_minutes: int,
        provider_id: int,
        limit: int = 500,
    ) -> list[dict]:
        """Per-row probe history. Multiple rows with the same provider_name
        (e.g. several NVIDIA keys) each get their own series, never mixed.

        Falls back to provider_name match if provider_id is 0 (so rows written
        before this field was reliable still surface).
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        ).isoformat()
        q = (
            self.client.table("provider_outage_log")
            .select("*")
            .gte("ts", cutoff)
            .eq("provider_id", provider_id)
            .order("ts", desc=True)
            .limit(limit)
        )
        res = q.execute()
        return list(res.data or [])

    def cleanup_provider_outage_log(self, retention_days: int = 7) -> int:
        """Drop rows older than `retention_days`. Returns the deleted count."""
        res = self.db.execute(
            "SELECT cleanup_provider_outage_log(%s)", (retention_days,)
        ).fetchone()
        return int(res[0]) if res else 0

    # ── Observation Graph ────────────────────────────────────────────────

    def rebuild_observation_graph(self) -> dict:
        try:
            data = self.db.execute(
                "SELECT rebuild_observation_graph()"
            ).fetchone()
            if data:
                val = data[0]
                if isinstance(val, str):
                    import json
                    return json.loads(val)
                return dict(val)
            return {"observations": 0, "evidence": 0}
        except Exception:
            return {"observations": 0, "evidence": 0}

    def rebuild_broker_graph(self) -> dict:
        try:
            data = self.db.execute(
                "SELECT rebuild_broker_graph()"
            ).fetchone()
            if data:
                val = data[0]
                if isinstance(val, str):
                    import json
                    return json.loads(val)
                return dict(val)
            return {"brokers": 0, "observations": 0}
        except Exception:
            return {"brokers": 0, "observations": 0}

    # ── Market Items / Brokers Feed ──────────────────────────────────────

    @staticmethod
    def _workspace_broker_key(phone: str = "", name: str = "") -> str:
        normalized_phone = _normalize_india_phone(phone or "")
        if normalized_phone:
            return f"phone:{normalized_phone}"
        normalized_name = _market_name_key(name or "")
        return f"name:{normalized_name}" if normalized_name else ""

    def get_workspace_blocked_brokers(self, organization_id: str | None = None) -> list[dict]:
        """Return broker suppressions for one workspace.

        Missing-table errors are treated as an empty list so a migration can
        roll out independently of the API image without breaking reads.
        """
        org_id = organization_id or self._tenant_id
        if not org_id:
            return []
        try:
            result = self.client.table("workspace_blocked_brokers").select(
                "id,organization_id,broker_key,broker_name,broker_phone,reason,created_at"
            ).eq("organization_id", org_id).order("created_at", desc=True).execute()
            return list(result.data or [])
        except Exception:
            _logger.debug("workspace broker block table unavailable", exc_info=True)
            return []

    def get_workspace_blocked_broker_keys(self, organization_id: str | None = None) -> set[str]:
        keys: set[str] = set()
        for row in self.get_workspace_blocked_brokers(organization_id):
            key = str(row.get("broker_key") or "").strip().lower()
            if key:
                keys.add(key)
        return keys

    def broker_is_workspace_blocked(
        self,
        phone: str = "",
        name: str = "",
        organization_id: str | None = None,
        blocked_keys: set[str] | None = None,
    ) -> bool:
        keys = blocked_keys if blocked_keys is not None else self.get_workspace_blocked_broker_keys(organization_id)
        phone_key = self._workspace_broker_key(phone=phone)
        name_key = self._workspace_broker_key(name=name)
        return bool((phone_key and phone_key.lower() in keys) or (name_key and name_key.lower() in keys))

    def block_broker_for_workspace(
        self,
        organization_id: str,
        *,
        phone: str = "",
        name: str = "",
        reason: str = "",
        created_by: str | None = None,
    ) -> list[dict]:
        """Suppress a broker by stable phone identity, with name fallback."""
        normalized_phone = _normalize_india_phone(phone or "")
        clean_name = _clean_person_name(name or "")
        keys = []
        phone_key = self._workspace_broker_key(phone=normalized_phone)
        name_key = self._workspace_broker_key(name=clean_name)
        if phone_key:
            keys.append((phone_key, normalized_phone))
        if name_key and name_key != phone_key:
            keys.append((name_key, ""))
        if not keys:
            raise ValueError("A broker phone or name is required")
        rows = [{
            "organization_id": organization_id,
            "broker_key": key,
            "broker_name": clean_name,
            "broker_phone": phone,
            "reason": str(reason or "").strip()[:500],
            "created_by": created_by,
        } for key, _phone in keys]
        result = self.client.table("workspace_blocked_brokers").upsert(
            rows, on_conflict="organization_id,broker_key"
        ).execute()
        return list(result.data or rows)

    def unblock_broker_for_workspace(self, organization_id: str, broker_key: str) -> bool:
        key = str(broker_key or "").strip().lower()
        if not key:
            return False
        result = self.client.table("workspace_blocked_brokers").delete().eq(
            "organization_id", organization_id
        ).eq("broker_key", key).execute()
        return bool(result.data)

    def _filter_workspace_blocked_rows(
        self, rows: list[dict], blocked_keys: set[str] | None = None
    ) -> list[dict]:
        if not rows:
            return rows
        keys = blocked_keys if blocked_keys is not None else self.get_workspace_blocked_broker_keys()
        if not keys:
            return rows
        return [
            row for row in rows
            if not self.broker_is_workspace_blocked(
                phone=str(row.get("broker_phone") or ""),
                name=str(row.get("broker_name") or ""),
                blocked_keys=keys,
            )
        ]

    def get_market_items_feed(self, limit: int = 50, offset: int = 0,
                              broker_key: str = "", intent: str = "",
                              result_type: str = "all",
                              market_localities: list[str] | None = None,
                              tenant_id: str | None = None) -> list[dict]:
        # Parsed market inventory is a shared network.  The request tenant is
        # still relevant for workspace-owned settings, but never filters the
        # market feed itself.
        tid = None
        if broker_key:
            return self._get_parsed_observations_for_broker(
                limit, offset, broker_key=broker_key, intent=intent,
                market_localities=market_localities,
                result_type=result_type, tenant_id=tid
            )
        return self._get_recent_market_observations(
            limit=limit,
            offset=offset,
            intent=intent,
            result_type=result_type,
            market_localities=market_localities,
            tenant_id=tid,
        )

    def get_market_item_detail(
        self, row_id: int, source_schema: str = "", raw_message_id: int | None = None,
        tenant_id: str | None = None,
    ) -> dict | None:
        """Fetch the expensive evidence projection only for an expanded card."""
        if source_schema not in _ALL_TYPED_TABLES:
            return None
        # Parsed market inventory is shared across the network. Do not apply
        # the active workspace tenant filter to either typed records or their
        # raw evidence.
        tid = None
        query = self.client.table(source_schema).select(
            _typed_read_columns(source_schema, include_evidence=True,
                                include_normalized_message=True,
                                include_raw_payload=True)
        ).eq("id", row_id).limit(1)
        if tid:
            query = query.eq("tenant_id", tid)
        rows = query.execute().data or []
        if not rows:
            return None
        typed = {**rows[0], "_typed_table": source_schema}
        if self.broker_is_workspace_blocked(
            phone=str(typed.get("broker_phone") or ""),
            name=str(typed.get("broker_name") or ""),
        ):
            return None
        raw_id = int(raw_message_id or typed.get("raw_message_id") or 0)
        raw = {}
        if raw_id:
            raw_query = self.client.table("raw_messages").select(
                "id,message,timestamp,created_at,sender,group_name,sender_phone"
            ).eq("id", raw_id).limit(1)
            if tid:
                raw_query = raw_query.eq("tenant_id", tid)
            raw_rows = raw_query.execute().data or []
            raw = raw_rows[0] if raw_rows else {}
        result = self._typed_row_to_legacy(typed)
        result["raw_message"] = str(raw.get("message") or "")
        payload = typed.get("raw_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                payload = {}
        payload = payload if isinstance(payload, dict) else {}
        source = str(payload.get("slice_text") or payload.get("full_text") or "")
        result["source_slice_text"] = _redact_market_source_text(
            _preferred_market_source_text(
                raw.get("message"), typed.get("normalized_message"), source
            )
        )
        result["source_message"] = _redact_market_source_text(
            _preferred_market_source_text(
                raw.get("message"), typed.get("normalized_message"), source
            )
        )
        result["observation_type"] = "REQUIREMENT" if source_schema.endswith("_requirements") else "LISTING"
        result["latest_raw_message_id"] = typed.get("raw_message_id")
        result["latest_parsed_id"] = typed.get("id")
        return result

    def _get_recent_market_observations(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        intent: str = "",
        result_type: str = "all",
        market_localities: list[str] | None = None,
        tenant_id: str | None = None,
    ) -> list[dict]:
        tid = None
        typed_rows, raw_map = self._fetch_recent_market_typed_rows(
            tenant_id=tid,
            limit=limit,
            offset=offset,
            intent=intent,
            result_type=result_type,
        )
        candidates: list[dict] = []
        for typed in typed_rows:
            if market_localities:
                values = [typed.get("micro_market"), typed.get("locality_raw"), typed.get("locality_resolved"), typed.get("building_name")]
                normalized_values = [_market_name_key(value) for value in values if value]
                wanted = [_market_name_key(value) for value in market_localities if value]
                if wanted and not any(
                    candidate == target or candidate in target or target in candidate
                    for candidate in normalized_values for target in wanted
                ):
                    continue
            raw_id = int(typed.get("raw_message_id") or 0)
            raw = raw_map.get(raw_id) or {}
            legacy = self._typed_row_to_legacy(typed)
            if intent and str(legacy.get("intent") or "").upper() != intent.upper():
                continue
            legacy["_typed_table"] = typed.get("_typed_table")
            legacy["raw_message"] = str(raw.get("message") or "")
            payload = typed.get("raw_payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError):
                    payload = {}
            payload = payload if isinstance(payload, dict) else {}
            source_slice = str(payload.get("slice_text") or payload.get("full_text") or "")
            source_slice = _relevant_market_source_slice(source_slice, typed.get("building_name"))
            legacy["source_slice_text"] = _redact_market_source_text(source_slice)
            legacy["source_message"] = _redact_market_source_text(
                _preferred_market_source_text(
                    raw.get("message"), legacy.get("normalized_message"), source_slice
                )
            )
            legacy["observation_type"] = "REQUIREMENT" if "requirement" in str(typed.get("_typed_table") or "") else "LISTING"
            legacy["latest_raw_message_id"] = typed.get("raw_message_id")
            legacy["latest_parsed_id"] = typed.get("id")
            legacy["first_seen"] = str(typed.get("created_at") or raw.get("timestamp") or "")
            legacy["last_seen"] = str(typed.get("last_seen_at") or typed.get("updated_at") or typed.get("created_at") or "")
            legacy["times_seen"] = 1
            candidates.append(legacy)
        merged = _merge_observation_rows(candidates)
        merged.sort(key=lambda row: str(row.get("last_seen") or row.get("created_at") or ""), reverse=True)
        return merged[offset:offset + limit]

    def _get_parsed_observations_for_broker(self, limit: int = 50, offset: int = 0,
                                            broker_key: str = "", intent: str = "",
                                            result_type: str = "all",
                                            market_localities: list[str] | None = None,
                                            tenant_id: str | None = None) -> list[dict]:
        if not broker_key:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        normalized_key = _normalize_india_phone(broker_key)
        is_name_key = broker_key.lower().startswith("name:")
        name_key = broker_key.replace("name:", "", 1).strip().lower() if is_name_key else ""
        tid = None
        requirement_filter = (
            True if result_type == "requirements"
            else False if result_type == "listings"
            else None
        )
        rows = self._fetch_typed_rows(
            requirements=requirement_filter,
            limit_per_table=max(25, min(250, limit + offset)),
            tenant_id=tid,
            all_tenants=True,
            card_only=True,
            broker_key=broker_key,
            transaction_type={"SELL": "sale", "RENT": "rent"}.get(str(intent or "").upper(), ""),
        )
        raw_map: dict[int, dict] = {}
        linked_names: set[str] = set()
        if normalized_key:
            for typed in rows:
                phone = _normalize_india_phone(typed.get("broker_phone") or "")
                if phone == normalized_key:
                    name = _clean_person_name(typed.get("broker_name") or "")
                    if name:
                        linked_names.add(name.lower())
        candidates: list[dict] = []
        for typed in rows:
            if market_localities:
                values = [typed.get("micro_market"), typed.get("locality_raw"), typed.get("locality_resolved"), typed.get("building_name")]
                normalized_values = [_market_name_key(value) for value in values if value]
                wanted = [_market_name_key(value) for value in market_localities if value]
                if wanted and not any(
                    candidate == target or candidate in target or target in candidate
                    for candidate in normalized_values for target in wanted
                ):
                    continue
            raw_id = int(typed.get("raw_message_id") or 0)
            raw = raw_map.get(raw_id) or {}
            seen_at = str(typed.get("last_seen_at") or typed.get("updated_at") or typed.get("created_at") or "")
            if seen_at and seen_at < cutoff:
                continue
            phone = _normalize_india_phone(typed.get("broker_phone") or "")
            name = _clean_person_name(typed.get("broker_name") or "")
            if normalized_key and phone != normalized_key and name.lower() not in linked_names:
                continue
            if name_key and name_key not in name.lower():
                continue
            legacy = self._typed_row_to_legacy(typed)
            if intent and str(legacy.get("intent") or "").upper() != intent.upper():
                continue
            legacy["_typed_table"] = typed.get("_typed_table")
            legacy["broker_name"] = name
            # A typed row may have been saved before broker resolution, so a
            # name-linked observation can have a blank phone.  Expose the
            # phone used for this broker lookup consistently without changing
            # the persisted typed row.
            effective_phone = phone or (
                normalized_key if normalized_key and name.lower() in linked_names else ""
            )
            legacy["broker_phone"] = effective_phone
            legacy["raw_message"] = str(raw.get("message") or "")
            payload = typed.get("raw_payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, json.JSONDecodeError):
                    payload = {}
            payload = payload if isinstance(payload, dict) else {}
            source_slice = str(payload.get("slice_text") or payload.get("full_text") or "")
            source_slice = _relevant_market_source_slice(source_slice, typed.get("building_name"))
            legacy["source_slice_text"] = _redact_market_source_text(source_slice)
            legacy["source_message"] = _redact_market_source_text(
                _preferred_market_source_text(
                    raw.get("message"), legacy.get("normalized_message"), source_slice
                )
            )
            legacy["observation_type"] = "REQUIREMENT" if "requirement" in str(typed.get("_typed_table") or "") else "LISTING"
            legacy["broker_key"] = normalized_key or effective_phone or name
            legacy["latest_raw_message_id"] = typed.get("raw_message_id")
            legacy["latest_parsed_id"] = typed.get("id")
            legacy["first_seen"] = str(typed.get("created_at") or raw.get("timestamp") or "")
            legacy["last_seen"] = str(typed.get("last_seen_at") or typed.get("updated_at") or typed.get("created_at") or "")
            legacy["times_seen"] = 1
            candidates.append(legacy)
        # The broker-scoped feed must use the same repost projection as the
        # workspace feed. Without this, every WhatsApp repost becomes a
        # separate card when a member has a linked broker identity.
        candidates = _merge_observation_rows(candidates)
        candidates.sort(key=lambda row: str(row.get("last_seen") or row.get("created_at") or ""), reverse=True)
        return candidates[offset:offset + limit]

    def get_brokers_feed(self, limit: int = 50, offset: int = 0,
                         min_observations: int = 1,
                         tenant_id: str | None = None) -> list[dict]:
        tid = tenant_id or self._tenant_id
        try:
            parsed_threads = self._get_parsed_market_threads(limit, offset, tenant_id=tid)
            result = []
            for thread in parsed_threads:
                identity = thread.get("conversation_key") or thread.get("chat_id") or ""
                phone = _normalize_india_phone(thread.get("broker_phone") or "")
                name = _clean_person_name(thread.get("broker_name") or thread.get("conversation_name") or "")
                if not phone and not name:
                    continue
                identity_key = phone or f"name:{_market_name_key(name)}"
                observation_count = thread.get("opportunity_count") or thread.get("message_count") or 0
                if observation_count < max(1, min_observations):
                    continue
                result.append({
                    "id": identity,
                    "identity_key": identity_key,
                    "primary_phone": phone,
                    "canonical_name": name or "Unknown broker",
                    "building_count": 1 if thread.get("building_name") else 0,
                    "active_days_30": None,
                    "observation_count": thread.get("opportunity_count") or thread.get("message_count") or 0,
                    "listing_count": thread.get("listing_count") or 0,
                    "requirement_count": thread.get("requirement_count") or 0,
                    "obs_count": thread.get("opportunity_count") or thread.get("message_count") or 0,
                    "last_active": thread.get("latest_message_at") or thread.get("timestamp") or thread.get("created_at"),
                    "first_seen": None,
                    "group_evidence_count": len(thread.get("source_group_names") or []),
                    "dm_evidence_count": 0,
                    "unique_channel_count": len(thread.get("source_group_names") or []),
                    "latest_title": thread.get("summary_title") or thread.get("message"),
                    "latest_intent": thread.get("intent") or thread.get("parsed_intent"),
                    "latest_micro_market": thread.get("micro_market"),
                    "specialty_localities": thread.get("specialty_localities") or [],
                    "specialty_property_types": thread.get("specialty_property_types") or [],
                    "channels": [
                        {"source": group_name, "type": "group"}
                        for group_name in (thread.get("source_group_names") or [])
                    ],
                })
            if result:
                merged: dict[str, dict] = {}
                for row in result:
                    phone = _normalize_india_phone(row.get("primary_phone") or "")
                    key = phone or row.get("identity_key") or f"name:{_market_name_key(row.get('canonical_name') or '')}"
                    existing = merged.get(key)
                    if not existing:
                        row["identity_key"] = key
                        row["primary_phone"] = phone
                        merged[key] = row
                        continue
                    for field in (
                        "observation_count", "obs_count", "listing_count",
                        "requirement_count", "building_count",
                    ):
                        existing[field] = (existing.get(field) or 0) + (row.get(field) or 0)
                    channels = {
                        (item.get("type"), item.get("source")): item
                        for item in [*(existing.get("channels") or []), *(row.get("channels") or [])]
                    }
                    existing["channels"] = list(channels.values())
                    existing["group_evidence_count"] = len(existing["channels"])
                    for field, maximum in (("specialty_localities", 3), ("specialty_property_types", 2)):
                        combined = []
                        seen = set()
                        for value in [*(existing.get(field) or []), *(row.get(field) or [])]:
                            normalized = str(value or "").strip()
                            key_value = normalized.lower()
                            if normalized and key_value not in seen:
                                seen.add(key_value)
                                combined.append(normalized)
                        existing[field] = combined[:maximum]
                    if str(row.get("last_active") or "") > str(existing.get("last_active") or ""):
                        for field in ("last_active", "latest_title", "latest_intent", "latest_micro_market"):
                            existing[field] = row.get(field)
                rows = sorted(
                    merged.values(),
                    key=lambda item: item.get("last_active") or "",
                    reverse=True,
                )
                if rows:
                    return rows
        except Exception:
            pass
        return []

    def get_brokers_feed_total(self, min_observations: int = 1,
                               tenant_id: str | None = None) -> int:
        """Return the count used by the broker directory pagination UI."""
        try:
            tid = tenant_id or self._tenant_id
            query = self.client.table("brokers").select("id", count="exact")\
                .eq("is_hidden", False)\
                .or_("listing_count.gt.0,requirement_count.gt.0")
            if tid:
                query = query.eq("tenant_id", tid)
            res = query.execute()
            return int(res.count or 0)
        except Exception:
            return 0

    def get_saved_inbox_views(self, tenant_id: str | None = None) -> list[dict]:
        try:
            tid = tenant_id or self._tenant_id
            q = self.client.table("saved_inbox_views")\
                .select("id, slug, name, description, filters, is_default, is_shared, created_at, updated_at")
            if tid:
                q = q.eq("tenant_id", tid)
            q = q.order("is_default", desc=True).order("name", desc=False)
            res = q.execute()
            return res.data if res.data else []
        except Exception:
            return []

    def get_saved_inbox_view(self, slug: str, tenant_id: str | None = None) -> dict | None:
        tid = tenant_id or self._tenant_id
        try:
            q = self.client.table("saved_inbox_views").select("*").eq("slug", slug)
            if tid:
                q = q.eq("tenant_id", tid)
            q = q.limit(1)
            res = q.execute()
            return res.data[0] if res.data else None
        except Exception:
            return None

    def create_saved_inbox_view(self, slug: str, name: str, filters: dict, description: str = "", is_default: bool = False, is_shared: bool = False, tenant_id: str | None = None) -> int | None:
        tid = tenant_id or self._tenant_id
        payload = {
            "slug": slug,
            "name": name,
            "description": description,
            "filters": filters or {},
            "is_default": bool(is_default),
            "is_shared": bool(is_shared),
        }
        if tid:
            payload["tenant_id"] = tid
        res = self.client.table("saved_inbox_views").insert(payload).execute()
        return res.data[0]["id"] if res.data else None

    def update_saved_inbox_view(self, slug: str, name: str | None = None, filters: dict | None = None, description: str | None = None, is_default: bool | None = None, is_shared: bool | None = None, tenant_id: str | None = None) -> bool:
        tid = tenant_id or self._tenant_id
        payload: dict = {"updated_at": datetime.now(timezone.utc).isoformat()}
        if name is not None:
            payload["name"] = name
        if filters is not None:
            payload["filters"] = filters
        if description is not None:
            payload["description"] = description
        if is_default is not None:
            payload["is_default"] = bool(is_default)
        if is_shared is not None:
            payload["is_shared"] = bool(is_shared)
        q = self.client.table("saved_inbox_views").update(payload).eq("slug", slug)
        if tid:
            q = q.eq("tenant_id", tid)
        res = q.execute()
        return bool(res.data)

    def delete_saved_inbox_view(self, slug: str, tenant_id: str | None = None) -> bool:
        tid = tenant_id or self._tenant_id
        q = self.client.table("saved_inbox_views").delete().eq("slug", slug)
        if tid:
            q = q.eq("tenant_id", tid)
        res = q.execute()
        return bool(res.data)

    def get_building_profile(self, building_db_id: int | str) -> dict | None:
        """Return the enrichment/profile view used by the building detail API.

        Keep the optional enrichment tables best-effort: a missing or partially
        migrated enrichment table must not turn a normal building lookup into a
        500 response.
        """
        try:
            building_res = self.client.table("buildings").select("*").eq("id", building_db_id).limit(1).execute()
            if not building_res.data:
                return None
            building = building_res.data[0]

            def read(table: str, columns: str = "*", *, order: str | None = None, limit: int | None = None, filters: dict | None = None) -> list[dict]:
                try:
                    query = self.client.table(table).select(columns)
                    for key, value in (filters or {}).items():
                        query = query.eq(key, value)
                    if order:
                        query = query.order(order, desc=True)
                    if limit:
                        query = query.limit(limit)
                    result = query.execute()
                    return result.data or []
                except Exception:
                    return []

            aliases = read("building_name_aliases", filters={"building_id": building["id"]})
            sources = read("building_enrichment_sources", order="enriched_at", filters={"building_id": building["id"]})
            history = read("building_enrichment_history", order="created_at", limit=50, filters={"building_id": building["id"]})

            canonical_name = building.get("canonical_name") or ""
            typed_rows = self._fetch_typed_rows(limit_per_table=5000)
            building_rows = [
                row for row in typed_rows
                if str(row.get("building_name") or "").strip().lower() == canonical_name.strip().lower()
            ]
            listings_count = sum("requirement" not in str(row.get("_typed_table") or "") for row in building_rows)
            requirements_count = sum("requirement" in str(row.get("_typed_table") or "") for row in building_rows)
            brokers_count = len({
                row.get("broker_phone") or row.get("broker_name")
                for row in building_rows
                if row.get("broker_phone") or row.get("broker_name")
            })

            price_stats = read("price_stats", filters={"micro_market": building.get("micro_market")})
            landmarks = read("building_landmarks", filters={"building_id": building["id"]})
            return {
                **building,
                "aliases": aliases,
                "sources": sources,
                "history": history,
                "observed_listings": listings_count,
                "observed_brokers": brokers_count,
                "observed_requirements": requirements_count,
                "price_stats": price_stats,
                "landmarks": landmarks,
                "markets": [],
            }
        except Exception as exc:
            print(f"Error getting building profile: {exc}")
            return None


        try:
            # Get building by database ID
            building_res = self.client.table("buildings").select("*").eq("id", building_db_id).limit(1).execute()
            if not building_res.data:
                return None
            building = building_res.data[0]
            
            # Get aliases
            aliases_res = self.client.table("building_name_aliases").select("*").eq("building_id", building["id"]).execute()
            aliases = aliases_res.data if aliases_res.data else []
            
            # Get enrichment sources
            sources_res = self.client.table("building_enrichment_sources").select("*").eq("building_id", building["id"]).order("enriched_at", desc=True).execute()
            sources = sources_res.data if sources_res.data else []
            
            # Get enrichment history
            history_res = self.client.table("building_enrichment_history").select("*").eq("building_id", building["id"]).order("created_at", desc=True).limit(50).execute()
            history = history_res.data if history_res.data else []
            
            # Get observed counts from the eight typed source tables.  Do not
            # use migration-era projection aliases here: they are not part of
            # the application contract and may be absent after cutover.
            building_rows = [
                row for row in self._fetch_typed_rows(limit_per_table=5000)
                if str(row.get("building_name") or "").strip().lower()
                == str(building.get("canonical_name") or "").strip().lower()
            ]
            listings_count = sum(
                "_requirement" not in str(row.get("_typed_table") or "")
                for row in building_rows
            )
            requirements_count = sum(
                "_requirement" in str(row.get("_typed_table") or "")
                for row in building_rows
            )
            brokers_count = len({
                row.get("broker_phone") or row.get("broker_name")
                for row in building_rows
                if row.get("broker_phone") or row.get("broker_name")
            })
            
            # Get price stats
            price_stats_res = self.client.table("price_stats").select("*").eq("micro_market", building.get("micro_market")).execute()
            price_stats = price_stats_res.data if price_stats_res.data else []
            
            # Get landmarks
            landmarks_res = self.client.table("building_landmarks").select("*,landmarks!inner(*)").eq("building_id", building["id"]).execute()
            landmarks = landmarks_res.data if landmarks_res.data else []
            
            # Get markets
            markets_res = self.client.table("building_landmarks").select("landmarks!inner(micro_market)").eq("building_id", building["id"]).execute()
            market_set = set()
            if markets_res.data:
                for m in markets_res.data:
                    if m.get("landmarks") and m["landmarks"].get("micro_market"):
                        market_set.add(m["landmarks"]["micro_market"])
            markets = list(market_set)
            
            return {
                **building,
                "aliases": aliases,
                "sources": sources,
                "history": history,
                "observed_listings": listings_count,
                "observed_brokers": brokers_count,
                "observed_requirements": requirements_count,
                "price_stats": price_stats,
                "landmarks": landmarks,
                "markets": [{"micro_market": m} for m in markets],
            }
        except Exception as e:
            print(f"Error getting building profile: {e}")
            return None

    def get_broker_summary(self, name: str = "", phone: str = "") -> dict:
        """On-the-fly broker summary from listings table."""
        try:
            empty = {"total_listings": 0, "intents": {}, "top_bhk": [], "markets": [], "price_range_sale": "", "price_range_rent": ""}
            if not name and not phone:
                return empty
            
            # Read the four typed listing tables directly.  The old
            # Typed listing tables are the application read path.
            rows = []
            normalized_phone = re.sub(r"\D", "", phone or "")
            for typed in self._fetch_typed_rows(requirements=False, limit_per_table=5000):
                broker_name = str(typed.get("broker_name") or "")
                broker_phone = re.sub(r"\D", "", str(typed.get("broker_phone") or ""))
                if name and name.lower() not in broker_name.lower():
                    continue
                if normalized_phone and normalized_phone not in broker_phone:
                    continue
                rows.append(self._typed_row_to_legacy(typed))
            
            total = len(rows)
            intents = {}
            bhk_dist = {}
            markets = {}
            prices_sale = []
            prices_rent = []
            
            for r in rows:
                intent = r.get("intent") or ("RENT" if r.get("monthly_rent") is not None else "SELL")
                intents[intent] = intents.get(intent, 0) + 1
                bhk = r.get("bhk") or "?"
                bhk_dist[bhk] = bhk_dist.get(bhk, 0) + 1
                market = r.get("micro_market") or "?"
                markets[market] = markets.get(market, 0) + 1
                price = r.get("monthly_rent") or r.get("total_asking_price") or r.get("price")
                if price is not None:
                    p = float(price)
                    if intent in ("RENT", "LEASE"):
                        prices_rent.append(p)
                    else:
                        prices_sale.append(p)
            
            def _fmt_price_range(prices: list[float]) -> str:
                if not prices:
                    return ""
                prices.sort()
                if len(prices) == 1:
                    return f"AED {prices[0]:,.0f}"
                return f"AED {prices[0]:,.0f} – {prices[-1]:,.0f}"
            
            top_markets = sorted(markets, key=markets.__getitem__, reverse=True)[:3]
            top_bhk = sorted(bhk_dist, key=bhk_dist.__getitem__, reverse=True)[:3]
            
            return {
                "total_listings": total,
                "intents": intents,
                "top_bhk": top_bhk,
                "markets": top_markets,
                "price_range_sale": _fmt_price_range(prices_sale),
                "price_range_rent": _fmt_price_range(prices_rent),
            }
        except Exception as e:
            print(f"Error getting broker summary: {e}")
            return empty

    # ── AI Usage Log (super-admin) ────────────────────────────────

    def get_ai_usage_stats(self, days: int = 7, tenant_id: str | None = None) -> dict:
        """Query ai_usage_log for the admin cost dashboard.

        Returns grouped totals by model×agent, a daily time series, and
        separate waste (truncated/failed) stats.
        """
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        try:
            q = self.client.table("ai_usage_log") \
                .select("agent,model,tokens_input,tokens_output,cost_usd,created_at,tenant_id") \
                .gte("created_at", cutoff)
            if tenant_id:
                q = q.eq("tenant_id", tenant_id)
            res = q.order("created_at", desc=True).limit(50000).execute()
            rows = res.data or []
        except Exception:
            rows = []

        if not rows:
            return {
                "total_cost_usd": 0,
                "total_calls": 0,
                "total_tokens_input": 0,
                "total_tokens_output": 0,
                "by_model_agent": [],
                "daily": [],
                "waste": {"calls": 0, "cost_usd": 0},
            }

        # ── Aggregate by model×agent ──────────────────────────────
        combos: dict[tuple[str, str], dict] = {}
        total_cost = 0.0
        total_in = 0
        total_out = 0
        for r in rows:
            key = (r.get("model") or "unknown", r.get("agent") or "unknown")
            if key not in combos:
                combos[key] = {"model": key[0], "agent": key[1], "calls": 0, "tokens_input": 0, "tokens_output": 0, "cost_usd": 0}
            c = combos[key]
            c["calls"] += 1
            c["tokens_input"] += r.get("tokens_input") or 0
            c["tokens_output"] += r.get("tokens_output") or 0
            c["cost_usd"] += float(r.get("cost_usd") or 0)
            total_cost += float(r.get("cost_usd") or 0)
            total_in += r.get("tokens_input") or 0
            total_out += r.get("tokens_output") or 0

        by_model_agent = sorted(combos.values(), key=lambda x: x["cost_usd"], reverse=True)

        # ── Daily time series ─────────────────────────────────────
        daily: dict[str, dict] = {}
        for r in rows:
            day = (r.get("created_at") or "")[:10]
            if not day:
                continue
            if day not in daily:
                daily[day] = {"date": day, "calls": 0, "cost_usd": 0, "tokens_input": 0, "tokens_output": 0}
            d = daily[day]
            d["calls"] += 1
            d["cost_usd"] += float(r.get("cost_usd") or 0)
            d["tokens_input"] += r.get("tokens_input") or 0
            d["tokens_output"] += r.get("tokens_output") or 0
        daily_list = sorted(daily.values(), key=lambda x: x["date"])

        # ── Waste (truncated calls cost money but produced nothing) ─
        waste_calls = sum(1 for r in rows if r.get("agent") == "extraction" and r.get("tokens_output", 0) == 0)
        waste_cost = sum(float(r.get("cost_usd") or 0) for r in rows if r.get("agent") == "extraction" and r.get("tokens_output", 0) == 0)

        return {
            "total_cost_usd": round(total_cost, 6),
            "total_calls": len(rows),
            "total_tokens_input": total_in,
            "total_tokens_output": total_out,
            "by_model_agent": by_model_agent,
            "daily": daily_list,
            "waste": {"calls": waste_calls, "cost_usd": round(waste_cost, 6)},
        }

    # ── Extraction backlog progress (super-admin) ──────────────────

    def get_semantic_embedding_status(self) -> dict:
        """Return one bounded, database-aggregated semantic index snapshot."""
        now = time.monotonic()
        cached = self._semantic_status_cache
        if cached and now - cached[0] < 60:
            return dict(cached[1])
        try:
            result = self.client.rpc("get_semantic_embedding_status", {})
            if hasattr(result, "execute"):
                result = result.execute()
            data = getattr(result, "data", result)
            if not isinstance(data, dict):
                raise RuntimeError("Semantic embedding status RPC returned an invalid response")
            snapshot = dict(data)
            self._semantic_status_cache = (now, snapshot)
            return snapshot
        except Exception:
            # A status panel should not disappear because a quality snapshot
            # timed out while ingestion is busy. This is still real data; the
            # timestamp in the payload tells the operator it is last-known.
            if cached and now - cached[0] < 900:
                return dict(cached[1])
            # Keep the panel useful when the heavyweight quality RPC is slow:
            # these bounded counts are the same live queue/vector sources the
            # worker uses, without the RPC's full structural-quality joins.
            def count_rows(table: str, **filters: object) -> int:
                query = self.client.table(table).select("id", count="exact")
                for key, value in filters.items():
                    query = query.eq(key, value)
                result = query.limit(1).execute()
                return int(getattr(result, "count", 0) or 0)

            total = count_rows("semantic_embedding_jobs")
            pending = count_rows("semantic_embedding_jobs", status="pending")
            running = count_rows("semantic_embedding_jobs", status="running")
            completed = count_rows("semantic_embedding_jobs", status="completed")
            failed = count_rows("semantic_embedding_jobs", status="failed")
            vectors = count_rows("semantic_embeddings")
            return {
                "jobs": {"total": total, "pending": pending, "running": running,
                         "completed": completed, "failed": failed, "exhausted": 0},
                "vectors": {"total": vectors, "model_count": 0},
                "model": "unknown",
                "dimensions": 1024,
                "last_completed_at": None,
                "last_stored_at": None,
                "latest_failure": None,
                "by_entity": [],
                "quality": {"expected_entities": total, "indexed_entities": vectors,
                             "coverage_pct": min(100, (100 * vectors / total) if total else 0),
                             "stale_entities": 0, "orphan_vectors": 0,
                             "unresolved_jobs": None, "invalid_documents": 0,
                             "duplicate_model_rows": 0,
                             "alias_checks": {"building_aliases": {"tested": 0, "hit_at_5": 0, "hit_rate_at_5": 0},
                                              "broker_aliases": {"tested": 0, "hit_at_5": 0, "hit_rate_at_5": 0}}},
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "degraded": True,
            }

    def list_semantic_retrieval_eval_cases(self, active_only: bool = False) -> list[dict]:
        query = (
            self.client.table("semantic_retrieval_eval_cases")
            .select("*")
            .order("id", desc=False)
        )
        if active_only:
            query = query.eq("active", True)
        result = query.execute()
        return list(result.data or [])

    def create_semantic_retrieval_eval_case(self, values: dict) -> dict:
        result = self.client.table("semantic_retrieval_eval_cases").insert(values).execute()
        rows = result.data or []
        if not rows:
            raise RuntimeError("Semantic retrieval evaluation case was not created")
        return dict(rows[0])

    def delete_semantic_retrieval_eval_case(self, case_id: int) -> bool:
        result = (
            self.client.table("semantic_retrieval_eval_cases")
            .delete()
            .eq("id", int(case_id))
            .execute()
        )
        return bool(result.data)

    def record_semantic_retrieval_eval_run(self, summary: dict) -> dict:
        result = self.client.table("semantic_retrieval_eval_runs").insert({
            "model": summary.get("model"),
            "summary": summary,
        }).execute()
        rows = result.data or []
        return dict(rows[0]) if rows else {}

    def get_latest_semantic_retrieval_eval_run(self) -> dict | None:
        result = (
            self.client.table("semantic_retrieval_eval_runs")
            .select("*")
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        row = (result.data or [None])[0]
        if not row:
            return None
        summary = dict(row.get("summary") or {})
        summary["run_id"] = row.get("id")
        summary["created_at"] = row.get("created_at")
        return summary

    def get_extraction_progress(
        self,
        rate_window_hours: int = 24,
        tenant_id: str | None = None,
    ) -> dict:
        """Super-admin view of the extraction backlog drain.

        Pure live counts — never fabricated. ``processed`` is derived from
        raw_messages, ``est_cost_usd`` reuses the internal ai_usage_log
        pricing estimate (not an external bill).
        """
        # Keep all dashboards on one server-side aggregate. The previous
        # implementation performed several exact REST scans and downloaded
        # the entire AI usage log on every refresh.
        try:
            rpc = self.client.rpc("get_extraction_progress", {
                "p_hours": max(1, int(rate_window_hours)),
                "p_tenant_id": tenant_id or None,
            })
            # PropAI's REST client returns decoded JSON directly; the
            # supabase-py-compatible client returns an execute() builder.
            if hasattr(rpc, "execute"):
                rpc = rpc.execute()
            data = getattr(rpc, "data", rpc)
            # PostgREST clients normally expose a JSONB object directly, but
            # some deployed client/schema-cache combinations wrap a single
            # JSONB return row in a one-item list. Accept both shapes without
            # reintroducing the old exact-count fallback scans.
            if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
                data = data[0]
            if isinstance(data, dict):
                result = dict(data)
                result["tenant_id"] = tenant_id
                result["processed_recent_%dh" % rate_window_hours] = result.get("processed_recent", 0)
                return result
            _logger.error(
                "Canonical extraction progress RPC returned unexpected data type: %s",
                type(data).__name__,
            )
        except Exception as exc:
            # Never fall back to several count="exact" PostgREST scans. On a
            # large raw_messages table that fallback creates an I/O stampede
            # which starves unrelated profile and WhatsApp control requests.
            # The canonical migration is mandatory; callers can retain their
            # last real snapshot while this endpoint reports unavailable.
            _logger.exception("Canonical extraction progress RPC failed")
            raise RuntimeError("Canonical extraction progress RPC is unavailable") from exc
        raise RuntimeError("Canonical extraction progress RPC returned an invalid response")

    def get_workspace_extraction_progress(
        self,
        rate_window_hours: int = 24,
        tenant_id: str | None = None,
    ) -> dict:
        """Return exact workspace queue metrics without unrelated telemetry scans.

        Broker-facing dashboards only need the raw ledger state. Keep AI usage
        and extraction-cache aggregates on the super-admin RPC so a busy
        telemetry query cannot make the live WhatsApp dashboard unavailable.
        """
        rpc = self.client.rpc("get_workspace_extraction_progress", {
            "p_hours": max(1, int(rate_window_hours)),
            "p_tenant_id": tenant_id or None,
        })
        if hasattr(rpc, "execute"):
            rpc = rpc.execute()
        data = getattr(rpc, "data", rpc)
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            data = data[0]
        if isinstance(data, dict):
            return dict(data)
        raise RuntimeError("Workspace extraction progress RPC returned an invalid response")
