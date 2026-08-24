from building_quality import is_valid_building_candidate, normalize_building_name
from storage.supabase import (
    _clean_person_name,
    _effective_broker_name,
    _jid_phone,
    _locality_fields,
)
from extraction import _clean_broker_name


def test_jid_names_are_rejected_and_phone_can_be_extracted():
    assert _clean_person_name("+918424000018@s.whatsapp.net") == ""
    assert _clean_person_name(":54@s.whatsapp.net") == ""
    assert _jid_phone("+918424000018@s.whatsapp.net") == "918424000018"
    assert _jid_phone(":54@s.whatsapp.net") == ""


def test_phone_numbers_never_become_broker_names():
    for value in ("9326462209", "+91 9326462209", "981-996-8785"):
        assert _clean_broker_name(value) is None
        assert _clean_person_name(value) == ""
    assert _clean_broker_name("Nisha Gandhi") == "Nisha Gandhi"


def test_source_broker_name_wins_over_whatsapp_display_name():
    assert _effective_broker_name(
        source_name="Kapil Gopal Ojha",
        display_name="Kapsy",
    ) == "Kapil Gopal Ojha"


def test_broker_cta_is_not_an_identity():
    assert _effective_broker_name(
        source_name="Please share suitable options",
        display_name="Kapsy",
    ) == "Kapsy"


def test_top_level_asset_classes_are_valid():
    from listing_validation import validate_listing

    for asset_type in ("residential", "commercial"):
        result = validate_listing({
            "asset_type": asset_type,
            "intent": "RENT",
            "price": 50000,
            "price_unit": "abs",
        })
        assert not any(flag.startswith("unrecognised_asset_type:") for flag in result.flags)


def test_absent_furnishing_is_not_invalid():
    from listing_validation import validate_listing

    result = validate_listing({"asset_type": "residential", "furnishing": "none"})
    assert "unrecognised_furnishing:none" not in result.flags


def test_observation_fingerprint_keeps_same_unit_from_different_brokers_separate():
    from storage.supabase import _merge_observation_rows

    base = {
        "observation_type": "LISTING",
        "intent": "RENT",
        "transaction_type": "rent",
        "bhk": "3 BHK",
        "building_name": "Ten BKC",
        "micro_market": "BKC",
        "area_sqft": 1360,
        "price": 300000,
        "created_at": "2026-08-16T10:00:00+00:00",
    }
    rows = [
        {**base, "broker_phone": "919773757759", "raw_message_id": 1},
        {**base, "broker_phone": "919999999999", "raw_message_id": 2},
    ]

    assert len(_merge_observation_rows(rows)) == 2


def test_merged_repost_rows_never_return_as_independent_inventory():
    from storage.supabase import _merge_observation_rows

    row = {
        "asset_type": "residential",
        "transaction_type": "sale",
        "building_name": "Skyline",
        "micro_market": "Bandra West",
        "broker_phone": "919999999999",
        "total_asking_price": 10000000,
        "duplicate_status": "merged",
    }

    assert _merge_observation_rows([row]) == []


def test_location_object_is_promoted_to_evidence_columns():
    assert _locality_fields({
        "area": None,
        "location": {"raw_mention": "20th Road, Khar West", "resolved_locality": "Khar West"},
    }) == ("20th Road, Khar West", "Khar West")


def test_building_normalizer_preserves_real_estate_acronyms():
    assert normalize_building_name("difc gate village") == "DIFC Gate Village"
    assert normalize_building_name("jbr shoreline apartments") == "JBR Shoreline Apartments"
    assert normalize_building_name("moe residence") == "MOE Residence"
    assert normalize_building_name("81-aureate") == "81-Aureate"


def test_building_candidate_filter_rejects_broker_chatter():
    assert not is_valid_building_candidate("Thanks and Regards")
    assert not is_valid_building_candidate("plzz call")
    assert not is_valid_building_candidate("ownership")
    assert is_valid_building_candidate("HDIL Metropolis")


def test_embedded_building_phone_is_quarantined_at_both_boundaries():
    from building_quality import is_valid_building_candidate
    from extraction_quality import building_name_problem

    for value in (
        "Marina Gate +971501234567",
        "Sunil -971561234567",
        "Office – 0501234567",
    ):
        assert building_name_problem(value) == "building_name_contains_phone"
        assert not is_valid_building_candidate(value)
