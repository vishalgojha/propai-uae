"""Structured extraction pipeline for broker WhatsApp messages.

Provider rotation uses the same deployment-configured chain as chat.

The pipeline does a deterministic document pass first:
1) reconstruct the message into logical listing blocks
2) classify the document
3) pass the reconstructed document to the model for field extraction

The model still owns the final structured fields, but it no longer sees a
flat blob of text with no document shape.

Usage:
    from ai_extraction import ai_extract
    result = await ai_extract(raw_text, ctx)
    if result["extraction_source"] == "ai":
        # Use result["extraction"] (the structured schema)
    else:
        # No structured extraction was produced; queue for review.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

from openai import OpenAI
from llm import get_configured_providers
from deterministic_splitters import split_message_into_chunks
from extraction_models import validate_source_semantics
from extraction_quality import building_name_problem
from price_normalization import canonical_price_aed, source_transaction_type
from agents.building_alias_engine import fuzzy_score

_logger = logging.getLogger(__name__)

# Reference data is effectively read-mostly.  Without a short-lived cache the
# extraction hot path performs a building-alias query for every WhatsApp
# message (and locality fallback can perform three more queries).  Keep these
# caches process-local and bounded; writes become visible after the TTL.
_REFERENCE_CACHE_TTL_SECONDS = max(30.0, float(os.getenv("EXTRACTION_REFERENCE_CACHE_TTL_SECONDS", "300")))
_REFERENCE_CACHE_MAX_TENANTS = 32
_REFERENCE_CACHE_LOCK = Lock()
_ALIAS_ROWS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_BUILDING_LOCALITY_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}

_BULK_INVENTORY_RE = re.compile(
    r"(?i)\b(?:direct\s+inventor(?:y|ies)|signature\s+spaces|property\s+portfolio|"
    r"multiple\s+(?:properties|options)|all\s+properties)\b"
)
_BULK_FOOTER_RE = re.compile(
    r"(?im)^\s*(?:[*_~\W]*)(?:client\s+profile\s+required|"
    r"for\s+more\s+details\s+and\s+inspections|"
    r"gurukirpa\s+realtors|harkirat\s+singh)\b"
)


def _trim_bulk_footer(text: str) -> str:
    """Keep broker signatures/CTA text out of the final listing block.

    The complete WhatsApp message remains in ``raw_text`` evidence. This only
    trims the extraction slice so phrases such as ``client profile required``
    cannot turn an inventory broadcast into a requirement.
    """
    value = (text or "").strip()
    match = _BULK_FOOTER_RE.search(value)
    if match and match.start() > 0:
        return value[:match.start()].rstrip(" \t\n-_*~")
    return value


def _coerce_float(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        for key in ("amount", "value", "number", "count", "min", "max"):
            if key in value:
                coerced = _coerce_float(value.get(key))
                if coerced is not None:
                    return coerced
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            coerced = _coerce_float(item)
            if coerced is not None:
                return coerced
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {".", "-", "+", "null", "none"}:
        return None
    if re.search(r"\.{2,}", text):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except (TypeError, ValueError):
        return None


def _coerce_int(value) -> int | None:
    coerced = _coerce_float(value)
    return int(coerced) if coerced is not None else None


# ── Provider configuration ────────────────────────────────────────────

# Chat and WhatsApp share one provider chain; extraction intentionally
# diverges from it.  Structured field extraction needs precision, not deep
# reasoning, and premium models (grid/merge) cost 15-30x more per token.
# Small fast models are therefore tried first and premium ones are kept as
# an escalation fallback for when every cheap provider fails or returns
# malformed JSON.  Set EXTRACTION_MODEL (e.g. "llama-3.1-8b-instant") to
# pin a specific model ahead of all others; otherwise any non-premium model
# present in the chain is preferred.
_PROVIDERS: list[dict] = list(get_configured_providers())


def _append_extraction_provider(
    providers: list[dict],
    *,
    env_prefix: str,
    name: str,
    default_base_url: str,
) -> None:
    """Append an extraction-only OpenAI-compatible provider, when configured.

    These credentials are intentionally separate from the chat provider chain:
    a temporary backlog-drain budget must not be consumed by interactive chat.
    """
    api_key = os.getenv(f"{env_prefix}_API_KEY", "").strip()
    model = os.getenv(f"{env_prefix}_MODEL", "").strip()
    base_url = os.getenv(f"{env_prefix}_BASE_URL", default_base_url).strip()
    if api_key and model:
        providers.append({
            "name": name,
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "reasoning_effort": "none",
        })
    elif api_key or model:
        _logger.warning(
            "Skipping extraction provider %s: set both %s_API_KEY and %s_MODEL",
            name,
            env_prefix,
            env_prefix,
        )


# Doubleword is the dedicated extraction provider. Merge remains available
# to the separate chat/provider chain and must not consume its extraction key.
_append_extraction_provider(
    _PROVIDERS,
    env_prefix="EXTRACTION_DOUBLEWORD",
    name="extraction-doubleword",
    default_base_url="https://api.doubleword.ai/v1",
)

# Append Gemini as a fallback provider (used when MERGE key is exhausted).
# Checks ENRICHMENT_GEMINI_KEY first (scoped for enrichment/extraction),
# then falls back to GEMINI_API_KEY (production key).
_gemini_key = os.getenv("ENRICHMENT_GEMINI_KEY") or os.getenv("GEMINI_API_KEY", "")
if _gemini_key:
    _PROVIDERS.append({
        "name": "gemini",
        "provider": "gemini",
        "api_key": _gemini_key,
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.1-flash-lite",
    })

_EXTRACTION_MODEL = os.getenv("EXTRACTION_MODEL", "").strip().lower()
try:
    _EXTRACTION_PROVIDER_TIMEOUT = max(
        30, int(os.getenv("EXTRACTION_PROVIDER_TIMEOUT_SECONDS", "180"))
    )
except ValueError:
    _logger.warning(
        "Invalid EXTRACTION_PROVIDER_TIMEOUT_SECONDS; using 180 seconds"
    )
    _EXTRACTION_PROVIDER_TIMEOUT = 180
_PREMIUM_MODEL_HINTS = (
    "claude", "opus", "sonnet", "haiku",
    "code-max", "code_max", "text-max", "text_max",
    "gpt-", "o1", "o3", "o4",
)


def _extraction_provider_priority(provider: dict) -> int:
    """Sort key used to order extraction providers cheap-first.

    Tier 0 = pinned/preferred cheap model, tier 1 = any other non-premium
    model, tier 2 = premium escalation.  The sort is stable, so within a
    tier the original chain order is preserved and round-robin still
    distributes load evenly across equal-cost providers.
    """
    model = (provider.get("model") or "").lower()
    if _EXTRACTION_MODEL and _EXTRACTION_MODEL in model:
        return 0
    if any(hint in model for hint in _PREMIUM_MODEL_HINTS):
        return 2
    return 1 if _EXTRACTION_MODEL else 0


_PROVIDERS.sort(key=_extraction_provider_priority)

# Round-robin pointer
_rr_index = 0
_rr_lock = __import__("threading").Lock()
_provider_cooldowns: dict[str, float] = {}
_provider_cooldown_lock = Lock()


def _response_headers(value) -> dict[str, str]:
    """Return the small set of rate-limit headers exposed by an SDK object."""
    headers = getattr(value, "headers", None)
    if headers is None:
        response = getattr(value, "response", None)
        headers = getattr(response, "headers", None)
    if not headers:
        return {}
    wanted = (
        "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining",
        "x-ratelimit-reset", "ratelimit-limit", "ratelimit-remaining",
        "ratelimit-reset",
    )
    return {
        key: str(headers[key])
        for key in wanted
        if key in headers
    }


def _retry_after_seconds(headers: dict[str, str]) -> float:
    raw = headers.get("retry-after", "")
    try:
        return max(1.0, min(120.0, float(raw)))
    except (TypeError, ValueError):
        return 5.0


def _wait_for_provider_cooldown(provider_name: str) -> None:
    with _provider_cooldown_lock:
        remaining = _provider_cooldowns.get(provider_name, 0.0) - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _cooldown_provider(provider_name: str, seconds: float) -> None:
    with _provider_cooldown_lock:
        _provider_cooldowns[provider_name] = max(
            _provider_cooldowns.get(provider_name, 0.0),
            time.monotonic() + seconds,
        )


def _next_provider() -> dict | None:
    global _rr_index
    if not _PROVIDERS:
        return None
    with _rr_lock:
        p = _PROVIDERS[_rr_index]
        _rr_index = (_rr_index + 1) % len(_PROVIDERS)
    return p


# ── Extraction prompt ─────────────────────────────────────────────────

# Runtime extraction uses `_UNIFIED_EXTRACTION_PROMPT` for the first pass so a
# message can be classified as mixed and produce per-item rough routes. Each
# rough item is then re-extracted with `_get_extraction_prompt()` using its own
# focused route. The unified prompt remains the fallback when a focused pass
# cannot be selected or fails.

# ── Schema validation ─────────────────────────────────────────────────

_PRICE_PARSING_INSTRUCTIONS = """PRICE PARSING — CRITICAL:
- Convert explicit units to absolute dirhams: 1 M = 1000000 and K = 1000.
- UAE broker shorthand: M/mn/million mean million dirhams; K/k/thousand mean
  thousand dirhams. Thus 1.5M = 1500000, 850K = 850000, and 95k = 95000.
  Preserve the source spelling in raw_price_text.
- “2.5M” means 2500000, never 2.5 or 250000.
- “85K” means 85000; rents are ANNUAL totals unless explicitly marked "/month".
- “1.5.M”, “2:25M”, and “95.K” use punctuation as a separator: parse them as 1.5M, 2.25M, and 95K.
- Preserve raw_price_text exactly as written in the source.
- For PSF/per-sqft quotes use unit “per_sqft” and keep amount as the per-sqft rate; otherwise use unit “total”.
- Never infer a price from unrelated numbers such as floor, parking, area, or phone numbers."""

# This is a compact, production-facing subset of the UAE broker glossary.
# Keep high-confidence dialect rules here; the full research document belongs
# in docs, not in every provider request. Deterministic guards remain the
# authority for values that can be normalized without an LLM.
_BROKER_GLOSSARY = """UAE BROKER DIALECT — FOLLOW STRICTLY:
- “lease” / “on lease” / “for rent” in a property context means RENT (an annual
  AED total unless marked "/month"), not a long-term contract.
