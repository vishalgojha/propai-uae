"""Async extraction pipeline — Layer 2-5 processing.

This module contains the shared extraction logic used by both:
  - The webhook background thread (runs per-message when webhook fires)
  - The extraction worker (poll-based, picks up unprocessed messages)

Extraction contract:
  1. Preserve the untouched WhatsApp event as the parent raw message.
  2. Deterministically split convincing bulk-broadcast templates into child
     raw messages, carrying shared headers into every property block.
  3. AI-extract each independent child (or the original single message).
  4. Persist source-grounded typed opportunities only.

Import pattern:
  from extraction import process_raw_message
"""

import json
import logging
import os
import hashlib
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_logger = logging.getLogger(__name__)

# A WhatsApp contact number is evidence for broker_phone, never a broker name.
_PHONE_LIKE_BROKER_NAME_RE = re.compile(r"^[+0-9 ()-]{7,15}$")
_BROKER_NAME_PREFIX_RE = re.compile(
    r"^(?:call\s+for\s+inspection|for\s+inspection|contact(?:\s+person)?|listed\s+by|listing\s+by|broker)"
    r"\s*(?:[:\-–|]\s*)?",
    re.IGNORECASE,
)
_BROKER_INSTRUCTION_RE = re.compile(
    r"\b(?:im+e?diately\s+)?(?:contact|call|whatsapp)\s*(?:no\.?|number)?\b|"
    r"\b(?:for\s+)?(?:details|inspection|visit|visits)\b|"
    r"\b(?:please\s+share|suitable\s+options)\b",
    re.IGNORECASE,
)
_BROKER_FIELD_LABELS = frozenset({
    "mobile", "phone", "contact", "contact person", "email", "e-mail",
    "whatsapp", "broker", "name", "address", "location",
})

# These are model-side absence markers, not domain values.  Persisting them
# makes empty fields look populated and, for boolean columns, can make the
# entire typed-row insert fail (for example: "Unknown" -> boolean).
_NULL_LIKE_EXTRACTION_VALUES = frozenset({
    "", "unknown", "not known", "not specified", "not available",
    "not identified", "not found", "n/a", "na", "none", "null", "nil",
})
_BOOLEAN_EXTRACTION_FIELDS = frozenset({
    "has_lift", "has_power_backup", "co_brokered", "plus_one_deal",
    "fee_sharing_required", "client_profile_required", "is_converted_unit",
    "is_combination_unit", "can_sell_separately", "balcony_present",
    "terrace_present", "sit_out_present", "vastu_compliant",
})

# Deterministic boundary detection must happen before the model. The model can
# still interpret each child slice, but a complete multi-property broadcast
# must never be sent to the model and split only after the call. Keep the old
# environment variable out of this decision: `EXTRACTION_LLM_FIRST=true` was
# allowing cross-listing leakage and wasting one model call per broadcast.


def _clean_extraction_value(value: object, *, key: str = "") -> object:
    """Convert provider absence markers to real nulls before persistence."""
    if isinstance(value, dict):
        return {
            child_key: _clean_extraction_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [cleaned for item in value
                if (cleaned := _clean_extraction_value(item, key=key)) is not None]
    if isinstance(value, str):
        text = re.sub(r"\s+", " ", value).strip()
        lowered = text.casefold()
        if lowered in _NULL_LIKE_EXTRACTION_VALUES:
            return None
        if key in _BOOLEAN_EXTRACTION_FIELDS:
            if lowered in {"true", "yes", "y", "1"}:
                return True
            if lowered in {"false", "no", "n", "0"}:
                return False
            return None
        return text
    return value


_GENERIC_TITLE_RE = re.compile(
    r"^(?:property|listing|property details|property opportunity|real estate property)"
    r"(?:\s+(?:for|on|available|details?|opportunity)\b.*)?$",
    re.IGNORECASE,
)


def _is_usable_extraction_title(title: object) -> bool:
    text = re.sub(r"\s+", " ", str(title or "")).strip(" -:|,;")
    if not text or re.match(r"^\[?unstructured\]?\b", text, re.IGNORECASE):
        return False
    if re.search(
        r"(?:—|–|\||:)\s*(?:none|null|unknown|not\s+(?:specified|identified|found))\s*$",
        text,
        re.IGNORECASE,
    ):
        return False
    return not _GENERIC_TITLE_RE.fullmatch(text)


def _clean_broker_name(value: object) -> str | None:
    text = str(value or "").strip()
    if re.search(r"(?:https?://)?(?:www\.)?wa\.me/|whatsapp", text, re.IGNORECASE):
        return None
    if not text or (_PHONE_LIKE_BROKER_NAME_RE.fullmatch(text) and re.search(r"\d", text)):
        return None
    text = _BROKER_NAME_PREFIX_RE.sub("", text).strip(" :-–|")
    if (
        not text
        or text.casefold().rstrip(":") in _BROKER_FIELD_LABELS
        or _BROKER_INSTRUCTION_RE.search(text)
        or (_PHONE_LIKE_BROKER_NAME_RE.fullmatch(text) and re.search(r"\d", text))
    ):
        return None
    return text


_REQUIREMENT_BUDGET_RANGE_RE = re.compile(
    r"\bbudget\s*[:\-]?\s*(?:aed\s*|dhs\s*)?([\d,.]+)\s*"
    r"(k|thousand|m|mn|million)?\s*"
    r"(?:-|\u2013|to|se)\s*(?:aed\s*|dhs\s*)?([\d,.]+)\s*"
    r"(k|thousand|m|mn|million)?\b",
    re.IGNORECASE,
)
_RENTAL_REQUIREMENT_CUE_RE = re.compile(
    r"\b(?:rent|rental|rantal|rant|lease|monthly|tenant|tenancy|family\s+party|"
    r"bachelor|company\s+lease|deposit|on\s+(?:l\s*&\s*l|ll|l\s*and\s*l))\b",
    re.IGNORECASE,
)
_REQUIREMENT_SINGLE_BUDGET_RE = re.compile(
    r"\b(?:budget|rent|rental|rantal|rant)\s*[:\-]?\s*(?:aed\s*|dhs\s*)?"
    r"([\d,.]+)\s*(k|thousand|m|mn|million)\b",
    re.IGNORECASE,
)
_REQUIREMENT_UP_TO_BUDGET_RE = re.compile(
    r"\bbudget\s*[:\-]?\s*(?:up\s*to|upto|maximum|max)\s*"
    r"(?:aed\s*|dhs\s*)?([\d,.]+)\s*"
    r"(k|thousand|m|mn|million)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_PG_RE = re.compile(
    r"\b(?:p\.?\s*g\.?|paying\s+guest|hostel|dorm(?:itory)?|co[-\s]?living)\b",
    re.IGNORECASE,
)


def _clean_budget_bound(value):
    """Store money bounds as stable AED integers, not float artifacts."""
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    rounded = round(numeric)
    return int(rounded) if abs(numeric - rounded) < 1e-6 else numeric


def _source_ground_requirement_item(item: dict, source_text: str) -> dict:
    """Correct requirement route/budget from explicit source evidence.

    Models sometimes misread ``85K`` as AED 8.5M and default a bare
    ``Requirement`` to purchase demand.  An explicit K-denominated budget in
    the normal monthly-rent range plus a tenancy cue is authoritative.  This
    runs after AI extraction and before typed-table routing.
    """
    corrected = dict(item or {})
    is_requirement = (
        corrected.get("listing_type") == "requirement"
        or corrected.get("message_class") == "requirement"
        or bool(re.search(r"\b(?:requirement|required|looking\s+for|need|wanted)\b", source_text, re.I))
    )
    if not is_requirement:
        return corrected

    match = _REQUIREMENT_BUDGET_RANGE_RE.search(source_text or "")
    if match:
        first_unit = (match.group(2) or match.group(4) or "").lower()
        second_unit = (match.group(4) or match.group(2) or "").lower()
        multipliers = {
            "k": 1_000, "thousand": 1_000,
            "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
        }
        try:
            low = float(match.group(1).replace(",", "")) * multipliers.get(first_unit, 1)
            high = float(match.group(3).replace(",", "")) * multipliers.get(second_unit, 1)
        except ValueError:
            low = high = None
        if low is not None and high is not None:
            corrected["budget_min"], corrected["budget_max"] = (
                _clean_budget_bound(value) for value in sorted((low, high))
            )
            if _RENTAL_REQUIREMENT_CUE_RE.search(source_text or ""):
                corrected["transaction_type"] = "rent"
                corrected["classified_transaction_type"] = "rent"
                locality = corrected.get("locality_options")
                locality_label = locality[0] if isinstance(locality, list) and locality else locality
                corrected["title"] = (
                    f"Residential Rental Requirement in {locality_label}"
                    if locality_label else "Residential Rental Requirement"
                )

    # A single capped budget is an upper bound, not a range. This source-level
    # correction is authoritative because providers have historically turned
    # "Up to AED 2M" into a nonsense 200K-20M spread.
    up_to = _REQUIREMENT_UP_TO_BUDGET_RE.search(source_text or "")
    if not match and up_to:
        multipliers = {
            "k": 1_000, "thousand": 1_000,
            "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
        }
        unit = up_to.group(2).lower()
        try:
            amount = float(up_to.group(1).replace(",", "")) * multipliers[unit]
        except (ValueError, KeyError):
            amount = None
        if amount is not None:
            corrected["budget_min"] = None
            corrected["budget_max"] = _clean_budget_bound(amount)

    single = _REQUIREMENT_SINGLE_BUDGET_RE.search(source_text or "")
    if not match and single and _RENTAL_REQUIREMENT_CUE_RE.search(source_text or ""):
        unit = single.group(2).lower()
        multipliers = {
            "k": 1_000, "thousand": 1_000,
            "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
        }
        try:
            amount = float(single.group(1).replace(",", "")) * multipliers[unit]
        except (ValueError, KeyError):
            amount = None
        if amount is not None:
            corrected["budget_max"] = _clean_budget_bound(amount)
            corrected["transaction_type"] = "rent"
            corrected["classified_transaction_type"] = "rent"

    # BHK options must be configurations, never descriptive prose such as
    # "furnished flat". `_normalized_bhk` accepts the valid numeric forms.
    if corrected.get("bhk_options") is not None and _normalized_bhk(corrected.get("bhk_options")) is None:
        corrected["bhk_options"] = []
        corrected["needs_review"] = True
    corrected["broker_name"] = _clean_broker_name(corrected.get("broker_name"))
    return corrected

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

from lab.storage.base import RawMessage, ParsedObservation, ResolverDecision, dict_to_dataclass
from storage import SupabaseStorage
from lab.embedding import create_engine, observation_text, pack_embedding
from lab.events import get_bus
from agents.building_alias_engine import fuzzy_score, normalize_building_name
from deterministic_splitters import parse_message as parse_template_message
from price_normalization import canonical_commercial_rental_price_rupees, canonical_price_rupees, canonical_rental_price_rupees, parse_explicit_price, rent_price_needs_review
from extraction_quality import building_name_problem, repair_building_assignment


def get_storage():
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise RuntimeError("Supabase is required. Set SUPABASE_URL and SUPABASE_SERVICE_KEY.")
    return SupabaseStorage(url, key)


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


_EXPLICIT_REQUIREMENT_HEADING_RE = re.compile(
    r"^\s*[\W_]*(?:(?:very|urgent|immediate)\s+)*"
    r"(?:(?:buyer|tenant|client)\s+)?"
    # Keep generic "looking for"/"seeking" out of this source guard: brokers
    # commonly use them as marketing hooks ("Looking for the perfect office?").
    # The extraction prompt handles those cues semantically; this guard is for
    # unambiguous request headings only.
    r"(?:requirements?|required|require|wanted|want|"
    r"need(?:s|ed)?|"
    r"any\s+(?:(?:one|1)\s+)?(?:\S+\s+){0,6}(?:available|has|have)\b|"
    r"koi\s+.*\b(?:hai|chahiye|milega|mil\s+sakta)\b|"
    r"(?:chahiye|chaahiye|dhoondh|dhundh|dhoondh\s+rahe)\b)",
    re.IGNORECASE,
)

_EXPLICIT_RENT_LISTING_RE = re.compile(
    r"\b(?:avl|available|for\s+rent|on\s+rent|rent\s*[:\-]?|for\s+lease|on\s+lease|lease)\b",
    re.IGNORECASE,
)

_EXPLICIT_DEMAND_RE = re.compile(
    r"\b(?:looking\s+for|seeking|need(?:s|ed)?|wanted|required)\b",
    re.IGNORECASE,
)

_EXPLICIT_INVENTORY_MARKER_RE = re.compile(
    r"(?:\b(?:avl|available|for\s+lease|on\s+lease|lease|for\s+sale|"
    r"on\s+sale|asking|price)\b|\brent\s*[:=\-])",
    re.IGNORECASE,
)

_EXPLICIT_LISTING_UNIT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:bhk|rk|bedroom)s?\b",
    re.IGNORECASE,
)


def _has_explicit_requirement_heading(text: str) -> bool:
    """Recognize a requirement heading without treating listing copy as one."""
    for line in (text or "").splitlines():
        if _EXPLICIT_REQUIREMENT_HEADING_RE.search(line):
            return True
    return False


def _has_explicit_rent_listing_language(text: str) -> bool:
    """Detect broker listing language that must not become a BUY demand."""
    value = text or ""
    return bool(
        _EXPLICIT_RENT_LISTING_RE.search(value)
        and re.search(r"\b\d+(?:\.\d+)?\s*(?:bhk|rk|bedroom)s?\b", value, re.IGNORECASE)
    )


def _explicit_source_inventory_type(text: str) -> str | None:
    """Return the transaction type explicitly stated by source inventory text."""
    value = str(text or "")
    if _has_explicit_requirement_heading(value) or not _EXPLICIT_LISTING_UNIT_RE.search(value):
        return None
    # A demand such as “looking for 2 BHK for rent” contains the word rent but
    # is not an offered property. Explicit inventory markers win when present.
    if _EXPLICIT_DEMAND_RE.search(value) and not _EXPLICIT_INVENTORY_MARKER_RE.search(value):
        return None
    # “For sale ... currently on lease” is a sale/pre-leased asset. Do not
    # route it to the rent table merely because the occupancy phrase contains
    # “lease”; the quoted amount is the sale consideration unless a separate
    # rent quote is explicitly labelled.
    if re.search(r"\b(?:for\s+sale|on\s+sale|outright|sale)\b", value, re.I) and re.search(
        r"\b(?:currently\s+on\s+lease|pre[- ]?(?:leased|rented)|already\s+leased)\b",
        value,
        re.I,
    ):
        return "sale"
    if (
        re.search(r"\b\d+(?:[.,]\d+)?\s*(?:mn|millions?|m)\b", value, re.IGNORECASE)
        and not re.search(
            r"\b(?:rent|rental|lease|leased|for\s+rent|on\s+rent|for\s+lease|on\s+lease)\b",
            value,
            re.IGNORECASE,
        )
    ):
        return "sale"
    if re.search(
        r"\b(?:rent|rental|lease|leased|for\s+rent|on\s+rent|for\s+lease|on\s+lease)\b",
        value,
        re.IGNORECASE,
    ):
        return "rent"
    if re.search(r"\b(?:sale|selling|for\s+sale|on\s+sale|asking|price)\b", value, re.IGNORECASE):
        return "sale"
    return None


def _normalize_source_inventory_route(item: dict, source_text: str) -> dict:
    """Make explicit source inventory language authoritative over stale AI routing."""
    corrected = dict(item or {})
    route = _explicit_source_inventory_type(source_text)
    if not route:
        return corrected
    corrected.update(
        {
            "listing_type": route,
            "routing_listing_type": route,
            "message_class": "listing",
            "is_requirement": False,
            "classified_is_requirement": False,
            "classified_transaction_type": route,
            "transaction_type": route,
        }
    )
    return corrected


def _apply_listing_transaction_guard(ai_items: list[dict], full_text: str, slices: list[str]) -> list[dict]:
    """Prefer explicit rent listing evidence over an ambiguous AI label.

    Messages such as ``Available 2 BHK For Rent`` are inventory posts.  The
    AI occasionally labels them as ``requirement`` because of words like
    ``available`` or ``rent``.  A real requirement has an explicit heading;
    absent that heading, source-local listing language is authoritative.
    """
    corrected: list[dict] = []
    for index, item in enumerate(ai_items):
        item = dict(item or {})
        source = slices[index] if index < len(slices) else full_text
        has_requirement_heading = _has_explicit_requirement_heading(source) or (
            len(ai_items) == 1 and _has_explicit_requirement_heading(full_text)
        )
        if not has_requirement_heading:
            item = _normalize_source_inventory_route(item, source)
        corrected.append(item)
    return corrected


def _apply_requirement_source_guard(ai_items: list[dict], full_text: str, slices: list[str]) -> list[dict]:
    """Correct an LLM listing label when the source explicitly heads a demand.

    A broker can write "VERY URGENT REQUIREMENT" and then mention the asset
    they want to buy. The word "sale" inside that description must not turn
    the buyer demand into inventory. Mixed documents remain item-scoped: only
    an item whose source slice has a requirement heading is corrected, while a
    single-item document may use the full source heading.
    """
    full_requirement = _has_explicit_requirement_heading(full_text)
    corrected: list[dict] = []
    for index, item in enumerate(ai_items):
        source = slices[index] if index < len(slices) else full_text
        is_requirement = _has_explicit_requirement_heading(source) or (
            len(ai_items) == 1 and full_requirement
        )
        if is_requirement and item.get("listing_type") != "requirement":
            item = {**item, "listing_type": "requirement"}
        corrected.append(item)
    return corrected


def _strip_icons(text):
    if text is None:
        return None
    cleaned = _EMOJI_ICON_RE.sub("", str(text))
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _sanitize_parsed_value(value):
    if isinstance(value, str):
        return _strip_icons(value)
    if isinstance(value, list):
        return [_sanitize_parsed_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_parsed_value(item) for key, item in value.items()}
    return value


def _explicit_bold_building_context(text: str) -> tuple[str | None, str | None, bool]:
    """Return an explicitly bolded building and adjacent locality.

    Dense broker broadcasts commonly use ``*Building* locality - 3 BHK``.
    The bold boundary is source evidence: text after it must not be appended
    to the building identity. Generic headings still fail the normal building
    quality guard.
    """
    for raw_line in str(text or "").splitlines():
        if not re.search(r"(?i)\b\d+(?:\.\d+)?\s*(?:bhk|rk)\b", raw_line):
            continue
        match = re.match(r"^\s*\*([^*\n]{2,70})\*", raw_line)
        if not match:
            continue
        candidate = match.group(1).strip(" .,;:|-_")
        if building_name_problem(candidate):
            return None, None, True

        remainder = raw_line[match.end():].strip(" \t-–—:|,;")
        locality_match = re.match(
            r"(?i)^([A-Za-z][A-Za-z .'/&-]{1,48}?)(?=\s*(?:[-–—:|]\s*)?\d+(?:\.\d+)?\s*(?:bhk|rk)\b)",
            remainder,
        )
        adjacent_locality = locality_match.group(1).strip(" .,;:|-_") if locality_match else None
        return candidate, adjacent_locality or None, True
    return None, None, False


