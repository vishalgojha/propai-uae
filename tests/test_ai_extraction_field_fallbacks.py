import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_extraction import (
    _apply_deterministic_field_fallbacks,
    _canonical_locality_from_mention,
    _normalize_extraction,
    _source_grounded_furnishing,
    _source_grounded_price,
    generate_title,
)
from extraction import _ai_extraction_to_parsed, _price_from_ai_and_raw
from extraction_models import validate_source_semantics


def test_commercial_message_recovers_obvious_schema_facts():
    text = """Available Commercial Office On Sale At Al Quoz
Area 2000 Carpet
Condition Bareshell
Car Park 2
New Building
Higher Floor"""

    out = _apply_deterministic_field_fallbacks(
        {"carpet_area_sqft": None, "fitout_status": None, "car_parking_count": None},
        text,
    )

    assert out["carpet_area_sqft"] == 2000.0
    assert out["fitout_status"] == "bare_shell"
    assert out["car_parking_count"] == 2
    assert "brand_new_building" in out["deal_tags"]


def test_commercial_requirement_recovers_range_budget_use_and_localities():
    text = """Commercial Space Required For A Tailoring Unit On Outright Basis
700-1000 sq.ft.
Anywhere in JVC Business Bay
Budget: 6.5M"""

    out = _apply_deterministic_field_fallbacks({}, text)

    assert out["area_min_sqft"] == 700.0
    assert out["area_max_sqft"] == 1000.0
    assert out["budget_max"] == 6_500_000.0
    assert out["locality_options"] == ["JVC", "Business Bay"]
    assert out["commercial_use_type"] == "tailoring unit"


def test_explicit_available_sale_overrides_wrong_llm_rent():
    text = """*Available Sale*
2 BHK Marina View Tower furnished
DMCC Metro station
Price,4.4M Negotiable
Rakesh Mishra"""

    out = _apply_deterministic_field_fallbacks(
        {"listing_type": "rent"},
        text,
    )

    assert out["listing_type"] == "sale"


def test_unqualified_multi_million_price_overrides_wrong_llm_rent():
    out = _apply_deterministic_field_fallbacks(
        {"listing_type": "rent"},
        "3 BHK Business Bay, 6.5M",
    )

    assert out["listing_type"] == "sale"


def test_rental_requirement_recovers_bhk_locations_tenant_and_amenities():
    text = """URGENT REQUIREMENT – 1 BHK ON RENT
Preferred Locations: Al Barsha South, JVC
Budget: Up to 95K
Tenant: Small Family (2 Members Only)
Open Car Parking Required
Modular Kitchen/Kitchen Trolley Required
Gas Pipeline Preferred"""

    out = _apply_deterministic_field_fallbacks({"listing_type": "requirement"}, text)

    assert out["bhk"] == 1.0
    assert out["budget_max"] == 95000.0
    assert out["locality_options"] == ["Al Barsha South", "JVC"]
    assert out["tenant_type"] == "Small Family (2 Members Only)"
    assert out["car_parking_min"] == 1
    assert out["amenity_requirements"] == ["modular_kitchen", "gas_pipeline"]


def test_source_quote_overrides_wrong_provider_amount():
    out = _source_grounded_price(
        {
            "listing_type": "rent",
            "price": {"amount": 1500000, "unit": "total", "raw_price_text": "Rent 130K"},
        },
        "Mandate 3 bhk flat furnished with White goods, Al Seba marg Dubai Marina.\n"
        "Rent 130K neqt",
    )

    assert out["price"] == {
        "amount": 130000.0,
        "unit": "total",
        "period": "per_month",
        "raw_price_text": "Rent 130K",
    }


def test_explicit_rent_amount_overrides_wrong_provider_psf_unit():
    assert _price_from_ai_and_raw({
        "amount": 200000,
        "unit": "per_sqft",
        "period": "one_time",
        "raw_price_text": "AED 200K.",
    }) == (200000.0, "abs")