- “outright” and the broker typo “outrate” mean SALE.
- “preleased” / “pre-rented” is SALE with an existing tenant; any rent stated is current tenant yield, not asking rent.
- “sale & rent” or “sale or lease” can describe both availability modes; preserve both in deal_tags and never silently convert one price into the other.
- “budget”, “urgent requirement”, “required”, “looking for”, or “client needs” indicate a REQUIREMENT; budget is not listing price.
- “nego” means negotiable; “nnego” is not a recognized term. “final” means fixed/non-negotiable.
- “cpt” means carpet area; “bup” means built-up area. In NUMBER @ NUMBER, first is area sqft and second is price only when the line is clearly a property price line.
- “Studio” is not “1BR”. Keep BR/configuration as text exactly as written.
- “chiller free” means the tenant does not pay AC/chiller charges; preserve it as an amenity/deal fact.
- Payments are commonly made via post-dated cheques: “4 cheques”, “6 chqs”, or "1 cheque" describes the payment plan, not the annual amount. Standalone “+1” / “My +1” means co-brokered.
- “builder finish”, “bare shell”, “warm shell”, and “untouched” are furnishing/fitout facts, not transaction types.
- “brand new building” / “new building” is a property-condition fact. Preserve it as the `brand_new_building` deal tag (and use the appropriate age/fitout field when the route exposes one). Do not treat it as a listing boundary or discard it as boilerplate.
- “company lease” means company-paid residential tenancy in residential context, and company as tenant in commercial context.
- Extract tenant preferences such as family, bachelors, working, student, company lease, and expat as facts; do not filter or omit them.
- Floors: ground/GF/G is street level; podium levels sit above ground; 1st floor is one level above ground.
- Never fabricate or estimate a price. If a unit is genuinely ambiguous, preserve raw_price_text and set needs_review=true.
- If multiple independent listings remain, return one item per listing; never collapse them into one item."""


def _classify_message_flags(text: str) -> tuple[str, str, bool]:
    """Classify the extraction route before asking an LLM for fields.

    This is deliberately conservative: a marketing phrase such as “looking
    for the perfect office” is not a requirement unless the message actually
    asks someone to source a property.
    """
    value = (text or "").lower()
    demand = re.search(
        r"\b(?:urgent\s+)?(?:requirement|required|wanted|want|need|needed|seeking|looking\s+for|looking\s+to\s+(?:buy|rent)|client\s+(?:needs?|is\s+looking)|buyer\s+required|tenant\s+required)\b",
        value,
    )
    supply = re.search(
        r"\b(?:available|inventory|direct\s+listing|for\s+(?:rent|sale)|rent\s*[-:]|sale\s*[-:]|asking|outright|inspection|carpet\s+area|possession)\b",
        value,
    )
    separator_count = len(re.findall(r"(?m)^\s*[━─]{3,}\s*$", text or ""))
    repeated_inventory_markers = len(re.findall(
        r"(?i)\b(?:rent|sale|carpet|area)\s*[-:–]", value
    ))
    bulk_inventory = bool(_BULK_INVENTORY_RE.search(value)) and (
        separator_count >= 2 or repeated_inventory_markers >= 3
    )
    # A footer may say that a client profile is required before a viewing. It
    # is an instruction attached to a supply broadcast, not buyer demand.
    is_requirement = bool(demand and not (supply and demand.start() > supply.start()))
    if bulk_inventory:
        is_requirement = False

    investor_unit_commercial = bool(
        re.search(r"\binvestor\s+unit\b", value)
        and re.search(r"\b(?:lease|rent|rental)\b", value)
        and re.search(r"\bpremises\b", value)
        and not re.search(
            r"\b(?:\d+(?:\.\d+)?\s*(?:bhk|rk)|flat|apartment|residential|villa|bungalow|independent\s+(?:house|home))\b",
            value,
        )
    )
    commercial = bool(re.search(
        r"\b(?:office|shop|showroom|warehouse|godown|industrial|retail|commercial|hotel|hospitality|restaurant|banquet|lodging|bare\s*shell|warm\s*shell|plug[- ]and[- ]play|chargeable\s+area|ceiling\s+height|mezzanine|cabin|workstation|conference\s+room|cam|lease\s+deed|power\s+load|food\s+court|otla)\b",
        value,
    )) or investor_unit_commercial
    rent = bool(re.search(
        r"\b(?:rent|rental|lease|monthly|per\s+month|deposit|tenancy|lock[- ]in|notice\s+period|lease\s+out)\b",
        value,
    ))
    sale = bool(re.search(
        r"\b(?:sale|sell|buy|purchase|outright|outrate|asking|quote|sale\s+price)\b",
        value,
    ))
    if is_requirement and rent:
        transaction = "rent"
    elif is_requirement and sale:
        transaction = "sale"
    elif rent and not sale:
        transaction = "rent"
    elif sale and not rent:
        transaction = "sale"
    elif rent and sale:
        transaction = "rent" if value.find("rent") < value.find("sale") else "sale"
    else:
        transaction = "sale"
    return ("commercial" if commercial else "residential", transaction, is_requirement)


def classify_message_type(text: str) -> tuple[str, str]:
    """Return the deterministic ``(asset_type, transaction_type)`` route."""
    asset, transaction, is_requirement = _classify_message_flags(text)
    return asset, ("requirement" if is_requirement else transaction)


_FOCUSED_FIELDS = {
    ("residential", "sale", False): "bhk, original_bhk, current_bhk, configuration_type, configuration_details, is_converted_unit, is_combination_unit, can_sell_separately, carpet_area_sqft, built_up_area_sqft, super_built_up_area_sqft, balcony_area_sqft, balcony_area_raw_text, terrace_area_sqft, covered_terrace_area_sqft, terrace_area_raw_text, sellable_area_sqft, price, price_basis, price_math, locality, building_name, wing, furnishing_status, unit_condition, availability_status, possession_status, possession_date, bathroom_count, car_parking_count, parking_type, parking_details, floor_range, floor_min, floor_max, floor_label, property_view, view_description, vastu_compliant, age_of_property, building_amenities, amenities, amenities_unverified_claim, brokerage_type, brokerage_context, co_brokered, token_amount, payment_plan, society_restrictions, society_restrictions_raw, showing_instructions, contact_instructions, broker_company, contacts, unstructured_facts, deal_tags, title",
    ("residential", "rent", False): "bhk, original_bhk, current_bhk, configuration_type, configuration_details, is_converted_unit, is_combination_unit, carpet_area_sqft, built_up_area_sqft, balcony_present, balcony_area_sqft, balcony_area_raw_text, terrace_area_sqft, covered_terrace_area_sqft, terrace_area_raw_text, sit_out_present, price, locality, building_name, furnishing_status, unit_condition, availability_status, availability_date_raw, available_from, possession_status, bathroom_count, car_parking_count, parking_type, parking_details, floor_range, floor_min, floor_max, floor_label, wing, has_lift, building_amenities, amenities, amenities_unverified_claim, property_view, view_description, deposit_amount, deposit_months, deposit_raw_text, pet_policy, tenant_type_preference, sharing_allowed, food_preference, lease_term_type, lease_term_min_months, lease_term_max_months, lease_term_raw_text, lock_in_period_months, notice_period_months, brokerage_type, brokerage_context, brokerage_terms_raw, plus_one_deal, fee_sharing_required, client_profile_required, society_restrictions, society_restrictions_raw, broker_company, contacts, company_lease_criteria, showing_instructions, contact_instructions, unstructured_facts, deal_tags, title",
    ("commercial", "sale", False): "commercial_use_type, carpet_area_sqft, built_up_area_sqft, chargeable_area_sqft, super_built_up_area_sqft, saleable_area_sqft, price, price_basis, price_math, locality, building_name, fitout_status, occupancy_status, ceiling_height, floor_level, floor_range, car_parking_count, power_load_kw, cabin_count, director_cabin_count, ceo_cabin_present, cubicle_count, workstation_count, conference_room_count, meeting_room_count, washroom_count, pantry_type, reception_area, server_room, storage_area, has_central_ac, has_power_backup, has_lift, terrace_area_sqft, covered_terrace_area_sqft, terrace_area_raw_text, frontage_ft, entrance_count, permitted_use_types, ideal_for, project_inventory, area_min_sqft, area_max_sqft, floor_plate_sqft, project_status, building_amenities, broker_rera_number, brokerage_type, deal_tags, title",
    ("commercial", "rent", False): "commercial_use_type, carpet_area_sqft, built_up_area_sqft, chargeable_area_sqft, mezzanine_area_sqft, area_raw_text, price, price_basis, price_math, locality, building_name, fitout_status, ceiling_height, floor_level, floor_range, deposit_amount, deposit_months, deposit_raw_text, cam_amount, cam_applicable, cam_unit, power_load_kw, cabin_count, director_cabin_count, ceo_cabin_present, cubicle_count, workstation_count, conference_room_count, conference_room_capacity, meeting_room_count, meeting_room_capacity, training_room_capacity, cafeteria_seat_count, washroom_count, pantry_type, reception_area, server_room, storage_area, accounts_area, lounge_area, terrace_area_sqft, covered_terrace_area_sqft, terrace_area_raw_text, frontage_ft, entrance_count, otla_area_sqft, otla_area_raw_text, heritage_space, permitted_use_types, ideal_for, automatic_shutter_count, room_count, suite_count, banquet_hall_count, restaurant_count, bar_facility, operational_status, rent_inclusions, possession_status, possession_date, availability_status, inspection_notice_minutes, license_type, short_term_allowed, lease_term_type, lock_in_period_months, notice_period_months, escalation_pct, escalation_frequency, rent_free_period_months, fitout_period_months, lease_deed_type, sub_leasing_allowed, building_amenities, broker_rera_number, brokerage_type, deal_tags, needs_review, title",
    ("residential", "sale", True): "bhk_options, budget_min, budget_max, area_min_sqft, area_max_sqft, locality_options, building_preferences, furnishing_preference, possession_preference, car_parking_min, buyer_type, transaction_nature, urgency, is_flexible, deal_tags, needs_review, title",
    ("residential", "rent", True): "bhk_options, configuration_preference, budget_min, budget_max, area_min_sqft, area_max_sqft, carpet_area_min_sqft, carpet_area_max_sqft, built_up_area_min_sqft, built_up_area_max_sqft, locality_options, building_preferences, furnishing_preference, possession_preference, age_preference, floor_preference, view_preference, deposit_budget_max, tenant_type, nationality, has_pets, sharing_acceptable, food_preference, car_parking_min, amenity_requirements, lease_term_preference, company_lease_criteria, brokerage_willingness, urgency, is_flexible, deal_tags, needs_review, title",
    ("commercial", "sale", True): "commercial_use_type, area_min_sqft, area_max_sqft, budget_min, budget_max, budget_per_sqft_max, locality_options, fitout_preference, car_parking_min, needs_mezzanine, needs_lift, needs_power_backup, needs_central_ac, min_power_load_kw, buyer_type, urgency, is_flexible, deal_tags, needs_review, title",
    ("commercial", "rent", True): "commercial_use_type, intended_use_details, area_min_sqft, area_max_sqft, area_basis_preference, budget_min, budget_max, budget_per_sqft_max, budget_includes_maintenance, locality_options, location_flexibility, fitout_preference, floor_min, floor_max, floor_count_max, floor_preference, consecutive_floors_required, car_parking_min, parking_required, needs_attached_washroom, needs_washroom, needs_pantry, needs_mezzanine, needs_lift, needs_power_backup, needs_central_ac, power_requirements, premium_building_required, glass_facade_required, residential_cum_commercial_ok, by_lanes_accepted, entrance_requirement, signage_required, loading_access_required, min_cabin_count, min_workstation_count, needs_conference_room, deposit_budget_max, lease_term_preference, max_lock_in_months, max_notice_period_months, company_type, team_size, media_requested, urgency, brokerage_context, brokerage_terms_raw, contacts, is_flexible, deal_tags, title",
}


_RESIDENTIAL_SALE_EXTRACTION_RULES = """
Residential sale listing rules:
- Bulk broadcasts: emit one item per property row/block. If a heading such as
  "3 BHK FOR SALE" or "ANDHERI WEST" applies to following blocks, carry that
  context into each item that uses it. Do not let one block's facts leak into
  another unrelated block.
- A section heading such as "Cuffe Parade / Nariman Point / Colaba" or "New
  Listings added" is context, not a property item or title. Item-specific
  locality overrides the shared heading. A generic/anonymized label such as
  "Cuffe Parade - Premium Tower", "Confidential Building", or "New Project"
  is not a building name: keep building_name null and retain any useful words
  only as unstructured facts. Never manufacture a building identity.
- Source evidence: each item should be faithful to its listing slice. Shared
  footer contact/company details may be copied to every item, but broker footer
  text must not become building, locality, price, requirement, or title data.
  Company/person/RERA/footer lines and broker instructions such as "allow 24
  hrs", "client profile needed", and "for details/visits" are never listings.
- "+1": "Available in+1", "available in +1", "my +1", or standalone "+1"
  means co-brokered. Set co_brokered=true and brokerage_context="+1". Never
  treat this as floor, area, deposit, price, or BHK.
- Terrace/balcony: never add balcony or terrace area into carpet_area_sqft.
  Use balcony_area_sqft/balcony_area_raw_text and
  terrace_area_sqft/covered_terrace_area_sqft/terrace_area_raw_text. Preserve
  the full wording in area_raw_text.
- Price math: if a PSF quote and explicit sellable/chargeable area are stated,
  set sellable_area_sqft, computed_total_asking_price, computed_price_confidence,
  and price_math with formula/inputs/source. If only carpet plus terrace is
  stated and no sellable area is stated, do not assume terrace weighting; record
  the areas and leave computed_total_asking_price null or low-confidence.
- Contacts: extract every explicit contact number, up to 8, into contacts.
  Preserve associated person/company names when present. broker_phone is only
  the primary contact; contacts should keep the additional team numbers.
- Society restrictions: extract explicit diet/community/religion/society
  conditions exactly as written into society_restrictions_raw and canonical
  tags in society_restrictions. Never infer these restrictions.
- Showing/access: "1 day notice", "key with me", "for inspection contact",
  "call for details" and similar operational instructions belong in
  showing_instructions or contact_instructions.
- Unit configuration: "3+2 BHK combination" means is_combination_unit=true and
  configuration_details. "3BHK converted into 2BHK" means
  is_converted_unit=true, original_bhk=3, current_bhk=2, bhk/current_bhk=2.
- Wing/floor: "G wing" means wing="G". "below 10th floor" means
  floor_label="below 10th floor" and floor_max=9. "higher floor" means
  floor_label="higher floor"; do not invent a floor number.
- Views/vastu: keep canonical searchable view in property_view when obvious,
  but preserve rich wording in view_description. "vastu compliant" means
  vastu_compliant=true; do not put it in orientation.
"""


_RESIDENTIAL_RENT_EXTRACTION_RULES = """
Residential rent listing rules:
- Bulk broadcasts: emit one item per independently actionable apartment. Carry
  a shared BHK, locality, or contact footer into each item only when the source
  structure clearly applies it. Do not let one item's price, area, or tenant
  rule leak into another item.
- A normal apartment rental is supported. PG, hostel, paying-guest, dormitory,
  co-living, room-sharing, and bed-by-bed offers are not supported inventory;
  do not emit typed listing items for them.
- Residential rents are ANNUAL AED totals unless explicitly marked "/month".
  85K=85000/year, 120k=120000/year, and 1.5M=1500000/year. Preserve the exact
  raw text and normalize to absolute annual dirhams. This rule does not apply
  to per_sqft rates, sale prices, deposits, maintenance, parking charges, or
  other fees. If the context is unclear, preserve the source quote and set
  needs_review=true.
- Lease language means RENT. Extract lease duration separately from lock-in:
  “3/5 years lease” is lease_term_min_months=36 and lease_term_max_months=60,
  while lock_in_period_months is only for an explicit lock-in period.
- Deposits: “5% deposit” or “10%” of annual rent and “N months deposit”
  wording both occur; a flat amount such as “10K deposit” populates
  deposit_amount. Preserve deposit_raw_text. Do not invent a deposit amount
  when only months or a percentage is given.
- Residential rent is normally maintenance/CAM inclusive. Do not invent or
  split residential maintenance or CAM fields.
- Tenant rules are facts: preserve family, bachelor, expat, company lease,
  pets, vegetarian, or similar wording. Do not turn them into requirements or
  silently omit them.
- Balcony, terrace, and sit-out are separate from carpet area. Never add them
  to carpet_area_sqft. Preserve raw wording and use the dedicated area fields.
- “No lift” and “No car park” are explicit negatives: set has_lift=false or
  car_parking_count=0. Missing information remains null.
- Views and vague claims: store a canonical property_view only when clear and
  preserve rich wording in view_description or unstructured_facts. “All
  amenities” remains an unverified claim; do not invent an amenity list.
- Contacts: extract every explicit phone number, up to 8, with associated
  person/company names. broker_phone is only the primary contact; contacts
  retains the team numbers.
- Indian brokerage language: “+1”, “plus one”, “sharing”, “joint deal”, or
  “mandate plus one” means a fee-sharing arrangement. Set plus_one_deal=true
  and fee_sharing_required=true, preserve brokerage_terms_raw, and never infer
  the fee percentage. “Mandate” alone is not exclusive; only explicit
  “exclusive mandate” means exclusive.
- Company lease criteria such as MNCs, consulates, paid-up capital, client
  profile required, or corporate tenant preference belong in
  company_lease_criteria, client_profile_required, tenant_type_preference, or
  unstructured_facts. Preserve the exact source wording.
- Typos: preserve the broker's original text. Resolve a known locality alias
  for search only when the match is strong and context supports it; never
  fabricate a building name. “Building name: please call” remains null.
- Availability dates without an unambiguous year should be preserved in
  availability_date_raw; do not invent a year. “Immediate” belongs in
  availability_status.
- A message saying sale and lease for the same property preserves both modes;
  do not silently choose one. A sale-priced million-dirham quote without rent language
  is not rent, even if a heading contains a typo.
- Same-inventory matching across brokers is not extraction. Preserve each
  broker's observation and source-specific facts; never merge or suppress it.
"""


_COMMERCIAL_RENT_EXTRACTION_RULES = """
Commercial rent listing rules:
- Commercial supply includes offices, shops, retail/showrooms, restaurants/cafes,
  warehouses/godowns/sheds, hotels/guest houses, and similar business premises.
  Preserve the specific use in commercial_use_type and permitted_use_types.
- Extract one independently actionable unit or floor-level option per item. A
  message with multiple floors and distinct rates becomes linked items; keep
  each floor's own area and rate. A project availability range is one project
  inventory item with area_min_sqft/area_max_sqft, not fabricated individual
  offices.
- Area basis matters: keep carpet_area_sqft, built_up_area_sqft, and
  chargeable_area_sqft separate. Treat “loft” and “mezzanine” as
  mezzanine_area_sqft; never add it to carpet or chargeable area. A per-sqft rent explicitly quoted on chargeable
  area must use chargeable_area_sqft; on carpet area use carpet_area_sqft. If
  the basis and total are clear, compute monthly_rent and preserve price_math
  with rate, basis, area, and formula. Never silently use carpet area for a
  chargeable-area quote.
- Commercial rent is usually an ANNUAL AED total; treat a bare quote such as
  “450K” as 450000/year unless "/month" is explicit. PSF rates are per square
  foot per year. Normalize “pkg”, “package”, “pckg”, and “packg” to ordinary
  rent; do not turn them into deposit, CAM, or a 1% charge.
  Preserve price.raw_price_text and set price_basis="annual" when the source
  gives only a package amount. CAM is separate only when explicitly stated.
- Deposits: “6 months deposit” sets deposit_months; a flat amount sets
  deposit_amount. Do not derive a deposit amount from months. Keep deposit_raw_text.
- Capture office capacity and facilities when stated: workstations, cabins,
  director/CEO cabins, cubicles, conference/meeting/training room capacities,
  cafeteria seats, pantry, reception, server/storage/accounts/lounge areas,
  washrooms, parking, lift, power backup, and central AC.
- Capture commercial-specific facts such as terrace/otla areas, covered terrace,
  frontage, entrance count, ceiling height, automatic shutters, heritage space,
  short-term/leave-and-license terms, inspection notice, operational hotel
  facilities, and rent inclusions. Do not fold terrace/otla into carpet area.
- “L&L” means leave-and-license; store license_type accordingly. “PKG/package”
  is not an old-style deposit calculation.
- Broker RERA numbers such as “RERA NO - A51900002370”, “MahaRERA”, or
  “RERA: ...” belong in broker_rera_number. Keep property/project RERA separate.
- Preserve typos in raw evidence, but normalize obvious search aliases only in
  search-oriented fields. Do not invent prices, areas, amenities, or building
  names. Keep missing building_name null.
- Locality must use {"raw_mention": ..., "resolved_locality": ..., "confidence": ...};
  never emit a `normalized` key. Use null for unresolved canonical locality.
- `needs_review` is required when a price/unit, area basis, or other material fact
  is ambiguous. Furnished, semi-furnished, bare-shell, and builder-finish are
  `fitout_status` values, never `deal_tags`. Deal tags must use only the documented
  whitelist.
- When a building is named, make the title specific using only the building
  name present in the current source message. Never use a remembered or
  illustrative building name in a title.
- Deal-source facts such as “deal side by side only”, “mandate”, “plus one”,
  and brokerage wording belong in brokerage_context/brokerage_terms_raw or
  unstructured_facts; do not merge inventory across different brokers.
"""


_COMMERCIAL_SALE_EXTRACTION_RULES = """
Commercial sale listing rules:
- Commercial sale supply includes offices, shops, showrooms, retail, restaurants,
  warehouses, and developer/project inventory. Preserve commercial_use_type.
- A message with multiple options under one building is multiple linked listing
  items. Keep each option's own area, floor, parking, furnishing, and asking price;
  never combine the options into one averaged row.
- A project/developer broadcast with an area range and no specific unit or price
  is one project_inventory item with area_min_sqft/area_max_sqft, developer name,
  project_status, floor_plate_sqft, and amenities. Never fabricate one listing per
  size in the range.