def _infer_building_name_from_source(text: str, locality: str | None = None) -> str | None:
    """Recover a clearly labelled building line when the model omits it.

    Broker formats commonly put the BHK headline first and the building on the
    next non-empty line. This is deliberately conservative: ad labels,
    locality lines, prices and contact text are never promoted to buildings.
    """
    explicit_building, _, explicit_boundary_seen = _explicit_bold_building_context(text)
    if explicit_boundary_seen:
        return explicit_building

    lines = [re.sub(r"[*_`~]", "", line).strip(" -:•") for line in str(text or "").splitlines()]
    # Prefer explicit labels wherever they occur. This handles common broker
    # blocks where the building line follows a heading rather than the BHK
    # line, e.g. "Building Name: Ten BKC".
    for line in lines:
        labelled = re.match(
            r"(?i)^(?:bildg|bldg|building(?:\s+name)?|project(?:\s+name)?|society)\s*[:=-]{1,2}\s*(.+)$",
            line,
        )
        if not labelled:
            continue
        candidate = labelled.group(1).strip(" .,;|-_")
        if (
            candidate
            and len(candidate) <= 70
            and re.search(r"[A-Za-z]", candidate)
            and candidate.casefold() not in {"on request", "price on request", "request", "unknown", "n/a", "na"}
            and not re.search(r"(?i)\b(?:rent|sale|lease|available|carpet|area|floor|parking|possession|contact|details)\b", candidate)
            and not re.search(r"(?:aed|dhs|\b\d{5,}\b|\b(?:sq\.?\s*ft|mn?|millions?|per\s+(?:month|year))\b)", candidate)
        ):
            return candidate
    # Numbered inventory headings often carry both identities in one line:
    # ``1. IndiaBulls Blu – Worli``. This is source evidence, not enrichment.
    for line in lines[:3]:
        if not re.match(r"(?i)^\s*(?:\(\s*\d+\s*\)|\d+[.)])\s*", line):
            continue
        heading = re.sub(r"(?i)^\s*(?:\(\s*\d+\s*\)|\d+[.)])\s*", "", line).strip()
        parts = re.split(r"\s+[–—-]\s+", heading, maxsplit=1)
        if len(parts) == 2:
            candidate, heading_locality = (part.strip(" .,;|-_") for part in parts)
            if (
                candidate
                and heading_locality
                and re.search(r"[A-Za-z]", candidate)
                and not re.search(r"(?i)\b(?:rent|sale|available|requirement|price)\b", candidate)
            ):
                return candidate
    # Some inventory headings put the project before the BHK marker:
    # "Adani TEN BKC - 2 BHK RESIDENCES".
    for line in lines:
        bhk_marker = re.search(r"(?i)\s*[-–:]\s*\d+(?:\.\d+)?\s*(?:bhk|rk)\b", line)
        if not bhk_marker:
            continue
        candidate = line[:bhk_marker.start()].strip(" .,;|-_")
        candidate = re.sub(r"(?i)^(?:inventory|options?|exclusive)\s*[:|-]?\s*", "", candidate).strip()
        if candidate and len(candidate) <= 70 and re.search(r"[A-Za-z]", candidate):
            return candidate
    for index, line in enumerate(lines):
        if not re.search(r"\b\d+(?:\.\d+)?\s*(?:bhk|rk)\b", line, re.IGNORECASE):
            continue
        for candidate in lines[index + 1:index + 5]:
            if not candidate or len(candidate) > 70 or (locality and candidate.lower() == locality.lower()):
                continue
            if re.match(r"(?i)^(?:bildg|bldg|building(?:\s+name)?|project(?:\s+name)?|society)\s*[:=-]", candidate):
                continue
            if re.search(r"\b(?:prime location|location|rent|sale|lease|available|carpet|area|status|floor|parking|possession|inspection|photos?|contact|details|site visit|brokerage)\b", candidate, re.IGNORECASE):
                continue
            if re.search(r"(?:aed|dhs|\b\d{5,}\b|\b(?:sq\.?\s*ft|mn?|millions?|per\s+(?:month|year))\b)", candidate, re.IGNORECASE):
                continue
            if re.search(r"[A-Za-z]", candidate):
                return candidate.strip(" .,")
    # Commercial posts may have no BHK line. If they explicitly name a
    # location, the next descriptive line is commonly the building/project.
    for index, line in enumerate(lines):
        if not re.match(r"(?i)^location\s*[:=-]", line):
            continue
        for candidate in lines[index + 1:index + 4]:
            if (
                candidate
                and len(candidate) <= 70
                and re.search(r"[A-Za-z]", candidate)
                and not re.search(
                    r"(?i)\b(?:area|floor|rent|sale|lease|parking|contact|details|brokerage|possession|photos?|available|road|location)\b|(?:aed|dhs)|\b\d{4,}\b",
                    candidate,
                )
            ):
                return candidate.strip(" .,")
    return None


