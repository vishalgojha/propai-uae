"""Source-grounded guards for fields that are commonly cross-wired by AI.

This module deliberately contains no database or provider code.  It is used at
both the AI normalisation boundary and the typed-row boundary so WhatsApp,
self-chat, WABA and MCP intake all get the same protection.
"""

from __future__ import annotations

import re


_PRICE_ONLY_RE = re.compile(
    r"^\s*(?:aed|dhs|dirhams?)?\s*\d+(?:[,.]\d+)?\s*"
    r"(?:k|m|thousand|million|mn)\b"
    r"(?:\s*(?:/\s*(?:month|mo|year|yr)|per\s*(?:month|mo|year|yr)|"
    r"yearly|annual|annum))?\s*"
    r"(?:negotiable)?\s*$",
    re.IGNORECASE,
)
_CONFIG_ONLY_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*(?:bhk|br|rk|bedroom)\s*$", re.IGNORECASE
)
_NUMBER_ONLY_RE = re.compile(r"^\s*[\d,.]+\s*(?:sq\.?\s*ft|sqft|sft)?\s*$", re.IGNORECASE)
_PHONE_ONLY_RE = re.compile(r"^\s*(?:\+?971[-\s]?)?[2-7]\d{8}\s*$")
_PHONE_IN_TEXT_RE = re.compile(
    r"(?<!\d)(?:\+?971[-\s]?[2-7]\d{7,8}|0?5\d[-\s]?\d{3}[-\s]?\d{4}|[2-7]\d{8})(?!\d)"
)
_LOCALITY_ONLY_RE = re.compile(
    r"^\s*(?:near\s+)?(?:marina|dubai\s+marina|jbr|jvc|jvt|jlt|"
    r"downtown(?:\s+dubai)?|business\s+bay|bbay|difc|palm(?:\s+jumeirah)?|"
    r"al\s+barsha|barsha|deira|bur\s+dubai|(?:al\s+)?karama|mirdif|"
    r"(?:al\s+)?furjan|dubai\s+hills(?:\s+estate)?|springs|meadows|lakes|"
    r"greens|views|sports\s+city|motor\s+city|arabian\s+ranches|ranches|"
    r"town\s+square|damac\s+hills|emirates\s+hills|jumeirah|sufouh|suqeim|"
    r"silicon\s+oasis|dso|festival\s+city|jaddaf|oud\s+metha|qusais|nahda|"
    r"warqa|khawaneej|mizhar|rashidiya|garhoud|international\s+city|warsan|"
    r"discovery\s+gardens|jebel\s+ali|jafza|impz|production\s+city|remraam|"
    r"mudon|arjan|dubailand|meydan|nad\s+al\s+sheba|al\s+barari|bluewaters|"
    r"city\s+walk|zabeel|za'abeel|szr|sheikh\s+zayed\s+road|al\s+wasl)"
    r"(?:\s+(?:1|2|3|north|south|east|west))?\s*$",
    re.IGNORECASE,
)

_LOCALITY_ALIASES = {
    "businessbay": "Business Bay",
    "buisness bay": "Business Bay",
    "jumeriah": "Jumeirah",
}


def canonical_locality_alias(value: object) -> str:
    """Return a conservative display locality for known source typos.

    This is used for search/enrichment context only; the original WhatsApp
    spelling remains in the stored evidence.
    """
    text = re.sub(r"\s+", " ", clean_source_line(value))
    return _LOCALITY_ALIASES.get(text.casefold(), text)

_NON_BUILDING_RE = re.compile(
    r"\b(?:fully\s+furnished|semi[-\s]?furnished|unfurnished|bare\s+shell|"
    r"\d+(?:\.\d+)?\s*(?:bhk|rk)|purchase|"
    r"higher\s+floor|middle\s+floor|lower\s+floor|ground\s+floor|"
    r"\d+(?:st|nd|rd|th)?\s+floor|car\s+parks?|parking|rent|sale|lease|"
    r"\d+(?:\.\d+)?\s+(?:bathrooms?|washrooms?|toilets?)|"
    r"price|budget|negotiable|available|on\s+request|direct\s+inventor(?:y|ies)|"
    r"for\s+more\s+details|contact|call|inspection|photos?|options?|"
    r"ownership|thanks?|regards?|pl(?:z|ease)|urgent|requirement|"
    r"client\s+(?:business\s+)?profile|allow\s+\d+\s*hrs?|set\s+up\s+visits?)\b",
    re.IGNORECASE,
)
_GENERIC_BUILDING_LABEL_RE = re.compile(
    r"^(?:(?:[a-z][a-z .'/&-]{1,45})\s*[-–—]\s*)?"
    r"(?:premium|confidential|unnamed|unknown|new)\s+"
    r"(?:tower|building|project|society|property)$",
    re.IGNORECASE,
)


def clean_source_line(value: object) -> str:
    """Remove WhatsApp decoration without changing the evidence text."""
    return re.sub(r"[*_`~]", "", str(value or "")).strip(" \t-–—:•")