- Keep carpet, built-up, chargeable, saleable, terrace, and covered-terrace areas
  separate. Never fold terrace or frontage into carpet. A per-sqft sale quote may
  be multiplied only when the source explicitly states the pricing area; preserve
  price_math with rate, basis, area, and formula. Otherwise keep price_per_sqft
  without inventing total_asking_price.
- Total quotes such as “AED 7.2M” populate total_asking_price. “Negotiable” is a
  price qualifier/deal tag, not a changed amount. Preserve raw price text.
- Capture office facilities and capacities: workstations, cabins, director/CEO
  cabins, cubicles, conference/meeting rooms, pantry, reception, server/storage,
  parking, lift, power backup, and central AC.
- Capture frontage, entrances, ceiling height, permitted uses, inspection/access
  instructions, and project amenities when explicitly stated. Preserve uncertain
  marketing claims as unverified facts rather than inventing structured values.
- Broker RERA formats belong in broker_rera_number; property/project RERA remains
  separate. Extract all explicit contacts, up to 8, with the primary broker phone
  kept distinct from the contacts list.
- Preserve the original source evidence and broker wording. Never merge inventory
  from different brokers; same building does not mean the same office/unit.
"""


_RESIDENTIAL_RENT_REQUIREMENT_RULES = """
Residential rent requirement rules:
- This is demand, not supply. Keep listing_type="requirement" and never create
  a listing from a requirement phrase such as "looking for" or "required".
- Extract BHK/configuration exactly, including 1 RK, 2.5 BHK, jodi, and converted
  layouts. Use bhk_options/configuration_preference; do not force a whole number.
- Extract annual budget in absolute dirhams. "70K" is 70000/year, "85k" is
  85000/year, and "up to 120K" sets budget_max=120000. Never treat a deposit
  as rent.
- Preserve locality corridors and alternatives in locality_options, including
  "Bandra to Santacruz West" or "Andheri East to Goregaon East". Keep the raw
  wording in the source evidence and do not collapse a corridor into one place.
- Extract area ranges, area basis, furnishing preference, floor preference,
  higher/lower/middle-floor wording, view preference, parking minimum, amenities,
  possession timing, and building preferences when explicitly stated.
- Tenant facts are important searchable requirements: family, bachelor, couple,
  student, expat, company lease, pets, sharing, vegetarian/food preference, and
  other explicit occupancy conditions. Preserve them without judging or filtering.
- Deposit requirements may be a flat amount or months of rent. Store a flat
  amount in deposit_budget_max only when explicitly stated; preserve month-based
  wording in the raw evidence and do not convert it without a rent basis.
- Lease terms such as 3/5 years, short-term, immediate possession, and company
  lease belong in lease_term_preference/possession_preference.
- Reject PG/hostel/bed-sharing-only demand as out of scope: return no item for it.
- Residential rent normally does not have separate CAM or maintenance fields;
  do not invent them. Brokerage wording and "side by side" belong in
  brokerage_willingness or the raw evidence.
- Set needs_review=true when budget units, BHK, locality, or another material
  requirement is ambiguous. Keep deal_tags restricted to the whitelist.
"""


_RESIDENTIAL_SALE_REQUIREMENT_RULES = """
Residential sale requirement rules:
- This is purchase demand, not supply. Keep listing_type="requirement" and
  never create a listing from phrases such as "looking to buy", "want to
  purchase", "buyer required", or "client is looking". Do not invent a
  building or available unit from the request.
- Extract every stated configuration into bhk_options, preserving 1 RK,
  fractional BHK, jodi, converted layouts, and alternatives. Do not force a
  single BHK when the buyer gives a range or multiple options.
- Budget is the purchase price, not rent. Normalize absolute totals
  into budget_min and budget_max:
  "700K" becomes 700000, "1.5M" becomes 1500000, "up to 2M"
  sets budget_max=2000000. Preserve
  whether the source says total purchase price or per-square-foot budget;
  never treat a rent, deposit, token, or maintenance amount as the purchase
  budget. If a per-sqft quote is ambiguous or cannot be separated from a
  total price, preserve the raw wording and flag the requirement for review.
- Extract area ranges into area_min_sqft/area_max_sqft. Keep the stated area
  basis and raw wording in the evidence; do not turn a carpet-area preference
  into a fabricated built-up or saleable area.
- Preserve locality corridors and alternatives in locality_options, including
  "JVC to Al Barsha", "Dubai Marina or JBR", and similar wording.
  Do not collapse a corridor into one locality or silently choose a preferred
  endpoint. Keep locality ambiguity flagged for review.
- building_preferences are preferences, not facts about an available
  property. Capture named buildings, societies, project types, or explicit
  building requirements only when stated; do not invent a building name.
- furnishing_preference, car_parking_min, possession_preference, and urgency
  are explicit constraints. "Ready possession" or "immediate" means an
  immediate/ready requirement; "under construction acceptable" and stated
  delivery timelines belong in possession_preference. Do not infer a
  possession timeline from a sale advertisement or a generic "buy" phrase.
- buyer_type is only "end-use", "investment", or the source's explicit
  equivalent when stated. Never infer end-use versus investment from budget,
  locality, or BHK.
- transaction_nature captures an explicit resale, new, builder, or
  under-construction preference. Do not infer resale merely because a buyer
  wants immediate possession, or new construction merely because the message
  mentions a project.
- is_flexible is true only for explicit flexibility such as "flexible budget",
  "negotiable", or "open to options"; preserve the qualifying wording and do
  not manufacture flexibility from a broad range. deal_tags remain restricted
  to the documented whitelist and require source evidence.
- Set needs_review=true when the budget unit or amount is ambiguous, a BHK or
  locality corridor cannot be resolved, total versus per-sqft pricing is
  unclear, or another material purchase constraint conflicts or is incomplete.
  Do not hide an explicitly stated budget, BHK, locality, or preference merely
  because the request is informal.
- title should describe the demand, such as "2BR Buyer Requirement in Dubai
  Marina", and must not read like an advertised available property.
"""


_COMMERCIAL_SALE_REQUIREMENT_RULES = """
Commercial sale requirement rules:
- This is purchase demand, not supply. Keep listing_type="requirement" and
  never invent a building, asking price, or available commercial unit from a
  request such as "looking to buy an office" or "client wants a shop".
- Extract commercial_use_type precisely: office, retail/shop, showroom,
  warehouse/godown, restaurant/cafe, industrial, or another explicit use.
  Preserve qualifiers such as "for a clinic", "ground-floor retail", or
  "warehouse for storage" in the structured use and title/evidence. Do not
  broaden a specific use into generic commercial space.
- Extract area ranges into area_min_sqft/area_max_sqft. Preserve whether the
  source says carpet, built-up, chargeable, or another basis in the evidence;
  do not fabricate a missing maximum from wording such as "800+".
- Budget is a purchase price or an explicit purchase PSF ceiling, never rent.
  Normalize "up to 5M" to budget_max=5000000 and "AED 2500 per sqft max"
  to budget_per_sqft_max=2500. A single total purchase figure sets the
  appropriate budget bound; a range sets budget_min/budget_max. Do not invent
  CAM, maintenance, deposit, or other rental fields because they do not exist
  in this sale-requirement schema. Flag total-versus-PSF or unit
  ambiguity for review while preserving the raw wording.
- Preserve locality corridors and flexibility in locality_options, including
  "Business Bay to DIFC", Sheikh Zayed Road preferences, alternatives, and
  "okay with nearby areas". Do not collapse a corridor into one locality or
  silently discard an explicit flexibility condition.
- fitout_preference captures only an explicit bare-shell, warm-shell,
  furnished, ready-office, or similar purchase preference. Do not infer fitout
  from the intended use or from a project name.
- car_parking_min is a minimum only when the buyer explicitly requests parking
  or a number of spaces. needs_mezzanine, needs_lift, needs_power_backup, and
  needs_central_ac are booleans only for explicit constraints; never infer
  them from building class, floor, locality, or commercial_use_type.
- min_power_load_kw is populated only from an explicit load requirement such
  as "minimum 20 kW". Preserve the unit and flag ambiguous electrical
  language for review; do not derive a load from area or intended use.
- buyer_type is end-use, investment, or another explicit buyer description
  only when stated. Never infer it from the property type, budget, or urgency.
- urgency and is_flexible require source evidence. "Immediate purchase",
  "urgent", "open to nearby locations", and "budget flexible" may be
  captured when explicitly stated; do not convert a generic inquiry into an
  urgent or flexible requirement.
- deal_tags remain restricted to the documented whitelist and require explicit
  evidence. Set needs_review=true when budget units, total-versus-PSF basis,
  area, commercial use, locality corridor, or any material amenity constraint
  is ambiguous or conflicting.
- title should describe the demand, such as "Commercial Office Purchase
  Requirement in Business Bay", and must not read like an available listing.
"""


_COMMERCIAL_RENT_REQUIREMENT_RULES = """
Commercial rent requirement rules:
- This is demand, not supply. Keep listing_type="requirement" and never invent
  a building, rent, or available unit from the request.
- Extract the intended use precisely: office, gaming office, cafe, retail, etc.
  Preserve qualifiers such as “for gaming purpose” or “cafe (induction)” in
  intended_use_details/unstructured_facts.
- Area ranges populate area_min_sqft/area_max_sqft and area_basis_preference
  (usually carpet). “800+” means area_min_sqft=800 with no fabricated maximum.
- Preserve location corridors and flexibility: “Business Bay to DIFC”,
  “JVC to Al Barsha on Al Khail”, “okay with nearby areas”, and similar
  wording belong in locality_options/location_flexibility. Do not collapse a
  corridor into one locality.
- Budget is an annual rental budget. “90K to 120K” becomes budget_min=90000
  and budget_max=120000. A single “up to 150K” sets budget_max=150000.
- Treat M/mn as million dirhams and K/k/thousand as thousand dirhams when the
  surrounding text clearly describes a budget. Preserve the raw wording and
  set needs_review=true if the unit remains ambiguous.
- Furnishing is a preference. Capture minimum cabins, workstations, conference
  rooms, washrooms, attached washroom, pantry, lift, parking, floor range, and
  building standards as explicit constraints. “Commercial building not
  necessary” means residential_cum_commercial_ok=true; “only commercial premium
  building” means premium_building_required=true and the commercial preference.
- Capture operational requirements such as a street-facing or visible entrance,
  signage/branding visibility, loading or vehicle access, power/load needs,
  ground-floor or floor-count limits, and consecutive-floor requirements. Keep
  “near [road]”, “road touch”, and preferred roads in location_flexibility rather
  than inventing a normalized locality.
- If the budget explicitly includes maintenance/CAM, set
  budget_includes_maintenance=true; otherwise leave it null. Do not invent CAM
  for a requirement merely because it is common in commercial leases.
- Extract “glass facade”, “photos/pics requested”, and similar non-price needs as
  structured searchable requirements when present. Never treat photos as proof
  that a listing has been verified.
- “Brokerage side by side” belongs in brokerage_context/brokerage_terms_raw.
  Extract every stated contact, up to 8, while keeping the primary broker phone
  separate. Preserve urgent wording in urgency.
"""


def _get_extraction_prompt(
    asset_type: str,
    transaction_type: str,
    is_requirement: bool = False,
    mixed_transaction: bool = False,
) -> str:
    """Build a small route-specific prompt instead of sending all 85 fields."""
    fields = _FOCUSED_FIELDS[(asset_type, transaction_type, is_requirement)]
    side = "DEMAND/REQUIREMENT" if is_requirement else "SUPPLY/LISTING"
    route_rules = ""
    if (asset_type, transaction_type, is_requirement) == ("residential", "sale", False):
        route_rules = _RESIDENTIAL_SALE_EXTRACTION_RULES
    elif (asset_type, transaction_type, is_requirement) == ("residential", "rent", False):
        route_rules = _RESIDENTIAL_RENT_EXTRACTION_RULES
    elif (asset_type, transaction_type, is_requirement) == ("commercial", "rent", False):
        route_rules = _COMMERCIAL_RENT_EXTRACTION_RULES
    elif (asset_type, transaction_type, is_requirement) == ("commercial", "sale", False):
        route_rules = _COMMERCIAL_SALE_EXTRACTION_RULES
    elif (asset_type, transaction_type, is_requirement) == ("commercial", "rent", True):
        route_rules = _COMMERCIAL_RENT_REQUIREMENT_RULES
    elif (asset_type, transaction_type, is_requirement) == ("residential", "rent", True):
        route_rules = _RESIDENTIAL_RENT_REQUIREMENT_RULES
    elif (asset_type, transaction_type, is_requirement) == ("residential", "sale", True):
        route_rules = _RESIDENTIAL_SALE_REQUIREMENT_RULES
    elif (asset_type, transaction_type, is_requirement) == ("commercial", "sale", True):
        route_rules = _COMMERCIAL_SALE_REQUIREMENT_RULES
    expected_listing_type = "requirement" if is_requirement else transaction_type
    listing_type_rule = (
        f'- listing_type: exactly "{expected_listing_type}".'
        if is_requirement
        else '- listing_type: infer "sale" or "rent" from the source block. The route below is only a field-schema hint, not a hard label; an explicit rent/sale marker or price label in the source overrides it. If the source contains independently actionable sale and rent options, emit one item per option.'
    )
    return f"""You are an expert AI real-estate extraction analyst for Indian WhatsApp broker messages.
First interpret what each source block means, then map it to the allowed JSON schema. You are extracting {side} data with an initial schema hint of {asset_type} {transaction_type}; this hint is not authoritative when the raw source says otherwise. Return only valid JSON:
{{"items": [{{...}}]}}. Emit one object per independently actionable property or requirement.
Use the raw source as the evidence, but infer the semantic role of clearly labelled
fields from normal broker wording (for example `Rent`, `Outright`, `Rental`, `Sale`,
`Quote`, `Budget`, `BHK`, `Carpet`, and `Building`). Do not require the source to use
the exact schema field names. Never invent facts from outside the message, average,
merge separate units, or summarize raw text. Preserve locality.raw_mention and
price.raw_price_text exactly. For requirements use arrays/ranges and never turn a
concrete advertised availability into a requirement.
Every explicit fact in the source must be returned when it belongs to the allowed
schema. For requirements, an explicitly stated BHK, budget, locality preference,
furnishing preference, tenant type, or lease/company-lease condition is mandatory;
do not omit it merely because it is not needed to identify the opportunity.

