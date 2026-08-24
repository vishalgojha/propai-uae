"""Listing validation layer — price plausibility, locality consistency, and
general field sanity checks that run BEFORE a parsed listing is persisted.

Design principles:
- Soft-flag by default: add validation_flags to the parsed dict rather than
  rejecting listings outright.  Only hard-reject on clearly impossible data
  (negative price, insane BHK count).
- Pure logic — no storage calls in the core validators.  The locality
  cross-check is a separate function because it needs DB access.
- Log every flag so the extraction worker log is the audit trail.

Integration points:
  1. extraction.py  → validate_listing(parsed_dict) called after
     the typed extraction adapter and before typed persistence.
  2. storage/supabase.py → validate_listing_locality(parsed_dict, storage)
     called inside upsert_listing_from_parsed() for a second pass that has
     DB context for building-locality cross-checks.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── Price plausibility ranges ────────────────────────────────────────
# Keyed by (property_category, transaction_type).
# Each value is (min_abs, max_abs) in AED.  These are generous Dubai-market
# bounds — intentionally wide to catch only clearly wrong extractions.
# Rents are ANNUAL totals (Dubai convention: yearly rent quoted up front,
# paid via post-dated cheques).  "abs" means absolute dirhams (not K/M).

_PRICE_RANGES: dict[tuple[str, str], tuple[float, float]] = {
    # ── Residential ──
    # Rent (annual)
    ("APARTMENT", "rent"):   (12_000, 3_500_000),      # AED 12K – 3.5M/yr
    ("VILLA", "rent"):       (30_000, 5_000_000),      # AED 30K – 5M/yr
    ("PENTHOUSE", "rent"):   (60_000, 8_000_000),      # AED 60K – 8M/yr
    ("STUDIO", "rent"):      (15_000, 250_000),         # AED 15K – 250K/yr
    ("ROW_HOUSE", "rent"):   (30_000, 2_500_000),      # townhouse
    ("PLOT", "rent"):        (10_000, 1_000_000),
    ("FARMHOUSE", "rent"):   (30_000, 2_000_000),
    ("DUPLEX", "rent"):      (40_000, 3_000_000),
    ("SOHO", "rent"):        (25_000, 1_500_000),

    # Sale (total)
    ("APARTMENT", "sale"):   (250_000, 200_000_000),   # AED 250K – 200M
    ("VILLA", "sale"):       (800_000, 300_000_000),   # AED 800K – 300M
    ("PENTHOUSE", "sale"):   (1_500_000, 250_000_000),
    ("STUDIO", "sale"):      (150_000, 20_000_000),
    ("ROW_HOUSE", "sale"):   (600_000, 50_000_000),
    ("PLOT", "sale"):        (300_000, 100_000_000),
    ("FARMHOUSE", "sale"):   (500_000, 80_000_000),
    ("DUPLEX", "sale"):      (700_000, 60_000_000),
    ("SOHO", "sale"):        (400_000, 30_000_000),

    # ── Commercial ──
    ("OFFICE_SPACE", "rent"):  (20_000, 12_000_000),   # annual
    ("OFFICE_SPACE", "sale"):  (300_000, 150_000_000),
    ("SHOP", "rent"):          (30_000, 8_000_000),
    ("SHOP", "sale"):          (250_000, 60_000_000),
    ("SHOWROOM", "rent"):      (50_000, 15_000_000),
    ("SHOWROOM", "sale"):      (500_000, 150_000_000),
    ("WAREHOUSE", "rent"):     (30_000, 3_000_000),
    ("WAREHOUSE", "sale"):     (400_000, 80_000_000),
    ("CO_WORKING", "rent"):    (12_000, 2_000_000),
    ("CO_WORKING", "sale"):    (500_000, 50_000_000),
    ("INDUSTRIAL", "rent"):    (30_000, 4_000_000),
    ("INDUSTRIAL", "sale"):    (300_000, 60_000_000),
}

_COMMERCIAL_CATEGORY_ALIASES = {
    "OFFICE": "OFFICE_SPACE",
    "OFFICE SPACE": "OFFICE_SPACE",
    "SHOP": "SHOP",
    "SHOWROOM": "SHOWROOM",
    "WAREHOUSE": "WAREHOUSE",
    "CO-WORKING": "CO_WORKING",
    "CO WORKING": "CO_WORKING",
    "COWORKING": "CO_WORKING",
    "INDUSTRIAL": "INDUSTRIAL",
}

# Catch-all for unknown property types — very wide to avoid false rejections.
_DEFAULT_RANGE = (5_000, 500_000_000)  # AED 5K – AED 500M

# ── Valid enums ──────────────────────────────────────────────────────
_VALID_INTENTS = {"SELL", "RENT", "BUY", "REQUIREMENT", "NO_ANCHOR", "DEMAND"}
_VALID_PRICE_UNITS = {"abs", "M", "K", None}
_VALID_FURNISHINGS = {
    None, "", "unfurnished", "semi_furnished", "fully_furnished",
    "bare_shell", "builder_finish", "not_specified", "none",
}
_VALID_POSSESSION = {
    None, "", "ready_to_move", "under_construction", "ready_possession",
    "oc_received", "preleased", "not_specified",
}
_VALID_PROPERTY_CATEGORIES = {
    None, "", "RESIDENTIAL", "COMMERCIAL",
    "APARTMENT", "VILLA", "PENTHOUSE", "STUDIO",
    "ROW_HOUSE", "PLOT", "FARMHOUSE", "DUPLEX", "SOHO",
    "OFFICE_SPACE", "SHOP", "SHOWROOM", "WAREHOUSE", "CO_WORKING",
    "INDUSTRIAL",
}

# ── BHK sanity ──────────────────────────────────────────────────────
_BHK_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:BHK|BR|RK)$")


# ─────────────────────────────────────────────────────────────────────
# Core validators
# ─────────────────────────────────────────────────────────────────────

class ValidationResult:
    """Accumulates flags and hard-reject decisions."""

    __slots__ = ("flags", "reject", "price_override")

    def __init__(self) -> None:
        self.flags: list[str] = []
        self.reject: bool = False
        self.price_override: float | None = None  # set to None to null the price

    def flag(self, code: str) -> None:
        self.flags.append(code)

    def hard_reject(self, code: str) -> None:
        self.flags.append(code)
        self.reject = True

    def set_price_override(self, price: float | None) -> None:
        self.price_override = price

    @property
    def ok(self) -> bool:
        return not self.reject


def _abs_price(price: float | None, price_unit: str | None) -> float | None:
    """Normalise price to absolute AED for range checking."""
    if price is None:
        return None
    unit = (price_unit or "").strip().lower()
    if unit == "m":
        return price * 1_000_000
    if unit == "k":
        return price * 1_000
    return price  # abs or unknown unit


def _parse_bhk_number(bhk: str | None) -> float | None:
    if not bhk:
        return None
    m = _BHK_PATTERN.match(bhk.strip())
    if m:
        return float(m.group(1))
    return None


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────

def validate_listing(parsed: dict[str, Any]) -> ValidationResult:
    """Validate a parsed listing dict. Returns flags + optional price override.

    Called in extraction.py AFTER _ai_extraction_to_parsed() and BEFORE
    typed-table persistence.
    """
    result = ValidationResult()

    # ── Price plausibility ───────────────────────────────────────────
    price = parsed.get("price")
    price_unit = parsed.get("price_unit")
    abs_price = _abs_price(price, price_unit)

    if abs_price is not None and abs_price <= 0:
        result.flag("price_negative_or_zero")
        result.set_price_override(None)
        abs_price = None

    if abs_price is not None:
        property_cat = (parsed.get("asset_type") or "").upper().replace("-", " ")
        if property_cat == "COMMERCIAL":
            use_type = str(parsed.get("commercial_use_type") or "").upper().replace("_", " ")
            property_cat = _COMMERCIAL_CATEGORY_ALIASES.get(use_type, "COMMERCIAL")
        intent = (parsed.get("intent") or "").upper()
        txn = "rent" if intent == "RENT" else "sale"

        range_key = (property_cat, txn)
        # Generic commercial inventory still needs a real estate-scale price.
        # Falling back to the generic AED 5K lower bound allowed values such as
        # AED 1,500 to appear as active commercial sale listings.
        lo, hi = _PRICE_RANGES.get(
            range_key,
            (250_000, 500_000_000) if property_cat == "COMMERCIAL" and txn == "sale" else _DEFAULT_RANGE,
        )

        if abs_price < lo:
            result.flag(f"price_below_range_{property_cat}_{txn}")
            # Price is implausibly low — null it so it shows as "Price on request"
            result.set_price_override(None)
        elif abs_price > hi:
            result.flag(f"price_above_range_{property_cat}_{txn}")
            # Could be a genuinely expensive property.  Flag but keep.

        raw_price = str(parsed.get("price_raw_text") or "").lower()
        if txn == "sale" and re.search(r"\b(?:monthly|rental)\s+income\b|\bmonthly\s+rent\b", raw_price):
            result.flag("price_is_rental_income")
            result.set_price_override(None)

        area = parsed.get("area_sqft") or parsed.get("carpet_area_sqft") or parsed.get("built_up_area_sqft")
        try:
            area_value = float(area) if area is not None else None
            rate = abs_price / area_value if area_value and area_value > 0 and txn == "sale" else None
            # A total sale price below AED 250/sq ft for a large land/plot-like
            # record is an extraction outlier, not a publishable market fact.
            if rate is not None and area_value >= 1_000 and rate < 250:
                result.flag("price_per_sqft_implausibly_low")
                result.set_price_override(None)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # ── Price unit validation ────────────────────────────────────────
    if price_unit not in _VALID_PRICE_UNITS:
        result.flag(f"invalid_price_unit:{price_unit}")

    # ── BHK sanity ──────────────────────────────────────────────────
    bhk = parsed.get("bhk")
    bhk_num = _parse_bhk_number(bhk)
    if bhk_num is not None:
        if bhk_num > 10:
            result.flag(f"bhk_too_high:{bhk}")
        if bhk_num == 0 and bhk and "RK" not in bhk.upper():
            result.flag("bhk_zero_not_rk")
    elif bhk and bhk.strip():
        # Non-standard BHK string — check it's at least plausible
        if not re.match(r"^\d+(?:\.\d+)?\s*(?:BHK|BR|RK|bhk|br|rk|Bedroom|bedroom)", bhk.strip()):
            result.flag(f"bhk_format_unusual:{bhk}")

    # ── Area sanity ──────────────────────────────────────────────────
    area = parsed.get("area_sqft") or parsed.get("carpet_area_sqft") or parsed.get("built_up_area_sqft")
    if area is not None:
        try:
            area_val = float(area)
            if area_val <= 0:
                result.flag("area_non_positive")
            elif area_val < 100:
                result.flag(f"area_out_of_range:{area_val}")
            elif area_val > 100_000:
                result.flag(f"area_out_of_range:{area_val}")
        except (TypeError, ValueError):
            result.flag("area_not_numeric")

    # ── Intent validation ────────────────────────────────────────────
    intent = (parsed.get("intent") or "").upper()
    if intent and intent not in _VALID_INTENTS:
        result.flag(f"invalid_intent:{parsed.get('intent')}")

    # ── Furnishing validation ────────────────────────────────────────
    furnishing = parsed.get("furnishing")
    if furnishing and furnishing.strip().lower() not in _VALID_FURNISHINGS:
        result.flag(f"unrecognised_furnishing:{furnishing}")

    possession = parsed.get("possession_status")
    if possession and possession.strip().lower() not in _VALID_POSSESSION:
        result.flag(f"unrecognised_possession:{possession}")

    # Required semantic anchors. Requirements may use a budget rather than a
    # listing price, but every persisted opportunity needs a transaction mode
    # and either a building or locality anchor.
    if intent not in {"NO_ANCHOR", ""}:
        if not (parsed.get("building_name") or parsed.get("micro_market") or parsed.get("location_raw")):
            result.flag("missing_building_or_locality")
        if intent in {"SELL", "RENT", "LEASE"} and parsed.get("price") is None:
            result.flag("missing_price")

    # Cross-validate explicit total = rate * area. Keep the source values but
    # quarantine the row for review when the relationship is materially off.
    total = parsed.get("total_asking_price") or parsed.get("monthly_rent")
    rate = parsed.get("price_per_sqft") or parsed.get("rent_per_sqft")
    area_for_math = parsed.get("area_sqft") or parsed.get("carpet_area_sqft")
    if total is not None and rate is not None and area_for_math:
        try:
            expected = float(rate) * float(area_for_math)
            if expected > 0 and abs(float(total) - expected) / expected > 0.15:
                result.flag("price_area_cross_validation_mismatch")
        except (TypeError, ValueError, ZeroDivisionError):
            result.flag("price_area_cross_validation_invalid")

    # ── Property category validation ─────────────────────────────────
    asset_type = parsed.get("asset_type")
    if asset_type and asset_type.upper() not in _VALID_PROPERTY_CATEGORIES:
        result.flag(f"unrecognised_asset_type:{asset_type}")

    # ── Deposit plausibility (rental) ────────────────────────────────
    deposit = parsed.get("deposit_amount")
    if deposit is not None and abs_price is not None:
        try:
            deposit_val = float(deposit)
            if deposit_val > 0:
                # UAE security deposits are typically ~5% of annual rent;
                # more than half a year's rent is suspicious.
                if intent == "RENT" and deposit_val > abs_price * 0.5:
                    result.flag(f"deposit_exceeds_half_annual_rent:{deposit_val}")
        except (TypeError, ValueError):
            pass

    # ── Cross-field consistency ──────────────────────────────────────
    # "RENT" intent + sale-like price is suspicious (> AED 5M/yr)
    if intent == "RENT" and abs_price is not None and abs_price > 5_000_000:
        result.flag("rent_price_over_5m_annual")
        result.set_price_override(None)
    # "SELL" intent + very low price (< AED 100K) is suspicious
    if intent == "SELL" and abs_price is not None and abs_price < 100_000:
        result.flag("sale_price_under_100k")
        result.set_price_override(None)

    return result


def validate_listing_locality(
    parsed: dict[str, Any],
    storage: Any,
) -> list[str]:
    """Second-pass locality validation with DB context.

    Called from upsert_listing_from_parsed() in storage/supabase.py.
    Returns flags only (no hard rejects — locality is already set).

    Checks:
    1. If micro_market is set, verify it exists in locality_reference.
    2. If building_name is set, check if other listings for the same building
       have a different micro_market — flag inconsistency.
    """
    flags: list[str] = []
    micro_market = (parsed.get("micro_market") or "").strip()
    building_name = (parsed.get("building_name") or "").strip()

    if not micro_market:
        return flags

    # Check 1: Does this micro_market exist in locality_reference?
    try:
        db = storage.client if hasattr(storage, "client") else None
        if db:
            res = (
                db.table("locality_reference")
                .select("sub_locality")
                .eq("parent_locality", micro_market)
                .limit(1)
                .execute()
            )
            if not res.data:
                # Also check if it's itself a sub_locality
                res2 = (
                    db.table("locality_reference")
                    .select("sub_locality")
                    .eq("sub_locality", micro_market)
                    .limit(1)
                    .execute()
                )
                if not res2.data:
                    flags.append(f"micro_market_not_in_reference:{micro_market}")
    except Exception as exc:
        logger.debug("locality_reference check failed: %s", exc)

    # Check 2: Building-locality consistency
    if building_name:
        try:
            db = storage.client if hasattr(storage, "client") else None
            if db:
                res = (
                    db.table("listings_unified")
                    .select("micro_market")
                    .ilike("building_name", building_name)
                    .neq("micro_market", "")
                    .not_.is_("micro_market", "null")
                    .limit(20)
                    .execute()
                )
                if res.data:
                    known_markets = {
                        (r.get("micro_market") or "").strip()
                        for r in res.data
                        if (r.get("micro_market") or "").strip()
                    }
                    known_markets.discard(micro_market)
                    if known_markets:
                        flags.append(
                            f"building_locality_mismatch:"
                            f"{micro_market}_vs_{','.join(sorted(known_markets))}"
                        )
        except Exception as exc:
            logger.debug("building-locality consistency check failed: %s", exc)

    return flags


def apply_validation(parsed: dict[str, Any], result: ValidationResult) -> dict[str, Any]:
    """Apply validation results to the parsed dict.

    - Adds `validation_flags` list (may be empty).
    - Sets `needs_review` if any validation flags exist.
    - Sets `price_validated` and `locality_validated` booleans.
    - If price_override is set, replaces price and adds a flag.
    - Does NOT modify the dict in place — returns a shallow copy.
    """
    out = dict(parsed)
    existing_flags = list(out.get("validation_flags") or [])
    if out.get("needs_review") and "ai_needs_review" not in existing_flags:
        existing_flags.append("ai_needs_review")
    out["validation_flags"] = existing_flags + result.flags

    # Determine if listing needs review
    needs_review = len(out["validation_flags"]) > 0
    out["needs_review"] = needs_review
    out["price_validated"] = not any(f.startswith("price_") for f in result.flags)
    out["locality_validated"] = not any(f.startswith("micro_market") or f.startswith("building_locality") for f in result.flags)

    if result.price_override is not None:
        out["_original_price"] = out.get("price")
        out["price"] = result.price_override
        out["validation_flags"].append("price_overridden_to_null")
    elif result.price_override is None and result.flags:
        # Check if any price-related flag indicates the price should be nullified
        price_flags = [f for f in result.flags if f.startswith("price_")]
        if price_flags:
            out["_original_price"] = out.get("price")
            out["price"] = None
            # Typed persistence uses the transaction-specific fields rather
            # than the generic `price` key. Clear those too, otherwise a
            # flagged value can still reach the market feed.
            if str(out.get("intent") or out.get("transaction_type") or "").lower() in {"sell", "sale"}:
                out["total_asking_price"] = None
                out["price_per_sqft"] = None
            out["validation_flags"].append("price_nullified_by_validation")

    return out