_CORE_BHK_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:bhk|bhd|rk|bed\s*rooms?|bedrooms?|br)\b", re.IGNORECASE)
_CORE_AREA_RE = re.compile(
    r"\b(?:carpet|built\s*[- ]?up|super\s*[- ]?built\s*[- ]?up|area|size)\s*"
    r"(?:is|:|-)?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|sft|square\s*feet)\b"
    r"|\b(\d[\d,]*(?:\.\d+)?)\s*(?:sq\.?\s*ft|sqft|sft|square\s*feet)\b",
    re.IGNORECASE,
)
_CORE_PRICE_RE = re.compile(
    r"(?:aed|dhs|dirhams?)?\s*(\d[\d,]*(?:\.\d+)?)\s*"
    r"(m|mn|millions?|k|thousands?)\b",
    re.IGNORECASE,
)
_MULTI_UNIT_BHK_RE = re.compile(
    r"\b(?P<count>\d+)\s*(?:x|×|\*|\s+)\s*"
    r"(?P<bhk>\d+(?:\.\d+)?)\s*(?:bhk|bhd|rk|bed\s*rooms?|bedrooms?|br)\b",
    re.IGNORECASE,
)
_PRICE_PER_SQFT_RE = re.compile(
    r"(?:rate|price)\s*(?:per|/)\s*(?:sq\.?\s*ft|sqft|sft|square\s*feet)\.?"
    r"(?:\s*on\s+(?:carpet|built[- ]?up|chargeable)\s*)?[:=\-]?\s*"
    r"(?:aed|dhs|dirhams?)?\s*(?P<rate>\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)

_SOURCE_COMMERCIAL_RE = re.compile(
    r"\b(?:office|shop|showroom|warehouse|godown|industrial|retail|commercial|hotel|hospitality|restaurant|banquet|lodging|bare\s*shell|warm\s*shell|plug[- ]and[- ]play|chargeable\s+area|ceiling\s+height|mezzanine|cabin|workstation|conference\s+room|cam|lease\s+deed|power\s+load|food\s+court|otla)\b",
    re.IGNORECASE,
)
_SOURCE_RESIDENTIAL_RE = re.compile(
    r"\b(?:\d+(?:\.\d+)?\s*(?:bhk|rk)|flat|apartment|residential|villa|bungalow|independent\s+(?:house|home))\b",
    re.IGNORECASE,
)


def _source_has_commercial_evidence(source_text: str) -> bool:
    source = str(source_text or "")
    if _SOURCE_COMMERCIAL_RE.search(source):
        return True
    return bool(
        re.search(r"\binvestor\s+unit\b", source, re.IGNORECASE)
        and re.search(r"\b(?:lease|rent|rental)\b", source, re.IGNORECASE)
        and re.search(r"\bpremises\b", source, re.IGNORECASE)
        and not _SOURCE_RESIDENTIAL_RE.search(source)
    )


def _source_has_price_evidence(source_text: str) -> bool:
    source = str(source_text or "")
    return bool(
        _PRICE_PER_SQFT_RE.search(source)
        or re.search(
            r"(?:aed|dhs|dirhams?)?\s*\d[\d,]*(?:\.\d+)?\s*(?:psf|per\s+sq\.?\s*ft|per\s+sqft)\b",
            source,
            re.IGNORECASE,
        )
        or _CORE_PRICE_RE.search(source)
        or re.search(r"(?:aed|dhs)\s*\d[\d,]*(?:\.\d+)?", source, re.IGNORECASE)
        or re.search(r"\d[\d,]*(?:\.\d+)?\s*(?:per\s*month|monthly|/\s*month)\b", source, re.IGNORECASE)
        or re.search(
            r"\b(?:rent|rental|monthly\s+rent|asking|price)\b\s*[:=\-]?\s*"
            r"(?:aed|dhs|dirhams?)?\s*\d[\d,]*(?:\.\d+)?"
            r"(?:\s*(?:m|mn|millions?|k|thousands?))?\b",
            source,
            re.IGNORECASE,
        )
    )


def _apply_source_evidence_gates(ai: dict, source_text: str) -> dict:
    """Remove model values that cannot be supported by this message slice."""
    source = str(source_text or "")
    flags = list(ai.get("validation_flags") or [])
    if _source_has_commercial_evidence(source) and not _SOURCE_RESIDENTIAL_RE.search(source):
        ai["property_category"] = "commercial"
        ai["asset_type"] = "commercial"
        flags.append("commercial_source_evidence")
    multi = _MULTI_UNIT_BHK_RE.search(source)
    source_bhk = _CORE_BHK_RE.search(source)
    if multi:
        ai["listing_count"] = int(multi.group("count"))
        ai["bhk"] = _safe_float(multi.group("bhk"))
    elif source_bhk:
        source_value = _safe_float(source_bhk.group(1))
        if source_value is not None:
            if ai.get("bhk") is not None and _safe_float(ai.get("bhk")) != source_value:
                flags.append("bhk_source_mismatch")
                ai["needs_review"] = True
            ai["bhk"] = source_value
    else:
        for key in (
            "bhk", "bhk_options", "original_bhk", "current_bhk",
            "configuration_type", "configuration_details",
        ):
            ai[key] = None
        for key in ("title", "summary_title"):
            if re.search(r"\b\d+(?:\.\d+)?\s*bhk\b", str(ai.get(key) or ""), re.IGNORECASE):
                ai[key] = None
        flags.append("bhk_source_missing")

    if not _source_has_price_evidence(source):
        ai["price"] = {}
        for key in ("monthly_rent", "total_asking_price", "rent_per_sqft", "price_per_sqft", "computed_total_asking_price", "price_math"):
            ai[key] = None
        flags.append("price_source_missing")
        ai["needs_review"] = True
        # A model-supplied confidence cannot remain high when the source
        # contains no price evidence. Keep the field blank and quarantine the
        # row for review instead of publishing an unsupported amount.
        ai["extraction_confidence"] = "low"
        ai["extraction_confidence_score"] = 0.0
    else:
        # A single typed row must not absorb several independent asking quotes
        # from a broker broadcast. Keep the row for audit/review, but remove
        # the ambiguous price so it cannot appear as a false active listing.
        absolute_quotes = re.findall(
            r"\b(?:asking|price|quote)\b\s*[:=\-]?\s*(?:aed|dhs|dirhams?)?\s*"
            r"\d[\d,]*(?:\.\d+)?\s*(?:m|mn|millions?|k|thousands?)?\b",
            source,
            re.IGNORECASE,
        )
        source_has_rent_mode = bool(re.search(r"\b(?:rent|rental|monthly\s+rent)\b", source, re.IGNORECASE))
        source_has_sale_mode = bool(re.search(r"\b(?:sale|sell|outright|price\s+sale)\b", source, re.IGNORECASE))
        if (
            str(ai.get("property_category") or "").casefold() == "commercial"
            and str(ai.get("listing_type") or ai.get("routing_listing_type") or "").casefold() == "sale"
            and len(absolute_quotes) >= 2
            and not (source_has_rent_mode and source_has_sale_mode)
        ):
            ai["price"] = {}
            for key in ("monthly_rent", "total_asking_price", "rent_per_sqft", "price_per_sqft", "computed_total_asking_price", "price_math"):
                ai[key] = None
            flags.append("multiple_sale_price_quotes_in_source_slice")
            ai["needs_review"] = True
            ai["extraction_confidence"] = "low"
            ai["extraction_confidence_score"] = 0.0
        elif source_has_rent_mode and source_has_sale_mode and len(absolute_quotes) >= 2:
            # A single typed row cannot represent both transaction modes, but
            # it must not discard the evidence or silently relabel one quote
            # as the other. Keep the model's selected route, retain the raw
            # message, and force review for a future multi-price projection.
            flags.append("mixed_sale_rent_price_quotes_in_source_slice")
            ai["needs_review"] = True
    ai["validation_flags"] = list(dict.fromkeys(flags))
    return ai


_EXPLICIT_SOURCE_LOCATION_RE = re.compile(
    r"^\s*(?:location|locality|micro\s*market|micro-market|area\s*location)"
    r"\s*[:=\-]?\s*(?P<mention>[^|]+?)\s*$",
    re.IGNORECASE,
)


def _source_explicit_location(source_text: str) -> str | None:
    """Return a locality explicitly labelled inside the current item slice.

    Broadcast headings and the model's context can mention a wider market than
    the individual property.  A labelled location in the contiguous source
    block is stronger evidence and must not be replaced by that context.
    """
    for line in str(source_text or "").splitlines():
        cleaned = re.sub(r"[*_`~]", "", line).strip(" -:•")
        match = _EXPLICIT_SOURCE_LOCATION_RE.match(cleaned)
        if not match:
            continue
        mention = re.sub(r"\s+", " ", match.group("mention")).strip(" .;,|-_")
        if mention and not re.fullmatch(r"\d[\d,]*(?:\.\d+)?\s*(?:sq\.?\s*ft|sqft|sf)", mention, re.I):
            return mention
    return None


def _ground_locality_to_source(ai: dict, source_text: str) -> dict:
    """Keep locality inference inside the exact property evidence boundary.

    The LLM may resolve a raw mention using broadcast-level context (for
    example, returning Bandra West for a slice explicitly labelled Sanpada).
    We still allow the LLM to infer a locality when no explicit source label
    exists, but an explicit label always wins and cannot be overwritten by a
    parent-market guess.
    """
    locality = ai.get("locality") if isinstance(ai.get("locality"), dict) else {}
    explicit = _source_explicit_location(source_text)
    if explicit:
        if locality.get("raw_mention") != explicit or locality.get("resolved_locality") != explicit:
            flags = list(ai.get("validation_flags") or [])
            flags.append("locality_repaired_from_explicit_source_boundary")
            ai["validation_flags"] = list(dict.fromkeys(flags))
        locality = dict(locality)
        locality["raw_mention"] = explicit
        locality["resolved_locality"] = explicit
        locality["confidence"] = max(float(locality.get("confidence") or 0), 0.9)
        ai["locality"] = locality
        ai["location_raw"] = explicit
        ai["micro_market"] = explicit
        return ai

    # No explicit label: retain the model's raw mention and resolution. This
    # is the intended AI inference path, with no unrelated heading injected.
    return ai


def _rescue_core_fields(parsed: dict, source_text: str) -> dict:
    """Recover only explicit core values the model omitted.

    This is intentionally conservative. It improves common broker shorthand
    but never invents values for media-only placeholders or ambiguous numbers.
    """
    source = str(source_text or "")
    multi = _MULTI_UNIT_BHK_RE.search(source)
    if multi:
        parsed["listing_count"] = int(multi.group("count"))
        parsed["bhk"] = f"{_safe_float(multi.group('bhk')):g} BHK"
    elif not parsed.get("bhk"):
        match = _CORE_BHK_RE.search(source)
        if match:
            value = float(match.group(1))
            parsed["bhk"] = "1 RK" if value == 0.5 else f"{int(value) if value.is_integer() else value:g} BHK"
    if parsed.get("area_sqft") is None:
        match = _CORE_AREA_RE.search(source)
        if match:
            parsed["area_sqft"] = _safe_float(match.group(1) or match.group(2))
    price_value = parsed.get("price")
    if isinstance(price_value, dict):
        price_value = price_value.get("amount")
    if price_value is None:
        psf = _PRICE_PER_SQFT_RE.search(source)
        match = None
        if psf:
            parsed["price"] = _safe_float(psf.group("rate").replace(",", ""))
            parsed["price_unit"] = "per_sqft"
        else:
            match = _CORE_PRICE_RE.search(source)
        if not psf and match:
            raw_price = match.group(0)
            parsed["price"] = _parse_raw_price_to_abs(raw_price)
            parsed["price_unit"] = "abs" if parsed.get("price") is not None else parsed.get("price_unit")
        elif re.search(r"(?:aed|dhs)\s*\d[\d,]*(?:\.\d+)?", source, re.IGNORECASE):
            plain = re.search(r"(?:aed|dhs)\s*(\d[\d,]*(?:\.\d+)?)", source, re.IGNORECASE)
            if plain:
                parsed["price"] = _safe_float(plain.group(1).replace(",", ""))
                parsed["price_unit"] = "abs" if parsed.get("price") is not None else parsed.get("price_unit")
        elif re.search(r"\d[\d,]*(?:\.\d+)?\s*(?:per\s*month|monthly|/\s*month|rent)\b", source, re.IGNORECASE):
            plain = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(?:per\s*month|monthly|/\s*month|rent)\b", source, re.IGNORECASE)
            if plain:
                parsed["price"] = _safe_float(plain.group(1).replace(",", ""))
                parsed["price_unit"] = "abs" if parsed.get("price") is not None else parsed.get("price_unit")
    price_value = parsed.get("price")
    if isinstance(price_value, dict):
        price_value = price_value.get("amount")
    if price_value is not None and parsed.get("price_unit") != "per_sqft":
        if parsed.get("intent") == "RENT":
            parsed["monthly_rent"] = price_value
        elif parsed.get("intent") == "SELL":
            parsed["total_asking_price"] = price_value
    building = parsed.get("building_name")
    if isinstance(building, str) and building.strip().casefold() in {"[document]", "[image]", "[video]", "[voice message]", "[sticker]"}:
        parsed["building_name"] = None
    if not parsed.get("building_name") and source.strip().casefold() not in {"[document]", "[image]", "[video]", "[voice message]", "[sticker]"}:
        parsed["building_name"] = _infer_building_name_from_source(source, parsed.get("micro_market"))
    return parsed


def _title_evidence_mismatch(title: object, source_text: object, building_name: object = None) -> bool:
    """Detect a title name that cannot be traced to the source message.

    This is deliberately a flag, not a rewrite. Titles are model-generated
    presentation text, while the source message is the evidence boundary.
    Keep generic titles such as ``3 BHK for rent in Bandra West`` valid, but
    quarantine a named lead phrase when that name is absent from the source.
    """
    title_text = str(title or "").strip()
    source = str(source_text or "").strip()
    if not title_text or not source:
        return False

    def compact(value: object) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    source_compact = compact(source)
    if building_name:
        building_compact = compact(building_name)
        if building_compact and building_compact not in source_compact:
            return True

    # A named title normally puts the building before an em dash, pipe, or
    # explicit property qualifier. Only inspect a multi-token lead phrase so
    # ordinary titles beginning with "Office" or "3 BHK" are not rejected.
    lead = re.split(r"\s*(?:—|–|\||:\s+|\s+-\s+)\s*", title_text, maxsplit=1)[0]
    lead = re.sub(
        r"(?i)^(?:\d+(?:\.\d+)?\s*(?:bhk|rk)\b|office|shop|showroom|commercial\s+space|property)\s+",
        "",
        lead,
    ).strip(" -:,|")
    lead_tokens = re.findall(r"[a-z0-9]+", lead.casefold())
    if len(lead_tokens) < 2:
        return False
    if all(token in {"for", "rent", "sale", "lease", "in", "at", "on", "office", "space", "property", "commercial"} for token in lead_tokens):
        return False
    lead_compact = compact(lead)
    return bool(lead_compact and lead_compact not in source_compact)


def _source_grounded_title(ai_extraction: dict, parsed: dict, source_text: str) -> str | None:
    """Choose a useful title without allowing generic or stale model text."""
    candidate = ai_extraction.get("title") if isinstance(ai_extraction, dict) else None
    flags = set((ai_extraction or {}).get("validation_flags") or [])
    # A model title can collapse "4 2BHK" into one arbitrary unit. Force the
    # deterministic source-grounded title for explicit multi-unit messages.
    if _MULTI_UNIT_BHK_RE.search(source_text or ""):
        candidate = None
    if (
        _is_usable_extraction_title(candidate)
        and "title_evidence_mismatch" not in flags
        and not _title_evidence_mismatch(candidate, source_text, parsed.get("building_name"))
    ):
        return re.sub(r"\s+", " ", str(candidate)).strip()

    # The deterministic title uses only fields rescued from this source
    # slice.  It is the fallback for generic AI titles and copied titles.
    try:
        from routers.infra import generate_summary_title
        deterministic = generate_summary_title(parsed, source_text)
    except Exception:
        deterministic = None
    if _is_usable_extraction_title(deterministic):
        return deterministic
    return None


# ── Building name normalization against known buildings ────────────
# The LLM often extracts ad text, locality names, or broker phrases as
# building_name.  We fuzzy-match against the canonical building names
# and aliases already in the DB to normalize to the correct name.

_BUILDING_DICT: dict | None = None  # {normalized_name: canonical_name}


def _load_building_dict() -> dict:
    """Load buildings + aliases into a fuzzy-matchable dict.

    Returns {normalized_name: canonical_name} for all known buildings
    and their aliases.  Cached for the lifetime of the worker.
    """
    global _BUILDING_DICT
    if _BUILDING_DICT is not None:
        return _BUILDING_DICT

    try:
        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            _BUILDING_DICT = {}
            return _BUILDING_DICT

        # Use PropAI's REST client.  The worker image intentionally does not
        # depend on supabase-py, and a top-level ``supabase`` import can also
        # resolve to an unrelated namespace in slim deployments.
        client = SupabaseStorage(url, key).client

        # Load canonical names
        resp = client.table("buildings").select("canonical_name").execute()
        rows = resp.data or []

        # Load aliases
        alias_resp = client.table("building_name_aliases").select("alias,canonical_name").execute()
        alias_rows = alias_resp.data or []

        d: dict[str, str] = {}
        for r in rows:
            cn = (r.get("canonical_name") or "").strip()
            if len(cn) >= 3:
                d[normalize_building_name(cn)] = cn
        for r in alias_rows:
            alias = (r.get("alias") or "").strip()
            cn = (r.get("canonical_name") or "").strip()
            if len(alias) >= 3 and len(cn) >= 3:
                d[normalize_building_name(alias)] = cn

        _BUILDING_DICT = d
        _logger.info("Loaded %d normalized building name entries", len(d))
    except Exception as exc:
        _logger.warning("Failed to load building dict: %s", exc)
        _BUILDING_DICT = {}

    return _BUILDING_DICT


def _normalize_building_to_canonical(name: str) -> str | None:
    """Match an extracted building name against known buildings.

    Returns the canonical name if a close match is found (>=0.80 score),
    or the original name unchanged if no match — never silently drops it.
    """
    if not name or len(name.strip()) < 3:
        return name

    bdict = _load_building_dict()
    if not bdict:
        return name

    norm = normalize_building_name(name)

    # 1. Exact match after normalization
    if norm in bdict:
        return bdict[norm]

    # 2. Fuzzy match against all known names
    best_canonical = None
    best_score = 0.0
    for norm_cn, canonical in bdict.items():
        score = fuzzy_score(name, canonical)
        if score > best_score:
            best_score = score
            best_canonical = canonical

    if best_score >= 0.80 and best_canonical:
        _logger.debug(
            "Building name normalized: %r -> %r (score %.2f)",
            name, best_canonical, best_score,
        )
        return best_canonical

    # 3. No good match — return original unchanged
    return name


def _message_hash(text: str) -> str:
    # Content-addressed cache key: the raw message body is the input identity.
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sender_template_key(sender_phone: str = "", sender_jid: str = "") -> str:
    phone = re.sub(r"\D+", "", sender_phone or "")
    if phone:
        if len(phone) >= 12 and phone.startswith("91"):
            phone = phone[-10:]
        if len(phone) >= 10:
            return f"phone:{phone[-10:]}"
    jid = (sender_jid or "").strip()
    if jid:
        return f"jid:{jid}"
    return ""


def _clone_parsed_rows(storage, source_raw_id: int, target_raw_id: int) -> tuple[list[int], list[int], list[int]]:
    """Copy typed extraction rows for a duplicate raw message."""
    try:
        rows = storage._fetch_typed_rows(
            raw_message_id=source_raw_id, requirements=None, limit_per_table=1000
        )
    except Exception:
        return [], [], []
    if not rows:
        return [], [], []

    parsed_ids: list[int] = []
    listing_ids: list[int] = []
    requirement_ids: list[int] = []
    for row in rows:
        payload = dict(row)
        table_name = payload.pop("_typed_table", "")
        if not table_name:
            continue
        payload.pop("id", None)
        payload.pop("created_at", None)
        payload.pop("updated_at", None)
        payload["raw_message_id"] = target_raw_id
        source_fp = hashlib.sha256(
            f"typed-observation:{target_raw_id}:{payload.get('listing_index') or 0}".encode()
        ).hexdigest()[:32]
        payload["source_fingerprint"] = source_fp
        try:
            new_id = storage.save_typed_listing(
                table_name, payload, _already_filtered=True, _source_id=target_raw_id
            )
            if new_id:
                parsed_ids.append(new_id)
                if table_name.endswith("_requirements"):
                    requirement_ids.append(new_id)
                else:
                    listing_ids.append(new_id)
        except Exception as exc:
            print(f"  [extract] duplicate typed extraction clone error for {target_raw_id}: {exc}", flush=True)
    return parsed_ids, listing_ids, requirement_ids


def _run_template_splitter(
    storage,
    msg_text: str,
    *,
    tenant_id: str | None,
    sender_phone: str = "",
    sender_jid: str = "",
) -> tuple[str | None, list[dict]]:
    """Try the per-sender cached splitter first, then the full pattern set."""
    sender_key = _sender_template_key(sender_phone, sender_jid)
    cache_row = None
    if sender_key:
        try:
            cache_row = storage.get_sender_splitter_cache(sender_key, tenant_id=tenant_id)
        except Exception:
            cache_row = None

    # Fast path: cached pattern, revalidated only every 50th hit.
    if cache_row and cache_row.get("pattern_id"):
        try:
            message_count = int(cache_row.get("message_count") or 0)
        except (TypeError, ValueError):
            message_count = 0
        should_revalidate = (message_count + 1) % 50 == 0
        if not should_revalidate:
            selected_pattern, parsed = parse_template_message(msg_text, preferred_pattern=str(cache_row.get("pattern_id") or ""))
            if selected_pattern == cache_row.get("pattern_id") and parsed:
                if len(parsed) > 1:
                    try:
                        storage.upsert_sender_splitter_cache(
                            sender_key=sender_key,
                            pattern_id=selected_pattern,
                            tenant_id=tenant_id,
                            sender_phone=sender_phone,
                            sender_jid=sender_jid,
                            message_hash=_message_hash(msg_text),
                            revalidated=True,
                        )
                    except Exception:
                        pass
                return selected_pattern, parsed

    selected_pattern, parsed = parse_template_message(msg_text)
    if selected_pattern and parsed and sender_key:
        try:
            storage.upsert_sender_splitter_cache(
                sender_key=sender_key,
                pattern_id=selected_pattern,
                tenant_id=tenant_id,
                sender_phone=sender_phone,
                sender_jid=sender_jid,
                message_hash=_message_hash(msg_text),
                revalidated=len(parsed) > 1,
            )
        except Exception:
            pass
    return selected_pattern, parsed


def _materialize_split_raw_messages(storage, parent_raw_id: int, ctx: dict, chunks: list[dict]) -> list[int]:
    """Persist deterministic broadcast chunks as child raw evidence rows.

    The parent stays as the immutable WhatsApp event. Children are the units
    sent through extraction, so each parsed item has one raw source slice and
    broker identity never needs to be rediscovered by the LLM.
    """
    if not parent_raw_id or len(chunks) < 2 or ctx.get("parent_message_id"):
        return []
    parent_uid = str(ctx.get("message_uid") or parent_raw_id)
    parent_payload = ctx.get("raw_payload")
    if not isinstance(parent_payload, dict):
        parent_payload = {
            "data": {
                "key": {
                    "id": ctx.get("message_id") or "",
                    "remoteJid": ctx.get("group") or "",
                    "participant": ctx.get("sender_jid") or "",
                },
                "pushName": ctx.get("push_name") or "",
                "sender": {
                    "id": ctx.get("sender_jid") or "",
                    "name": ctx.get("sender_name") or "",
                },
            }
        }
    child_ids: list[int] = []
    for index, parsed in enumerate(chunks, start=1):
        payload = json.loads(json.dumps(parent_payload))
        slice_text = ""
        raw_payload = parsed.get("raw_payload") if isinstance(parsed, dict) else None
        if isinstance(raw_payload, dict):
            slice_text = str(raw_payload.get("full_text") or raw_payload.get("slice_text") or "").strip()
        if not slice_text:
            slice_text = str(parsed.get("normalized_message") or "").strip()
        if not slice_text:
            continue
        payload["split"] = {
            "parent_message_id": parent_raw_id,
            "split_index": index,
            "pattern_id": ctx.get("split_pattern") or "",
            "slice_text": slice_text,
        }
        child_uid = f"{parent_uid}:split:{index}"
        try:
            get_raw_by_uid = getattr(storage, "get_raw_by_uid", None)
            existing = get_raw_by_uid(child_uid) if callable(get_raw_by_uid) else None
            if existing:
                child_ids.append(int(existing.id))
                continue
            child = RawMessage(
                group_name=ctx.get("group_name") or "",
                sender=ctx.get("sender_name") or "",
                sender_jid=ctx.get("sender_jid") or "",
                sender_phone=ctx.get("sender_phone") or "",
                message=slice_text,
                message_type="text",
                attachments="[]",
                reply_context="{}",
                timestamp=ctx.get("timestamp") or "",
                source=ctx.get("source") or "WHATSAPP",
                raw_payload=json.dumps(payload),
                message_uid=child_uid,
                pipeline_version=ctx.get("pipeline_version"),
                synced_at=ctx.get("synced_at"),
                event_id=ctx.get("event_id") or ctx.get("message_id") or "",
                is_group=bool(ctx.get("is_group", not ctx.get("is_dm"))),
                processed=False,
                tenant_id=ctx.get("tenant_id") or None,
                parent_message_id=parent_raw_id,
                split_index=index,
            )
            child_id = storage.save_raw_message(child)
            if child_id:
                child_ids.append(int(child_id))
        except Exception as exc:
            _logger.warning("raw_id=%s split child %s failed: %s", parent_raw_id, index, exc)
    return child_ids


def _sanitize_parsed_listing(parsed: dict) -> dict:
    cleaned = {key: _sanitize_parsed_value(value) for key, value in parsed.items()}

    # Multi-listing parsers historically used ``floor`` or
    # ``floor_description`` while parsed_output stores ``floor_range``. Keep
    # one canonical save key for residential and commercial observations so a
    # reviewed split cannot lose the option that made it a separate card.
    if not cleaned.get("floor_range"):
        cleaned["floor_range"] = (
            cleaned.get("floor_description") or cleaned.get("floor")
        )

    # A project heading is the building identity for Market Inbox purposes.
    # Tower/wing remain evidence metadata; they must never be promoted to a
    # locality or allowed to replace an explicit sibling building.
    if not cleaned.get("building_name") and cleaned.get("project_name"):
        cleaned["building_name"] = cleaned["project_name"]

    payload = cleaned.get("raw_payload")
    if isinstance(payload, dict):
        hierarchy = {
            key: cleaned.get(key)
            for key in ("project_name", "tower_name", "wing_name")
            if cleaned.get(key)
        }
        if hierarchy:
            payload = dict(payload)
            payload.setdefault("hierarchy", {}).update(hierarchy)
            cleaned["raw_payload"] = payload

    return cleaned


# Defence-in-depth validators for AI-only fields (deal_tags, additional_charges).
# ai_extract() already runs _normalize_extraction in ai_extraction.py, but if
# any code path bypasses that (mocked in tests, future schema migration, raw
# LLM output without normalization), the row should still be safe to save.
_VALID_DEAL_TAGS_STORAGE = frozenset({
    "distress_sale", "urgent_sale", "negotiable", "bank_auction",
    "resale", "exclusive_mandate", "price_drop", "brand_new_building",
})
_VALID_CHARGE_TYPES_STORAGE = frozenset({"fixed", "percent_of_price"})


def _safe_deal_tags(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for t in raw:
        if not isinstance(t, str):
            continue
        key = t.strip().lower()
        if key and key in _VALID_DEAL_TAGS_STORAGE:
            out.append(key)
    return out


def _safe_additional_charges(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        label = c.get("label")
        amount = c.get("amount")
        amount_type = c.get("amount_type")
        if not isinstance(label, str) or not label.strip():
            continue
        if not isinstance(amount_type, str) or amount_type.strip().lower() not in _VALID_CHARGE_TYPES_STORAGE:
            continue
        try:
            amount_f = float(amount)
        except (TypeError, ValueError):
            continue
        if not (amount_f == amount_f):  # NaN check
            continue
        out.append({"label": label.strip(), "amount": amount_f, "amount_type": amount_type.strip().lower()})
    return out


def _safe_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("amount", "value", "price"):
            coerced = _safe_float(value.get(key))
            if coerced is not None:
                return coerced
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            coerced = _safe_float(item)
            if coerced is not None:
                return coerced
        return None
    text = str(value).strip().replace(",", "")
    if re.search(r"\.{2,}", text):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _safe_int(value) -> int | None:
    coerced = _safe_float(value)
    return int(coerced) if coerced is not None else None


import re as _re

_UNIT_TO_ABS = {
    "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
    "k": 1_000, "thousand": 1_000,
}

def _parse_raw_price_to_abs(raw_price_text: str) -> float | None:
    """Best-effort parse of raw_price_text into absolute AED.

    Returns None if the text is unparseable.  Used to cross-check the AI
    extraction amount which sometimes returns 10x/100x the correct value.
    """
    explicit = parse_explicit_price(raw_price_text)
    if explicit:
        amount, unit = explicit
        return canonical_price_rupees(amount, unit)
    if not raw_price_text:
        return None
    # Brokers commonly write prices as `1.5.M`, `95.K`, or
    # `AED 2.80 to 3.35 Million`.  The old expression stopped at the decimal
    # punctuation and therefore could validate the AI value against `1.5`
    # dirhams instead of 1.5 million.
    m = _re.search(
        r'([\d,]+(?:(?:\.\d+)|(?::\d+))?)\s*[.\-/]*\s*'
        r'(m|mn|millions?|k|thousands?)\b',
        raw_price_text.lower(),
    )
    if not m:
        return None
    try:
        amount = float(m.group(1).replace(",", "").replace(":", "."))
    except ValueError:
        return None
    unit = (m.group(2) or "").rstrip("s")
    multiplier = _UNIT_TO_ABS.get(unit, 1)
    return amount * multiplier


def _parse_raw_price_native(raw_price_text: str) -> tuple[float, str] | None:
    """Return the first explicitly stated broker price in its native unit.

    This is deliberately source-grounded.  The model is asked for absolute
    dirhams, but persisted inbox values use native units (M/K).  If the
    source contains an explicit unit, it is safer to use that source value
    than to trust a model conversion which can be off by 10x/1000x.
    """
    if not raw_price_text:
        return None
    m = _re.search(
        r'([\d,]+(?:\.\d+)?)\s*[.:/\-]*\s*'
        r'(m|mn|millions?|k|thousands?)\b',
        raw_price_text.lower(),
    )
    if not m:
        return None
    try:
        amount = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = m.group(2).rstrip("s")
    if unit in {"m", "mn", "million"}:
        return amount, "M"
    return amount, "K"


def _price_from_ai_and_raw(
    price_info: dict,
    source_text: str | None = None,
) -> tuple[float | None, str | None]:
    """Return an absolute AED amount, using the source phrase as a guardrail.

    Models occasionally return ``8.5`` for ``8.5 Cr`` or shift a decimal.
    When the source contains an explicit money unit, that literal source value
    wins.  PSF remains a rate and is deliberately not converted here.
    """
    if not isinstance(price_info, dict):
        return None, None
    source = str(source_text or "")
    psf = _PRICE_PER_SQFT_RE.search(source)
    if psf:
        return _safe_float(psf.group("rate").replace(",", "")), "per_sqft"
    if source_text is not None and not _source_has_price_evidence(source):
        return None, None
    raw = str(price_info.get("raw_price_text") or "").strip()
    # Some providers return a normalized amount/unit but omit the required
    # provenance phrase. The exact listing slice remains authoritative: an
    # explicit UAE money unit there prevents decimal shifts such as
    # `AED 1.85M` becoming `AED 185K`.
    if not raw and source_text:
        raw = str(source_text)
    unit = str(price_info.get("unit") or "").strip().lower()
    # A model can mislabel a normal rent quote such as ``AED 200K`` as
    # per-square-foot.  An explicit million/thousand quote is authoritative
    # unless the source itself contains a PSF marker.
    has_explicit_native_unit = bool(re.search(
        r"\d+(?:[.,]\d+)?\s*(?:m|mn|millions?|k|thousands?)\b",
        raw.lower(),
    ))
    has_psf_marker = bool(re.search(r"\b(?:psf|per\s+sq\.?\s*ft)\b", raw.lower()))
    if has_psf_marker or (unit in {"per_sqft", "psf"} and not has_explicit_native_unit):
        try:
            return float(price_info.get("amount")), "per_sqft"
        except (TypeError, ValueError):
            return None, "per_sqft"
    source_amount = _parse_raw_price_to_abs(raw)
    if source_amount is not None:
        return source_amount, "abs"
    try:
        return float(price_info.get("amount")), "abs"
    except (TypeError, ValueError):
        return None, None


def _source_rent_price_text(source_text: str | None) -> str | None:
    """Return the explicit rent quote, without picking a deposit or sale quote."""
    match = re.search(
        r"\b(?:rent|rental|monthly\s+rent)\s*[:=\-]?\s*"
        r"((?:aed|dhs\s*)?\d[\d,.]*\s*"
        r"(?:m|mn|millions?|k|thousands?)?"
        r"(?:\s*(?:per\s*month|/\s*month|p\.?\s*m\.?)\b)?)",
        str(source_text or ""),
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else None


def _parse_deposit(raw_text: str, monthly_rent: float | None = None) -> dict:
    """Parse the compact deposit conventions used in broker messages."""
    text = str(raw_text or "")
    lower = text.lower()
    if not re.search(r"\bdeposit\b|\d+(?:\.\d+)?\s*[kkl]?(?:\s*[+/&/]\s*\d+)?", lower):
        return {}
    amount = None
    months = None
    needs_review = False
    explicit = re.search(
        r"\bdeposit\b\s*[:\-]?\s*(?:aed|dhs)?\s*"
        r"([\d,.]+)(?:\s*(k|m|mn|million|thousand|months?|mo))?",
        lower,
    )
    if explicit:
        value = _safe_float(explicit.group(1))
        if value is not None:
            unit = (explicit.group(2) or "").rstrip("s")
            if unit in {"month", "mo"} or (not unit and value <= 12):
                months = value
            else:
                amount = _price_from_ai_and_raw({
                    "amount": value,
                    "unit": unit,
                    "raw_price_text": f"{value} {unit}".strip(),
                })[0]
    combined = re.search(
        r"([\d,.]+)\s*(k|m|mn|million|thousand)?\s*[+/&/]\s*"
        r"([\d,.]+)\s*(k|m|mn|million|thousand|months?|mo)?",
        lower,
    )
    if combined:
        value = _safe_float(combined.group(3))
        if value is not None:
            unit = (combined.group(4) or "").rstrip("s")
            if unit in {"month", "mo"} or (not unit and value <= 12):
                months = value
            elif unit or value > 12:
                amount = _price_from_ai_and_raw({
                    "amount": value, "unit": unit, "raw_price_text": f"{value} {unit}".strip(),
                })[0]
    range_match = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*months?", lower)
    if range_match:
        months = (float(range_match.group(1)) + float(range_match.group(2))) / 2
        needs_review = True
    if months is not None and monthly_rent is not None and amount is None:
        amount = monthly_rent * months
    result = {
        "deposit_amount": amount,
        "deposit_months": months,
        "deposit_applicable": True,
        "deposit_raw_text": text.strip(),
    }
    if needs_review:
        result["needs_review"] = True
    return result


def _ai_extraction_to_parsed(ai_extraction: dict, raw_text: str, sender_name: str, push_name: str, slice_text: str | None = None) -> dict:
    """Convert AI extraction schema to the existing parsed dict format.

    This bridges the new AI extraction result to the legacy parsed_observation
    columns so the rest of the pipeline (resolver, listing upsert, etc.)
    remains unchanged. The full AI result is stored separately in the
    `ai_extraction` JSONB column.
    """
    # Normalize provider absence markers before any routing or typed-field
    # coercion.  This keeps webhook and worker persistence on one contract.
    ai_extraction = _clean_extraction_value(dict(ai_extraction or {}))
    listing_type = ai_extraction.get("routing_listing_type") or ai_extraction.get("listing_type")
    if listing_type == "sale":
        intent = "SELL"
    elif listing_type == "rent":
        intent = "RENT"
    elif listing_type == "requirement":
        intent = "BUY"
    else:
        intent = None

    category = ai_extraction.get("property_category")
    asset_type = category.lower() if category else None
    classified_transaction = ai_extraction.get("transaction_type") or listing_type
    if classified_transaction not in {"sale", "rent", "lease", "pg", "joint_venture"}:
        classified_transaction = "sale"

    bhk_val = ai_extraction.get("bhk")
    bhk_str = None
    if bhk_val is not None:
        if bhk_val == 0.5:
            bhk_str = "1 RK"
        elif bhk_val == int(bhk_val):
            bhk_str = f"{int(bhk_val)} BHK"
        else:
            bhk_str = f"{bhk_val} BHK"

    price_info = ai_extraction.get("price", {})
    price_unit_price = price_info.get("unit") if isinstance(price_info, dict) else None
    price_period = price_info.get("period") if isinstance(price_info, dict) else None
    source_for_inference = slice_text or raw_text
    ai_extraction = _apply_source_evidence_gates(ai_extraction, source_for_inference)
    ai_extraction = _ground_locality_to_source(ai_extraction, source_for_inference)
    listing_count = ai_extraction.get("listing_count")
    bhk_val = ai_extraction.get("bhk")
    bhk_str = None
    if bhk_val is not None:
        if bhk_val == 0.5:
            bhk_str = "1 RK"
        elif bhk_val == int(bhk_val):
            bhk_str = f"{int(bhk_val)} BHK"
        else:
            bhk_str = f"{bhk_val} BHK"
    price_info = ai_extraction.get("price", {})
    price, price_unit = _price_from_ai_and_raw(price_info, source_for_inference)
    source_rent_raw = _source_rent_price_text(source_for_inference) if listing_type == "rent" else None
    if listing_type == "rent" and source_rent_raw:
        source_price = _parse_raw_price_to_abs(source_rent_raw)
        if source_price is not None:
            price, price_unit = source_price, "abs"
    category = ai_extraction.get("property_category")
    asset_type = category.lower() if category else None
    if listing_type == "rent" and price_unit != "per_sqft" and price is not None:
        if price_info.get("amount") is not None or price_info.get("raw_price_text"):
            price = canonical_rental_price_rupees(
                price_info.get("amount"),
                price_info.get("unit"),
                price_info.get("raw_price_text"),
            )
    # Use the source-grounded unit returned above, not the provider's raw unit.
    price_model = "psf" if price_unit == "per_sqft" else None

    locality = ai_extraction.get("locality", {})
    if isinstance(locality, dict):
        rl = locality.get("resolved_locality")
        micro_market = rl if rl and str(rl).strip().lower() != "none" else None
        rm = locality.get("raw_mention")
        location_raw = rm if rm and str(rm).strip().lower() != "none" else None
    else:
        micro_market = None
        location_raw = None

    source_for_inference = slice_text or raw_text
    inferred_building, inferred_locality, explicit_boundary_seen = _explicit_bold_building_context(source_for_inference)
    inferred_building = inferred_building or _infer_building_name_from_source(source_for_inference, micro_market)
    # Keep explicit source labels authoritative when the model returns null.
    # This covers broker shorthand such as ``Bildg : Vardhaman Estate`` and
    # commercial blocks such as ``Location: Lower Parel West``.
    source_lines = [re.sub(r"[*_`~]", "", line).strip(" -:•") for line in str(source_for_inference).splitlines()]
    if source_lines and not explicit_boundary_seen:
        heading = re.sub(r"(?i)^\s*(?:\(\s*\d+\s*\)|\d+[.)])\s*", "", source_lines[0]).strip()
        heading_parts = re.split(r"\s+[–—-]\s+", heading, maxsplit=1)
        if len(heading_parts) == 2 and not location_raw:
            heading_locality = heading_parts[1].strip(" .,;|-_")
            if heading_locality and re.search(r"[A-Za-z]", heading_locality):
                location_raw = heading_locality
                if not micro_market:
                    micro_market = heading_locality
    for source_line in source_lines:
        location_match = re.match(r"(?i)^location\s*[:=-]\s*(.+)$", source_line)
        if location_match and not location_raw:
            location_raw = location_match.group(1).strip(" .,;|-_")
            if not micro_market:
                micro_market = location_raw
        building_match = re.match(r"(?i)^(?:bildg|bldg|building(?:\s+name)?)\s*[:=-]\s*(.+)$", source_line)
        if building_match and not inferred_building:
            inferred_building = building_match.group(1).strip(" .,;|-_")
    if inferred_locality and not micro_market:
        micro_market = inferred_locality
    if inferred_locality and not location_raw:
        location_raw = inferred_locality
    ai_building = ai_extraction.get("building_name")
    # A multi-listing model response can copy the previous block's building
    # into the next item. If the proposed name has no meaningful token in this
    # item's source slice, prefer the source-grounded candidate instead.
    building_source_repaired = False
    if ai_building and inferred_building:
        ai_tokens = _meaningful_name_tokens(ai_building)
        source_tokens = _meaningful_name_tokens(source_for_inference)
        normalized_ai = " ".join(sorted(ai_tokens))
        normalized_inferred = " ".join(sorted(_meaningful_name_tokens(inferred_building)))
        inferred_is_strict_subset = (
            normalized_inferred
            and normalized_ai != normalized_inferred
            and _meaningful_name_tokens(inferred_building) < ai_tokens
        )
        if (ai_tokens and ai_tokens.isdisjoint(source_tokens)) or inferred_is_strict_subset:
            ai_building = inferred_building
            building_source_repaired = True

    # Never retain a model-supplied building that is absent from this exact
    # source slice. Without this guard, a multi-listing response can copy a
    # building from a neighboring item into an otherwise unrelated listing.
    if ai_building and not inferred_building:
        ai_tokens = _meaningful_name_tokens(ai_building)
        source_tokens = _meaningful_name_tokens(source_for_inference)
        if ai_tokens and ai_tokens.isdisjoint(source_tokens):
            ai_building = None
            ai_extraction["title"] = None
            building_source_repaired = True
            flags = list(ai_extraction.get("validation_flags") or [])
            flags.append("building_name_removed_without_source_evidence")
            ai_extraction["validation_flags"] = list(dict.fromkeys(flags))

    if building_source_repaired:
        ai_extraction["building_name"] = ai_building
        ai_extraction["title"] = None
        flags = list(ai_extraction.get("validation_flags") or [])
        flags.append("building_name_repaired_from_explicit_source_boundary")
        ai_extraction["validation_flags"] = list(dict.fromkeys(flags))

    title = ai_extraction.get("title") or None

    # ── v2 schema fields — physical / deal attributes ──────────────
    bathroom_count = ai_extraction.get("bathroom_count")
    car_parking_count = ai_extraction.get("car_parking_count")
    parking_type = ai_extraction.get("parking_type")
    deposit_amount = ai_extraction.get("deposit_amount")
    oc_status = ai_extraction.get("oc_status")
    interior_value = ai_extraction.get("interior_value")
    ceiling_height = ai_extraction.get("ceiling_height")
    price_basis = ai_extraction.get("price_basis")
    configuration_type = ai_extraction.get("configuration_type")
    if configuration_type is not None:
        configuration_type_text = str(configuration_type).strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", configuration_type_text):
            configuration_number = _safe_float(configuration_type_text)
            configuration_type = "1 RK" if configuration_number == 0.5 else f"{configuration_number:g} BHK"
        elif re.fullmatch(r"(\d+(?:\.\d+)?)\s*(BHK|RK)", configuration_type_text, re.IGNORECASE):
            configuration_type = re.sub(r"\s+", " ", configuration_type_text).upper()
        else:
            configuration_type = configuration_type_text
    elif bhk_val is not None:
        configuration_type = "1 RK" if bhk_val == 0.5 else f"{bhk_val:g} BHK"
    lease_term_type = ai_extraction.get("lease_term_type")
    contacts = ai_extraction.get("contacts") if isinstance(ai_extraction.get("contacts"), list) else []
    society_restrictions = ai_extraction.get("society_restrictions")
    if not isinstance(society_restrictions, list):
        society_restrictions = []

    # ── v2 schema — amenities split ────────────────────────────────
    # building_amenities → routed to buildings.amenities via building_amenities key
    building_amenities = ai_extraction.get("building_amenities") or []
    # amenities → unit-specific items
    unit_amenities = ai_extraction.get("amenities") or []
    # vague claims → plain text, never structured
    amenities_unverified_claim = ai_extraction.get("amenities_unverified_claim") or None

    # ── v2 schema — rental / tenancy policy ────────────────────────
    pet_policy = ai_extraction.get("pet_policy") or None
    tenant_type_preference = ai_extraction.get("tenant_type_preference") or None
    sharing_allowed = ai_extraction.get("sharing_allowed") or None
    company_lease_criteria = ai_extraction.get("company_lease_criteria") or None
    # IMPORTANT: tenant_nationality_preference is INTERNAL/BROKER-FACING ONLY.
    # Must NEVER appear in any public-facing API response, search filter,
    # or badge on propai.live / consumer surfaces.
    tenant_nationality_preference = ai_extraction.get("tenant_nationality_preference") or None
    brokerage_type = ai_extraction.get("brokerage_type") or None

    deposit_fields = _parse_deposit(
        str(ai_extraction.get("deposit_raw_text") or raw_text),
        price if listing_type == "rent" and price_unit != "per_sqft" else None,
    )
    if deposit_amount is not None:
        parsed_deposit_amount = _safe_float(deposit_amount)
        if parsed_deposit_amount is not None:
            deposit_fields["deposit_amount"] = parsed_deposit_amount
            deposit_fields["deposit_applicable"] = True
    if ai_extraction.get("deposit_months") is not None:
        deposit_fields["deposit_months"] = ai_extraction.get("deposit_months")

    parsed = {
        "intent": intent,
        "principal": None,
        "bhk": bhk_str,
        "listing_count": listing_count,
        "configuration": None,
        "price": price,
        "price_unit": price_unit,
        "price_model": price_model,
        "price_per_sqft": price if listing_type == "sale" and price_unit == "per_sqft" else None,
        "monthly_rent": price if listing_type == "rent" and price_unit != "per_sqft" else None,
        "total_asking_price": price if listing_type in ("sale",) and price_unit != "per_sqft" else None,
        "area_sqft": ai_extraction.get("carpet_area_sqft"),

        "furnishing": ai_extraction.get("furnishing_status") or None,
        "furnishing_canonical": None,

        "location_raw": location_raw,
        "building_name": ai_building or inferred_building,
        "landmark_name": None,
        "street_name": None,
        "area": None,
        "micro_market": micro_market,
        "developer": None,

        "asset_type": asset_type,
        "property_type": None,
        "transaction_type": ai_extraction.get("transaction_type") or classified_transaction,
        "commercial_use_type": ai_extraction.get("commercial_use_type"),
        "fitout_status": ai_extraction.get("fitout_status"),
        "occupancy_type": ai_extraction.get("occupancy_status") or None,
        "floor_range": None,
        "rent_per_sqft": price if listing_type == "rent" and price_unit == "per_sqft" else None,

        "availability_status": None,
        "possession_status": ai_extraction.get("possession_status") or None,
        "possession_date": ai_extraction.get("possession_date") or None,
        "available_from": ai_extraction.get("available_from") or None,
        "availability_date_raw": ai_extraction.get("availability_date_raw") or None,
        "ready_by": None,
        "construction_stage": None,
        "launch_timeline": None,
        "expected_possession": None,

        "deposit": None,
        "lock_in_period": None,

        "broker_name": None,
        "broker_phone": None,
        "forwarded": 0,
        "confidence": max(0.0, min(1.0, float(
            ai_extraction.get("extraction_confidence_score")
            if ai_extraction.get("extraction_confidence_score") is not None
            else ai_extraction.get("confidence", 0.0)
        ))),
        "needs_review": bool(ai_extraction.get("needs_review")),
        "validation_flags": list(ai_extraction.get("validation_flags") or []),
        "raw_payload": {"full_text": raw_text, "slice_text": slice_text or raw_text},
        "normalized_message": _redact_indian_mobiles(slice_text or raw_text),
        "location": None,
        "message_type": listing_type,

        # v2 schema — physical / deal attributes
        "carpet_area_sqft": ai_extraction.get("carpet_area_sqft"),
        "built_up_area_sqft": ai_extraction.get("built_up_area_sqft"),
        "bathroom_count": _safe_int(bathroom_count),
        "car_parking_count": _safe_int(car_parking_count),
        "parking_type": parking_type,
        **deposit_fields,
        "deposit_amount": _safe_float(deposit_amount) if deposit_amount is not None else deposit_fields.get("deposit_amount"),
        "deposit_months": ai_extraction.get("deposit_months") or deposit_fields.get("deposit_months"),
        "deposit_raw_text": ai_extraction.get("deposit_raw_text") or deposit_fields.get("deposit_raw_text"),
        "oc_status": oc_status,
        "interior_value": _safe_float(interior_value),
        "ceiling_height": ceiling_height,
        "price_basis": price_basis,
        "brokerage_type": brokerage_type,
        "brokerage_context": ai_extraction.get("brokerage_context"),
        "brokerage_terms_raw": ai_extraction.get("brokerage_terms_raw"),
        "co_brokered": ai_extraction.get("co_brokered"),
        "plus_one_deal": ai_extraction.get("plus_one_deal"),
        "fee_sharing_required": ai_extraction.get("fee_sharing_required"),
        "client_profile_required": ai_extraction.get("client_profile_required"),
        "configuration_type": configuration_type,
        "configuration_details": ai_extraction.get("configuration_details"),
        "original_bhk": _safe_float(ai_extraction.get("original_bhk")),
        "current_bhk": _safe_float(ai_extraction.get("current_bhk")),
        "is_converted_unit": ai_extraction.get("is_converted_unit"),
        "is_combination_unit": ai_extraction.get("is_combination_unit"),
        "can_sell_separately": ai_extraction.get("can_sell_separately"),
        "lease_term_type": lease_term_type,
        "availability_status": ai_extraction.get("availability_status"),
        "wing": ai_extraction.get("wing"),
        "floor_min": _safe_int(ai_extraction.get("floor_min")),
        "floor_max": _safe_int(ai_extraction.get("floor_max")),
        "floor_label": ai_extraction.get("floor_label"),
        "balcony_area_sqft": _safe_float(ai_extraction.get("balcony_area_sqft")),
        "balcony_area_raw_text": ai_extraction.get("balcony_area_raw_text"),
        "balcony_present": ai_extraction.get("balcony_present"),
        "terrace_area_sqft": _safe_float(ai_extraction.get("terrace_area_sqft")),
        "covered_terrace_area_sqft": _safe_float(ai_extraction.get("covered_terrace_area_sqft")),
        "terrace_area_raw_text": ai_extraction.get("terrace_area_raw_text"),
        "sit_out_present": ai_extraction.get("sit_out_present"),
        "sellable_area_sqft": _safe_float(ai_extraction.get("sellable_area_sqft")),
        "computed_total_asking_price": _safe_float(ai_extraction.get("computed_total_asking_price")),
        "computed_price_confidence": ai_extraction.get("computed_price_confidence"),
        "price_math": ai_extraction.get("price_math") if isinstance(ai_extraction.get("price_math"), dict) else {},
        "unit_condition": ai_extraction.get("unit_condition"),
        "vastu_compliant": ai_extraction.get("vastu_compliant"),
        "view_description": ai_extraction.get("view_description"),
        "has_lift": ai_extraction.get("has_lift"),
        "has_power_backup": ai_extraction.get("has_power_backup"),
        "power_load_kw": ai_extraction.get("power_load_kw"),
        "parking_details": ai_extraction.get("parking_details") if isinstance(ai_extraction.get("parking_details"), dict) else {},
        "society_restrictions": society_restrictions,
        "society_restrictions_raw": ai_extraction.get("society_restrictions_raw"),
        "broker_company": ai_extraction.get("broker_company"),
        "contacts": contacts[:8],
        "showing_instructions": ai_extraction.get("showing_instructions"),
        "contact_instructions": ai_extraction.get("contact_instructions"),
        "unstructured_facts": ai_extraction.get("unstructured_facts") if isinstance(ai_extraction.get("unstructured_facts"), dict) else {},
        "availability_status": ai_extraction.get("availability_status"),
        "availability_date_raw": ai_extraction.get("availability_date_raw"),
        "unit_condition": ai_extraction.get("unit_condition"),
        "lease_term_min_months": _safe_int(ai_extraction.get("lease_term_min_months")),
        "lease_term_max_months": _safe_int(ai_extraction.get("lease_term_max_months")),
        "lease_term_raw_text": ai_extraction.get("lease_term_raw_text"),

        # v2 schema — amenities
        "amenities": unit_amenities if isinstance(unit_amenities, list) else [],
        "amenities_unverified_claim": amenities_unverified_claim,
        "building_amenities": building_amenities if isinstance(building_amenities, list) else [],

        # v2 schema — rental / tenancy policy
        "pet_policy": pet_policy,
        "tenant_type_preference": tenant_type_preference,
        "sharing_allowed": sharing_allowed,
        "company_lease_criteria": company_lease_criteria,
        "tenant_nationality_preference": tenant_nationality_preference,
    }
    parsed = _rescue_core_fields(parsed, source_for_inference)
    parsed["summary_title"] = _source_grounded_title(ai_extraction, parsed, source_for_inference)
    if _title_evidence_mismatch(
        title,
        raw_text or source_for_inference,
        parsed.get("building_name"),
    ):
        flags = list(parsed.get("validation_flags") or [])
        flags.append("title_evidence_mismatch")
        parsed["validation_flags"] = list(dict.fromkeys(flags))
        parsed["needs_review"] = True
        ai_extraction["needs_review"] = True
        ai_extraction["validation_flags"] = list(dict.fromkeys(
            list(ai_extraction.get("validation_flags") or []) + ["title_evidence_mismatch"]
        ))
    if listing_type == "rent" and rent_price_needs_review(parsed.get("monthly_rent"), raw_text):
        parsed["needs_review"] = True
        parsed["confidence"] = 0.3
    return parsed


def _ai_extraction_to_typed(
    ai_extraction: dict,
    raw_text: str,
    sender_name: str = "",
    push_name: str = "",
    slice_text: str | None = None,
    *,
    raw_message_id: int | None = None,
    tenant_id: str | None = None,
    broker_id: int | None = None,
    broker_phone: str | None = None,
    listing_index: int = 0,
) -> tuple[str, dict]:
    """Convert one normalized LLM item into a row for a typed table.

    This is deliberately pure: it does not perform I/O and therefore can be
    tested before a worker is allowed to write to Supabase.  ``save_parsed``
    remains a compatibility wrapper for the existing resolver flow, while new
    callers can use this explicit table/row contract directly.
    """
    source_text = (slice_text or raw_text or "").strip()
    ai = _clean_extraction_value(dict(ai_extraction or {}))
    ai = _normalize_source_inventory_route(ai, source_text)
    ai = _source_ground_requirement_item(ai, source_text)
    ai = _apply_source_evidence_gates(ai, source_text)
    ai = _ground_locality_to_source(ai, source_text)
    ai = _normalize_source_inventory_route(ai, source_text)
    asset = str(ai.get("property_category") or "residential").lower()
    if asset not in {"residential", "commercial"}:
        asset = "residential"
    # `listing_type` is the contract used by the extraction schema and is
    # source-grounded for this item.  Some providers have historically
    # emitted a conflicting classified_transaction_type (for example,
    # listing_type=sale with classified_transaction_type=rent); routing on
    # the latter sends the row into the wrong typed table.
    listing_type = str(ai.get("routing_listing_type") or ai.get("listing_type") or "").strip().lower()
    explicit_route = _explicit_source_inventory_type(source_text)
    if explicit_route:
        listing_type = explicit_route
        tx = explicit_route
    else:
        tx = str(ai.get("transaction_type") or ai.get("classified_transaction_type") or listing_type or "sale").lower()
        if tx not in {"sale", "rent"}:
            tx = "sale"
    # ``mixed`` describes the document, not every item in it. A mixed
    # broadcast can contain both supply and demand; route each item from its
    # own listing_type and only treat the item as demand when it is explicitly
    # a requirement.
    is_requirement = not explicit_route and (
        listing_type == "requirement"
        or ai.get("classified_is_requirement") is True
        or (
            ai.get("message_class") == "requirement"
            and listing_type not in {"sale", "rent"}
        )
    )
    table_map = {
        ("residential", "sale", False): "residential_sale_listings",
        ("residential", "rent", False): "residential_rent_listings",
        ("commercial", "sale", False): "commercial_sale_listings",
        ("commercial", "rent", False): "commercial_rent_listings",
        ("residential", "sale", True): "residential_sale_requirements",
        ("residential", "rent", True): "residential_rent_requirements",
        ("commercial", "sale", True): "commercial_sale_requirements",
        ("commercial", "rent", True): "commercial_rent_requirements",
    }
    table = table_map[(asset, tx, is_requirement)]
    flat = _ai_extraction_to_parsed(ai, raw_text, sender_name, push_name, slice_text)
    locality = ai.get("locality") if isinstance(ai.get("locality"), dict) else {}
    raw_locality = locality.get("raw_mention") or flat.get("location_raw")
    resolved_locality = locality.get("resolved_locality") or flat.get("micro_market")
    fingerprint = hashlib.sha256(source_text.lower().encode("utf-8")).hexdigest()
    building_name = flat.get("building_name") or ai.get("building_name") or _infer_building_name_from_source(source_text, resolved_locality)
    bhk_str = flat.get("bhk")
    row = {
        "raw_message_id": raw_message_id,
        "tenant_id": tenant_id,
        "listing_index": listing_index,
        "listing_count": _safe_int(ai.get("listing_count")),
        "asset_type": asset,
        "transaction_type": tx,
        "source_fingerprint": fingerprint,
        "building_name": building_name,
        "locality_raw": raw_locality,
        "locality_resolved": resolved_locality,
        "micro_market": resolved_locality,
        "landmark_name": ai.get("landmark_name"),
        "street_name": ai.get("street_name"),
        "developer_name": ai.get("developer_name") or ai.get("developer"),
        "broker_id": broker_id,
        "broker_name": _clean_broker_name(ai.get("broker_name") or sender_name or push_name),
        "broker_phone": broker_phone,
        "broker_rera_number": ai.get("broker_rera_number"),
        "group_name": ai.get("group_name"),
        # Rebuild the title when a source guard repaired/quarantined the
        # building field; never publish the stale AI title containing the bad
        # token. Otherwise retain the richer model-generated title.
        "summary_title": _source_grounded_title(ai, flat, source_text),
        "normalized_message": _redact_indian_mobiles(source_text),
        "raw_payload": {"full_text": raw_text, "slice_text": slice_text or raw_text},
        "ai_extraction": ai,
        "deal_tags": ai.get("deal_tags") or [],
        "additional_charges": ai.get("additional_charges") or [],
        "validation_flags": ai.get("validation_flags") or [],
        "needs_review": bool(ai.get("needs_review")),
        "extraction_confidence": ai.get("extraction_confidence") or "medium",
        "extraction_confidence_score": max(0.0, min(1.0, float(ai.get("extraction_confidence_score") or ai.get("confidence") or 0.0))),
    }
    price_info = ai.get("price") if isinstance(ai.get("price"), dict) else {}
    price_value, price_unit = _price_from_ai_and_raw(price_info, source_text)
    source_rent_raw = _source_rent_price_text(source_text) if tx == "rent" else None
    if tx == "rent" and source_rent_raw:
        source_price = _parse_raw_price_to_abs(source_rent_raw)
        if source_price is not None:
            price_value, price_unit = source_price, "abs"
    if tx == "rent" and price_unit != "per_sqft" and price_value is not None:
        # If the model omitted price entirely, the helper already recovered
        # the explicit quote from this exact source slice. Do not pass the
        # model's null amount into the rental normalizer.
        if price_info.get("amount") is not None or price_info.get("raw_price_text"):
            normalizer = canonical_commercial_rental_price_rupees if asset == "commercial" else canonical_rental_price_rupees
            price_value = normalizer(
                price_info.get("amount"),
                price_info.get("unit"),
                price_info.get("raw_price_text") or source_text,
            )
    area = _safe_float(ai.get("carpet_area_sqft") or flat.get("area_sqft"))
    bhk = _normalized_bhk(flat.get("bhk") or ai.get("bhk") or ai.get("bhk_options"))
    if not is_requirement:
        row.update({
            "bhk": bhk,
            "original_bhk": _safe_float(ai.get("original_bhk")),
            "current_bhk": _safe_float(ai.get("current_bhk")),
            "carpet_area_sqft": area,
            "built_up_area_sqft": ai.get("built_up_area_sqft"),
            "super_built_up_area_sqft": ai.get("super_built_up_area_sqft"),
            "area_raw_text": ai.get("area_raw_text"),
            "balcony_area_sqft": _safe_float(ai.get("balcony_area_sqft")),
            "balcony_area_raw_text": ai.get("balcony_area_raw_text"),
            "terrace_area_sqft": _safe_float(ai.get("terrace_area_sqft")),
            "covered_terrace_area_sqft": _safe_float(ai.get("covered_terrace_area_sqft")),
            "terrace_area_raw_text": ai.get("terrace_area_raw_text"),
            "sellable_area_sqft": _safe_float(ai.get("sellable_area_sqft")),
            "price_raw_text": price_info.get("raw_price_text") or source_rent_raw,
            "price_basis": ai.get("price_basis"),
            "computed_total_asking_price": _safe_float(ai.get("computed_total_asking_price")),
            "computed_price_confidence": ai.get("computed_price_confidence"),
            "price_math": ai.get("price_math") if isinstance(ai.get("price_math"), dict) else {},
            "furnishing_status": ai.get("furnishing_status"),
            "unit_condition": ai.get("unit_condition"),
            "availability_status": ai.get("availability_status"),
            "possession_status": ai.get("possession_status"),
            "possession_date": ai.get("possession_date"),
            "bathroom_count": ai.get("bathroom_count"),
            "car_parking_count": ai.get("car_parking_count"),
            "parking_type": ai.get("parking_type"),
            "parking_details": ai.get("parking_details") if isinstance(ai.get("parking_details"), dict) else {},
            "floor_range": ai.get("floor_range"),
            "floor_min": _safe_int(ai.get("floor_min")),
            "floor_max": _safe_int(ai.get("floor_max")),
            "floor_label": ai.get("floor_label"),
            "wing": ai.get("wing"),
            "building_amenities": ai.get("building_amenities") or [],
            "unit_amenities": ai.get("amenities") or [],
            "amenities_unverified_claim": ai.get("amenities_unverified_claim"),
            "brokerage_type": ai.get("brokerage_type"),
            "brokerage_context": ai.get("brokerage_context"),
            "co_brokered": ai.get("co_brokered"),
            "plus_one_deal": ai.get("plus_one_deal"),
            "fee_sharing_required": ai.get("fee_sharing_required"),
            "brokerage_terms_raw": ai.get("brokerage_terms_raw"),
            "client_profile_required": ai.get("client_profile_required"),
            "availability_status": ai.get("availability_status"),
            "availability_date_raw": ai.get("availability_date_raw"),
            "has_lift": ai.get("has_lift"),
            "balcony_present": ai.get("balcony_present"),
            "sit_out_present": ai.get("sit_out_present"),
            "lease_term_min_months": _safe_int(ai.get("lease_term_min_months")),
            "lease_term_max_months": _safe_int(ai.get("lease_term_max_months")),
            "lease_term_raw_text": ai.get("lease_term_raw_text"),
            "broker_company": ai.get("broker_company"),
            "contacts": ai.get("contacts")[:8] if isinstance(ai.get("contacts"), list) else [],
            "showing_instructions": ai.get("showing_instructions"),
            "contact_instructions": ai.get("contact_instructions"),
            "unit_condition": ai.get("unit_condition"),
            "view_description": ai.get("view_description"),
            "parking_details": ai.get("parking_details") if isinstance(ai.get("parking_details"), dict) else {},
            "society_restrictions_raw": ai.get("society_restrictions_raw"),
            "unstructured_facts": ai.get("unstructured_facts") if isinstance(ai.get("unstructured_facts"), dict) else {},
            "configuration_details": ai.get("configuration_details"),
            "is_converted_unit": ai.get("is_converted_unit"),
            "is_combination_unit": ai.get("is_combination_unit"),
            "can_sell_separately": ai.get("can_sell_separately"),
            "vastu_compliant": ai.get("vastu_compliant"),
            "view_description": ai.get("view_description"),
            "society_restrictions": ai.get("society_restrictions") if isinstance(ai.get("society_restrictions"), list) else [],
            "society_restrictions_raw": ai.get("society_restrictions_raw"),
            "broker_company": ai.get("broker_company"),
            "contacts": ai.get("contacts")[:8] if isinstance(ai.get("contacts"), list) else [],
            "showing_instructions": ai.get("showing_instructions"),
            "contact_instructions": ai.get("contact_instructions"),
            "unstructured_facts": ai.get("unstructured_facts") if isinstance(ai.get("unstructured_facts"), dict) else {},
        })
        if tx == "sale":
            row["total_asking_price"] = price_value if price_unit != "per_sqft" else None
            row["price_per_sqft"] = price_value if price_unit == "per_sqft" else None
            if price_unit == "per_sqft":
                price_basis = str(ai.get("price_basis") or "").lower()
                pricing_area = ai.get("chargeable_area_sqft") if asset == "commercial" and "chargeable" in price_basis else area
                if pricing_area:
                    row["total_asking_price"] = price_value * pricing_area
                    row["price_math"] = {
                        "rate": price_value,
                        "basis": "chargeable_area_sqft" if asset == "commercial" and "chargeable" in price_basis else "carpet_area_sqft",
                        "area_sqft": pricing_area,
                        "formula": f"{price_value} * {pricing_area}",
                        "computed_total_asking_price": row["total_asking_price"],
                    }
        else:
            row["monthly_rent"] = price_value if price_unit != "per_sqft" else None
            row["rent_per_sqft"] = price_value if price_unit == "per_sqft" else None
            if price_unit == "per_sqft":
                price_basis = str(ai.get("price_basis") or "").lower()
                pricing_area = (
                    ai.get("chargeable_area_sqft")
                    if asset == "commercial" and "chargeable" in price_basis
                    else area
                )
                if pricing_area:
                    row["monthly_rent"] = price_value * pricing_area
                    row["price_math"] = {
                        "rate": price_value,
                        "basis": "chargeable_area_sqft" if asset == "commercial" and "chargeable" in price_basis else "carpet_area_sqft",
                        "area_sqft": pricing_area,
                        "formula": f"{price_value} * {pricing_area}",
                        "computed_monthly_rent": row["monthly_rent"],
                    }
        if asset == "commercial":
            # Unknown commercial use is not evidence of mixed use. Persist the
            # subtype only when extraction found one in the source message.
            row["commercial_use_type"] = ai.get("commercial_use_type")
            row["fitout_status"] = ai.get("fitout_status")
            row["ceiling_height"] = ai.get("ceiling_height")
            row["power_load_kw"] = ai.get("power_load_kw")
            row["cam_amount"] = ai.get("cam_amount")
            row["cam_applicable"] = ai.get("cam_applicable")
            row["cam_unit"] = ai.get("cam_unit")
            for field in (
                "broker_rera_number", "floor_level", "floor_count", "mezzanine_area_sqft", "possession_status",
                "possession_date", "availability_status", "rent_inclusions", "license_type",
                "short_term_allowed", "inspection_notice_minutes", "frontage_ft",
                "entrance_count", "otla_area_sqft", "otla_area_raw_text", "terrace_area_sqft",
                "covered_terrace_area_sqft", "terrace_area_raw_text", "heritage_space",
                "permitted_use_types", "ideal_for", "automatic_shutter_count", "room_count",
                "suite_count", "banquet_hall_count", "restaurant_count", "bar_facility",
                "operational_status", "director_cabin_count", "ceo_cabin_present",
                "cubicle_count", "conference_room_capacity", "meeting_room_capacity",
                "training_room_capacity", "cafeteria_seat_count", "accounts_area", "lounge_area",
                "developer_name", "super_built_up_area_sqft", "saleable_area_sqft",
                "project_inventory", "area_min_sqft", "area_max_sqft", "floor_plate_sqft",
                "project_status",
            ):
                if ai.get(field) is not None:
                    row[field] = ai.get(field)
            row["rent_inclusions"] = ai.get("rent_inclusions")
        if tx == "rent":
            rent = row.get("monthly_rent")
            row.update(_parse_deposit(str(ai.get("deposit_raw_text") or raw_text), rent))
            if rent_price_needs_review(rent, raw_text):
                row["needs_review"] = True
                row["extraction_confidence"] = "low"
    else:
        budget_min = _clean_budget_bound(ai.get("budget_min"))
        budget_max = _clean_budget_bound(ai.get("budget_max") or price_value)
        # A single stated requirement budget is a point budget, not a sale
        # price. Keep both bounds populated for the UI/search contract unless
        # the wording explicitly makes it an upper ceiling.
        budget_text = f"{source_text} {slice_text or ''}".lower()
        has_upper_ceiling = bool(re.search(r"\b(?:up\s*to|upto|max(?:imum)?|under|below|not\s+exceed(?:ing)?)\b", budget_text))
        if budget_min is None and budget_max is not None and not has_upper_ceiling:
            budget_min = budget_max
        row.update({
            # A requirement can be for rent or purchase. The old hard-coded
            # BUY here made rental requirements render as "buy" cards even
            # when the source/model clearly classified them as rent.
            "intent": "RENT" if tx == "rent" else "BUY",
            "budget_min": budget_min,
            "budget_max": budget_max,
            "budget_currency": "INR",
            "area_min_sqft": ai.get("area_min_sqft") or area,
            "area_max_sqft": ai.get("area_max_sqft") or area,
            "locality_options": ai.get("locality_options") or ([resolved_locality] if resolved_locality else []),
            "is_flexible": bool(ai.get("is_flexible")),
            "urgency": ai.get("urgency") or "normal",
            "status": "active",
            "furnishing_preference": ai.get("furnishing_preference"),
            "possession_preference": ai.get("possession_preference"),
            "amenity_requirements": ai.get("amenity_requirements") or [],
        })
        # BHK is a residential-only requirement dimension. Do not put the
        # generic field into commercial payloads and make storage discard it
        # later with a warning.
        if asset == "residential":
            row["bhk_options"] = [bhk] if bhk is not None else []
        if asset == "commercial":
            row["commercial_use_type"] = ai.get("commercial_use_type") or []
            for field in (
                "intended_use_details", "area_basis_preference", "location_flexibility",
                "floor_min", "floor_max", "floor_count_max", "floor_preference",
                "consecutive_floors_required", "parking_required",
                "needs_attached_washroom", "needs_washroom", "needs_pantry",
                "needs_mezzanine", "needs_lift", "needs_power_backup", "needs_central_ac",
                "power_requirements", "premium_building_required", "glass_facade_required",
                "residential_cum_commercial_ok", "by_lanes_accepted", "media_requested",
                "entrance_requirement", "signage_required", "loading_access_required",
                "budget_includes_maintenance",
                "min_cabin_count", "min_workstation_count", "needs_conference_room",
                "brokerage_context", "brokerage_terms_raw", "contacts",
            ):
                if ai.get(field) is not None:
                    row[field] = ai.get(field)
            row["urgency"] = ai.get("urgency") or row.get("urgency")
        elif tx == "rent":
            row.update({
                "tenant_type": ai.get("tenant_type"),
                "nationality": ai.get("nationality"),
                "has_pets": ai.get("has_pets"),
                "sharing_acceptable": ai.get("sharing_acceptable"),
                "food_preference": ai.get("food_preference"),
                "floor_preference": ai.get("floor_preference"),
                "view_preference": ai.get("view_preference"),
                "building_preferences": ai.get("building_preferences") or [],
                "age_preference": ai.get("age_preference"),
                "car_parking_min": ai.get("car_parking_min"),
                "amenity_requirements": ai.get("amenity_requirements") or [],
                "lease_term_preference": ai.get("lease_term_preference"),
                "deposit_budget_max": ai.get("deposit_budget_max"),
                "brokerage_willingness": ai.get("brokerage_willingness"),
            })
    row = _clean_extraction_value(row)
    return table, {k: v for k, v in row.items() if v is not None}


def _normalized_bhk(value) -> float | None:
    """Return a comparable BHK value without guessing when none is present."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if "rk" in text:
        return 0.5
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _deterministic_price_rupees(item: dict) -> float | None:
    """Convert a deterministic boundary price to absolute AED."""
    return canonical_price_rupees(item.get("price"), item.get("price_unit"), item.get("price_raw_text"))


def _meaningful_name_tokens(value) -> set[str]:
    if not value:
        return set()
    generic = {
        "apartment", "apartments", "building", "bungalow", "commercial",
        "flat", "office", "project", "residential", "residency", "society",
        "tower", "towers",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value).lower())
        if len(token) >= 3 and token not in generic
    }


def _ai_item_matches_boundary(ai_item: dict, boundary: dict, source_text: str) -> tuple[bool, list[str]]:
    """Reject explicit cross-unit conflicts between AI output and a split block.

    Missing fields are allowed because AI may legitimately recover an anchor the
    regex splitter missed. Explicitly conflicting BHK, area, price, or building
    values are not allowed: those are the common signs that sibling properties
    were merged or reordered.
    """
    parsed = _ai_extraction_to_parsed(ai_item, source_text, "", "")
    conflicts: list[str] = []

    boundary_bhk = _normalized_bhk(boundary.get("bhk"))
    ai_bhk = _normalized_bhk(parsed.get("bhk"))
    if boundary_bhk is not None and ai_bhk is not None and boundary_bhk != ai_bhk:
        conflicts.append(f"bhk {ai_bhk:g}!={boundary_bhk:g}")

    try:
        boundary_area = float(boundary.get("area_sqft")) if boundary.get("area_sqft") not in (None, "") else None
        ai_area = float(parsed.get("area_sqft")) if parsed.get("area_sqft") not in (None, "") else None
    except (TypeError, ValueError):
        boundary_area = ai_area = None
    if boundary_area is not None and ai_area is not None:
        tolerance = max(25.0, boundary_area * 0.03)
        if abs(ai_area - boundary_area) > tolerance:
            conflicts.append(f"area {ai_area:g}!={boundary_area:g}")

    boundary_price = _deterministic_price_rupees(boundary)
    try:
        ai_price = float(parsed.get("price")) if parsed.get("price") not in (None, "") else None
    except (TypeError, ValueError):
        ai_price = None
    if boundary_price is not None and ai_price is not None:
        tolerance = max(25_000.0, boundary_price * 0.02)
        if abs(ai_price - boundary_price) > tolerance:
            conflicts.append(f"price {ai_price:g}!={boundary_price:g}")

    boundary_building = _meaningful_name_tokens(
        boundary.get("building_name") or boundary.get("project_name")
    )
    ai_building = _meaningful_name_tokens(parsed.get("building_name"))
    if boundary_building and ai_building and boundary_building.isdisjoint(ai_building):
        conflicts.append("building mismatch")

    return not conflicts, conflicts


def check_share_eligibility(parsed: dict, org_privacy: dict, conv_type: str = "unknown") -> tuple[bool, str]:
    """Return whether a parsed item may participate in shared-market views.

    Raw messages and tenant-owned observations are always retained. This flag
    only controls cross-tenant sharing/materialized market surfaces.
    """
    mode = str(org_privacy.get("privacy_mode") or "private").lower()
    if mode in {"private", "tenant_private"}:
        return False, "organization_privacy_private"

    intent = str(parsed.get("intent") or conv_type or "").upper()
    is_requirement = intent in {"BUY", "REQUIREMENT", "RENTAL_SEEKER", "TENANT", "DEMAND"}
    if is_requirement and not org_privacy.get("share_requirements", False):
        return False, "requirements_sharing_disabled"
    if not is_requirement and not org_privacy.get("share_listings", False):
        return False, "listing_sharing_disabled"
    return True, "eligible"


_INDIAN_MOBILE_IN_TEXT = re.compile(r'(?<!\d)(?:\+?91[-.\s]?)?[6-9]\d{9}(?!\d)')
_UAE_MOBILE_IN_TEXT = re.compile(
    r"(?<!\d)(?:\+?971[-\s]?[2-7]\d{7,8}|0?5\d[-\s]?\d{3}[-\s]?\d{4})(?!\d)"
)
_SIGNATURE_PHONE_IN_TEXT = re.compile(
    rf"{_INDIAN_MOBILE_IN_TEXT.pattern}|{_UAE_MOBILE_IN_TEXT.pattern}"
)

_REDACTED_MARKER = "[Contact redacted — see agent]"
_INDIAN_MOBILE_LOOSE = re.compile(r'(?<!\d)(?:\+?91[-.\s]?)?[6-9]\d{4}[-\s.]?\d{5}(?!\d)')
# 11-digit bare phones (with optional '0' STD prefix) that real brokers paste
# without separators. The strict LOOSE pattern misses them because its
# 5-trailing-digit lookahead hits the leftover 11th digit. Lookbehind/lookahead
# keep the digit boundary tight (no embedding in longer runs).
_INDIAN_MOBILE_LONG = re.compile(r'(?<!\d)(?:0[6-9]\d{9}|[6-9]\d{10})(?!\d)')

def _redact_indian_mobiles(text: str) -> str:
    """Replace Indian mobile numbers with a redaction marker for display.

    Catches standard 10-digit (9876543210, +91 9876543210, 98765-43210),
    11-digit bare phones (90048427759, 84335469487) and 12-digit STD-prefixed
    numbers (09004842775). The original digits still live in
    raw_payload.full_text for audit and broker-resolution paths.

    Note: 3+3+4 / 2+2+2+2+2 obfuscation is intentionally NOT covered —
    a pre-cleaning regex would mangle prices like "Rs8.5L" into "Rs85L".
    """
    if not text:
        return ""
    cleaned = _INDIAN_MOBILE_LOOSE.sub(_REDACTED_MARKER, text)
    cleaned = _INDIAN_MOBILE_LONG.sub(_REDACTED_MARKER, cleaned)
    cleaned = _INDIAN_MOBILE_IN_TEXT.sub(_REDACTED_MARKER, cleaned)
    while _REDACTED_MARKER + " " + _REDACTED_MARKER in cleaned:
        cleaned = cleaned.replace(_REDACTED_MARKER + " " + _REDACTED_MARKER, _REDACTED_MARKER)
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    return cleaned.strip()


def _llm_source_slices_are_grounded(msg_text: str, ai_items: list[dict]) -> list[str]:
    """Accept model-provided slices only when they are exclusive raw evidence."""
    if not ai_items:
        return []

    def compact(value: object) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())

    source = compact(msg_text)
    slices = [str(item.get("source_slice") or "").strip() for item in ai_items]
    compact_slices = [compact(value) for value in slices]
    if not source or any(not value or value not in source for value in compact_slices):
        return []
    if len(set(compact_slices)) != len(compact_slices):
        return []
    # Reject nested slices: one item must not claim the complete broadcast or
    # another item's block as its own evidence.
    for index, value in enumerate(compact_slices):
        if any(
            index != other_index and (value in other or other in value)
            for other_index, other in enumerate(compact_slices)
        ):
            return []
    return slices


def _slice_blocks_for_ai_items(msg_text: str, ai_items: list) -> list[str]:
    """Assign per-listing slice text from the document segmenter output.

    Prefer semantic item-to-block matching when the model and segmenter
    disagree on counts. Falling back to the whole broadcast makes every card
    look like the original dump and defeats source-grounded extraction.
    """
    if not ai_items:
        return []
    try:
        from ai_extraction import _segment_document
        segments = _segment_document(msg_text or "")
    except Exception:
        return [msg_text] * len(ai_items)
    blocks = (segments or {}).get("blocks") or []
    if not blocks:
        return [msg_text] * len(ai_items)

    def normalize(value: object) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    def item_phrases(item: dict) -> list[str]:
        locality = item.get("locality") if isinstance(item.get("locality"), dict) else {}
        values = [
            item.get("building_name"),
            item.get("title"),
            item.get("summary_title"),
            locality.get("raw_mention"),
            locality.get("resolved_locality"),
            item.get("locality_raw"),
        ]
        phrases = []
        for value in values:
            phrase = normalize(value)
            if phrase and phrase not in phrases:
                phrases.append(phrase)
        return phrases

    def score(item: dict, block: dict) -> int:
        block_text = normalize(block.get("text"))
        if not block_text:
            return 0
        total = 0
        for phrase in item_phrases(item):
            if phrase in block_text:
                total += 100 + len(phrase.split())
            else:
                words = [word for word in phrase.split() if len(word) >= 4]
                total += sum(1 for word in words if word in block_text)
        for key in ("bhk", "carpet_area_sqft", "area_sqft", "price", "monthly_rent"):
            value = normalize(item.get(key))
            if value and value in block_text:
                total += 5
        return total

    # Dense broker broadcasts often encode one complete property per line.
    # Treat those lines as candidate evidence blocks too; otherwise a section
    # header or the next listing can be attached to the current AI item.
    line_blocks = [
        {"text": line.strip()}
        for line in str(msg_text or "").splitlines()
        if line.strip()
    ]

    def property_anchor_count(value: str) -> int:
        checks = (
            r"\b\d+(?:\.\d+)?\s*(?:bhk|rk)\b",
            r"\b\d[\d,]*(?:\.\d+)?\s*(?:sq\.?\s*ft|sqft|sft|carpet|bup)\b",
            r"(?:aed|\bdhs\.?\s*)?\d+(?:[,.]\d+)?\s*(?:m|mn|million|k)\b",
        )
        return sum(bool(re.search(pattern, value, re.IGNORECASE)) for pattern in checks)

    selected: set[int] = set()
    selected_line_indices: set[int] = set()
    result: list[str] = []
    for item_index, item in enumerate(ai_items):
        ranked_lines = sorted(
            ((score(item, block), line_index) for line_index, block in enumerate(line_blocks) if line_index not in selected_line_indices),
            key=lambda pair: (pair[0], -pair[1]),
            reverse=True,
        )
        if ranked_lines:
            line_score, line_index = ranked_lines[0]
            line_text = line_blocks[line_index]["text"]
            # A semantic phrase match (100+) plus two independent property
            # anchors is strong enough to isolate a one-line listing safely.
            if line_score >= 100 and property_anchor_count(line_text) >= 2:
                selected_line_indices.add(line_index)
                result.append(line_text)
                continue
        ranked = sorted(
            ((score(item, block), block_index) for block_index, block in enumerate(blocks)),
            key=lambda pair: (pair[0], -pair[1]),
            reverse=True,
        )
        best_score, best_index = ranked[0]
        if best_score <= 0 or best_index in selected:
            # The model preserves document order in normal operation. If no
            # semantic anchor survives normalization, use the corresponding
            # document block rather than leaking the complete broadcast.
            best_index = min(item_index, len(blocks) - 1)
        selected.add(best_index)
        text = (blocks[best_index].get("text") or "").strip()
        result.append(text or msg_text)
    return result


def _is_actionable_property_slice(value: str) -> bool:
    """Reject footer/header-only AI items before they become typed rows.

    A text-only buyer requirement can be actionable, so absence of a numeric
    property anchor is not sufficient for rejection. We reject only slices
    that positively identify themselves as broker boilerplate.
    """
    text = str(value or "")
    if _UNSUPPORTED_PG_RE.search(text):
        return False
    if re.fullmatch(
        r"(?is)[\s*_~\W]*(?:(?:updated|new|latest|available)\s+)*"
        r"(?:\d+(?:\.\d+)?\s*(?:bhk|rk)\s+)?"
        r"(?:residential\s+)?(?:outright|sale|rent|lease)?\s*list(?:ings?)?"
        r"[\s*_~\W]*",
        text,
    ):
        return False
    if re.search(
        r"\b\d+(?:\.\d+)?\s*(?:bhk|rk)\b|"
        r"\b\d[\d,]*(?:\.\d+)?\s*(?:sq\.?\s*ft|sqft|sft|carpet|bup)\b|"
        r"(?:aed|\bdhs\.?\s*)?\d+(?:[,.]\d+)?\s*(?:m|mn|million|k)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if _INDIAN_MOBILE_IN_TEXT.search(text):
        return False
    if re.search(
        r"(?i)\b(?:maha\s*rera|rera\s+regd|client\s+(?:business\s+)?profile|"
        r"allow\s+\d+\s*hrs?|set\s+up\s+visits?|for\s+(?:further|more)\s+details)\b",
        text,
    ):
        return False
    if re.fullmatch(
        r"(?is)[\s*_~\W]*(?:new\s+listings?(?:\s+added)?|residential\s+outright|"
        r"commercial\s+(?:rent|sale|lease))[\s*_~\W]*",
        text,
    ):
        return False
    return bool(re.search(r"[A-Za-z]", text))


def _extract_broker_contact_from_text(text: str) -> tuple[str | None, str | None]:
    """Extract Indian mobile number and optional broker name from message body text.

    Returns (phone, name) where phone is the 10-digit validated number (first
    match), and name is any text on the same line immediately before the number
    that doesn't look like another number.
    """
    if not text:
        return None, None
    # Do not remove newlines between separate phone numbers. Bulk broker
    # footers commonly put one contact per line; collapsing those lines turns
    # two valid 10-digit numbers into one invalid 20-digit number.
    cleaned = re.sub(r'(?<=\d)[-. ]+(?=\d)', '', text)
    phone = None
    name = None
    for m in _INDIAN_MOBILE_IN_TEXT.finditer(cleaned):
        num = m.group()
        num_clean = num[-10:] if re.match(r'^\+?91', num) else num
        if not re.fullmatch(r'[6-9]\d{9}', num_clean):
            continue
        if phone is None:
            phone = num_clean
            line_start = cleaned.rfind('\n', 0, m.start()) + 1
            preceding = cleaned[line_start:m.start()].strip().rstrip(':,').strip()
            if not preceding or re.search(r'\d', preceding) or preceding.casefold().rstrip(":") in _BROKER_FIELD_LABELS:
                lines_before = cleaned[:line_start].rstrip('\n').split('\n')
                candidates = []
                for candidate in reversed(lines_before[-4:]):
                    candidate = candidate.strip().rstrip(':,').strip()
                    if (
                        candidate
                        and candidate.casefold().rstrip(":") not in _BROKER_FIELD_LABELS
                        and not re.search(r'\d', candidate)
                        and 1 < len(candidate) <= 60
                    ):
                        candidates.append(candidate)
                if candidates:
                    # A location/company line often sits between the person's
                    # name and `Mobile:`. Prefer the most name-like multi-word
                    # candidate instead of the literal field label or city.
                    preceding = max(candidates, key=lambda value: (len(value.split()), -len(value)))
            if preceding and not re.search(r'\d', preceding) and len(preceding) > 1:
                name = re.sub(r"[^\w.' &-]", " ", _strip_icons(preceding))
                name = re.sub(r"\s+", " ", name).strip() or None
        elif num_clean != phone:
            break
    return phone, name


def _extract_broker_signature_names(text: str) -> set[str]:
    """Return every name attached to a phone in a trailing broker signature.

    Broadcasts can contain both the forwarding broker and the originating
    broker.  The primary contact resolver intentionally chooses one identity,
    but building validation must reject all footer identities so a second
    signature cannot become a synthetic building/listing.
    """
    non_empty = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not non_empty:
        return set()

    # Signatures live at the end of broker broadcasts. Keeping this window
    # bounded avoids treating a phone next to an actual inventory line near
    # the beginning of a long message as proof that the property is a person.
    footer_lines = non_empty[-8:]
    property_anchor_re = re.compile(
        r"\b\d+(?:\.\d+)?\s*(?:bhk|rk)\b|"
        r"\b\d[\d,]*(?:\.\d+)?\s*(?:sq\.?\s*ft|sqft|sft|carpet|bup)\b|"
        r"(?:aed|\bdhs\.?\s*)?\d+(?:[,.]\d+)?\s*(?:m|mn|million|k)\b",
        re.IGNORECASE,
    )
    last_property_index = max(
        (idx for idx, value in enumerate(footer_lines) if property_anchor_re.search(value)),
        default=-1,
    )
    names: set[str] = set()
    for index, line in enumerate(footer_lines):
        match = _SIGNATURE_PHONE_IN_TEXT.search(line)
        if not match:
            continue
        preceding = line[:match.start()].strip().rstrip(":,-").strip()
        candidates = [preceding] if preceding else []
        # A common footer is company, person, RERA label, then phone. Capture
        # both company and person; skip the compliance label itself.
        candidates.extend(footer_lines[max(last_property_index + 1, index - 3):index])
        for candidate in candidates:
            candidate = re.sub(r"[^\w.' &-]", " ", _strip_icons(candidate or ""))
            candidate = re.sub(r"\s+", " ", candidate).strip(" -_:,.*")
            if (
                1 < len(candidate) <= 60
                and not re.search(r"\d", candidate)
                and not re.search(
                    r"(?i)\b(?:bhk|rk|rent|sale|lease|carpet|area|floor|building|"
                    r"apartment|flat|shop|office|plot|terrace|rera|details|visits?|"
                    r"client|profile|required|regd)\b",
                    candidate,
                )
            ):
                names.add(candidate.casefold())
    return names


def _quarantine_broker_signature_building(
    parsed_item: dict,
    ai_item: dict,
    signature_names: set[str],
) -> bool:
    """Clear an exact footer-identity match before typed-row persistence."""
    building_name = str(parsed_item.get("building_name") or "").strip()
    if not building_name or building_name.casefold() not in signature_names:
        return False
    parsed_item["building_name"] = None
    flags = list(parsed_item.get("validation_flags") or [])
    parsed_item["validation_flags"] = list(dict.fromkeys(
        flags + ["building_name_is_broker_signature", "building_name_unresolved"]
    ))
    parsed_item["needs_review"] = True
    ai_item["building_name"] = None
    ai_item["validation_flags"] = parsed_item["validation_flags"]
    ai_item["needs_review"] = True
    return True


def process_raw_message(raw_id: int, ctx: dict, storage=None):
    """Process a single raw message through the full extraction pipeline.

    This is the async workhorse — called from both webhook background threads
    and the extraction worker.  It never touches the webhook request; all
    context is passed explicitly via `ctx`.

    ctx keys:
      sender_name, push_name, sender_jid, sender_phone,
      group, group_name, msg_text, instance, is_dm,
      message_uid, message_id, msg (raw message dict with image/video flags)
    """
    if storage is None:
        storage = get_storage()

    # Ensure tenant context is set for this extraction run
    if ctx.get("tenant_id"):
        storage.tenant_id = ctx["tenant_id"]

    from lab.config import load_excluded_groups

    msg_text = ctx["msg_text"]
    sender_name = ctx["sender_name"]
    push_name = ctx["push_name"]
    sender_jid = ctx["sender_jid"]
    sender_phone = ctx["sender_phone"]
    group = ctx["group"]
    group_name = ctx["group_name"]
    instance = ctx["instance"]
    is_dm = ctx["is_dm"]
    message_uid = ctx["message_uid"]
    message_id = ctx["message_id"]
    msg = ctx.get("msg", {})

    # Re-import app-level helpers (they depend on app.py globals)
    from app import (
        generate_summary_title, compute_embedding,
    )
    # Share eligibility is deterministic and evaluated before persistence. It
    # keeps private tenant output out of shared-market consumers while leaving
    # the raw observation available to the owning tenant.

    # Skip excluded groups
    try:
        excluded = load_excluded_groups()
        if group in excluded:
            storage.mark_raw_processed(raw_id)
            return {
                "raw_id": raw_id,
                "parsed_ids": [],
                "listing_ids": [],
                "requirement_ids": [],
                "storage_status": "skipped",
                "extraction_source": "excluded_group",
            }
    except Exception:
        pass

    # ── Classify conversation for privacy filtering ──────────────────
    conv_type = None
    org_privacy = {"privacy_mode": "private"}
    # classify_conversation removed (dead code) — skip privacy filtering
    org_id = ctx.get("tenant_id") or storage._tenant_id or "00000000-0000-0000-0000-000000000010"
    org = storage.get_organization(org_id)
    if org:
        org_privacy = {
            "privacy_mode": org.get("privacy_mode", "private"),
            "share_listings": org.get("share_listings", False),
            "share_requirements": org.get("share_requirements", False),
            "share_price_trends": org.get("share_price_trends", False),
            "share_market_activity": org.get("share_market_activity", False),
            "share_building_intelligence": org.get("share_building_intelligence", False),
            "share_broker_network": org.get("share_broker_network", False),
            "share_broker_reputation": org.get("share_broker_reputation", False),
            "share_demand_signals": org.get("share_demand_signals", False),
        }

    # ── Parse (content-hash dedup, deterministic bulk boundaries, then AI) ──
    preparsed_input = ctx.get("preparsed_listings")
    parsed_listings: list[dict] = (
        [
            _sanitize_parsed_listing(dict(item))
            for item in preparsed_input
            if isinstance(item, dict)
        ]
        if isinstance(preparsed_input, list)
        else []
    )
    ai_extractions_raw: list[dict | None] = []
    extraction_source: str | None = None
    ai_result: dict | None = None
    message_hash = (ctx.get("message_hash") or "").strip() or _message_hash(msg_text)
    if message_hash:
        try:
            storage.set_raw_message_hash(raw_id, message_hash)
        except Exception:
            pass

    # A broker commonly reposts the identical *root WhatsApp event* into many
    # groups. The root event is retained as raw evidence and can be stopped as
    # an observation before extraction. A repaired/split child is different: it
    # is already an extraction unit derived from a root event that passed this
    # gate. Running the gate again on the child would make the admin evidence
    # show only e.g. "1 BHK..." instead of the complete broker dump, and could
    # incorrectly suppress one property inside a multi-property broadcast.
    if not ctx.get("parent_message_id"):
        try:
            from message_identity import author_content_fingerprint
            repeat_fingerprint = (
                ctx.get("author_content_fingerprint")
                or author_content_fingerprint(
                    sender_phone=sender_phone, sender_jid=sender_jid, message=msg_text
                )
            )
            if repeat_fingerprint:
                setter = getattr(storage, "set_raw_author_content_fingerprint", None)
                if setter:
                    setter(raw_id, repeat_fingerprint)
                finder = getattr(storage, "find_author_content_repeat", None)
                if finder:
                    repeat = finder(
                        repeat_fingerprint,
                        tenant_id=org_id,
                        exclude_raw_id=raw_id,
                        sender_phone=sender_phone,
                        sender_jid=sender_jid,
                        message=msg_text,
                    )
                    if repeat and repeat.get("raw") and repeat.get("parsed"):
                        touched = storage.record_repeat_observation(
                            raw_id,
                            int(repeat["raw"]["id"]),
                            repeat["parsed"],
                            observed_at=ctx.get("timestamp"),
                        )
                        return {
                            "raw_id": raw_id,
                            "parsed_ids": touched,
                            "listing_ids": [],
                            "requirement_ids": [],
                            "storage_status": "stored",
                            "extraction_source": "repeat_observation",
                        }
        except Exception as exc:
            _logger.warning("repeat observation gate skipped for raw_id=%s: %s", raw_id, exc)

    # Semantic similarity is only a cheap candidate lookup.  The message is
    # still sent through extraction; structured fields decide repost identity
    # after the typed row has been written.
    possible_duplicate = None
    try:
        possible_duplicate = storage.find_near_duplicate_candidate(
            msg_text,
            tenant_id=org_id,
            broker_phone=sender_phone,
            raw_timestamp=ctx.get("timestamp"),
        )
    except Exception as exc:
        print(f"  [extract] near-duplicate lookup skipped: {exc}", flush=True)

    if isinstance(preparsed_input, list):
        extraction_source = "reviewed_reparse_preview"
        ai_result = {"extraction_source": extraction_source, "extractions": []}
    elif not parsed_listings:
        # Split first, then let DeepSeek parse each materialized child. This is
        # the source-boundary gate; it is independent of the LLM extraction
        # strategy and therefore cannot be disabled by stale Coolify config.
        detected_split_pattern, detected_split_items = _run_template_splitter(
            storage,
            msg_text,
            tenant_id=org_id,
            sender_phone=sender_phone,
            sender_jid=sender_jid,
        )
        duplicate_source = None
        # Never clone a historical partial parse for a message whose source
        # now proves it is a bulk broadcast. Older pipeline versions may have
        # cached only the shared header as one listing.
        if not ctx.get("parent_message_id") and message_hash and len(detected_split_items) < 2:
            try:
                duplicate_source = storage.get_raw_message_by_hash(
                    message_hash,
                    tenant_id=org_id,
                    processed=True,
                    exclude_raw_id=raw_id,
                    with_parsed=True,
                )
            except Exception:
                duplicate_source = None
            if duplicate_source and duplicate_source.get("raw") and duplicate_source.get("parsed"):
                cloned = _clone_parsed_rows(storage, int(duplicate_source["raw"]["id"]), raw_id)
                # Keep test/plugin callers that still return the old two-item
                # tuple compatible while the clone path now reports demand IDs.
                if len(cloned) == 2:
                    parsed_ids, listing_ids = cloned
                    requirement_ids = []
                else:
                    parsed_ids, listing_ids, requirement_ids = cloned
                if parsed_ids:
                    try:
                        storage.mark_raw_processed(raw_id)
                    except Exception:
                        pass
                    return {
                        "raw_id": raw_id,
                        "parsed_ids": parsed_ids,
                        "listing_ids": listing_ids,
                        "requirement_ids": requirement_ids,
                        "storage_status": "stored",
                        "extraction_source": "hash_duplicate",
                    }

        # A convincing broker-broadcast template is source structure, not a
        # semantic guess. Materialize one child raw message per property so a
        # model can never collapse a 35-property broadcast into one header row.
        # The parent remains immutable evidence and is marked processed only
        # after every child has been queued successfully.
        split_pattern, split_items = detected_split_pattern, detected_split_items
        if split_pattern and len(split_items) > 1 and not ctx.get("parent_message_id"):
            split_ctx = {**ctx, "split_pattern": split_pattern}
            child_ids = _materialize_split_raw_messages(storage, raw_id, split_ctx, split_items)
            if len(child_ids) != len(split_items):
                raise RuntimeError(
                    f"bulk split materialization incomplete: expected {len(split_items)}, got {len(child_ids)}"
                )
            storage.mark_raw_processed(raw_id)
            return {
                "raw_id": raw_id,
                "parsed_ids": [],
                "listing_ids": [],
                "requirement_ids": [],
                "child_raw_ids": child_ids,
                "storage_status": "split_queued",
                "extraction_source": f"deterministic_split:{split_pattern}",
            }

    if not parsed_listings:
        # AI receives one independent source unit: either the original message
        # or one deterministically materialized child block.
        try:
            from ai_extraction import ai_extract
            from extraction_dedup import cache_lookup, cache_store

            _tenant_for_cache = ctx.get("tenant_id") or getattr(storage, "_tenant_id", "") or ""
            ai_result = cache_lookup(storage, _tenant_for_cache, msg_text)
            cache_needs_store = ai_result is None
            if ai_result is not None:
                _logger.info("raw_id=%d extraction cache hit", raw_id)
            else:
                ai_result = ai_extract(msg_text, ctx, storage=storage)
            extraction_source = ai_result.get("extraction_source")
            raw_ai_items = ai_result.get("extractions") or ([ai_result["extraction"]] if ai_result.get("extraction") else [])
            ai_items = [item for item in raw_ai_items if isinstance(item, dict)]
            if len(ai_items) > 1:
                from ai_extraction import _single_property_document
                if _single_property_document(msg_text):
                    def item_score(item: dict) -> int:
                        price = item.get("price") if isinstance(item.get("price"), dict) else {}
                        raw_price = str(price.get("raw_price_text") or "").lower()
                        score = 0
                        score += 10 if re.search(r"\b(?:asking|total)\s+price\b|\basking\s+price\b", raw_price) else 0
                        score -= 10 if re.search(r"\b(?:monthly|rental|income|rent)\b", raw_price) else 0
                        amount = price.get("amount")
                        try:
                            score += min(8, int(float(amount) / 10_000_000)) if amount is not None else 0
                        except (TypeError, ValueError):
                            pass
                        return score
                    chosen = max(enumerate(ai_items), key=lambda pair: (item_score(pair[1]), -pair[0]))[1]
                    chosen = dict(chosen)
                    chosen["needs_review"] = True
                    chosen["validation_flags"] = list(dict.fromkeys(
                        list(chosen.get("validation_flags") or []) + ["single_property_multiple_items_collapsed"]
                    ))
                    ai_items = [chosen]
            if extraction_source == "ai" and ai_items:
                # AI owns semantic fields, while deterministic document
                # segmentation supplies each item's evidence slice. This
                # prevents a model from copying the first building into every
                # later item in a broadcast.
                slice_texts = _llm_source_slices_are_grounded(msg_text, ai_items)
                if slice_texts:
                    _logger.info("raw_id=%s using model-provided source slices", raw_id)
                else:
                    slice_texts = _slice_blocks_for_ai_items(msg_text, ai_items)
                # A multi-item answer is publishable only when every item has
                # exclusive source evidence. If segmentation failed, each item
                # otherwise receives the complete broadcast and fields can be
                # silently cross-wired (BHK/price/building from neighbours).
                # Keep the raw message reviewable, but never manufacture cards
                # from an unbounded multi-listing response.
                distinct_slices = {
                    re.sub(r"\s+", " ", str(slice_text or "").strip()).casefold()
                    for slice_text in slice_texts
                }
                if len(ai_items) > 1 and len(distinct_slices) != len(ai_items):
                    _logger.warning(
                        "raw_id=%d rejected %d AI items without exclusive source slices",
                        raw_id,
                        len(ai_items),
                    )
                    ai_items = []
                    slice_texts = []
                    cache_needs_store = False
                ai_items = _apply_listing_transaction_guard(ai_items, msg_text, slice_texts)
                ai_items = _apply_requirement_source_guard(ai_items, msg_text, slice_texts)
                grounded_pairs = [
                    (item, slice_text)
                    for item, slice_text in zip(ai_items, slice_texts)
                    if _is_actionable_property_slice(slice_text)
                ]
                if len(grounded_pairs) != len(ai_items):
                    _logger.warning(
                        "raw_id=%d dropped %d footer/header-only AI item(s)",
                        raw_id,
                        len(ai_items) - len(grounded_pairs),
                    )
                ai_items = [item for item, _slice in grounded_pairs]
                slice_texts = [slice_text for _item, slice_text in grounded_pairs]
                ai_items = [
                    _source_ground_requirement_item(item, sl)
                    for item, sl in zip(ai_items, slice_texts)
                ]
                parsed_listings = [
                    _ai_extraction_to_parsed(item, msg_text, sender_name, push_name, slice_text=sl)
                    for item, sl in zip(ai_items, slice_texts)
                ]
                signature_names = _extract_broker_signature_names(msg_text)
                # The model may return a plausible field from a neighbouring
                # line in a broadcast block (e.g. "3lacs" or "Fully
                # Furnished" as building_name). Validate each item against its
                # own source slice before any broker/building upsert occurs.
                # A repair is conservative: a clearly named building is kept,
                # a bad token is replaced only when a safe source line exists;
                # otherwise the field stays NULL and is marked for review.
                for idx, (parsed_item, ai_item, slice_text) in enumerate(
                    zip(parsed_listings, ai_items, slice_texts)
                ):
                    before = parsed_item.get("building_name")
                    if not _quarantine_broker_signature_building(
                        parsed_item, ai_item, signature_names
                    ):
                        repair_building_assignment(parsed_item, slice_text, ai_item=ai_item)
                    if parsed_item.get("building_name") != before:
                        # Do not retain a model title whose property token was
                        # proven to belong to price/spec text.
                        ai_item["title"] = None
                ai_extractions_raw = ai_items
                _logger.info("raw_id=%d AI extraction: %d structured item(s) via %s", raw_id, len(ai_items), ai_result.get("provider_used"))
            if cache_needs_store:
                cache_store(
                    storage,
                    _tenant_for_cache,
                    msg_text,
                    ai_result,
                    provider_used=ai_result.get("provider_used"),
                )
        except Exception as exc:
            _logger.warning("raw_id=%d ai_extract error: %s", raw_id, exc)

        # Provider failure is never a "no anchor". When every provider is down
        # (or the AI call itself raised), the message must stay unprocessed so a
        # later cycle retries it. Treating it as a non-listing here would mark a
        # real listing as consumed with a NO_ANCHOR stub and lose it forever.
        if ai_result is None or ai_result.get("extraction_source") == "ai_unavailable":
            raise RuntimeError(
                f"raw_id={raw_id} extraction unavailable — "
                f"{ai_result.get('error') if ai_result else 'no provider response'}"
            )

    if not parsed_listings:
        try:
            get_bus().publish("extraction.skipped", {
                "raw_id": raw_id, "reason": "no_real_estate_anchor", "message": msg_text[:200],
            })
        except Exception:
            pass
        # Save a no-anchor stub so the message still surfaces in the inbox
        # feed (broker cards show message_count, not just listing_count).
        # Without this, [Image]/[Video] placeholders get marked processed
        # silently and brokers that only share images/videos appear empty.
        msg_class = "unstructured"
        try:
            broker_id = storage.resolve_broker(
                broker_phone=sender_phone or "",
                sender_phone=sender_phone or "",
                sender_jid=sender_jid or "",
                broker_name=sender_name or push_name or "",
                profile_name=sender_name or push_name or "",
                sender=sender_name or push_name or "",
            )
            stub = ParsedObservation(
                raw_message_id=raw_id,
                message_type=msg_class,
                intent="NO_ANCHOR",
                broker_name=sender_name or push_name or "",
                broker_phone=sender_phone or "",
                profile_name=sender_name or push_name or "",
                confidence=0.0,
                raw_payload=json.dumps({
                    "note": "no_real_estate_anchor",
                    "message_class": msg_class,
                    "message_preview": msg_text[:200],
                }),
                # Do not present an internal classifier label as a property
                # title. The raw message remains the evidence for review.
                summary_title=None,
                ai_extraction={"reason": "no_real_estate_anchor", "class": msg_class},
                broker_id=broker_id,
                group_name=group_name,
            )
            storage.save_typed_observation(stub)
        except Exception as exc:
            print(f"  [extract] save_parsed stub error for {raw_id}: {exc}", flush=True)
        try:
            storage.mark_raw_processed(raw_id)
        except Exception:
            pass
        return {
            "raw_id": raw_id,
            "parsed_ids": [],
            "listing_ids": [],
            "requirement_ids": [],
            "storage_status": "skipped",
            "extraction_source": "no_anchor",
        }

    # ── Listing validation (price / locality / general) ────────────
    # Runs AFTER AI extraction + _ai_extraction_to_parsed() and BEFORE
    # broker attribution + typed observation persistence. Flags are stored on
    # each parsed dict so they flow through to the typed table's validation_flags.
    try:
        from listing_validation import validate_listing, apply_validation
        for idx, pl in enumerate(parsed_listings):
            vr = validate_listing(pl)
            if vr.flags:
                _logger.info(
                    "raw_id=%d validation flags: %s",
                    raw_id, ", ".join(vr.flags),
                )
            parsed_listings[idx] = apply_validation(pl, vr)
    except Exception as vexc:
        _logger.warning("raw_id=%d validation error: %s", raw_id, vexc)

    # ── Broker attribution ──────────────────────────────────────
    # Only store broker_phone from validated Indian mobile numbers (10-12 digits,
    # starting with 6-9, optional +91/91 prefix).  WhatsApp LIDs (15 digits starting
    # with 1-2) are never valid phone numbers — reject them silently.
    # When sender_phone is empty (e.g. @lid senders), fall back to scanning the
    # message body text for explicitly stated contact numbers — brokers routinely
    # self-publish their number in posts.
    sender_label = _clean_broker_name(sender_name or push_name) or ""
    sender_label_is_name = bool(
        sender_label
        and sender_label.lower() not in {"unknown", "unknown sender", "whatsapp"}
        and not re.fullmatch(r"[+\d\s().:@_-]+", sender_label)
    )
    sender_digits = re.sub(r"\D+", "", sender_phone or "")
    if sender_digits.startswith("91") and len(sender_digits) >= 12:
        sender_digits = sender_digits[-10:]
    sender_phone_from_label = None
    if sender_label_is_name:
        # Bulk posters often append several inspection contacts. Prefer the
        # number explicitly printed beside the WhatsApp sender's name rather
        # than assigning every listing to the first unrelated contact.
        lower_body = (msg_text or "").lower()
        label_pos = lower_body.find(sender_label.lower())
        if label_pos >= 0:
            label_window = (msg_text or "")[label_pos:label_pos + 140]
            sender_phone_from_label, _ = _extract_broker_contact_from_text(label_window)

    # A broker broadcast often ends with a signature containing the contact
    # name and phone. This is source evidence already present in WhatsMeow's
    # captured message; attach it to every split item without another LLM call.
    signature_phone, signature_name = _extract_broker_contact_from_text(msg_text or "")

    for pl in parsed_listings:
        is_valid_mobile = bool(re.fullmatch(r'^(\+?91)?[6-9]\d{9}$', sender_phone or ''))
        if sender_label_is_name:
            pl["broker_name"] = _clean_broker_name(sender_label)
            if sender_phone_from_label:
                pl["broker_phone"] = sender_phone_from_label
            elif len(sender_digits) == 10 and sender_digits[0] in "6789":
                pl["broker_phone"] = sender_digits
        if not pl.get("broker_name") or not pl.get("broker_phone"):
            if not pl.get("broker_phone"):
                if is_valid_mobile:
                    pl["broker_phone"] = sender_phone[-10:]

        # Text-based fallback: if broker_phone is still missing, scan the message
        # body for explicitly stated Indian mobile numbers.
        if not pl.get("broker_phone"):
            raw_text_body = ""
            rp = pl.get("raw_payload")
            if isinstance(rp, dict):
                raw_text_body = rp.get("full_text") or ""
            if not raw_text_body:
                raw_text_body = msg_text if msg_text else ""
            phone_from_text, name_from_text = _extract_broker_contact_from_text(raw_text_body)
            if phone_from_text:
                pl["broker_phone"] = phone_from_text
                if name_from_text and not pl.get("broker_name"):
                    pl["broker_name"] = _clean_broker_name(name_from_text)
                existing_flags = list(pl.get("validation_flags") or [])
                existing_flags.append("broker_phone_text_extracted")
                pl["validation_flags"] = existing_flags
        if signature_phone and not pl.get("broker_phone"):
            pl["broker_phone"] = signature_phone
        if signature_name and not pl.get("broker_name"):
            pl["broker_name"] = _clean_broker_name(signature_name)

        # Final boundary: older extraction branches can populate broker_name
        # independently of the attribution logic above.
        pl["broker_name"] = _clean_broker_name(pl.get("broker_name"))

    if parsed_listings:
        for pl in parsed_listings:
            # _attribution_suffix removed (dead code) — skip suffix appending
            pass
            # suffix = _attribution_suffix(pl.get("broker_name"), pl.get("broker_phone"))
            # if suffix:
            #     rp = pl.get("raw_payload")
            #     if isinstance(rp, dict) and isinstance(rp.get("full_text"), str):
            #         rp["full_text"] = rp["full_text"].rstrip() + suffix

    # Preview mode deliberately stops before save_parsed, listing upserts,
    # graph writes, processed flags, or any deletion. The caller can validate
    # the exact proposed cards and later pass them back as preparsed_listings.
    if ctx.get("preview_only"):
        return {
            "raw_id": raw_id,
            "parsed_listings": parsed_listings,
            "proposed_count": len(parsed_listings),
            "storage_status": "preview",
            "message_class": "ai_structured" if parsed_listings else "unstructured",
            "extraction_source": extraction_source or "ai_unavailable",
        }

    # ── Save parsed observations ────────────────────────────────
    parsed_ids: list[int] = []
    listing_ids: list[int] = []
    requirement_ids: list[int] = []
    for idx, parsed in enumerate(parsed_listings):
        ai_item = ai_extractions_raw[idx] if idx < len(ai_extractions_raw) else None
        share_eligible, share_reason = check_share_eligibility(
            parsed, org_privacy, conv_type or parsed.get("intent") or "unknown"
        )
        if not share_eligible:
            parsed["_can_share_to_market"] = False
            parsed["_share_reason"] = share_reason
        else:
            parsed["_can_share_to_market"] = True
            parsed["_share_reason"] = share_reason

        # Durable embeddings are queued by the typed-table DB trigger and
        # generated asynchronously by semantic_embedding_worker.py.
        embedding_blob = None
        block_text = None
        if isinstance(parsed.get("raw_payload"), dict):
            block_text = parsed["raw_payload"].get("full_text")
        source_text = block_text or msg_text

        # Building identity is an AI extraction field. Deterministic code only
        # validates tenant ownership and persists/discovers the entity.
        resolver_result = {
            "building_id": ai_item.get("building_id") if ai_item else None,
            "building_name": parsed.get("building_name"),
            "resolver_confidence": ai_item.get("building_resolution_confidence", 0.0) if ai_item else 0.0,
            "final_confidence": float(parsed.get("confidence") or 0.0),
            "method": "ai_context" if ai_item and ai_item.get("building_id") else "unresolved",
        }

        # Resolve broker identity for this observation
        try:
            broker_id = storage.resolve_broker(
                broker_phone=parsed.get("broker_phone") or "",
                sender_phone=sender_phone or "",
                sender_jid=sender_jid or "",
                broker_name=parsed.get("broker_name") or "",
                profile_name=sender_name or push_name or "",
                sender=sender_name or push_name or "",
            )
        except Exception as exc:
            print(f"  [extract] resolve_broker error: {exc}", flush=True)
            broker_id = None

        obs = ParsedObservation(
            raw_message_id=raw_id,
            listing_index=idx,
            message_type=parsed.get("message_type"),
            intent=parsed.get("intent"),
            principal=parsed.get("principal"),
            bhk=parsed.get("bhk"),
            configuration=parsed.get("configuration"),
            price=parsed.get("price"),
            price_unit=parsed.get("price_unit"),
            price_model=parsed.get("price_model"),
            price_per_sqft=parsed.get("price_per_sqft"),
            monthly_rent=parsed.get("monthly_rent"),
            total_asking_price=parsed.get("total_asking_price"),
            area_sqft=parsed.get("area_sqft"),
            furnishing=parsed.get("furnishing"),
            furnishing_canonical=parsed.get("furnishing_canonical"),
            location_raw=parsed.get("location_raw"),
            location=json.dumps(parsed.get("location")) if parsed.get("location") else None,
            building_name=parsed.get("building_name"),
            landmark_name=parsed.get("landmark_name"),
            street_name=parsed.get("street_name"),
            area=parsed.get("area"),
            micro_market=parsed.get("micro_market"),
            developer=parsed.get("developer"),
            asset_type=parsed.get("asset_type"),
            property_type=parsed.get("property_type"),
            transaction_type=parsed.get("transaction_type"),
            commercial_use_type=parsed.get("commercial_use_type"),
            fitout_status=parsed.get("fitout_status"),
            occupancy_type=parsed.get("occupancy_type"),
            floor_range=parsed.get("floor_range"),
            rent_per_sqft=parsed.get("rent_per_sqft"),
            availability_status=parsed.get("availability_status"),
            possession_status=parsed.get("possession_status"),
            possession_date=parsed.get("possession_date"),
            available_from=parsed.get("available_from"),
            ready_by=parsed.get("ready_by"),
            construction_stage=parsed.get("construction_stage"),
            launch_timeline=parsed.get("launch_timeline"),
            expected_possession=parsed.get("expected_possession"),
            broker_name=parsed.get("broker_name"),
            broker_phone=parsed.get("broker_phone"),
            profile_name=sender_name or push_name,
            car_parking_count=parsed.get("car_parking_count"),
            parking_type=parsed.get("parking_type"),
            has_lift=parsed.get("has_lift"),
            has_power_backup=parsed.get("has_power_backup"),
            power_load_kw=parsed.get("power_load_kw"),
            forwarded=parsed.get("forwarded", 0),
            confidence=parsed.get("confidence", 0.0),
            extraction_confidence_score=parsed.get("confidence", 0.0),
            raw_payload=json.dumps(parsed.get("raw_payload", {})),
            embedding=embedding_blob,
            summary_title=parsed.get("summary_title") or generate_summary_title(parsed, source_text),
            normalized_message=parsed.get("normalized_message"),
            ai_extraction=ai_item,
            # deal_tags + additional_charges are AI-only signals (regex parser
            # doesn't know about them). When AI extraction fails/times out we
            # fall back to an empty list so the row still saves. We also
            # re-run the whitelist/dict-shape validator here so a junk value
            # from any code path (LLM drift, future schema changes, mocked
            # ai_extract in tests) can't poison the row.
            deal_tags=_safe_deal_tags(
                ai_item.get("deal_tags") if ai_item else parsed.get("deal_tags")
            ),
            additional_charges=_safe_additional_charges(
                ai_item.get("additional_charges") if ai_item else parsed.get("additional_charges")
            ),
            broker_id=broker_id,
            group_name=group_name,
            validation_flags=parsed.get("validation_flags", []),
            needs_review=bool(parsed.get("needs_review")),
        )
        try:
            parsed_id = storage.save_typed_observation(obs)
            parsed_ids.append(parsed_id)
            if parsed.get("message_type") not in {"REQUIREMENT", "BUY"} and str(parsed.get("intent") or "").upper() not in {"BUY", "BUYER", "REQUIREMENT", "RENTAL_SEEKER", "TENANT", "DEMAND"}:
                try:
                    storage.record_listing_repost(
                        parsed_id,
                        possible_duplicate,
                        raw_timestamp=ctx.get("timestamp"),
                        broker_id=broker_id,
                    )
                except Exception as exc:
                    print(f"  [extract] repost classification skipped: {exc}", flush=True)
            else:
                # Demand repeats need the same explicit review workflow as
                # listing reposts. Keep both source records and only flag a
                # conservative structured match; never auto-merge.
                try:
                    from storage.supabase import _typed_route
                    duplicate_reviewer = getattr(storage, "record_requirement_duplicate", None)
                    if duplicate_reviewer:
                        requirement_table, _, _ = _typed_route(parsed)
                        duplicate_reviewer(
                            parsed_id,
                            table=requirement_table,
                            tenant_id=org_id,
                        )
                except Exception as exc:
                    print(f"  [extract] requirement duplicate review skipped: {exc}", flush=True)
        except Exception as exc:
            print(f"  [extract] save_parsed error: {exc}", flush=True)
            continue

        # Persist AI-selected buildings when the ID belongs to this tenant;
        # otherwise discover the named building and attach a new alias.
        if parsed.get("building_name"):
            try:
                discovered_building = None
                ai_building_id = (
                    ai_item.get("building_id")
                    if ai_item and ai_item.get("building_context_allowed")
                    else None
                )
                if ai_building_id:
                    try:
                        candidate = storage.get_building(building_db_id=int(ai_building_id))
                        if candidate and (not org_id or not candidate.get("tenant_id") or candidate.get("tenant_id") == org_id):
                            discovered_building = candidate
                    except Exception:
                        discovered_building = None
                if not discovered_building:
                    discovered_building = storage.ensure_building_from_observation(
                        parsed["building_name"], parsed.get("micro_market"), tenant_id=org_id
                    )
                if discovered_building:
                    if ai_building_id:
                        storage.create_building_alias_for_building(
                            int(discovered_building["id"]), parsed["building_name"],
                            discovered_building.get("canonical_name") or parsed["building_name"],
                            confidence=float(resolver_result.get("resolver_confidence") or 0.0),
                            source="ai",
                        )
                    resolver_result["building_id"] = discovered_building.get("id")
                    resolver_result["building_name"] = discovered_building.get(
                        "canonical_name", parsed["building_name"]
                    )
                    storage.link_typed_observation_to_building(
                        parsed_id, int(discovered_building["id"]), parsed
                    )
            except Exception as exc:
                # Building discovery is additive and must never make a valid
                # typed listing disappear; keep the listing while surfacing
                # the resolver failure in worker logs.
                print(f"  [extract] ensure building error: {exc}", flush=True)

        # New typed observations retain AI building resolution in ai_extraction;
        # the legacy resolver_decisions table is not written from this path.

        # Bridge the fully enriched observation to its correct market
        # destination only after the resolver pass. Supply and demand are
        # separate projections; a requirement must never become a listing.
        try:
            observation = dict(parsed)
            observation["id"] = parsed_id
            if str(observation.get("message_type") or "").upper() in {"REQUIREMENT", "BUY"} or str(observation.get("intent") or "").upper() in {"BUY", "BUYER", "REQUIREMENT", "RENTAL_SEEKER", "TENANT", "DEMAND"}:
                requirement_id = storage.upsert_market_requirement_from_parsed(parsed_id)
                if requirement_id:
                    requirement_ids.append(requirement_id)
            else:
                listing_id = storage.upsert_listing_from_parsed(parsed_id)
                if listing_id:
                    listing_ids.append(listing_id)
        except Exception as lexc:
            print(f"  [extract] market destination upsert error: {lexc}", flush=True)

        # ── Merge building amenities into buildings table ───────────
        # building_amenities are building-shared (gym, pool, etc.) and go
        # to buildings.amenities, not listings.amenities.
        bldg_amenities = parsed.get("building_amenities") or []
        if bldg_amenities and parsed.get("building_name"):
            try:
                storage.merge_building_amenities(
                    parsed["building_name"],
                    bldg_amenities,
                    parsed.get("micro_market"),
                )
            except Exception as bexc:
                print(f"  [extract] merge_building_amenities error: {bexc}", flush=True)

    # ── Publish events ─────────────────────────────────────────────
    try:
        get_bus().publish("extraction.completed", {
            "parsed_ids": parsed_ids, "raw_id": raw_id, "count": len(parsed_ids),
            "intent": parsed_listings[0].get("intent") if parsed_listings else None,
            "broker": parsed_listings[0].get("broker_name") if parsed_listings else None,
        })
    except Exception:
        pass
    if parsed_ids:
        try:
            get_bus().publish("resolution.completed", {
                "parsed_ids": parsed_ids, "raw_id": raw_id,
                "building": resolver_result.get("building_name"),
                "method": resolver_result.get("method", "unresolved"),
                "confidence": resolver_result.get("final_confidence", 0),
            })
        except Exception:
            pass

    # ── Extract implicit observations ─────────────────────────────
    # _process_observations removed (dead code) — skip
    if msg_text and len(msg_text) > 30 and parsed_listings:
        pass
        # try:
        #     _process_observations(
        #         msg_text,
        #         parsed_listings[0].get("broker_name", ""),
        #         parsed_listings[0].get("broker_phone", ""),
        #         parsed_ids,
        #         raw_id,
        #     )
        # except Exception as exc:
        #     print(f"  [extract] _process_observations error: {exc}", flush=True)

    # ── Mark processed ──────────────────────────────────────────────
    if parsed_listings and not parsed_ids:
        print(
            f"  [extract] leaving raw message {raw_id} unprocessed: "
            "all typed parsed-row inserts failed",
            flush=True,
        )
        return {"raw_id": raw_id, "parsed_ids": [], "listing_ids": [], "requirement_ids": [], "storage_status": "failed", "extraction_source": extraction_source or "no_anchor"}
    try:
        storage.mark_raw_processed(raw_id)
    except Exception as exc:
        print(f"  [extract] mark_raw_processed error: {exc}", flush=True)
    return {
        "raw_id": raw_id,
        "parsed_ids": parsed_ids,
        "listing_ids": listing_ids,
        "requirement_ids": requirement_ids,
        "storage_status": "stored",
        "extraction_source": extraction_source or "ai",
    }