Every item MUST include these discriminator fields:
{listing_type_rule}
- property_category: exactly "{asset_type}".
- extraction_confidence: one of "high", "medium", or "low".
Fields allowed for the remaining route-specific data: {fields}.
{_PRICE_PARSING_INSTRUCTIONS}
{_BROKER_GLOSSARY}
{route_rules}
For listing price, return price={{amount, unit, period, raw_price_text}}. For a requirement,
return budget_min/budget_max instead of pretending the budget is a listing price.
Return no markdown or explanation."""

_VALID_LISTING_TYPES = frozenset({"sale", "rent", "lease", "pg", "joint_venture", "requirement"})
_VALID_CATEGORIES = frozenset({"residential", "commercial"})
_VALID_FURNISHING = frozenset({"unfurnished", "semi_furnished", "fully_furnished"})
_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})
_VALID_POSSESSION = frozenset({
    "ready_to_move", "under_construction", "ready_possession",
    "oc_received", "preleased", "not_specified",
})
_VALID_AVAILABILITY = frozenset({
    "available", "sold", "let_out", "withdrawn", "closed", "not_specified",
})
_VALID_FURNISHING_CANONICAL = frozenset({
    "fully_furnished", "semi_furnished", "unfurnished", "bare_shell",
    "builder_finish", "not_specified",
})
_VALID_PRICE_UNITS = frozenset({"total", "per_sqft"})
_VALID_PRICE_PERIODS = frozenset({"one_time", "per_month"})

# Alias maps bridge common LLM variants to the canonical enum values used
# downstream.  Higher recall here directly means more rows survive
# `_normalize_extraction` instead of being dropped with "no valid listings".
_LISTING_TYPE_ALIASES = {
    "sale": "sale",
    "for_sale": "sale",
    "selling": "sale",
    "sell": "sale",
    "rent": "rent",
    "for_rent": "rent",
    "rental": "rent",
    "rentals": "rent",
    "rent_out": "rent",
    "lease": "rent",
    "requirement": "requirement",
    "requirements": "requirement",
    "needed": "requirement",
    "need": "requirement",
    "wanted": "requirement",
    "want": "requirement",
    "seeking": "requirement",
    "looking_for": "requirement",
    "listing": "sale",
    "sale_listing": "sale",
    "rental_listing": "rent",
    "rent_listing": "rent",
    "demand": "requirement",
    "buyer_requirement": "requirement",
    "tenant_requirement": "requirement",
}
_CATEGORY_ALIASES = {
    "residential": "residential",
    "resi": "residential",
    "residential_apartment": "residential",
    "residential_property": "residential",
    "home": "residential",
    "commercial": "commercial",
    "comm": "commercial",
    "commercial_property": "commercial",
    "office": "commercial",
    "shop": "commercial",
    "retail": "commercial",
}
_FURNISHING_ALIASES = {
    "unfurnished": "unfurnished",
    "bare": "unfurnished",
    "semi_furnished": "semi_furnished",
    "semi-furnished": "semi_furnished",
    "semifurnished": "semi_furnished",
    "semi": "semi_furnished",
    "fully_furnished": "fully_furnished",
    "fully-furnished": "fully_furnished",
    "fully_loaded": "fully_furnished",
    "fully-loaded": "fully_furnished",
    "full_furnished": "fully_furnished",
    "furnished": "fully_furnished",
}
_VALID_DEAL_TAGS = frozenset({
    "distress_sale",
    "urgent_sale",
    "negotiable",
    "bank_auction",
    "resale",
    "exclusive_mandate",
    "price_drop",
    "brand_new_building",
})
_VALID_CHARGE_TYPES = frozenset({"fixed", "percent_of_price"})

# Fields are intentionally copied after the discriminator-specific
# normalisation below.  Keeping this allow-list explicit prevents arbitrary
# provider output from reaching storage while ensuring the eight route schemas
# do not silently lose valid commercial/residential attributes.
_PASSTHROUGH_FIELDS = frozenset({
    "built_up_area_sqft", "chargeable_area_sqft", "mezzanine_area_sqft", "area_raw_text",
    "broker_rera_number", "floor_level", "floor_count", "possession_status",
    "super_built_up_area_sqft", "saleable_area_sqft", "project_inventory",
    "area_min_sqft", "area_max_sqft", "floor_plate_sqft", "project_status",
    "possession_date", "availability_status", "price_math", "rent_inclusions",
    "license_type", "short_term_allowed", "inspection_notice_minutes",
    "frontage_ft", "entrance_count", "otla_area_sqft", "otla_area_raw_text",
    "terrace_area_sqft", "covered_terrace_area_sqft", "terrace_area_raw_text",
    "heritage_space", "permitted_use_types", "ideal_for", "automatic_shutter_count",
    "room_count", "suite_count", "banquet_hall_count", "restaurant_count",
    "bar_facility", "operational_status", "director_cabin_count",
    "ceo_cabin_present", "cubicle_count", "conference_room_capacity",
    "meeting_room_capacity", "training_room_capacity", "cafeteria_seat_count",
    "accounts_area", "lounge_area",
    "price_basis", "commercial_use_type", "fitout_status", "ceiling_height",
    "floor_range", "car_parking_count", "parking_type", "power_load_kw",
    "cabin_count", "workstation_count", "conference_room_count",
    "meeting_room_count", "washroom_count", "pantry_type",
    "has_central_ac", "has_power_backup", "has_lift", "building_amenities",
    "amenities_unverified_claim", "bathroom_count", "parking_count",
    "property_view", "view_description", "age_of_property", "configuration_type",
    "configuration_details", "original_bhk", "current_bhk", "is_converted_unit",
    "is_combination_unit", "can_sell_separately", "availability_status",
    "brokerage_context", "co_brokered", "wing", "floor_min", "floor_max",
    "floor_label", "balcony_area_sqft", "balcony_area_raw_text",
    "terrace_area_sqft", "covered_terrace_area_sqft", "terrace_area_raw_text",
    "sellable_area_sqft", "computed_total_asking_price",
    "computed_price_confidence", "price_math", "unit_condition",
    "vastu_compliant", "parking_details", "society_restrictions",
    "society_restrictions_raw", "broker_company", "contacts",
    "showing_instructions", "contact_instructions", "unstructured_facts",
    "possession_date", "oc_status", "brokerage_type", "token_amount",
    "payment_plan", "transaction_nature", "deposit_amount", "deposit_months",
    "deposit_raw_text", "cam_amount", "cam_applicable", "cam_unit",
    "lease_term_type", "lock_in_period_months", "notice_period_months", "occupancy_status",
    "deal_tags", "needs_review", "title",
    # Requirement-only fields. These must survive normalization so the
    # typed requirement tables receive ranges, budgets, and preferences.
    "area_min_sqft", "area_max_sqft", "budget_min", "budget_max",
    "budget_per_sqft_max", "locality_options", "fitout_preference",
    "intended_use_details", "area_basis_preference", "location_flexibility",
    "floor_min", "floor_max", "floor_count_max", "floor_preference",
    "consecutive_floors_required", "parking_required",
    "needs_attached_washroom", "needs_washroom", "needs_pantry",
    "power_requirements", "entrance_requirement", "signage_required",
    "loading_access_required", "budget_includes_maintenance",
    "premium_building_required", "glass_facade_required",
    "residential_cum_commercial_ok", "by_lanes_accepted", "media_requested",
    "min_cabin_count", "min_workstation_count", "needs_conference_room",
    "brokerage_terms_raw",
    "car_parking_min", "needs_mezzanine", "needs_lift", "needs_power_backup",
    "needs_central_ac", "min_power_load_kw", "buyer_type", "urgency",
    "is_flexible", "transaction_nature", "building_preferences", "age_preference",
    "view_preference", "brokerage_willingness",
    "bhk_options", "furnishing_preference", "tenant_type",
    "sharing_acceptable", "food_preference", "amenity_requirements",
    "company_lease_criteria", "lease_term_preference", "nationality",
})

_NUMERIC_PASSTHROUGH_FIELDS = frozenset({
    "built_up_area_sqft", "chargeable_area_sqft", "car_parking_count",
    "power_load_kw", "cabin_count", "workstation_count",
    "conference_room_count", "meeting_room_count", "washroom_count",
    "bathroom_count", "parking_count", "token_amount", "deposit_amount",
    "deposit_months", "cam_amount", "lock_in_period_months",
    "notice_period_months", "area_min_sqft", "area_max_sqft",
    "budget_min", "budget_max", "budget_per_sqft_max", "car_parking_min",
    "min_power_load_kw", "original_bhk", "current_bhk", "floor_min",
    "floor_max", "floor_count_max", "balcony_area_sqft", "terrace_area_sqft",
    "covered_terrace_area_sqft", "sellable_area_sqft",
    "computed_total_asking_price", "super_built_up_area_sqft", "saleable_area_sqft",
    "floor_plate_sqft",
    "frontage_ft", "otla_area_sqft", "entrance_count", "automatic_shutter_count",
    "room_count", "suite_count", "banquet_hall_count", "restaurant_count",
    "director_cabin_count", "cubicle_count", "conference_room_capacity",
    "meeting_room_capacity", "training_room_capacity", "cafeteria_seat_count",
    "inspection_notice_minutes",
    "min_cabin_count", "min_workstation_count", "floor_min", "floor_max",
})

_INTEGER_PASSTHROUGH_FIELDS = frozenset({
    "car_parking_count", "cabin_count", "workstation_count",
    "conference_room_count", "meeting_room_count", "washroom_count",
    "bathroom_count", "parking_count", "deposit_months",
    "lock_in_period_months", "notice_period_months", "car_parking_min",
    "floor_min", "floor_max",
    "floor_count", "entrance_count", "automatic_shutter_count", "room_count",
    "suite_count", "banquet_hall_count", "restaurant_count", "director_cabin_count",
    "cubicle_count", "conference_room_capacity", "meeting_room_capacity",
    "training_room_capacity", "cafeteria_seat_count", "inspection_notice_minutes",
    "min_cabin_count", "min_workstation_count", "floor_min", "floor_max",
})


_BLOCK_START_KEYWORDS = (
    "available", "requirement", "requirements", "wanted", "looking for",
    "need", "offer", "offering", "for sale", "for rent", "lease",
    "rental", "inventory", "project", "building", "tower", "flat",
    "apartment", "residential", "commercial", "office", "shop", "plot",
    "showroom", "warehouse", "godown", "villa", "bungalow", "duplex",
    "jodi", "pre launch", "prelaunch", "new launch", "market update",
    "update", "broadcast", "group", "broker", "property", "realty",
    "estate", "exclusive", "urgent", "hot", "direct", "with pictures",
)


def _document_lines(raw_text: str) -> list[str]:
    return [line.rstrip() for line in raw_text.splitlines()]


def _is_separator_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(re.fullmatch(r"[-=*_•\s]{3,}", stripped))


def _is_numbered_item(line: str) -> bool:
    return bool(re.match(r"^\s*\d{1,3}[\)\.\-:](?!\d)\s*\S+", line))


def _is_explicit_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if re.fullmatch(r"\d+(?:\.\d+)?\s*(?:bhk|rk)", lowered):
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?\s*(?:carpet|built[- ]?up|super[- ]?built[- ]?up|sq\.?\s*ft\.?|sqft|sq\.?\s*m\.?)", lowered):
        return False
    if re.fullmatch(r"(?:rent|quote|price|deposit)\s*[:\-]?\s*.*", lowered):
        return False
    if re.fullmatch(r"(?:lower|middle|higher)\s+floor", lowered):
        return False
    if re.fullmatch(r"(?:semi|fully)\s*furnished", lowered) or lowered in {"unfurnished", "furnished"}:
        return False
    if lowered.startswith(("available", "requirement", "requirements")):
        return True
    if any(keyword in lowered for keyword in _BLOCK_START_KEYWORDS):
        # Keep the heuristic conservative: short title-like lines only.
        word_count = len(re.findall(r"\b[\w&/-]+\b", stripped))
        if word_count <= 12 and len(stripped) <= 96:
            return True
    if stripped == stripped.upper() and len(stripped) <= 96:
        # Uppercase broker headings and project names.
        alpha_count = sum(1 for ch in stripped if ch.isalpha())
        return alpha_count >= 4
    if len(stripped) <= 64 and stripped[0].isalpha() and stripped[-1] not in ".!?":
        # Title-case / project-name lines like "Bandra Broker Group".
        word_count = len(re.findall(r"\b[\w&/-]+\b", stripped))
        if 1 <= word_count <= 8:
            titleish = stripped == stripped.title() or any(part.isupper() for part in stripped.split())
            if titleish and any(keyword in lowered for keyword in ("bhk", "rent", "sale", "lease", "group", "tower", "project", "building", "flat", "apartment", "estate", "realty", "properties", "available")):
                return True
    return False


def _is_block_start(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return _is_numbered_item(stripped) or _is_explicit_heading(stripped)


def _classify_document(lines: list[str]) -> str:
    """Classify the WhatsApp document before extraction."""
    non_empty = [line.strip() for line in lines if line.strip()]
    if not non_empty:
        return "Unknown"

    starts = [line for line in non_empty if _is_block_start(line)]
    if not starts:
        lowered = " ".join(non_empty).lower()
        if any(word in lowered for word in ("hello", "hi", "thanks", "thank you", "good morning", "good evening", "how are you")):
            return "Discussion"
        if any(word in lowered for word in ("update", "today", "yesterday", "status")):
            return "Update"
        return "Unknown"

    lowered = " ".join(non_empty).lower()
    has_requirement = any(word in lowered for word in ("requirement", "wanted", "looking for", "need "))
    has_listing = any(word in lowered for word in ("available", "for rent", "for sale", "lease", "inventory", "offer"))
    if has_requirement and has_listing:
        return "Mixed Listing + Requirement"
    if has_requirement:
        return "Requirement"
    if len(starts) > 1:
        return "Multi Listing"
    return "Single Listing"


def _extract_json_object(raw: str | None) -> object | None:
    """Robustly extract a JSON object/array from LLM output.

    Many providers occasionally:
    - add a sentence of prose before/after the JSON ("Here is the JSON:")
    - wrap in ```json fences and forget a closing fence
    - JSON is the last `{}` block in the response

    Strategy:
    1. Direct ``json.loads`` on the trimmed response.
    2. Find the first balanced ``{...}`` or ``[...]`` substring (string-aware
       so embedded braces in strings do not throw off depth tracking) and try
       ``json.loads`` on each one until success.

    Returns the parsed Python value, or ``None`` if nothing usable is found.
    """
    if not raw:
        return None
    s = raw.strip()

    # Strip a single ``` / ```json fence pair if present at the start.
    if s.startswith("```"):
        rest = s.split("\n", 1)[-1] if "\n" in s else s[3:]
        # Drop trailing ``` boundary if present, otherwise keep everything.
        if rest.rstrip().endswith("```"):
            rest = rest.rstrip()[:-3]
        s = rest.strip()

    if not s:
        return None

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    for opener, closer in (('{', '}'), ('[', ']')):
        idx = s.find(opener)
        while idx != -1:
            depth = 0
            in_str = False
            esc = False
            end = -1
            for i in range(idx, len(s)):
                c = s[i]
                if esc:
                    esc = False
                    continue
                if in_str:
                    if c == '\\':
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                    continue
                if c == opener:
                    depth += 1
                elif c == closer:
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end != -1:
                try:
                    return json.loads(s[idx:end + 1])
                except json.JSONDecodeError:
                    pass
            idx = s.find(opener, idx + 1)
    return None


def _normalize_configuration_type(value, bhk=None) -> str | None:
    """Return the canonical display/storage label for a unit configuration."""
    text = str(value).strip() if value is not None else ""
    fallback = _coerce_float(bhk)
    if not text and fallback is None:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        number = _coerce_float(text)
        if number == 0.5:
            return "1 RK"
        return f"{number:g} BHK" if number is not None else None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(BHK|RK)", text, re.IGNORECASE)
    if match:
        number = _coerce_float(match.group(1))
        kind = match.group(2).upper()
        if number == 0.5 and kind == "BHK":
            return "1 RK"
        return f"{number:g} {kind}" if number is not None else None
    if text:
        return text
    if fallback == 0.5:
        return "1 RK"
    return f"{fallback:g} BHK" if fallback is not None else None