def test_explicit_psf_quote_keeps_psf_unit():
    assert _price_from_ai_and_raw({
        "amount": 200,
        "unit": "per_sqft",
        "period": "per_month",
        "raw_price_text": "AED 200 per sq.ft.",
    }) == (200.0, "per_sqft")


def test_source_semantics_rejects_psf_without_source_psf_marker():
    out = validate_source_semantics({
        "listing_type": "rent",
        "property_category": "residential",
        "price": {
            "amount": 120,
            "unit": "per_sqft",
            "period": "one_time",
            "raw_price_text": "AED 120K",
        },
        "extraction_confidence": "high",
    }, "3 BHK available on lease. Rent: AED 120K.")

    assert out["listing_type"] == "rent"
    assert out["price"]["unit"] == "total"
    assert out["price"]["period"] == "per_month"
    assert out["needs_review"] is True


def test_source_semantics_preserves_explicit_rent_psf_rate():
    out = validate_source_semantics({
        "listing_type": "rent",
        "property_category": "residential",
        "price": {
            "amount": 143,
            "unit": "per_sqft",
            "period": "one_time",
            "raw_price_text": "AED 143 per sq.ft.",
        },
        "extraction_confidence": "high",
    }, "3 BHK available on lease. Rent rate AED 143 per sq.ft. per year.")

    assert out["price"]["unit"] == "per_sqft"
    assert out["price"]["period"] == "per_month"


def test_listing_recovers_possession_and_no_parking():
    out = _apply_deterministic_field_fallbacks(
        {"listing_type": "rent", "possession_date": None, "car_parking_count": None},
        "Possession 1st September 2026.\nNo car parking",
    )

    assert out["possession_date"] == "2026-09-01"
    assert out["possession_status"] == "available"
    assert out["car_parking_count"] == 0
    assert out["parking_type"] == "none"


def test_embedded_short_locality_mention_resolves_to_canonical_market():
    assert _canonical_locality_from_mention("Marina") == "Dubai Marina"
    assert _canonical_locality_from_mention("Dubai Marina") == "Dubai Marina"


def test_provider_price_without_source_quote_is_discarded():
    out = _source_grounded_price(
        {"price": {"amount": 8500000, "unit": "total", "raw_price_text": "8.5M"}},
        "Ready Fully Furnished Office for Sale in Business Bay. Very reasonably priced.",
    )
    assert out["price"]["amount"] is None
    assert out["needs_review"] is True


def test_provider_price_with_source_quote_is_kept():
    out = _source_grounded_price(
        {"price": {"amount": 8500000, "unit": "total", "raw_price_text": "8.5M"}},
        "Ready office for sale. Price 8.5M in Business Bay.",
    )
    assert out["price"]["amount"] == 8500000


def test_mixed_psf_quotes_choose_the_quote_for_the_current_route():
    source = """Commercial office for lease
Quote - 500 psf
Price Sale - 85k Per sqft
"""
    rent = _source_grounded_price({
        "listing_type": "rent",
        "price": {"amount": 85000, "unit": "per_sqft", "raw_price_text": "Price Sale - 85k Per sqft"},
    }, source)
    sale = _source_grounded_price({
        "listing_type": "sale",
        "price": {"amount": 500, "unit": "per_sqft", "raw_price_text": "Quote - 500 psf"},
    }, source)

    assert rent["price"]["amount"] == 500
    assert rent["price"]["unit"] == "per_sqft"
    assert sale["price"]["amount"] == 85000


def test_preleased_is_preserved_as_occupancy_status():
    out = _apply_deterministic_field_fallbacks(
        {"listing_type": "sale", "occupancy_status": None},
        "Pre-Leased Investment Opportunity in Business Bay",
    )
    assert out["occupancy_status"] == "pre_leased"


