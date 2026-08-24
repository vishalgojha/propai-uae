from extraction import (
    _ai_extraction_to_parsed,
    _extract_broker_signature_names,
    _infer_building_name_from_source,
    _is_actionable_property_slice,
    _quarantine_broker_signature_building,
    _slice_blocks_for_ai_items,
)
from extraction_quality import repair_building_assignment


DUBAI_BROADCAST = """Dear Associates

*Residential Outright*
*Dubai Marina / JBR / Business Bay*- *New Listings added*

*Marina Sail* - Dubai Marina 3 BHK - *2880 sq ft* - Fully Furnished - *40M*
*Marina Gate Premium Tower* - 4 BHK - *2600 sq ft* - *24.5M*
*Bay Vista towers* Near Bay Square - 3000 sq ft - 31.5M
*Iris Bay* - Business Bay 3 BHK 1500 sq ft - Partly furnished

*Kindly allow 24 Hrs to set up visits - Client Business profile needed*
Vibrant Elite Estates
Prem Vibrant
RERA Regd.
0501234567 / 0509876543"""


def test_dense_bulk_rows_receive_distinct_source_slices():
    items = [
        {"building_name": "Marina Sail", "bhk": 3, "carpet_area_sqft": 2880},
        {"building_name": "Marina Gate Premium Tower", "bhk": 4, "carpet_area_sqft": 2600},
        {"building_name": "Bay Vista towers", "carpet_area_sqft": 3000},
        {"building_name": "Iris Bay", "bhk": 3, "carpet_area_sqft": 1500},
        {"building_name": "Vibrant Elite Estates"},
    ]

    slices = _slice_blocks_for_ai_items(DUBAI_BROADCAST, items)

    assert slices[0].startswith("*Marina Sail*") and "Premium Tower" not in slices[0]
    assert slices[1].startswith("*Marina Gate Premium Tower*") and "Marina Sail" not in slices[1]
    assert slices[2].startswith("*Bay Vista towers*") and "Iris Bay" not in slices[2]
    assert slices[3].startswith("*Iris Bay*") and "Vibrant Elite" not in slices[3]
    assert not _is_actionable_property_slice(slices[4])


def test_footer_company_and_person_are_quarantined_as_buildings():
    signatures = _extract_broker_signature_names(DUBAI_BROADCAST)
    assert signatures == {"vibrant elite estates", "prem vibrant"}

    for value in signatures:
        parsed = {"building_name": value, "validation_flags": []}
        ai_item = {"building_name": value}
        assert _quarantine_broker_signature_building(parsed, ai_item, signatures)
        assert parsed["building_name"] is None


def test_generic_tower_and_broker_note_are_never_repaired_as_buildings():
    generic = {"building_name": "Cuffe Parade - Premium Tower", "micro_market": "Cuffe Parade"}
    repair_building_assignment(
        generic,
        "*Cuffe Parade - Premium Tower* - 4 BHK - 2600 sq ft - 24.50 Cr",
    )
    assert generic["building_name"] is None
    assert "building_name_is_generic_descriptor" in generic["validation_flags"]

    note = {
        "building_name": "Kindly allow 24 Hrs to set up visits - Client Business profile needed",
        "micro_market": "Colaba",
    }
    repair_building_assignment(
        note,
        "*Kindly allow 24 Hrs to set up visits - Client Business profile needed*",
    )
    assert note["building_name"] is None
    assert "building_name_is_listing_text" in note["validation_flags"]


def test_bold_building_boundary_does_not_absorb_adjacent_locality():
    assert _infer_building_name_from_source(
        "*Rustomjee Crown* prabhadevi - 3BHK - 1335 Sq ft - 9.25 Cr"
    ) == "Rustomjee Crown"
    assert _infer_building_name_from_source(
        "*Ansal Heights* - Worli - 3.5 BHK - 1450 sq ft - 7.50 Cr"
    ) == "Ansal Heights"
    assert _infer_building_name_from_source(
        "*Cuffe Parade - Premium Tower* - 4 BHK - 2600 sq ft - 24.50 Cr"
    ) is None


def test_ai_glued_building_and_locality_are_separated_from_source_evidence():
    ai_item = {
        "listing_type": "sale",
        "transaction_type": "sale",
        "property_category": "residential",
        "building_name": "Marina Sail dubai marina",
        "bhk": 3,
        "carpet_area_sqft": 1335,
        "price": {"amount": 9.25, "unit": "m", "raw_price_text": "9.25M"},
        "locality": {"raw_mention": None, "resolved_locality": None},
        "extraction_confidence_score": 0.9,
    }
    source = "*Marina Sail* dubai marina - 3BHK - 1335 Sq ft - 9.25M"

    parsed = _ai_extraction_to_parsed(ai_item, source, "", "", slice_text=source)

    assert parsed["building_name"] == "Marina Sail"
    assert parsed["location_raw"] == "dubai marina"
    assert parsed["micro_market"] == "dubai marina"
    assert "building_name_repaired_from_explicit_source_boundary" in parsed["validation_flags"]