def _segment_document(raw_text: str) -> dict:
    """Reconstruct a WhatsApp message into logical blocks."""
    inline_pattern, inline_chunks = split_message_into_chunks(raw_text)
    # Do not discard deterministic boundaries just because they came from a
    # non-inline pattern. Previously dash-separated broadcasts were correctly
    # detected here, then thrown away and sent to the model as one flat blob.
    if inline_pattern and len(inline_chunks) >= 2:
        cleaned_chunks = [_trim_bulk_footer(chunk) for chunk in inline_chunks]
        cleaned_chunks = [chunk for chunk in cleaned_chunks if chunk]
        blocks = [
            {
                "index": index,
                "start_line": None,
                "line_count": len(chunk.splitlines()) or 1,
                "text": chunk.strip(),
                "lines": chunk.splitlines() or [chunk.strip()],
            }
            for index, chunk in enumerate(cleaned_chunks)
        ]
        return {
            "document_type": "Multi Listing",
            "header": None,
            "block_count": len(blocks),
            "blocks": blocks,
            "raw_text": raw_text,
        }

    lines = _document_lines(raw_text)
    header_lines: list[str] = []
    blocks: list[dict] = []
    current: list[str] = []
    current_start_index: int | None = None

    def flush() -> None:
        nonlocal current, current_start_index
        if current:
            blocks.append({
                "index": len(blocks),
                "start_line": current_start_index,
                "line_count": len(current),
                "text": _trim_bulk_footer("\n".join(current)),
                "lines": current[:],
            })
            current = []
            current_start_index = None

    # Numbered inventory is the strongest deterministic boundary available.
    # Do this before the broad heading heuristic: field/value lines such as
    # "Furnished office" or "Self-contained" can look title-like, but they
    # belong to the preceding numbered property until the next item begins.
    numbered_starts = [
        index for index, line in enumerate(lines)
        if _is_numbered_item(line.strip())
    ]
    if len(numbered_starts) >= 2:
        first_start = numbered_starts[0]
        header_lines = lines[:first_start]
        for block_index, start in enumerate(numbered_starts):
            end = numbered_starts[block_index + 1] if block_index + 1 < len(numbered_starts) else len(lines)
            block_lines = lines[start:end]
            text = _trim_bulk_footer("\n".join(block_lines))
            if not text:
                continue
            blocks.append({
                "index": len(blocks),
                "start_line": start,
                "line_count": len(block_lines),
                "text": text,
                "lines": block_lines[:],
            })
        return {
            "document_type": "Multi Listing",
            "header": "\n".join(header_lines).strip() or None,
            "block_count": len(blocks),
            "blocks": blocks,
            "raw_text": raw_text,
        }

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            if current:
                current.append(line)
            elif header_lines:
                header_lines.append(line)
            continue

        if _is_separator_line(stripped):
            flush()
            continue

        if _is_block_start(stripped):
            flush()
            current = [line]
            current_start_index = idx
            continue

        if current:
            current.append(line)
        else:
            header_lines.append(line)

    flush()

    # A single labelled listing can be mistaken for a block when its broker
    # footer/company line matches the broad heading heuristic. In that case
    # the property fields end up in ``header`` while the focused pass receives
    # only the footer. Reattach the header when it clearly contains the
    # listing's own structured signals.
    if len(blocks) == 1 and header_lines:
        header_text = "\n".join(header_lines).strip()
        if re.search(
            r"(?i)\b(?:bhk|rk|config(?:uration)?|location|furnishing|rent|sale|carpet|possession)\b",
            header_text,
        ):
            merged_text = _trim_bulk_footer("\n".join([header_text, blocks[0]["text"]]).strip())
            blocks[0] = {
                **blocks[0],
                "start_line": 0,
                "line_count": len(merged_text.splitlines()) or 1,
                "text": merged_text,
                "lines": merged_text.splitlines() or [merged_text],
            }
            header_lines = []

    document_type = _classify_document(lines)
    return {
        "document_type": document_type,
        "header": "\n".join(header_lines).strip() or None,
        "block_count": len(blocks),
        "blocks": blocks,
        "raw_text": raw_text,
    }


def _building_alias_context(raw_text: str, ctx: dict | None, storage=None) -> list[dict]:
    """Return a small tenant-scoped alias shortlist for the model.

    This is context retrieval, not resolution: the model still decides whether
    a name refers to one of these buildings.  The shortlist prevents thousands
    of aliases from being sent on every request.
    """
    if storage is None:
        return []
    tenant_id = (ctx or {}).get("tenant_id") or getattr(storage, "_tenant_id", None)
    try:
        cache_key = str(tenant_id or "__shared__")
        now = time.monotonic()
        with _REFERENCE_CACHE_LOCK:
            cached = _ALIAS_ROWS_CACHE.get(cache_key)
        if cached and now - cached[0] < _REFERENCE_CACHE_TTL_SECONDS:
            rows = cached[1]
        else:
            query = storage.client.table("building_name_aliases").select(
                "building_id,alias,canonical_name"
            )
            if tenant_id:
                query = query.or_(f"tenant_id.eq.{tenant_id},tenant_id.is.null")
            rows = query.limit(2500).execute().data or []
            with _REFERENCE_CACHE_LOCK:
                if len(_ALIAS_ROWS_CACHE) >= _REFERENCE_CACHE_MAX_TENANTS and cache_key not in _ALIAS_ROWS_CACHE:
                    oldest = min(_ALIAS_ROWS_CACHE, key=lambda key: _ALIAS_ROWS_CACHE[key][0])
                    _ALIAS_ROWS_CACHE.pop(oldest, None)
                _ALIAS_ROWS_CACHE[cache_key] = (now, rows)
        scored = []
        for row in rows:
            alias = str(row.get("alias") or "").strip()
            if not alias or not row.get("building_id"):
                continue
            score = fuzzy_score(raw_text, alias)
            # Token containment is a useful retrieval signal for long WhatsApp
            # messages where whole-document similarity is naturally low.
            text_tokens = set(re.findall(r"[a-z0-9]+", raw_text.casefold()))
            alias_tokens = set(re.findall(r"[a-z0-9]+", alias.casefold()))
            overlap = len(text_tokens & alias_tokens) / max(len(alias_tokens), 1)
            scored.append((max(score, overlap), row))
        scored.sort(key=lambda item: item[0], reverse=True)
        result = []
        seen = set()
        for score, row in scored:
            key = row.get("building_id")
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "building_id": key,
                "alias": row.get("alias"),
                "canonical_name": row.get("canonical_name"),
            })
            if len(result) >= 20:
                break
        return result
    except Exception as exc:
        _logger.debug("building alias context unavailable: %s", exc)
        return []


_LOCALITY_CONTEXT_CACHE: tuple[float, list[dict]] = (0.0, [])


def _locality_reference_context(storage=None) -> list[dict]:
    """Return a small cached locality gazetteer for extraction grounding."""
    global _LOCALITY_CONTEXT_CACHE
    if storage is None:
        return []
    cached_at, cached = _LOCALITY_CONTEXT_CACHE
    if cached and time.time() - cached_at < 3600:
        return cached
    try:
        rows = storage.client.table("locality_reference").select(
            "sub_locality,parent_locality,alternate_names"
        ).limit(300).execute().data or []
        context = [
            {
                "locality": row.get("sub_locality"),
                "parent": row.get("parent_locality"),
                "aliases": row.get("alternate_names") or [],
            }
            for row in rows
            if row.get("sub_locality")
        ]
        _LOCALITY_CONTEXT_CACHE = (time.time(), context)
        return context
    except Exception as exc:
        _logger.debug("locality reference context unavailable: %s", exc)
        return []


_UNIFIED_EXTRACTION_PROMPT = """You extract structured real-estate opportunities from one raw WhatsApp message.
The message below is authoritative evidence. Do not rewrite, split, normalize, or
strip it before interpreting it. Return JSON only with this shape:
{
  "message_class": "listing" | "requirement" | "market_chatter" | "irrelevant" | "mixed",
  "listing_count": number,
  "items": [
    {
      "listing_type": "sale" | "rent" | "lease" | "pg" | "joint_venture" | "requirement",
      "property_category": "residential" | "commercial",
      "building_name": string | null,
      "building_id": number | null,
      "building_resolution_confidence": number,
      "locality": {"raw_mention": string | null, "resolved_locality": string | null, "confidence": number},
      "bhk": number | null,
      "carpet_area_sqft": number | null,
      "built_up_area_sqft": number | null,
      "super_built_up_area_sqft": number | null,
      "price": {"amount": number | null, "unit": "total" | "per_sqft", "period": "one_time" | "per_month" | null, "raw_price_text": string | null},
      "transaction_type": "sale" | "rent" | "lease" | "pg" | "joint_venture" | null,
      "possession_status": "ready_to_move" | "under_construction" | "ready_possession" | "oc_received" | "preleased" | "not_specified",
      "furnishing_status": "fully_furnished" | "semi_furnished" | "unfurnished" | "bare_shell" | "builder_finish" | "not_specified",
      "availability_status": "available" | "sold" | "let_out" | "withdrawn" | "closed" | "not_specified",
      "price_basis": "carpet" | "built_up" | "super_built_up" | "saleable" | "not_specified",
      "extraction_confidence_score": number,
      "field_confidence": {"field_name": number},
      "provenance": {"field_name": "exact quote from the raw message"},
      "source_slice": "the exact contiguous raw-message block belonging only to this item"
    }
  ]
}
For each item, preserve exact source wording in provenance and copy the complete,
contiguous source block into source_slice. source_slice must be copied verbatim,
including the item's heading and fields, but must not include the next item,
shared footer, broker signature, or unrelated header. A requirement is demand,
not inventory. Never invent a building_id: use only the supplied alias context, and
return null when no context entry is an actual match. Confidence values are 0.0-1.0.

The user context may include approved_correction_examples. Treat these as
tenant-scoped guidance for similar wording only. They are not authoritative
facts and must never override an explicit quote in the current raw message.

Location separation is strict: a locality/area/neighborhood such as Bandra West,
Andheri East, Powai, or Khar West belongs in locality.raw_mention and locality.resolved_locality,
not building_name. building_name is only the specific named society, tower, project,
or building (for example Marina Gate or Burj Vista). If a message says
"2BR in Dubai Marina" with no named tower, building_name must be null and the
locality must be Dubai Marina. Use the supplied locality_reference context to
distinguish locality names from building names; do not promote a locality into a
building merely because it appears in a heading.

Field ownership is strict inside every listing block. Never put a price or price
header (for example "85K", "AED 1.5M", "2M"), furnishing line ("Fully
Furnished"), floor line, parking line, configuration line, broker footer, or
generic ad phrase into building_name. Those belong in their dedicated fields or
unstructured_facts. If the block has no specifically named building, return
building_name=null. Never borrow a building name, price, or locality from the
previous or next block. Preserve the full source slice in source_slice so an
uncertain item can be reviewed rather than guessed.

This is extraction, not a location-answering task. Do not add facts from memory,
Google, portals, maps, or general knowledge. If the source says "Lower Parel West",
return that source locality; do not add wards, stations, coordinates, descriptions,
or rent opinions. A known building may be resolved only when it appears in the
supplied known_buildings context or in the current source block. If the source says
"Rent: 300/- Rs p.sf", preserve it as per_sqft and never convert it into a monthly
total without an explicit total. For requirements, use budget fields only; never
emit asking-price fields.
"""


def _normalize_extraction(raw: dict) -> dict:
    """Normalize and validate LLM extraction response."""
    result = {}

    # listing_type — accept enum value directly, then map common LLM variants
    # to the canonical set.  Without this normalization step, providers often
    # emit "for_sale"/"rental"/"wanted" which currently drop the entire
    # candidate as "no valid listings".
    lt_raw = str(raw.get("listing_type", "")).strip().lower()
    lt_raw = lt_raw.replace(" ", "_").replace("-", "_")
    result["listing_type"] = _LISTING_TYPE_ALIASES.get(lt_raw, lt_raw)
    if result["listing_type"] not in _VALID_LISTING_TYPES:
        result["listing_type"] = None
    # Typed routing currently has sale/rent destinations. Preserve the AI's
    # richer transaction_type separately while routing lease/PG/JV to rent.
    if result["listing_type"] in {"lease", "pg", "joint_venture"}:
        result["routing_listing_type"] = "rent"
    result["transaction_type"] = str(raw.get("transaction_type") or lt_raw or "").strip().lower() or None

    # property_category — same alias pattern
    pc_raw = str(raw.get("property_category", "")).strip().lower()
    pc_raw = pc_raw.replace(" ", "_").replace("-", "_")
    result["property_category"] = _CATEGORY_ALIASES.get(pc_raw, pc_raw)
    if result["property_category"] not in _VALID_CATEGORIES:
        result["property_category"] = None

    # bhk
    result["bhk"] = _coerce_float(raw.get("bhk"))
    result["configuration_type"] = _normalize_configuration_type(raw.get("configuration_type"), result["bhk"])

    # carpet_area_sqft
    result["carpet_area_sqft"] = _coerce_float(raw.get("carpet_area_sqft"))

    # price
    price = raw.get("price", {})
    if isinstance(price, dict):
        amount = price.get("amount")
        result["price"] = {
            "amount": _coerce_float(amount),
            "unit": str(price.get("unit", "")).strip().lower() if price.get("unit") else None,
            "period": str(price.get("period", "")).strip().lower() if price.get("period") else None,
            "raw_price_text": str(price.get("raw_price_text", "")).strip() or None,
        }
        if result["price"]["unit"] not in _VALID_PRICE_UNITS:
            result["price"]["unit"] = None
        if result["price"]["period"] not in _VALID_PRICE_PERIODS:
            result["price"]["period"] = None
    else:
        result["price"] = {"amount": None, "unit": None, "period": None, "raw_price_text": None}

    # locality
    loc = raw.get("locality", {})
    if isinstance(loc, dict):
        raw_conf = loc.get("confidence")
        conf = str(raw_conf).strip().lower()
        rm = loc.get("raw_mention")
        rl = loc.get("resolved_locality")
        result["locality"] = {
            "raw_mention": str(rm).strip() if rm is not None else None,
            "resolved_locality": str(rl).strip() if rl is not None else None,
            "confidence": raw_conf if isinstance(raw_conf, (int, float)) else (conf if conf in _VALID_CONFIDENCE else "low"),
        }
    else:
        result["locality"] = {"raw_mention": None, "resolved_locality": None, "confidence": "low"}

    # building_name — reject garbage patterns that the LLM sometimes extracts
    # as building names (broker names, ad text, property types, deal terms, etc.)
    bn = raw.get("building_name")
    bn_str = str(bn).strip() if bn and str(bn).strip() else None
    if bn_str:
        bn_lower = bn_str.lower()
        _GARBAGE_BUILDING_PATTERNS = (
            # deal terms / specs
            "stamp duty", "furnish", "carpet", "bhk", "sqft", "sq ft",
            "ready to move", "negotiable", "balcony", "sea view",
            "amenities", "parking", "deposit", "possession",
            " available", "available ", "options", "benefit",
            "family", "bachelor", "veg ", " non-veg",
            " near ", "opp ", "opposite", "behind", "floor",
            "brokerage", "car park", "higher flr", "lower flr",
            "1st floor", "2nd floor", "3rd floor", " ground ",
            # ad text
            "pics ", " video ", "photos ", "virtual tour",
            "for more details", "contact", "call ", "whatsapp",
            "limited period", "hurry", "urgent", "exclusive",
            "convenient nearby", "prime location", "strategic location",
            "rental inventory", "inventory", "direct inventor",
            "type ", "size ", "configuration",
            # property types (not building names)
            "restaurant", "cafe", "café", "shop ", "retail",
            "office", "showroom", "warehouse", "godown",
            " bungalow", "villa ", "penthouse",
            # broker / firm names
            "realtor", "estate ", "consultant", "properties",
            " realty", "real estate", " deals", "advisors",
            "infra ", "developers", "constructions",
            "from :", "from:",
        )
        if any(pat in bn_lower for pat in _GARBAGE_BUILDING_PATTERNS):
            bn_str = None
        elif len(bn_str) < 3 or len(bn_str) > 80:
            bn_str = None
        # reject if starts with a digit (deal terms like "4.5bhk", "1 Car Park")
        elif bn_str[0].isdigit():
            bn_str = None
    result["building_name"] = bn_str
    if bn_str:
        building_problem = building_name_problem(
            bn_str,
            locality=(result.get("locality") or {}).get("resolved_locality"),
        )
        if building_problem:
            # Preserve the rejected token for the slice-level repair pass;
            # never persist it as the actual building_name.
            result["building_name_raw_candidate"] = bn_str
            result["building_name"] = None
            result["needs_review"] = True
            result["validation_flags"] = [building_problem, "building_name_unresolved"]

    # Some providers call the commercial loft field by its synonym. The
    # typed schema uses mezzanine_area_sqft for both concepts.
    if raw.get("mezzanine_area_sqft") is None and raw.get("loft_area_sqft") is not None:
        result["mezzanine_area_sqft"] = _coerce_float(raw.get("loft_area_sqft"))

    # furnishing_status — enum + aliases (LLM writes "semi-furnished",
    # "fully furnished", "bare" etc.)
    fs_raw = str(raw.get("furnishing_status", "")).strip().lower()
    fs_raw = {
        "ff": "fully_furnished",
        "fully furnished": "fully_furnished",
        "furnished": "fully_furnished",
        "sf": "semi_furnished",
        "semi furnished": "semi_furnished",
        "pf": "semi_furnished",
        "none": "unfurnished",
        "unfurnished": "unfurnished",
    }.get(fs_raw, fs_raw)
    result["furnishing_status"] = fs_raw if fs_raw in _VALID_FURNISHING_CANONICAL else None

    # amenities
    amenities = raw.get("amenities", [])
    if isinstance(amenities, list):
        result["amenities"] = [str(a).strip() for a in amenities if a and str(a).strip()]
    else:
        result["amenities"] = []

    # possession_status
    ps = str(raw.get("possession_status") or "").strip().lower()
    ps = {
        "immediate": "ready_to_move",
        "ready": "ready_to_move",
        "ready to move": "ready_to_move",
        "available": "ready_to_move",
        "oc avlb": "oc_received",
        "oc available": "oc_received",
    }.get(ps, ps)
    result["possession_status"] = ps if ps in _VALID_POSSESSION else None

    availability = str(raw.get("availability_status") or "").strip().lower()
    result["availability_status"] = availability if availability in _VALID_AVAILABILITY else None

    result["building_id"] = _coerce_int(raw.get("building_id"))
    score = _coerce_float(raw.get("building_resolution_confidence"))
    result["building_resolution_confidence"] = max(0.0, min(1.0, score or 0.0))
    result["field_confidence"] = raw.get("field_confidence") if isinstance(raw.get("field_confidence"), dict) else {}
    result["provenance"] = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
    source_slice = raw.get("source_slice")
    result["source_slice"] = str(source_slice).strip() if source_slice and str(source_slice).strip() else None
    result["message_class"] = raw.get("message_class")
    result["listing_count"] = _coerce_int(raw.get("listing_count"))
    score = _coerce_float(raw.get("extraction_confidence_score"))
    if score is None and result["field_confidence"]:
        values = [_coerce_float(value) for value in result["field_confidence"].values()]
        values = [value for value in values if value is not None]
        score = sum(values) / len(values) if values else 0.0
    result["extraction_confidence_score"] = max(0.0, min(1.0, score or 0.0))
    result["confidence"] = result["extraction_confidence_score"]

    # title
    title = raw.get("title")
    result["title"] = str(title).strip() if title and str(title).strip() else None

    # extraction_confidence
    ec = str(raw.get("extraction_confidence", "")).strip().lower()
    result["extraction_confidence"] = ec if ec in _VALID_CONFIDENCE else "medium"

    # deal_tags — whitelist-filter list of lowercase strings.
    tags = raw.get("deal_tags", [])
    if isinstance(tags, list):
        result["deal_tags"] = [
            str(t).strip().lower()
            for t in tags
            if str(t).strip().lower() in _VALID_DEAL_TAGS
        ]
    else:
        result["deal_tags"] = []

    # additional_charges — array of {label, amount, amount_type} with
    # amount_type in {"fixed", "percent_of_price"}. Junk entries (missing
    # label, missing amount, bad amount_type, non-numeric amount) are
    # silently dropped so a malformed entry can't poison the whole row.
    charges = raw.get("additional_charges", [])
    normalized_charges: list[dict] = []
    if isinstance(charges, list):
        for c in charges:
            if not isinstance(c, dict):
                continue
            label = str(c.get("label", "")).strip()
            amount = c.get("amount")
            amount_type = str(c.get("amount_type", "")).strip().lower()
            if not label or amount is None or amount_type not in _VALID_CHARGE_TYPES:
                continue
            try:
                normalized_charges.append({
                    "label": label,
                    "amount": float(amount),
                    "amount_type": amount_type,
                })
            except (ValueError, TypeError):
                continue
    result["additional_charges"] = normalized_charges

    # Preserve valid route-specific schema fields that are not represented by
    # the small common normalisation block above. Previously these fields were
    # silently discarded, which made messages such as "Area 2000 Carpet /
    # Condition Bareshell / Car Park 2" appear empty in the admin UI.
    for field in _PASSTHROUGH_FIELDS:
        if field in result or field not in raw:
            continue
        value = raw.get(field)
        if value is not None and value != "":
            if field in _INTEGER_PASSTHROUGH_FIELDS:
                value = _coerce_int(value)
            elif field in _NUMERIC_PASSTHROUGH_FIELDS:
                value = _coerce_float(value)
            if value is not None and value != "":
                result[field] = value

    return result


