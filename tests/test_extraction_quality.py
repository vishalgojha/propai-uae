from extraction_quality import building_name_problem, repair_building_assignment


def test_price_only_value_can_never_be_a_building_name():
    assert building_name_problem("3k") == "building_name_is_price"
    assert building_name_problem("AED 8 k/month") == "building_name_is_price"


def test_listing_text_is_not_promoted_to_building_name():
    assert building_name_problem("Fully Furnished") == "building_name_is_listing_text"
    assert building_name_problem("Al Barsha South") == "building_name_is_locality"
    assert building_name_problem(
        "Kindly allow 24 Hrs to set up visits - Client Business profile needed"
    ) == "building_name_is_listing_text"
    assert building_name_problem(
        "Cuffe Parade - Premium Tower"
    ) == "building_name_is_generic_descriptor"


def test_bad_building_value_is_repaired_from_its_own_slice_only():
    item = {
        "building_name": "3k",
        "micro_market": "Dubai Marina",
        "bhk": "3 BHK",
    }
    repair_building_assignment(
        item,
        "3 BHK\nDeepak Silverline\nDubai Marina\nRent - AED 300 k",
    )
    assert item["building_name"] == "Deepak Silverline"
    assert item["needs_review"] is True
    assert "building_name_is_price" in item["validation_flags"]


def test_locality_only_slice_stays_null_when_no_building_is_named():
    item = {
        "building_name": "Fully Furnished",
        "micro_market": "Al Barsha South",
        "bhk": "4 BHK",
    }
    repair_building_assignment(
        item,
        "4 BHK\nAL BARSHA SOUTH\n2,000 sqft\nFully Furnished\nRent - AED 250 k",
    )
    assert item["building_name"] is None
    assert "building_name_unresolved" in item["validation_flags"]


def test_sibling_building_is_not_allowed_to_cross_block_boundary():
    item = {
        "building_name": "First Tower",
        "micro_market": "Dubai Marina",
        "bhk": "2 BHK",
    }
    repair_building_assignment(
        item,
        "2 BHK\nSecond Tower\nDubai Marina\nRent - AED 120 k",
    )
    assert item["building_name"] == "Second Tower"
    assert "building_name_not_in_source_slice" in item["validation_flags"]
