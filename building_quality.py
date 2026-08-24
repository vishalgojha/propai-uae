"""Deterministic building-name normalization and candidate validation."""

from __future__ import annotations

import re


# Dubai real-estate abbreviations which must survive title-casing.
# Dubai real-estate abbreviations which must survive title-casing.
BUILDING_ACRONYMS = frozenset({
    "BR", "DIFC", "DIP", "DSO", "DXB", "DWC", "JAFZA", "JBR", "JLT",
    "JVC", "JVT", "LLC", "MJL", "MOE", "SZR",
})

_DISPLAY_OVERRIDES = {"HANDM": "HandM"}
_JUNK_PHRASES = frozenset({
    "thanks and regards", "thanks regards", "plz call", "pls call",
    "please call", "call", "plzz call", "plz", "pls", "ownership",
    "untouch flat", "old bldg", "old building", "regards", "thank you",
})
_JUNK_RE = re.compile(r"\b(?:pl+z|pl+ease|pls?)\b.*\bcall\b", re.I)
_PHONE_IN_TEXT_RE = re.compile(
    r"(?<!\d)(?:\+?971[-\s]?[2-7]\d{7,8}|0?5\d[-\s]?\d{3}[-\s]?\d{4})(?!\d)"
)
_BROKER_NOTE_RE = re.compile(
    r"\b(?:client\s+(?:business\s+)?profile|allow\s+\d+\s*hrs?|"
    r"set\s+up\s+visits?|for\s+(?:further|more)\s+details)\b",
    re.I,
)
_PROPERTY_DETAIL_RE = re.compile(
    r"^\d+(?:\.\d+)?\s+(?:bathrooms?|washrooms?|toilets?)$",
    re.I,
)
_GENERIC_BUILDING_LABEL_RE = re.compile(
    r"^(?:(?:[a-z][a-z .'/&-]{1,45})\s*[-–—]\s*)?"
    r"(?:premium|confidential|unnamed|unknown|new)\s+"
    r"(?:tower|building|project|society|property)$",
    re.I,
)


def normalize_building_name(value: str | None) -> str:
    """Canonical display casing without changing the observed words."""
    text = " ".join(str(value or "").split()).strip(" .,;:")
    if not text:
        return ""

    def normalize_piece(piece: str) -> str:
        if not piece:
            return piece
        key = re.sub(r"[^A-Za-z0-9]", "", piece).upper()
        if key in _DISPLAY_OVERRIDES:
            return _DISPLAY_OVERRIDES[key]
        if key in BUILDING_ACRONYMS:
            return key
        return piece[:1].upper() + piece[1:].lower()

    # Keep separators such as 81-Aureate while normalizing each side.
    return " ".join(
        "-".join(normalize_piece(part) for part in token.split("-"))
        for token in text.split()
    )


def is_valid_building_candidate(value: str | None) -> bool:
    """Reject obvious broker chatter before it becomes canonical inventory."""
    text = " ".join(str(value or "").split()).strip(" .,;:-")
    if len(text) < 3 or not re.search(r"[A-Za-z]", text):
        return False
    # Contact numbers must never become canonical building entities, even
    # when attached to a broker name or a generic property label.
    if _PHONE_IN_TEXT_RE.search(text):
        return False
    folded = text.casefold()
    if folded in _JUNK_PHRASES or _JUNK_RE.search(text):
        return False
    if (
        _BROKER_NOTE_RE.search(text)
        or _PROPERTY_DETAIL_RE.fullmatch(text)
        or _GENERIC_BUILDING_LABEL_RE.fullmatch(text)
    ):
        return False
    if len(text.split()) == 1 and folded in {"thanks", "regards", "ownership", "call"}:
        return False
    # A candidate made entirely from generic chatter is not a building name.
    if re.fullmatch(r"(?:thanks?|regards?|call|contact|available|ownership|flat|bldg|building)(?:\s+\w+)?", folded):
        return False
    return True