def _single_property_document(text: str) -> bool:
    """Identify a long brochure describing one property, not a broadcast."""
    value = str(text or "")
    if not value.strip():
        return False
    repeated_property_headers = len(re.findall(
        r"(?im)^\s*(?:[*_]?\s*)?(?:property overview|asking price|plot area|building carpet area)\b",
        value,
    ))
    explicit_items = len(re.findall(r"(?im)^\s*\d+[.)]\s+\S+", value))
    return repeated_property_headers >= 2 and explicit_items < 2


def _source_ground_asset_category(item: dict, source_text: str) -> dict:
    """Do not turn an unsupported bare plot/land mention into commercial."""
    corrected = dict(item or {})
    if corrected.get("property_category") != "commercial":
        return corrected
    if not re.search(r"\b(?:plot|land)\b", source_text or "", re.I):
        return corrected
    if re.search(
        r"\b(?:office|shop|showroom|warehouse|industrial|commercial|hotel|hospitality|restaurant|banquet|lodging|factory|godown)\b",
        source_text or "",
        re.I,
    ):
        return corrected
    corrected["property_category"] = "residential"
    corrected["needs_review"] = True
    corrected["validation_flags"] = list(dict.fromkeys(
        list(corrected.get("validation_flags") or []) + ["asset_type_unresolved_for_plot"]
    ))
    return corrected


def _source_grounded_furnishing(extraction: dict, raw_text: str) -> dict:
    """Never persist a furnishing state that the WhatsApp source does not say.

    Providers frequently fill enum fields with a plausible default (most
    commonly ``unfurnished``). That is still fabricated inventory data when
    the message contains no furnishing statement, so the safe value is null
    plus a review flag.
    """
    corrected = dict(extraction or {})
    furnishing = str(corrected.get("furnishing_status") or "").strip().lower()
    if not furnishing:
        return corrected

    evidence_patterns = {
        "fully_furnished": r"\b(?:fully\s+furnished|furnished|fully\s+loaded)\b",
        "semi_furnished": r"\bsemi[-\s]?furnished\b",
        "unfurnished": r"\bunfurnished\b",
        "bare_shell": r"\bbare[-\s]?shell\b",
        "builder_finish": r"\bbuilder[-\s]?finish(?:ed)?\b",
    }
    pattern = evidence_patterns.get(furnishing)
    if pattern and re.search(pattern, str(raw_text or ""), flags=re.IGNORECASE):
        return corrected

    corrected["furnishing_status"] = None
    corrected["needs_review"] = True
    corrected["validation_flags"] = list(dict.fromkeys(
        list(corrected.get("validation_flags") or [])
        + ["furnishing_without_source_evidence"]
    ))
    return corrected


def _repair_locality_only_building(extraction: dict, locality_context: list[dict]) -> dict:
    """Move an exact locality misclassified as a building into locality."""
    building = str(extraction.get("building_name") or "").strip()
    if not building or not locality_context:
        return extraction
    key = re.sub(r"[^a-z0-9]+", " ", building.casefold()).strip()
    for item in locality_context:
        candidates = [item.get("locality"), *(item.get("aliases") or [])]
        if any(
            key == re.sub(r"[^a-z0-9]+", " ", str(candidate).casefold()).strip()
            for candidate in candidates
            if candidate
        ):
            locality = extraction.get("locality")
            if not isinstance(locality, dict):
                locality = {"raw_mention": None, "resolved_locality": None, "confidence": "low"}
            locality["raw_mention"] = locality.get("raw_mention") or building
            locality["resolved_locality"] = locality.get("resolved_locality") or item.get("parent")
            locality["confidence"] = locality.get("confidence") or "high"
            extraction["locality"] = locality
            extraction["building_name"] = None
            break
    return extraction


def _source_grounded_price(extraction: dict, raw_text: str) -> dict:
    """Drop provider prices that have no matching money quote in the source.

    A provider can return a syntactically valid price even when the broker
    never stated one. The raw WhatsApp message is authoritative, so a price
    is retained only when its quoted number/unit is present in the source.
    """
    price = extraction.get("price")
    if not isinstance(price, dict) or price.get("amount") is None:
        return extraction
    source = str(raw_text or "")
    listing_type = str(extraction.get("listing_type") or "").strip().lower()

    # Broker messages often put both modes in one block using shorthand such
    # as ``Quote - 500 psf`` and ``Price Sale - 85k per sqft``. The generic PSF
    # matcher cannot safely choose between them, so prefer the quote whose
    # label matches the item's route before trusting the provider value.
    psf_quotes = []
    for line in source.splitlines():
        clean_line = re.sub(r"[*_]", "", line).strip()
        match = re.search(
            r"\b(?P<label>price\s+sale|sale\s+price|sale|quote|rent|rental|rate)\b"
            r"[^0-9]{0,80}(?P<rate>\d[\d,]*(?:\.\d+)?)\s*"
            r"(?P<multiplier>m|mn|million|k)?\s*"
            r"(?:psf|per\s*/?\s*sq\.?\s*ft|per\s+sqft|per\s+square\s+feet)\b",
            clean_line,
            re.IGNORECASE,
        )
        if not match:
            continue
        label = re.sub(r"\s+", " ", match.group("label").casefold()).strip()
        rate = _coerce_float(match.group("rate").replace(",", ""))
        multiplier = (match.group("multiplier") or "").casefold()
        if rate is None:
            continue
        if multiplier == "k":
            rate *= 1000
        elif multiplier in {"m", "mn", "million"}:
            rate *= 1_000_000
        psf_quotes.append((label, rate, clean_line))

    selected_psf = None
    if psf_quotes:
        sale_labels = {"price sale", "sale price", "sale"}
        rent_labels = {"quote", "rent", "rental", "rate"}
        preferred = [
            item for item in psf_quotes
            if item[0] in (sale_labels if listing_type == "sale" else rent_labels)
        ]
        selected_psf = (preferred or psf_quotes)[0]
        if len(psf_quotes) > 1:
            extraction["needs_review"] = True
            extraction["validation_flags"] = list(dict.fromkeys(
                list(extraction.get("validation_flags") or []) + ["mixed_sale_rent_price_quotes"]
            ))

    # Mixed blocks can contain separate quotes such as ``For Rent 2.25 L``
    # and ``For Sale 5.25 Cr``. Providers occasionally attach the first quote
    # to both items, so select the quote attached to this item's mode first.
    labeled_quotes = re.findall(
        r"(?im)\bfor\s+(rent|sale)\b[^\n]{0,80}?"
        r"(?:aed|dhs)?\s*([\d,]+(?:[.:]\d+)?)\s*"
        r"(m|mn|millions?|k|thousands?)\b",
        source,
    )
    mode_quotes = [quote for quote in labeled_quotes if quote[0].lower() == listing_type]
    if mode_quotes:
        mode, amount_text, unit_text = mode_quotes[0]
        amount = _coerce_float(amount_text.replace(":", "."))
        unit = unit_text.lower().rstrip("s")
        if amount is not None:
            normalized_unit = "M" if unit in {"m", "mn", "million"} else unit
            extraction["price"] = {
                **price,
                "amount": canonical_price_aed(amount, normalized_unit),
                "unit": "total",
                "period": "per_month" if listing_type == "rent" else "one_time",
                "raw_price_text": f"For {mode.title()} {amount_text} {unit_text}",
            }
            price = extraction["price"]

    # Explicit labels in the broker's source outrank provider guesses. This
    # prevents a nearby number (for example ``1280`` in a generated title)
    # from displacing ``PRICE 1.5M`` in the actual message.
    explicit_quote = None if mode_quotes else re.search(
        r"(?im)(?:^|\n)\s*[*_\s]*(?:price|asking(?:\s+price)?|sale\s+price|rent)\b"
        r"[^0-9]*(?:aed|dhs)?\s*"
        r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<unit>m|mn|millions?|k|thousands?)\b",
        source,
    )
    if explicit_quote:
        amount = _coerce_float(explicit_quote.group("amount").replace(":", "."))
        unit = explicit_quote.group("unit").lower().rstrip("s")
        if amount is not None:
            normalized_unit = "M" if unit in {"m", "mn", "million"} else unit
            extraction["price"] = {
                **price,
                "amount": canonical_price_aed(amount, normalized_unit),
                "unit": "total",
                "period": "per_month" if listing_type == "rent" else "one_time",
                "raw_price_text": re.sub(r"[*_]", "", explicit_quote.group(0)).strip(),
            }
            price = extraction["price"]
    if selected_psf is not None:
        extraction["price"] = {
            **price,
            "amount": selected_psf[1],
            "unit": "per_sqft",
            "period": None,
            "raw_price_text": selected_psf[2],
        }
        return extraction

    source_numbers = {
        value.replace(",", "")
        for value in re.findall(r"\d+(?:[.,]\d+)?", source)
    }
    raw_quote = str(price.get("raw_price_text") or "")
    quote_numbers = {
        value.replace(",", "")
        for value in re.findall(r"\d+(?:[.,]\d+)?", raw_quote)
    }
    quote_units = re.findall(r"\b(?:m|mn|millions?|k|thousands?)\b", raw_quote.lower())
    source_lower = source.lower()
    quote_is_present = bool(quote_numbers) and quote_numbers.issubset(source_numbers)
    units_are_present = all(re.search(rf"\b{re.escape(unit)}\b", source_lower) for unit in quote_units)
    has_explicit_money = bool(re.search(
        r"(?:aed|dhs)\s*\d|\d+(?:[.,]\d+)?\s*"
        r"(?:m|mn|millions?|k|thousands?)\b",
        source,
        re.I,
    ))
    if not (has_explicit_money and quote_is_present and units_are_present):
        extraction["price"] = {"amount": None, "unit": None, "period": None, "raw_price_text": None}
        extraction["needs_review"] = True
    return extraction