def building_name_problem(value: object, *, locality: str | None = None) -> str | None:
    """Return a stable validation code when ``value`` is not a building name."""
    text = clean_source_line(value)
    if not text:
        return None
    compact = re.sub(r"\s+", " ", text).strip()
    lowered = compact.casefold()
    if _PRICE_ONLY_RE.fullmatch(compact):
        return "building_name_is_price"
    if _CONFIG_ONLY_RE.fullmatch(compact):
        return "building_name_is_configuration"
    if _NUMBER_ONLY_RE.fullmatch(compact):
        return "building_name_is_number"
    if _PHONE_ONLY_RE.fullmatch(compact):
        return "building_name_is_phone"
    embedded_phone = _PHONE_IN_TEXT_RE.search(compact)
    if embedded_phone:
        # Remove the contact before re-validating the remainder so punctuation
        # and whitespace do not hide the fact that this is broker/contact text.
        remainder = clean_source_line(_PHONE_IN_TEXT_RE.sub(" ", compact))
        if len(remainder) < 3:
            return "building_name_contains_phone"
        if remainder != compact and building_name_problem(remainder, locality=locality):
            return "building_name_contains_phone"
        # A text-like remainder ("Sailee", "Office", etc.) is still not
        # evidence of a physical building. Quarantine the original value and
        # let source-slice repair look for a real building name.
        return "building_name_contains_phone"
    if locality and lowered == clean_source_line(locality).casefold():
        return "building_name_is_locality"
    if _LOCALITY_ONLY_RE.fullmatch(compact):
        return "building_name_is_locality"
    if _NON_BUILDING_RE.search(compact):
        return "building_name_is_listing_text"
    if _GENERIC_BUILDING_LABEL_RE.fullmatch(compact):
        return "building_name_is_generic_descriptor"
    if len(compact) < 3 or len(compact) > 100:
        return "building_name_bad_length"
    return None


def _candidate_lines(source_text: str) -> list[str]:
    return [clean_source_line(line) for line in str(source_text or "").splitlines()]


def _meaningful_tokens(value: object) -> set[str]:
    stop = {
        "the", "and", "at", "in", "for", "near", "road", "west", "east",
        "tower", "building", "heights", "residency", "residential",
    }
    return {
        token for token in re.findall(r"[a-z0-9]+", str(value or "").casefold())
        if len(token) >= 3 and token not in stop
    }


def infer_building_from_slice(
    source_text: str,
    *,
    locality: str | None = None,
    bhk: object = None,
) -> str | None:
    """Find a conservative building line in the current listing slice.

    This is intentionally allowed to return ``None``.  It must never promote a
    price, floor, furnishing, locality, broker footer, or configuration into a
    building merely to fill a blank field.
    """
    lines = _candidate_lines(source_text)
    bhk_re = re.compile(r"\b\d+(?:\.\d+)?\s*(?:bhk|br|rk|bedroom)\b", re.IGNORECASE)
    start = 0
    for index, line in enumerate(lines):
        if bhk_re.search(line) or (bhk is not None and str(bhk).strip() and str(bhk).casefold() in line.casefold()):
            start = index + 1
            break
    locality_value = clean_source_line(locality).casefold() if locality else None
    for line in lines[start:start + 6]:
        if not line or len(line) > 90:
            continue
        if locality_value and line.casefold() == locality_value:
            continue
        if building_name_problem(line, locality=locality):
            continue
        if re.search(r"\b(?:sq\.?\s*ft|sqft|carpet|area|rent|sale|lease|deposit|floor|"
                     r"parking|possession|inspection|details|contact|call)\b", line, re.IGNORECASE):
            continue
        if not re.search(r"[A-Za-z]", line):
            continue
        return line.strip(" .,;:")
    return None


def repair_building_assignment(
    item: dict,
    source_text: str,
    *,
    ai_item: dict | None = None,
) -> dict:
    """Repair or quarantine a building value against its own source slice."""
    locality = item.get("micro_market") or item.get("location_raw")
    current = (
        item.get("building_name")
        or item.get("building_name_raw_candidate")
        or (ai_item or {}).get("building_name_raw_candidate")
    )
    problem = building_name_problem(current, locality=locality)
    if not problem and current:
        # A valid-looking name copied from a sibling block is still unsafe.
        # Require at least one meaningful token in this item's source slice;
        # aliases can then be resolved downstream without allowing a totally
        # unrelated building to leak across blocks.
        if _meaningful_tokens(current).isdisjoint(_meaningful_tokens(source_text)):
            problem = "building_name_not_in_source_slice"
    if not problem:
        return item

    replacement = infer_building_from_slice(
        source_text,
        locality=locality,
        bhk=item.get("bhk"),
    )
    item["building_name"] = replacement
    flags = list(item.get("validation_flags") or [])
    flags.append(problem)
    flags.append("building_name_source_repaired" if replacement else "building_name_unresolved")
    item["validation_flags"] = list(dict.fromkeys(flags))
    item["needs_review"] = True

    if isinstance(ai_item, dict):
        ai_item["building_name"] = replacement
        ai_flags = list(ai_item.get("validation_flags") or [])
        ai_item["validation_flags"] = list(dict.fromkeys(ai_flags + flags))
        ai_item["needs_review"] = True
    return item