def test_requirement_preserves_preferences_and_dialect():
    out = _apply_deterministic_field_fallbacks(
        {"listing_type": "requirement", "locality_options": [], "furnishing_preference": "fully_loaded"},
        "Require\n1 Bhk Fully Loaded for Expat - Company Lease\nPreferred Locations: JVC, Dubai Marina\nBudget 100K",
    )
    assert out["locality_options"] == ["JVC", "Dubai Marina"]
    assert out["furnishing_preference"] == "fully_furnished"
    assert out["tenant_type"] == "expat"
    assert out["lease_term_preference"] == "company_lease"
    assert out["company_lease_criteria"] is True


def test_requirement_recovers_bhk_from_source_when_provider_omits_it():
    out = _apply_deterministic_field_fallbacks(
        {"listing_type": "requirement", "bhk": None, "bhk_options": []},
        "Require\n1 Bhk Fully Loaded for Expat - Company Lease\nBudget 100K",
    )
    assert out["bhk"] == 1.0


def test_normalizer_tolerates_object_and_punctuation_numeric_fields():
    out = _normalize_extraction({
        "listing_type": "rent",
        "property_category": "residential",
        "bhk": {"value": 3},
        "carpet_area_sqft": {"amount": "1,250"},
        "price": {"amount": {"value": "."}, "unit": "total", "raw_price_text": "Rent on request"},
        "car_parking_count": "Steel parking",
    })

    assert out["bhk"] == 3.0
    assert out["carpet_area_sqft"] == 1250.0
    assert out["price"]["amount"] is None
    assert "car_parking_count" not in out


def test_furnishing_is_dropped_when_source_does_not_support_it():
    out = _source_grounded_furnishing(
        {"furnishing_status": "unfurnished"},
        "3bhk large\nMagnus\n3+3 jodi\nSeasons",
    )

    assert out["furnishing_status"] is None
    assert out["needs_review"] is True
    assert "furnishing_without_source_evidence" in out["validation_flags"]


def test_explicit_furnishing_evidence_is_preserved():
    out = _source_grounded_furnishing(
        {"furnishing_status": "fully_furnished"},
        "Fully furnished 3 BHK in Magnus",
    )

    assert out["furnishing_status"] == "fully_furnished"


def test_title_generation_ignores_non_numeric_provider_amount():
    title = generate_title({
        "listing_type": "rent",
        "property_category": "residential",
        "bhk": {"value": 2},
        "price": {"amount": {"value": "."}, "raw_price_text": "Rent on request"},
        "locality": {"resolved_locality": "Dubai Marina"},
    })

    assert "2 BHK" in title
    assert "Rent on request" in title


def test_parsed_conversion_tolerates_dirty_numeric_fields():
    parsed = _ai_extraction_to_parsed({
        "listing_type": "rent",
        "property_category": "residential",
        "price": {"amount": 125000, "unit": "total", "raw_price_text": "Rent 125K"},
        "bhk": 2,
        "deposit_amount": {"value": "."},
        "bathroom_count": "Steel parking",
        "car_parking_count": "Steel parking",
        "interior_value": {"value": "."},
    }, "Rent 125K", "Broker", "Broker")

    assert parsed["monthly_rent"] == 125000
    assert parsed["deposit_amount"] is None
    assert parsed["bathroom_count"] is None
    assert parsed["car_parking_count"] is None
    assert parsed["interior_value"] is None


def test_deposit_parser_tolerates_punctuation_only_amounts():
    parsed = _ai_extraction_to_parsed({
        "listing_type": "rent",
        "property_category": "residential",
        "price": {"amount": 125000, "unit": "total", "raw_price_text": "Rent 125K"},
        "bhk": 2,
        "deposit_raw_text": "deposit ..2",
    }, "Rent 125K deposit ..2", "Broker", "Broker")

    assert parsed["monthly_rent"] == 125000
    assert parsed["deposit_amount"] is None
    assert parsed["deposit_months"] is None