def _apply_deterministic_field_fallbacks(extraction: dict, raw_text: str) -> dict:
    """Recover unambiguous schema facts when a provider omits them.

    Intent is corrected here when the raw WhatsApp text contains an
    unambiguous transaction marker.  This protects the database from an LLM
    guessing ``rent`` for messages such as ``Available Sale ... Price 1.90
    Cr``.  The correction is deliberately limited to exclusive markers; a
    message advertising both sale and rent still needs item-level parsing.
    """
    text = raw_text or ""
    lowered = text.lower()

    # Recover high-signal facts that are often omitted by providers despite
    # being plainly present in the source message.
    if extraction.get("possession_date") is None:
        possession_match = re.search(
            r"\bpossession\s*[:\-]?\s*(\d{1,2})(?:st|nd|rd|th)?\s+"
            r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
            r"dec(?:ember)?)\s+(20\d{2})",
            text,
            re.I,
        )
        if possession_match:
            months = {
                "jan": 1, "january": 1, "feb": 2, "february": 2,
                "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
                "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8,
                "august": 8, "sep": 9, "september": 9, "oct": 10,
                "october": 10, "nov": 11, "november": 11, "dec": 12,
                "december": 12,
            }
            month = months[possession_match.group(2).lower()]
            extraction["possession_date"] = (
                f"{possession_match.group(3)}-{month:02d}-{int(possession_match.group(1)):02d}"
            )
            extraction["possession_status"] = extraction.get("possession_status") or "available"

    if extraction.get("car_parking_count") is None and re.search(
        r"\bno\s+(?:car\s+)?parking\b", lowered
    ):
        extraction["car_parking_count"] = 0
        extraction["parking_type"] = extraction.get("parking_type") or "none"

    locality = extraction.get("locality")
    if not isinstance(locality, dict):
        locality = {"raw_mention": None, "resolved_locality": None, "confidence": "low"}
        extraction["locality"] = locality
    if not locality.get("raw_mention"):
        # A common compact format is ``Building, Ahimsa marg Khar``. Keep the
        # source wording; locality resolution later can canonicalize it.
        comma_match = re.search(r"(?im)^.*?,\s*([^\n,]+)$", text)
        if comma_match:
            candidate = comma_match.group(1).strip(" *.,")
            if re.search(
                r"\b(?:marina|jbr|jvc|jlt|business\s+bay|downtown|difc|"
                r"palm\s+jumeirah|barsha|furjan|springs|meadows|greens|"
                r"hills?|ranches|deira|karama|mirdif|road|street)\b",
                candidate,
                re.I,
            ):
                locality["raw_mention"] = candidate
                locality["confidence"] = "high"

    explicit_sale = re.search(
        r"\b(?:available\s+(?:for\s+)?sale|for\s+sale|sale\s+price|outright|outrate)\b",
        lowered,
    )
    explicit_rent = re.search(
        r"\b(?:available\s+(?:for\s+)?rent|for\s+rent|monthly\s+rent|rent\s*[-:])\b",
        lowered,
    )
    if explicit_sale and not explicit_rent and extraction.get("listing_type") in {"rent", "sale"}:
        extraction["listing_type"] = "sale"
        extraction["needs_review"] = False
    elif explicit_rent and not explicit_sale and extraction.get("listing_type") in {"rent", "sale"}:
        extraction["listing_type"] = "rent"
        extraction["needs_review"] = False
    if extraction.get("listing_type") in {"rent", "sale"}:
        extraction["listing_type"] = source_transaction_type(text, extraction.get("listing_type"))

    # Requirement messages often contain unambiguous ranges/budgets but the
    # provider may omit the route-specific fields. Recover only explicit
    # values; never infer a budget or area from a listing-like phrase.
    if extraction.get("listing_type") == "requirement" or re.search(
        r"\b(?:require|required|requirement|looking\s+for|need|wanted)\b", lowered
    ):
        if extraction.get("bhk") is None and not extraction.get("bhk_options"):
            bhk_match = re.search(r"\b(\d+(?:\.\d+)?)\s*bhk\b", text, re.I)
            if bhk_match:
                extraction["bhk"] = _coerce_float(bhk_match.group(1))

        if extraction.get("area_min_sqft") is None:
            range_match = re.search(
                r"\b([\d,]+)\s*[-–]\s*([\d,]+)\s*(?:sq\.?\s*ft\.?|sqft|sft)\b",
                text, re.I,
            )
            if range_match:
                extraction["area_min_sqft"] = _coerce_float(range_match.group(1))
                extraction["area_max_sqft"] = _coerce_float(range_match.group(2))

        if extraction.get("budget_max") is None:
            budget_match = re.search(
                r"\bbudget\s*[:\-]?\s*(?:up\s+to\s*)?(?:aed|dhs\s*)?([\d,.]+)\s*(m|mn|millions?|k|thousands?)?\b",
                text, re.I,
            )
            if budget_match:
                amount = _coerce_float(budget_match.group(1))
                if amount is not None:
                    unit = (budget_match.group(2) or "").lower().rstrip("s")
                    multiplier = {"m": 1_000_000, "mn": 1_000_000, "million": 1_000_000, "k": 1_000, "thousand": 1_000}.get(unit, 1)
                    extraction["budget_max"] = amount * multiplier

        if not extraction.get("locality_options"):
            locality_match = re.search(
                r"\b(?:anywhere\s+in|location|preferred\s+locations?)\s*[:\-]?\s*([^\n]+)",
                text, re.I,
            )
            if locality_match:
                locality_text = locality_match.group(1).strip(" *")
                parts = re.split(r"\s*(?:,|&|\band\b)\s*", locality_text, flags=re.I)
                if len(parts) == 1 and re.search(r"\b(?:anywhere|preferred|location)\b", locality_match.group(0), re.I):
                    known = re.findall(
                        r"\b(?:Marina|JBR|JVC|JLT|Business\s+Bay|Downtown|DIFC|Barsha|Furjan|Springs|Meadows|Greens|Deira|Karama|Mirdif)\b",
                        locality_text,
                        re.I,
                    )
                    if known:
                        parts = known
                extraction["locality_options"] = [p.strip() for p in parts if p.strip()]

        furnishing_preference = str(extraction.get("furnishing_preference") or "").strip().lower().replace(" ", "_").replace("-", "_")
        if furnishing_preference in _FURNISHING_ALIASES:
            extraction["furnishing_preference"] = _FURNISHING_ALIASES[furnishing_preference]
        elif re.search(r"\bfully\s+loaded\b", lowered):
            extraction["furnishing_preference"] = "fully_furnished"

        if extraction.get("tenant_type") is None:
            tenant_match = re.search(r"\btenant\s*[:\-]?\s*([^\n]+)", text, re.I)
            if tenant_match:
                extraction["tenant_type"] = tenant_match.group(1).strip(" *")

        if re.search(r"\bexpat\b", lowered) and not extraction.get("tenant_type"):
            extraction["tenant_type"] = "expat"
        if re.search(r"\bcompany\s+lease\b", lowered):
            extraction["company_lease_criteria"] = True
            extraction["lease_term_preference"] = extraction.get("lease_term_preference") or "company_lease"

        if extraction.get("car_parking_min") is None and re.search(
            r"\b(?:open|covered)?\s*car\s*parking\s+required\b|\bparking\s+required\b",
            lowered,
        ):
            extraction["car_parking_min"] = 1

        amenity_requirements = list(extraction.get("amenity_requirements") or [])
        if re.search(r"\bmodular\s+kitchen|kitchen\s+trolley\b", lowered):
            if "modular_kitchen" not in amenity_requirements:
                amenity_requirements.append("modular_kitchen")
        if re.search(r"\bgas\s+pipeline\b", lowered):
            if "gas_pipeline" not in amenity_requirements:
                amenity_requirements.append("gas_pipeline")
        if amenity_requirements:
            extraction["amenity_requirements"] = amenity_requirements

        if not extraction.get("commercial_use_type"):
            use_match = re.search(r"\bfor\s+a\s+([a-z][a-z ]{2,40}?)\s+(?:on|basis|in)\b", text, re.I)
            if use_match:
                extraction["commercial_use_type"] = use_match.group(1).strip().lower()

    if extraction.get("carpet_area_sqft") is None:
        area_match = re.search(
            r"(?i)\b(?:carpet\s*)?area\s*[:\-]?\s*([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*ft\.?|sqft|sft|carpet)?",
            text,
        )
        if area_match:
            extraction["carpet_area_sqft"] = _coerce_float(area_match.group(1))
            extraction["area_raw_text"] = area_match.group(0).strip()

    if not extraction.get("fitout_status"):
        if re.search(r"\bbare\s*shell\b|\bbareshell\b", lowered):
            extraction["fitout_status"] = "bare_shell"
        elif re.search(r"\bwarm\s*shell\b", lowered):
            extraction["fitout_status"] = "warm_shell"
        elif re.search(r"\bbuilder(?:'s)?\s*finish\b", lowered):
            extraction["fitout_status"] = "builder_finish"

    if extraction.get("occupancy_status") is None and re.search(
        r"\bpre[-\s]?leased\b|\bpre[-\s]?rented\b", lowered
    ):
        extraction["occupancy_status"] = "pre_leased"

    if extraction.get("car_parking_count") is None:
        parking_match = re.search(
            r"(?i)\b(?:car\s*)?park(?:ing)?\s*[:\-]?\s*(\d+)\b|\b(\d+)\s*car\s*parks?\b",
            text,
        )
        if parking_match:
            extraction["car_parking_count"] = _coerce_int(next(g for g in parking_match.groups() if g))

    tags = list(extraction.get("deal_tags") or [])
    if re.search(r"\b(?:brand\s*new|new)\s+building\b", lowered) and "brand_new_building" not in tags:
        tags.append("brand_new_building")
    extraction["deal_tags"] = tags
    return extraction


# ── Locality resolution ───────────────────────────────────────────────

_LIKE_ESCAPE_RE = re.compile(r"([%_\\])")


def _escape_like(s: str) -> str:
    return _LIKE_ESCAPE_RE.sub(r"\\\1", s)


def resolve_locality(raw_mention: str | None, storage=None) -> dict:
    """Resolve a raw locality mention to its parent locality.

    Steps:
        1. Exact match against locality_reference.sub_locality
        2. Case-insensitive match
        3. Substring / like match
        4. If storage is None, return AI-inferred value as-is
    """
    if not raw_mention or not raw_mention.strip():
        return {"resolved_locality": None, "confidence": "low", "raw_mention": raw_mention}

    mention = raw_mention.strip()

    if storage is None:
        return {"resolved_locality": None, "confidence": "low", "raw_mention": mention}

    try:
        db = storage.client if hasattr(storage, "client") else None
        if not db:
            return {"resolved_locality": None, "confidence": "low", "raw_mention": mention}

        # Try exact match first
        res = db.table("locality_reference").select("parent_locality, confidence").eq(
            "sub_locality", mention
        ).limit(1).execute()
        if res.data:
            row = res.data[0]
            return {
                "resolved_locality": row["parent_locality"],
                "confidence": row.get("confidence") or "medium",
                "raw_mention": mention,
            }

        # Case-insensitive via ilike
        res = db.table("locality_reference").select("parent_locality, confidence").ilike(
            "sub_locality", mention
        ).limit(1).execute()
        if res.data:
            row = res.data[0]
            return {
                "resolved_locality": row["parent_locality"],
                "confidence": row.get("confidence") or "medium",
                "raw_mention": mention,
            }

        # Substring match — check if mention contains a known sub-locality
        res = db.table("locality_reference").select("sub_locality, parent_locality, confidence").limit(200).execute()
        if res.data:
            mention_lower = mention.lower()
            for row in res.data:
                sub = (row.get("sub_locality") or "").lower()
                if sub and sub in mention_lower:
                    return {
                        "resolved_locality": row["parent_locality"],
                        "confidence": row.get("confidence") or "medium",
                        "raw_mention": mention,
                    }

    except Exception:
        _logger.warning("locality_reference query failed for %r", mention, exc_info=True)

    return {"resolved_locality": None, "confidence": "low", "raw_mention": mention}


def _canonical_locality_from_mention(raw_mention: str | None) -> str | None:
    """Resolve embedded locality mentions through the shared Mumbai rules.

    ``locality_reference`` is intentionally conservative and may not contain
    street + locality phrases such as ``Ahimsa Marg Khar``. The deterministic
    location module can still identify the unique locality, then apply the
    existing implied-direction rule (Khar -> Khar West).
    """
    if not raw_mention or not str(raw_mention).strip():
        return None
    try:
        from location import canonical_micro_market_slug, infer_unique_micro_market

        inferred = infer_unique_micro_market(str(raw_mention))
        slug = canonical_micro_market_slug(inferred or str(raw_mention))
        if slug:
            return slug.replace("-", " ").title()
    except Exception:
        _logger.debug("deterministic locality inference failed for %r", raw_mention, exc_info=True)
    return None


def locality_from_building_name(building_name: str | None, storage=None) -> dict:
    """Look up a building's known locality from the buildings table.

    Used as a fallback when the raw message didn't mention a locality but
    the LLM extracted a building name. Returns the building's
    micro_market if found, so the listing gets the correct locality
    instead of inheriting one from the WhatsApp group name.
    """
    if not building_name or not building_name.strip():
        return {"resolved_locality": None, "confidence": "low"}

    if storage is None:
        return {"resolved_locality": None, "confidence": "low"}

    try:
        db = storage.client if hasattr(storage, "client") else None
        if not db:
            return {"resolved_locality": None, "confidence": "low"}

        name = building_name.strip()
        tenant_id = str(getattr(storage, "tenant_id", None) or getattr(storage, "_tenant_id", None) or "__shared__")
        cache_key = (tenant_id, name.casefold())
        now = time.monotonic()
        with _REFERENCE_CACHE_LOCK:
            cached = _BUILDING_LOCALITY_CACHE.get(cache_key)
        if cached and now - cached[0] < _REFERENCE_CACHE_TTL_SECONDS:
            return dict(cached[1])

        def remember(value: dict) -> dict:
            with _REFERENCE_CACHE_LOCK:
                if len(_BUILDING_LOCALITY_CACHE) >= _REFERENCE_CACHE_MAX_TENANTS * 256 and cache_key not in _BUILDING_LOCALITY_CACHE:
                    oldest = min(_BUILDING_LOCALITY_CACHE, key=lambda key: _BUILDING_LOCALITY_CACHE[key][0])
                    _BUILDING_LOCALITY_CACHE.pop(oldest, None)
                _BUILDING_LOCALITY_CACHE[cache_key] = (time.monotonic(), dict(value))
            return value

        res = db.table("buildings").select("micro_market").eq(
            "canonical_name", name
        ).limit(1).execute()
        if res.data and res.data[0].get("micro_market"):
            return remember({
                "resolved_locality": res.data[0]["micro_market"],
                "confidence": "high",
                "source": "buildings_table",
            })

        # Case-insensitive fallback
        res = db.table("buildings").select("micro_market").ilike(
            "canonical_name", name
        ).limit(1).execute()
        if res.data and res.data[0].get("micro_market"):
            return remember({
                "resolved_locality": res.data[0]["micro_market"],
                "confidence": "high",
                "source": "buildings_table",
            })

        # Try building_name_aliases
        res = db.table("building_name_aliases").select("canonical_name").ilike(
            "alias", name
        ).limit(1).execute()
        if res.data:
            canonical = res.data[0].get("canonical_name")
            if canonical:
                res2 = db.table("buildings").select("micro_market").eq(
                    "canonical_name", canonical
                ).limit(1).execute()
                if res2.data and res2.data[0].get("micro_market"):
                    return remember({
                        "resolved_locality": res2.data[0]["micro_market"],
                        "confidence": "medium",
                        "source": "building_name_aliases",
                    })

    except Exception:
        _logger.warning("building_name locality lookup failed for %r", building_name, exc_info=True)

    return remember({"resolved_locality": None, "confidence": "low"})


# ── Title generation (shared between app + www) ────────────────────────

def generate_title(extraction: dict) -> str:
    """Generate human-readable title from structured extraction fields.

    This is the canonical title builder — used by both the app and www.
    Never copy-pastes raw broker text as title.
    """
    listing_type = extraction.get("listing_type")
    property_category = extraction.get("property_category")
    bhk = extraction.get("bhk")
    building_name = extraction.get("building_name")
    locality = extraction.get("locality", {})
    resolved_locality = locality.get("resolved_locality") if isinstance(locality, dict) else None
    raw_mention = locality.get("raw_mention") if isinstance(locality, dict) else None
    price = extraction.get("price", {})
    amenities = extraction.get("amenities", [])

    pieces = []

    if listing_type == "requirement":
        pieces.append("Requirement:")

    # BHK / property type prefix
    bhk_value = _coerce_float(bhk)
    if bhk_value:
        if bhk_value == 0.5:
            pieces.append("1 RK")
        elif bhk_value == int(bhk_value):
            pieces.append(f"{int(bhk_value)} BHK")
        else:
            pieces.append(f"{bhk_value:g} BHK")
    elif property_category == "commercial":
        pieces.append("Commercial")

    # Transaction type
    if listing_type == "sale":
        pieces.append("for Sale")
    elif listing_type == "rent":
        pieces.append("for Rent")

    # Locality
    loc_parts = []
    if resolved_locality and resolved_locality.strip():
        loc_parts.append(resolved_locality)
        if raw_mention and raw_mention.lower() != resolved_locality.lower():
            loc_parts.append(f"({raw_mention})")
    elif raw_mention:
        loc_parts.append(raw_mention)
    if loc_parts:
        pieces.append("in " + " ".join(loc_parts))

    # Building
    if building_name:
        pieces.append(f"— {building_name}")

    # Price
    price_amount = None
    price_raw = None
    if isinstance(price, dict):
        price_amount = _coerce_float(price.get("amount"))
        price_raw = price.get("raw_price_text")

    if price_amount is not None and price_amount > 0:
        period = price.get("period") if isinstance(price, dict) else None
        is_rent = listing_type == "rent" or period == "per_month"
        price_str = _format_price_amount(price_amount, is_rent)
        pieces.append(f"— {price_str}")
    elif price_raw:
        pieces.append(f"— {price_raw}")
    elif listing_type == "requirement":
        if isinstance(price, dict) and price.get("raw_price_text"):
            pieces.append(f"— Budget {price['raw_price_text']}")

    title = " ".join(pieces)
    return title.strip() if title.strip() else "Listing"


_PRICE_SCALES = [
    (1_000_000, "M", 1_000_000),
    (1_000, "K", 1_000),
]
_MAX_PLAUSIBLE_ANNUAL_RENT = 5_000_000


def _format_price_amount(amount: float, is_rent: bool = False) -> str:
    if amount <= 0:
        return "Price on request"
    if is_rent and amount > _MAX_PLAUSIBLE_ANNUAL_RENT:
        _logger.warning(
            "Annual rent exceeds plausibility ceiling; formatting as non-rent amount: %s",
            amount,
        )
        is_rent = False
    for threshold, label, divisor in _PRICE_SCALES:
        if amount >= threshold:
            value = amount / divisor
            if value == int(value):
                formatted_value = str(int(value))
            else:
                # Preserve the source precision needed to distinguish prices
                # such as 8.75M from 8.8M; only remove insignificant zeros.
                formatted_value = f"{value:.2f}".rstrip("0").rstrip(".")
            fmt = f"AED {formatted_value} {label}"
            if is_rent:
                fmt += "/yr"
            return fmt
    fmt = f"AED {int(amount):,}"
    if is_rent:
        fmt += "/yr"
    return fmt


