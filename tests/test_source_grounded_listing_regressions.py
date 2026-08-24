import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("pandas", ModuleType("pandas"))


def test_bathroom_count_is_never_a_building_name():
    from building_quality import is_valid_building_candidate
    from extraction_quality import building_name_problem

    for value in ("2 Bathrooms", "1 bathroom", "3 Washrooms", "2 toilets"):
        assert building_name_problem(value) == "building_name_is_listing_text"
        assert not is_valid_building_candidate(value)


def test_exact_source_million_price_overrides_shifted_model_amount():
    from extraction import _price_from_ai_and_raw

    source = """🏡 FOR SALE
📍 4 Townhouses, Dubai Marina
✨ 1 BHK | Fully Furnished with Electronics
💰 Asking: AED 18.5M
"""
    amount, unit = _price_from_ai_and_raw(
        {"amount": 185, "unit": "k", "raw_price_text": None},
        source,
    )

    assert amount == 18_500_000
    assert unit == "abs"


def test_explicit_slice_location_overrides_broadcast_market_context():
    from extraction import _ground_locality_to_source

    ai = {
        "locality": {
            "raw_mention": "Bandra West",
            "resolved_locality": "Bandra West",
            "confidence": 0.9,
        },
        "micro_market": "Bandra West",
    }
    source = "AVAILABLE FOR RENT\nCARPET 1100 SQFT\nRENT 2.10K\nLOCATION SANPADA"

    repaired = _ground_locality_to_source(ai, source)

    assert repaired["locality"]["raw_mention"] == "SANPADA"
    assert repaired["locality"]["resolved_locality"] == "SANPADA"
    assert repaired["micro_market"] == "SANPADA"
    assert "locality_repaired_from_explicit_source_boundary" in repaired["validation_flags"]
