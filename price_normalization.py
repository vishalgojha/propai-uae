"""Source-grounded price normalization shared by extraction write paths.

Dubai convention: prices are AED. Rents are quoted as ANNUAL totals
(frequently paid via post-dated cheques); sale prices are absolute totals.
Shorthand units are K (thousand) and M (million).
"""

from __future__ import annotations

import re

_UNIT_MULTIPLIERS = {
    "m": 1_000_000,
    "mn": 1_000_000,
    "million": 1_000_000,
    "millions": 1_000_000,
    "k": 1_000,
    "thousand": 1_000,
    "thousands": 1_000,
}
_EXPLICIT_PRICE_RE = re.compile(
    r"(?:aed|dhs|dirhams?)?\s*([\d,]+(?:[.:]\d+)?)\s*[.\-/]*\s*"
    r"(m|mn|millions?|k|thousands?)\b",
    re.IGNORECASE,
)
_RENTAL_LANGUAGE_RE = re.compile(
    r"\b(?:rent|rental|lease|monthly|per\s+month|per\s+year|yearly|annual|"
    r"annum|deposit|tenancy|ejari|"
    r"lock[- ]?in|notice\s+period|lease\s+out|for\s+rent|on\s+rent|"
    r"\d\s*(?:cheqs?|cheques?)\b|chq)\b",
    re.IGNORECASE,
)
_SALE_LANGUAGE_RE = re.compile(
    r"\b(?:sale|sell|resale|purchase|outright|outrate|for\s+sale|"
    r"available\s+sale|sale\s+price|asking)\b",
    re.IGNORECASE,
)


def parse_explicit_price(raw_text: str | None) -> tuple[float, str] | None:
    match = _EXPLICIT_PRICE_RE.search(str(raw_text or ""))
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", "").replace(":", "."))
    except ValueError:
        return None
    unit = match.group(2).lower().rstrip("s")
    return amount, unit


def price_to_aed(value: object, unit: object = None) -> float | None:
    if value in (None, ""):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    unit_key = str(unit or "").strip().lower()
    multiplier = _UNIT_MULTIPLIERS.get(unit_key)
    if multiplier:
        return amount * multiplier
    # Absolute dirham values pass through unchanged.
    return amount


def canonical_price_aed(value: object, unit: object = None, raw_text: str | None = None) -> float | None:
    explicit = parse_explicit_price(raw_text)
    if explicit:
        amount, explicit_unit = explicit
        return price_to_aed(amount, explicit_unit)
    return price_to_aed(value, unit)


def canonical_rental_price_aed(
    value: object,
    unit: object = None,
    raw_text: str | None = None,
) -> float | None:
    """Normalize an annual rent quote to absolute AED.

    Dubai rents are annual totals. ``85K`` means 85,000/year and ``1.5M``
    means 1,500,000/year; bare dirham amounts pass through unchanged. There
    is no lakh-style rescaling ambiguity in the UAE market.
    """
    return canonical_price_aed(value, unit, raw_text)


def canonical_commercial_rental_price_aed(
    value: object,
    unit: object = None,
    raw_text: str | None = None,
) -> float | None:
    """Normalize a commercial rent quote to absolute annual AED.

    Commercial rents are also quoted annually. A PSF quote is a rate per
    square foot per year and is returned as-is when explicitly marked.
    """
    text = str(raw_text or "")
    psf_quote = re.search(
        r"(?:aed|dhs|dirhams?\s*)\s*(\d+(?:\.\d+)?)\s*(?:p\.?\s*s\.?\s*f\.?|per\s*(?:sq\.?\s*ft|square\s*foot))\b",
        text,
        re.IGNORECASE,
    )
    if psf_quote:
        try:
            return float(psf_quote.group(1).replace(",", ""))
        except ValueError:
            pass
    return canonical_price_aed(value, unit, raw_text)


# Backward-compatible aliases (legacy INR naming kept for existing callers).
price_to_rupees = price_to_aed
canonical_price_rupees = canonical_price_aed
canonical_rental_price_rupees = canonical_rental_price_aed
canonical_commercial_rental_price_rupees = canonical_commercial_rental_price_aed


def source_transaction_type(raw_text: str | None, proposed: str | None) -> str:
    """Source-ground a provider transaction label without whole-message guessing."""
    text = str(raw_text or "")
    explicit = parse_explicit_price(text)
    has_sale_marker = bool(_SALE_LANGUAGE_RE.search(text))
    has_rent_marker = bool(_RENTAL_LANGUAGE_RE.search(text))
    # “For sale ... currently on lease” describes a sale of an occupied/
    # pre-leased asset. The lease is the tenant's current occupancy, not the
    # asking-rent mode. This must win over the generic lease marker.
    if has_sale_marker and re.search(
        r"\b(?:currently\s+on\s+lease|pre[- ]?(?:leased|rented)|already\s+leased)\b",
        text,
        re.IGNORECASE,
    ):
        return "sale"
    if has_sale_marker and not has_rent_marker:
        return "sale"
    if has_rent_marker and not has_sale_marker:
        return "rent"
    if explicit and not has_rent_marker:
        return "sale"
    return proposed if proposed in {"sale", "rent"} else "sale"


def rent_price_needs_review(monthly_rent: object, raw_text: str | None) -> bool:
    """Annual rent above AED 5M/yr is implausible for standard stock."""
    amount = price_to_aed(monthly_rent)
    return amount is not None and amount > 5_000_000