# ── Image detection ──────────────────────────────────────────────────

def _has_flyer_image(ctx: dict) -> bool:
    msg = ctx.get("msg", {})
    if not isinstance(msg, dict):
        return False
    has_image = "imageMessage" in msg
    if not has_image:
        return False
    msg_text = ctx.get("msg_text", "")
    return len(msg_text.strip()) < 100


# ── Main extraction function ──────────────────────────────────────────

def _call_provider(
    provider: dict,
    messages: list[dict],
    timeout: int = _EXTRACTION_PROVIDER_TIMEOUT,
    *,
    source_id: int | None = None,
    tenant_id: str | None = None,
) -> dict | list | None:
    """Call a single LLM provider. Returns a parsed JSON object/array or None.

    Logs every completed API call (success or truncated) to ai_usage_log so
    cost is never silently lost.
    """
    from usage_logger import log_ai_usage

    started = time.monotonic()
    try:
        _wait_for_provider_cooldown(provider["name"])
        client = OpenAI(api_key=provider["api_key"], base_url=provider["base_url"])
        request = dict(
            model=provider["model"],
            messages=messages,
            temperature=0.1,
            max_tokens=4096,
            timeout=timeout,
        )
        # Enable JSON mode for providers that support it (Haiku 4.5, etc.)
        request["response_format"] = {"type": "json_object"}
        # Keep backlog extraction fast and predictable.  Doubleword accepts
        # the OpenAI-compatible reasoning_effort field, not provider-specific
        # `thinking` payloads.
        if provider.get("reasoning_effort"):
            request["reasoning_effort"] = provider["reasoning_effort"]
        resp = client.chat.completions.create(**request)
        rate_headers = _response_headers(resp)
        if rate_headers:
            _logger.info("Provider %s rate-limit headers: %s", provider["name"], rate_headers)
        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0

        choice = resp.choices[0]
        raw = choice.message.content
        truncated_no_content = False
        if not raw or not raw.strip():
            reasoning = getattr(choice.message, "reasoning_content", None)
            finish_reason = getattr(choice, "finish_reason", None)
            truncated_no_content = True
            if reasoning:
                _logger.warning(
                    "Provider %s returned reasoning but no final JSON (finish=%s)",
                    provider["name"], finish_reason,
                )
            else:
                _logger.warning(
                    "Provider %s returned empty content (finish=%s)",
                    provider["name"], finish_reason,
                )
            # Log the spend even though the output was empty
            log_ai_usage(
                agent="extraction",
                model=provider["model"],
                tokens_input=tokens_in,
                tokens_output=tokens_out,
                source="raw_message",
                source_id=source_id,
                provider_name=provider["name"],
                tenant_id=tenant_id,
                truncated=True,
            )
            return None

        # Log successful call
        log_ai_usage(
            agent="extraction",
            model=provider["model"],
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            source="raw_message",
            source_id=source_id,
            provider_name=provider["name"],
            tenant_id=tenant_id,
        )

        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
        parsed = _extract_json_object(cleaned)
        if parsed is None:
            _logger.warning("Provider %s returned unparseable output (%d chars)", provider["name"], len(raw))
            return "MALFORMED"
        # Preserve the structured envelope. `message_class` and `listing_count`
        # are document-level evidence needed by the route-aware second pass
        # and must survive into each normalized item. Legacy array responses
        # remain supported by the caller.
        return parsed
    except json.JSONDecodeError:
        _logger.warning("Provider %s returned malformed JSON", provider["name"])
        return "MALFORMED"
    except Exception as exc:
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        elapsed = time.monotonic() - started
        if status == 429:
            rate_headers = _response_headers(exc)
            cooldown = _retry_after_seconds(rate_headers)
            _cooldown_provider(provider["name"], cooldown)
            _logger.warning(
                "Provider %s rate-limited (429); cooling down %.1fs headers=%s",
                provider["name"], cooldown, rate_headers or "unavailable",
            )
            return "RATE_LIMITED"
        else:
            _logger.warning(
                "Provider %s failed after %.1fs (status=%s, type=%s): %s",
                provider["name"],
                elapsed,
                status or "unknown",
                type(exc).__name__,
                exc,
            )
        return None


def ai_extract(raw_text: str, ctx: dict | None = None, storage=None) -> dict:
    """Extract one source unit using route-aware AI provider fallbacks.

    Convincing bulk templates are split into child raw messages by the
    orchestrator before this function is called. Ambiguous/mixed prose remains
    supported by the unified first pass here.

    Returns a dict with:
        extraction: dict — first normalized extraction result (compatibility)
        extractions: list[dict] — every normalized opportunity in the message
        extraction_source: "ai" | "ai_unavailable" | "image_unprocessed"
        needs_review: bool
        provider_used: str | None
        error: str | None
    """
    start = time.time()
    result = {
        "extraction": None,
        "extractions": [],
        "extraction_source": None,
        "needs_review": False,
        "provider_used": None,
        "error": None,
        "document": None,
    }

    # Empty messages with attachment/reply metadata still go through the AI
    # contract; the model may classify them as irrelevant or review-needed.
    if not raw_text and not (ctx or {}).get("attachments") and not (ctx or {}).get("reply_context"):
        result["extraction_source"] = "ai_unavailable"
        result["needs_review"] = True
        result["extraction"] = None
        _logger.info("ai_extract: text too short (%s)", time.time() - start)
        return result

    # Semantic work belongs to the model.  Keep the raw body byte-for-byte in
    # the user message; reply and attachment metadata are separate context.
    alias_context = _building_alias_context(raw_text, ctx, storage=storage)
    locality_context = _locality_reference_context(storage)
    learning_examples = []
    if storage is not None and hasattr(storage, "get_extraction_learning_examples"):
        try:
            learning_examples = storage.get_extraction_learning_examples(
                raw_text,
                tenant_id=(ctx or {}).get("tenant_id"),
                limit=3,
            )
        except Exception:
            _logger.debug("extraction learning examples unavailable", exc_info=True)
    raw_context = {
        "message": raw_text,
        "reply_context": (ctx or {}).get("reply_context") or {},
        "attachments": (ctx or {}).get("attachments") or [],
        "group_name": (ctx or {}).get("group_name") or "",
        "tenant_id": (ctx or {}).get("tenant_id"),
        "known_buildings": alias_context,
        "known_localities": locality_context,
        # These are approved, tenant-scoped corrections. They are guidance,
        # never facts: the source message remains authoritative.
        "approved_correction_examples": [
            {
                "source": str(item.get("source_text") or "")[:1600],
                "field": item.get("field_name"),
                "corrected_value": item.get("corrected_value"),
            }
            for item in learning_examples
        ],
    }
    result["document"] = {"raw": True, "alias_context_count": len(alias_context)}

    classified_asset, classified_transaction, classified_requirement = _classify_message_flags(raw_text)

    def build_messages(system_prompt: str, message_text: str) -> list[dict]:
        focused_context = dict(raw_context)
        focused_context["message"] = message_text
        return [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "Interpret this raw message and return the requested JSON.\n\n"
                    f"{json.dumps(focused_context, ensure_ascii=False)}"
                ),
            },
        ]

    # The first pass must remain route-neutral: a single WhatsApp broadcast
    # can contain residential, commercial, sale, rent, and requirement items.
    messages = build_messages(_UNIFIED_EXTRACTION_PROMPT, raw_text)

    # Try providers in round-robin, up to total provider count attempts
    attempts = 0
    max_attempts = len(_PROVIDERS) * 2  # Allow two full rotations
    last_error = None
    _src_id = ctx.get("raw_id") if ctx else None
    if not isinstance(_src_id, int) and ctx:
        _src_id = ctx.get("message_id")
    if not isinstance(_src_id, int):
        _src_id = None
    _tid = ctx.get("tenant_id") if ctx else None

    def normalize_provider_response(
        raw_response,
        provider_name: str,
        *,
        source_text: str = raw_text,
        fallback_route: tuple[str, str, bool] | None = None,
        message_class_override: str | None = None,
        listing_count_override: int | None = None,
    ) -> tuple[list[dict], str | None]:
        envelope = raw_response if isinstance(raw_response, dict) else {}
        message_class = message_class_override or envelope.get("message_class")
        listing_count = (
            listing_count_override
            if listing_count_override is not None
            else envelope.get("listing_count")
        )
        fallback_asset, fallback_transaction, fallback_requirement = fallback_route or (
            classified_asset, classified_transaction, classified_requirement
        )
        candidates = envelope.get("items") if isinstance(envelope.get("items"), list) else raw_response
        candidates = candidates if isinstance(candidates, list) else [candidates]
        normalized_items: list[dict] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate = dict(candidate)
            if not candidate.get("listing_type"):
                candidate["listing_type"] = "requirement" if fallback_requirement else fallback_transaction
            if not candidate.get("property_category"):
                candidate["property_category"] = fallback_asset
            candidate.update({
                "message_class": message_class,
                "listing_count": listing_count,
            })
            normalized = _normalize_extraction(candidate)
            normalized = _source_ground_asset_category(normalized, source_text)
            normalized = _source_grounded_furnishing(normalized, source_text)
            normalized = _repair_locality_only_building(normalized, locality_context)
            normalized["building_context_allowed"] = bool(
                normalized.get("building_id")
                and any(item.get("building_id") == normalized.get("building_id") for item in alias_context)
            )
            normalized = _source_grounded_price(normalized, source_text)
            normalized = validate_source_semantics(normalized, source_text)
            if normalized.get("listing_type") is None:
                _logger.warning(
                    "Provider %s: skipped item listing_type=%r transaction_type=%r category=%r keys=%s",
                    provider_name, candidate.get("listing_type"),
                    candidate.get("transaction_type"), candidate.get("property_category"),
                    sorted(candidate.keys()),
                )
                continue
            normalized["classified_asset_type"] = fallback_asset
            normalized["classified_transaction_type"] = fallback_transaction
            normalized["classified_is_requirement"] = fallback_requirement
            if not normalized.get("title"):
                normalized["title"] = generate_title(normalized)
            normalized_items.append(normalized)
        return normalized_items, message_class

    while attempts < max_attempts:
        provider = _next_provider()
        if provider is None:
            last_error = "No providers configured"
            break

        attempts += 1
        raw_extraction = _call_provider(
            provider,
            messages,
            timeout=_EXTRACTION_PROVIDER_TIMEOUT,
            source_id=_src_id,
            tenant_id=_tid,
        )

        if raw_extraction == "MALFORMED":
            # Provider returned content but it can't be parsed as JSON.
            # No point sleeping — try the next provider immediately; they
            # produce structurally different outputs so ask Gemini next
            # instead of looping the same lane.
            continue

        if raw_extraction == "RATE_LIMITED":
            last_error = f"Provider {provider['name']} rate limited"
            # The provider-specific cooldown is set from Retry-After above.
            # Keep this worker task from immediately cycling through the same
            # account while another lane is also backing off.
            time.sleep(1.0)
            continue

        if raw_extraction is None:
            # Network/429/empty — small backoff suits a few concurrent workers
            # sharing rate-limited headroom without burning the whole timeout.
            import time as _time
            _time.sleep(min(attempts * 1.0, 3))
            continue

        if isinstance(raw_extraction, dict) and isinstance(raw_extraction.get("items"), list) and not raw_extraction["items"]:
            result["extraction_source"] = "ai"
            result["provider_used"] = provider["name"]
            result["message_class"] = raw_extraction.get("message_class")
            return result
        normalized_items, message_class = normalize_provider_response(raw_extraction, provider["name"])

        if not normalized_items:
            _logger.warning("Provider %s: schema validation failed (no valid listings)", provider["name"])
            continue

        if not message_class:
            message_class = "requirement" if classified_requirement else "listing"
            for item in normalized_items:
                item["message_class"] = message_class

        # Second pass: preserve the unified pass for classification and rough
        # item discovery, then use each item's focused route prompt for the
        # fields that are actually written. Mixed messages are segmented by
        # source block so one item's rules cannot leak into its neighbor.
        rough_items = normalized_items
        segments = (_segment_document(raw_text) or {}).get("blocks") or []
        focused_items: list[dict] = []
        for item_index, rough_item in enumerate(rough_items):
            item_listing_type = str(rough_item.get("listing_type") or "").lower()
            item_asset = str(rough_item.get("property_category") or "").lower()
            item_transaction = str(
                rough_item.get("transaction_type")
                or rough_item.get("routing_listing_type")
                or item_listing_type
                or ""
            ).lower()
            if item_transaction in {"lease", "pg", "joint_venture"}:
                item_transaction = "rent"
            item_requirement = item_listing_type == "requirement" or (
                message_class == "requirement"
            )
            if item_asset not in {"residential", "commercial"} or item_transaction not in {"sale", "rent"}:
                item_asset, item_transaction, item_requirement = _classify_message_flags(
                    (segments[item_index].get("text") if item_index < len(segments) else raw_text) or raw_text
                )
            route = (item_asset, item_transaction, item_requirement)
            if route not in _FOCUSED_FIELDS:
                focused_items.append(rough_item)
                continue
            source_slice = (
                str(segments[item_index].get("text") or "").strip()
                if item_index < len(segments)
                else raw_text
            ) or raw_text
            focused_messages = build_messages(
                _get_extraction_prompt(
                    item_asset,
                    item_transaction,
                    item_requirement,
                    mixed_transaction=message_class == "mixed",
                ),
                source_slice,
            )
            focused_raw = _call_provider(
                provider,
                focused_messages,
                timeout=_EXTRACTION_PROVIDER_TIMEOUT,
                source_id=_src_id,
                tenant_id=_tid,
            )
            focused_normalized, _ = normalize_provider_response(
                focused_raw,
                provider["name"],
                source_text=source_slice,
                fallback_route=route,
                message_class_override=message_class,
                listing_count_override=len(rough_items),
            )
            focused_items.extend(focused_normalized or [rough_item])
        used_focused_pass = bool(focused_items)
        if focused_items:
            normalized_items = focused_items

        # One bounded critic/repair pass. It is deliberately not recursive:
        # repeated prompting would increase cost without a reliable quality
        # guarantee. Keep the original response if repair does not improve it.
        if any(bool(item.get("needs_review")) for item in normalized_items) and not used_focused_pass:
            repair_messages = messages + [{
                "role": "user",
                "content": (
                    "Review the extraction below against the raw message. Repair only fields "
                    "that are unsupported, contradictory, or low-confidence. Return the same "
                    "JSON schema, preserving source-grounded values. Candidate:\n"
                    + json.dumps(raw_extraction, ensure_ascii=False, default=str)
                ),
            }]
            repaired_raw = _call_provider(
                provider,
                repair_messages,
                timeout=_EXTRACTION_PROVIDER_TIMEOUT,
                source_id=_src_id,
                tenant_id=_tid,
            )
            if isinstance(repaired_raw, (dict, list)):
                repaired_items, repaired_message_class = normalize_provider_response(repaired_raw, provider["name"])
                if repaired_items and sum(bool(item.get("needs_review")) for item in repaired_items) < sum(bool(item.get("needs_review")) for item in normalized_items):
                    normalized_items = repaired_items
                    message_class = repaired_message_class or message_class

        result["extraction"] = normalized_items[0]
        result["extractions"] = normalized_items
        result["extraction_source"] = "ai"
        result["provider_used"] = provider["name"]
        result["message_class"] = message_class
        # A successful provider response is not automatically trustworthy:
        # field-level source guards can quarantine one or more items while the
        # rest of the message remains usable.
        result["needs_review"] = any(
            bool(item.get("needs_review")) for item in normalized_items
        )

        _logger.info(
            "ai_extract: %d item(s) via %s in %.1fs",
            len(normalized_items), provider["name"], time.time() - start,
        )
        return result

    # ── All providers failed — retain raw source for review ───────
    result["extraction_source"] = "ai_unavailable"
    result["needs_review"] = True
    result["error"] = last_error or f"All {len(_PROVIDERS)} providers failed after {attempts} attempts"

    _logger.warning(
        "ai_extract: all providers failed in %.1fs — %s",
        time.time() - start, result["error"],
    )
    return result
