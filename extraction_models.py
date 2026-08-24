"""Typed and source-aware validation for the high-risk extraction fields.

Pydantic validates the shape and enum values; the semantic pass below validates
relationships that a JSON schema cannot know, especially price units versus
the broker's wording.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ExtractedPrice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    amount: float | None = Field(default=None, ge=0)
    unit: Literal["total", "per_sqft"] | None = None
    period: Literal["one_time", "per_month"] | None = None
    raw_price_text: str | None = None


class ExtractionDiscriminators(BaseModel):
    model_config = ConfigDict(extra="ignore")

    listing_type: Literal["sale", "rent", "requirement"]
    property_category: Literal["residential", "commercial"] | None = None
    extraction_confidence: Literal["high", "medium", "low"] = "medium"


_RENT_SIGNAL = re.compile(
    r"\b(?:rent|rental|lease|monthly|per\s+month|deposit|tenancy|"
    r"lock[- ]?in|notice\s+period|lease\s+out)\b",
    re.I,
)
_SALE_SIGNAL = re.compile(
    r"\b(?:sale|sell|buy|purchase|outright|outrate|asking\s+price|"
    r"for\s+sale)\b",
    re.I,
)
_PSF_SIGNAL = re.compile(r"\b(?:psf|per\s+sq\.?\s*ft|per\s+square\s+foot)\b", re.I)


def validate_source_semantics(extraction: dict, source_text: str) -> dict:
    """Apply typed validation and source-grounded corrections in place.

    A provider's ``per_sqft`` value is accepted only when the source contains
    an explicit PSF marker. This preserves legitimate rate × area calculations
    while preventing ordinary K/M rents from becoming PSF rates.
    """
    try:
        discriminators = ExtractionDiscriminators.model_validate({
            "listing_type": extraction.get("listing_type"),
            "property_category": extraction.get("property_category"),
            "extraction_confidence": extraction.get("extraction_confidence") or "medium",
        })
    except ValidationError:
        # The existing normalizer remains responsible for deciding whether an
        # incomplete candidate should be dropped. Do not manufacture a type.
        return extraction

    price = extraction.get("price")
    try:
        typed_price = ExtractedPrice.model_validate(price or {})
    except ValidationError:
        extraction["price"] = {
            "amount": None,
            "unit": None,
            "period": None,
            "raw_price_text": None,
        }
        extraction["needs_review"] = True
        return extraction

    source = str(source_text or "")
    raw_quote = typed_price.raw_price_text or ""
    psf_explicit = bool(_PSF_SIGNAL.search(raw_quote) or _PSF_SIGNAL.search(source))
    rent_explicit = bool(_RENT_SIGNAL.search(source))
    sale_explicit = bool(_SALE_SIGNAL.search(source))

    # Source transaction evidence outranks a provider's discriminator when the
    # message has one unambiguous side.
    if rent_explicit and not sale_explicit and discriminators.listing_type in {"rent", "sale"}:
        extraction["listing_type"] = "rent"
    elif sale_explicit and not rent_explicit and discriminators.listing_type in {"rent", "sale"}:
        extraction["listing_type"] = "sale"

    listing_type = extraction.get("listing_type")
    if typed_price.unit == "per_sqft" and not psf_explicit:
        # The model supplied a valid enum but the source disproves its meaning.
        typed_price.unit = "total"
        typed_price.period = "per_month" if listing_type == "rent" or rent_explicit else "one_time"
        extraction["needs_review"] = True
    elif listing_type == "rent" and typed_price.unit == "per_sqft":
        typed_price.period = "per_month"
    elif listing_type == "rent" and typed_price.unit == "total":
        typed_price.period = "per_month"

    extraction["price"] = typed_price.model_dump()
    return extraction
