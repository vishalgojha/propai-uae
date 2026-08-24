"""Regression coverage for the typed extraction bridge.

These tests deliberately stop before Supabase.  They protect the deterministic
classification, source-grounded price conversion, and typed-table routing
which must be correct before a worker is allowed to persist a row.
"""

from ai_extraction import _get_extraction_prompt, _normalize_extraction, _source_grounded_price, classify_message_type
from storage.supabase import _normalize_requirement_urgency
from extraction import _ai_extraction_to_typed, _explicit_source_inventory_type, _parse_deposit
from price_normalization import canonical_commercial_rental_price_rupees, canonical_price_rupees, canonical_rental_price_rupees, source_transaction_type


def _item(text, **fields):
    item = {
        "listing_type": "sale",
        "property_category": "residential",
        "price": {"amount": None, "unit": "total", "raw_price_text": None},
    }
    item.update(fields)
    return _ai_extraction_to_typed(item, text, sender_name="Broker")


def test_type_classifier_covers_listing_and_requirement_routes():
    assert classify_message_type("3 BHK for sale in Dubai Marina, 4.4M") == ("residential", "sale")
    assert classify_message_type("Commercial office for rent, 2000 sqft, AED 150 PSF") == ("commercial", "rent")
    assert classify_message_type("Looking for 2 BHK for rent in JVC, budget 60-70K") == (
        "residential", "requirement"
    )
    assert classify_message_type("Shop for sale, 500 sqft, 2.5M") == ("commercial", "sale")


def test_investor_lease_premises_routes_commercial_without_inventing_bhk():
    source = """*AMORE EDGE – Investor Unit Available for Lease**
*Sheikh Zayed Road, Al Quoz*
*Higher Floor Unit*
*430 Sq. Ft. Carpet Area*
*Well-Finished Premises*
*Rent: AED 145K + CAM*"""
    assert classify_message_type(source) == ("commercial", "rent")
    table, row = _ai_extraction_to_typed(
        {
            "listing_type": "rent",
            "property_category": "residential",
            "title": "1 BHK for Rent — Amore Edge",
            "summary_title": "1 BHK for Rent — Amore Edge",
            "bhk": 1,
            "configuration_type": "BHK",
            "configuration_details": "1 BHK",
            "price": {"amount": 145, "unit": "k", "raw_price_text": "AED 145K"},
        },
        source,
        sender_name="Broker",
    )
    assert table == "commercial_rent_listings"
    assert row.get("bhk") is None
    assert "BHK" not in str(row.get("summary_title") or "").upper()


def test_source_inventory_overrides_stale_requirement_route():
    source = """AVL 2BHK's FOR LEASE
*MARINA VIEW TOWER*
Location : Al Seba Street, Dubai Marina
*Asking : 175K*
Parking: 1 Car Park"""
    table, row = _ai_extraction_to_typed(
        {
            "listing_type": "requirement",
            "routing_listing_type": "requirement",
            "message_class": "requirement",
            "classified_transaction_type": "requirement",
            "transaction_type": "rent",
            "property_category": "residential",
            "title": "2 BHK for Rent — None",
            "summary_title": "2 BHK for Rent — None",
            "bhk": 2,
            "configuration_type": "BHK",
            "configuration_details": "2 BHK",
            "price": {"amount": 175, "unit": "k", "raw_price_text": "Asking : 175K"},
        },
        source,
        sender_name="Broker",
    )

    assert table == "residential_rent_listings"
    assert row["transaction_type"] == "rent"
    assert row["monthly_rent"] == 175_000
    assert "None" not in str(row.get("summary_title") or "")


def test_available_on_lease_inventory_is_not_requirement():
    source = """3 BHK with Deck Available on Lease
Building Name: Tuscany
Area: 1600 sqft carpet
Rent: 250K Not Negotiable
Location: Dubai Marina"""
    table, row = _ai_extraction_to_typed(
        {
            "listing_type": "requirement",
            "routing_listing_type": "requirement",
            "message_class": "requirement",
            "classified_transaction_type": "requirement",
            "transaction_type": "rent",
            "property_category": "residential",
            "title": "3 BHK for Rent in Dubai Marina — Tuscany — AED 250K/year",
            "summary_title": "3 BHK for Rent in Dubai Marina — Tuscany — AED 250K/year",
            "bhk": 3,
            "configuration_type": "BHK",
            "configuration_details": "3 BHK",
            "price": {"amount": 250, "unit": "k", "raw_price_text": "250K"},
        },
        source,
        sender_name="Broker",
    )

    assert table == "residential_rent_listings"
    assert row["transaction_type"] == "rent"
    assert row["monthly_rent"] == 250_000


def test_explicit_labeled_price_overrides_provider_guess():
    source = """**NEW** *SALES* *ARRIVAL*
*AVL 1 BHK ON URGENT* *SALE IN*
*MARINA VIEW TOWER*
*PRICE* *4.4M* *FINAL*"""
    result = _source_grounded_price(
        {
            "listing_type": "sale",
            "price": {"amount": 1280, "unit": "m", "raw_price_text": "1280 M"},
        },
        source,
    )
    assert result["price"]["amount"] == 4_400_000
    assert "4.4M" in result["price"]["raw_price_text"].upper()


def test_provider_enum_aliases_survive_normalization():
    normalized = _normalize_extraction({
        "listing_type": "for sale",
        "property_category": "office",
        "price": {"amount": 1, "unit": "total"},
    })
    assert normalized["listing_type"] == "sale"
    assert normalized["property_category"] == "commercial"


def test_requirement_urgency_is_canonicalized_for_db_enum():
    assert _normalize_requirement_urgency("Immediate deal") == "urgent"
    assert _normalize_requirement_urgency("no hurry") == "flexible"
    assert _normalize_requirement_urgency(None) == "normal"


def test_focused_prompt_contains_route_specific_fields_and_price_guardrails():
    prompt = _get_extraction_prompt("residential", "sale", False)
    assert "price" in prompt
    assert "monthly_rent" not in prompt
    assert "1.5M" in prompt
    assert "1500000" in prompt

    rent_prompt = _get_extraction_prompt("commercial", "rent", False)
    assert "commercial_use_type" in rent_prompt
    assert "cam_amount" in rent_prompt
    assert "escalation_pct" in rent_prompt

    residential_rent_prompt = _get_extraction_prompt("residential", "rent", False)
    assert "balcony_area_sqft" in residential_rent_prompt
    assert "fee_sharing_required" in residential_rent_prompt
    assert "PG" in residential_rent_prompt
    assert "85K" in residential_rent_prompt

    assert "mezzanine_area_sqft" in rent_prompt
    assert "needs_review" in rent_prompt

    residential_requirement_prompt = _get_extraction_prompt("residential", "rent", True)
    assert "family" in residential_requirement_prompt
    assert "higher/lower/middle-floor" in residential_requirement_prompt
    assert "PG/hostel" in residential_requirement_prompt


def test_commercial_rent_normalization_maps_provider_aliases_to_typed_fields():
    normalized = _normalize_extraction({
        "listing_type": "rent",
        "property_category": "commercial",
        "locality": {"raw_mention": "Dubai Marina Station", "normalized": "Dubai Marina"},
        "loft_area_sqft": 110,
        "fitout_status": "furnished",
        "deal_tags": ["furnished", "negotiable"],
        "needs_review": True,
    })
    assert normalized["locality"]["raw_mention"] == "Dubai Marina Station"
    assert normalized["mezzanine_area_sqft"] == 110
    assert normalized["fitout_status"] == "furnished"
    assert normalized["deal_tags"] == ["negotiable"]
    assert normalized["needs_review"] is True

    requirement_prompt = _get_extraction_prompt("commercial", "rent", True)
    assert "street-facing" in requirement_prompt
    assert "loading" in requirement_prompt
    assert "maintenance/CAM" in requirement_prompt
    assert "intended_use_details" in requirement_prompt
    assert "needs_attached_washroom" in requirement_prompt
    assert "floor_min" in requirement_prompt
    assert "brokerage side by side" in requirement_prompt.lower()


def test_unknown_commercial_subtype_is_not_fabricated_as_mixed_use():
    table, row = _ai_extraction_to_typed(
        {
            "listing_type": "rent",
            "property_category": "commercial",
            "carpet_area_sqft": 10000,
            "price": {
                "amount": 450,
                "unit": "k",
                "raw_price_text": "AED 450K",
            },
        },
        "Independent building, 10,000 sqft, rent AED 450K, Al Quoz",
        sender_name="Broker",
    )

    assert table == "commercial_rent_listings"
    assert row.get("commercial_use_type") is None


def test_commercial_rent_prompt_covers_package_and_operational_schema():
    prompt = _get_extraction_prompt("commercial", "rent", False)
    assert "broker_rera_number" in prompt
    assert "chargeable_area_sqft" in prompt
    assert "PKG" in prompt
    assert "automatic shutters" in prompt
    assert "room_count" in prompt


def test_sale_price_is_absolute_dirhams_even_when_model_returns_native_unit():
    table, row = _item(
        "3 BHK for sale, 8.5M",
        bhk=3,
        carpet_area_sqft=1200,
        price={"amount": 8.5, "unit": "m", "raw_price_text": "8.5M"},
    )
    assert table == "residential_sale_listings"
    assert row["total_asking_price"] == 8_500_000
    assert row["price_raw_text"] == "8.5M"


def test_unqualified_multi_million_listing_cannot_be_routed_to_rent_by_ai():
    table, row = _item(
        "3 BHK in Business Bay, 6.5M",
        listing_type="rent",
        price={"amount": 6.5, "unit": "m", "raw_price_text": "6.5M"},
    )
    assert table == "residential_sale_listings"
    assert row["transaction_type"] == "sale"
    assert row["total_asking_price"] == 6_500_000
    assert "monthly_rent" not in row


def test_residential_sale_preserves_rich_broker_fields():
    table, row = _item(
        "Available in+1 2Bhk Al Seba Road, Dubai Marina Area:1100carpet+90carpet balcony "
        "furnished apartment with 1 covered car parking @1.85M very slightly negotiable. "
        "For inspection Ctct Vibrant Properties 0501234567 0509876543",
        bhk=2,
        building_name=None,
        price={"amount": 1.85, "unit": "m", "raw_price_text": "1.85M"},
        carpet_area_sqft=1100,
        balcony_area_sqft=90,
        balcony_area_raw_text="90carpet balcony",
        area_raw_text="1100carpet+90carpet balcony",
        locality={"raw_mention": "Al Seba Road, Dubai Marina", "resolved_locality": "Dubai Marina"},
        availability_status="available",
        co_brokered=True,
        brokerage_context="+1",
        parking_type="covered",
        car_parking_count=1,
        showing_instructions="For inspection contact",
        broker_company="Vibrant Properties",
        contacts=[
            {"phone": "0501234567", "role": "primary"},
            {"phone": "0509876543", "role": "team"},
        ],
    )

    assert table == "residential_sale_listings"
    assert row["total_asking_price"] == 1_850_000
    assert row["carpet_area_sqft"] == 1100
    assert row["balcony_area_sqft"] == 90
    assert row["availability_status"] == "available"
    assert row["co_brokered"] is True
    assert row["brokerage_context"] == "+1"
    assert row["parking_type"] == "covered"
    assert row["showing_instructions"] == "For inspection contact"
    assert row["broker_company"] == "Vibrant Properties"
    assert len(row["contacts"]) == 2


def test_price_conversion_is_source_grounded_for_k_and_m_variants():
    assert canonical_price_rupees(6, "m", "6M") == 6_000_000
    assert canonical_price_rupees(6, None, "6 M") == 6_000_000
    assert canonical_price_rupees(850, None, "AED 850K NEG.") == 850_000
    assert canonical_price_rupees(850, "k", "AED 850K NEG.") == 850_000
    assert source_transaction_type("3 BHK, AED 5.5M", "rent") == "sale"
    assert source_transaction_type("3 BHK for rent, AED 110K", "sale") == "rent"
    assert source_transaction_type("3 BHK for sale, AED 5.5M", "rent") == "sale"
    preleased = (
        "Business Bay Irina Tower 2bhk for sale 1100 sqft currently on lease @2.4M negotiable"
    )
    assert source_transaction_type(preleased, "rent") == "sale"
    assert _explicit_source_inventory_type(preleased) == "sale"


def test_rental_decimal_k_means_thousand_dirhams():
    assert canonical_rental_price_rupees(1.30, "k", "1.30k") == 1_300
    assert canonical_rental_price_rupees(85, "k", "Rent 85K") == 85_000
    assert canonical_rental_price_rupees(130, "k", "130k") == 130_000

    table, row = _item(
        "2 BHK rent 130K",
        listing_type="rent",
        price={"amount": 130, "unit": "k", "raw_price_text": "130K"},
    )
    assert table == "residential_rent_listings"
    assert row["monthly_rent"] == 130_000


def test_valid_low_cost_decimal_k_rent_remains_thousands():
    assert canonical_rental_price_rupees(5, "k", "1 BHK rent 5k") == 5_000
    assert canonical_rental_price_rupees(14.5, "k", "1bhk 14.5k rent 2nd floor") == 14_500
    assert canonical_rental_price_rupees(25, "k", "Rent 25k") == 25_000

    table, row = _item(
        "1bhk 14.5k rent 2nd floor",
        listing_type="rent",
        price={"amount": 14.5, "unit": "k", "raw_price_text": "14.5k"},
    )
    assert table == "residential_rent_listings"
    assert row["monthly_rent"] == 14_500


def test_source_rent_is_persisted_when_model_omits_price():
    source = """2 BHK FLAT AVAILABLE ON RENT
New Building Location: Al Seba Street, Opp. Marina View Tower, Dubai Marina
Carpet Area: 940 Sq. Ft. Condition: Unfurnished Rent: AED 130K - Negotiable
Security Deposit: AED 130K
Car Parking: 1"""
    table, row = _item(
        source,
        listing_type="rent",
        property_category="residential",
        bhk=2,
        price={"amount": None, "unit": "total", "raw_price_text": None},
    )
    assert table == "residential_rent_listings"
    assert row["monthly_rent"] == 130_000


def test_requirement_k_range_and_tenancy_cue_route_to_rent_without_inflation():
    source = """Requirement furnished flat budget 38k se 45k
Location al barsha
Family party
Immidately contact no 0501234567"""
    table, row = _item(
        source,
        listing_type="requirement",
        message_class="requirement",
        transaction_type="requirement",
        classified_transaction_type="sale",
        budget_min=380_000,
        budget_max=450_000,
        locality_options=["al barsha"],
        bhk_options="furnished flat",
        broker_name="Immidately contact no",
    )

    assert table == "residential_rent_requirements"
    assert row["budget_min"] == 38_000
    assert row["budget_max"] == 45_000
    assert row["bhk_options"] == []
    assert "broker_name" not in row
    assert row["needs_review"] is True


def test_requirement_k_range_without_tenancy_evidence_does_not_force_rent():
    table, row = _item(
        "Requirement budget 38k se 45k",
        listing_type="requirement",
        message_class="requirement",
        transaction_type="sale",
    )
    assert table == "residential_sale_requirements"
    assert row["budget_min"] == 38_000
    assert row["budget_max"] == 45_000


def test_rental_requirement_with_budget_range_preserves_rent_intent():
    source = """Direct client Requirement
1 BR on rent for a single lady working in DIFC
Prefer Semi furnished
Dubai Marina only
Budget 100K - 110K
Chiller free n parking mandatory"""
    table, row = _item(
        source,
        listing_type="requirement",
        message_class="requirement",
        transaction_type="sale",
        budget_min=100000.0,
        budget_max=110000.00000000001,
        locality_options=["Dubai Marina"],
    )
    assert table == "residential_rent_requirements"
    assert row["intent"] == "RENT"
    assert row["transaction_type"] == "rent"
    assert row["budget_min"] == 100_000
    assert row["budget_max"] == 110_000


def test_bare_dirham_quote_passes_through_without_rescaling():
    assert canonical_rental_price_rupees(14000, "total", "Monthly Rent :- 14000") == 14_000
    assert canonical_rental_price_rupees(120000, "total", "rent 120000 nego") == 120_000

    table, row = _item(
        "2 BHK on lease, annual rent 140000",
        listing_type="rent",
        price={"amount": 140000, "unit": "total", "raw_price_text": "annual rent 140000"},
    )
    assert table == "residential_rent_listings"
    assert row["monthly_rent"] == 140_000


def test_residential_rent_preserves_rental_broker_facts():
    table, row = _item(
        "Available luxury 3 BHK on lease, Marina Sail, 2000 sqft + 500 sqft terrace, "
        "AED 600K negotiable, MNCs/consulates, client profile mandatory, mandate plus one",
        listing_type="rent",
        bhk=3,
        price={"amount": 600, "unit": "k", "raw_price_text": "AED 600K"},
        carpet_area_sqft=2000,
        terrace_area_sqft=500,
        terrace_area_raw_text="2000 sqft + 500 sqft terrace",
        furnishing_status="semi_furnished",
        lease_term_min_months=36,
        lease_term_max_months=60,
        company_lease_criteria={"tenant_types": ["MNC", "consulate"]},
        client_profile_required=True,
        plus_one_deal=True,
        fee_sharing_required=True,
        brokerage_terms_raw="mandate plus one",
        contacts=[{"name": "Yogesh Bajaj", "phone": "0501234567"}],
    )
    assert table == "residential_rent_listings"
    assert row["monthly_rent"] == 600_000
    assert row["terrace_area_sqft"] == 500
    assert row["lease_term_min_months"] == 36
    assert row["lease_term_max_months"] == 60
    assert row["client_profile_required"] is True
    assert row["plus_one_deal"] is True
    assert row["fee_sharing_required"] is True
    assert row["brokerage_terms_raw"] == "mandate plus one"
    assert len(row["contacts"]) == 1


def test_implausible_rent_is_marked_for_review_and_not_high_confidence():
    table, row = _item(
        "3 BHK monthly rental quote",
        listing_type="rent",
        extraction_confidence="high",
        price={"amount": 2_500_000_000, "unit": "total", "raw_price_text": None},
    )
    assert table == "residential_rent_listings"
    # The model-only price is discarded when the source slice contains no
    # price evidence; never persist an unsupported rent amount.
    assert row.get("monthly_rent") is None
    assert row["needs_review"] is True
    assert row["extraction_confidence"] == "low"


def test_price_and_psf_routing_for_rent():
    table, row = _item(
        "2 BHK rent, 110K",
        listing_type="rent",
        price={"amount": 110, "unit": "k", "raw_price_text": "110K"},
    )
    assert table == "residential_rent_listings"
    assert row["monthly_rent"] == 110_000

    table, row = _item(
        "Commercial shop for rent, AED 95 PSF, 681 sqft",
        property_category="commercial",
        listing_type="rent",
        carpet_area_sqft=681,
        price={"amount": 95, "unit": "per_sqft", "raw_price_text": "AED 95 PSF"},
    )
    assert table == "commercial_rent_listings"
    assert row["rent_per_sqft"] == 95
    assert row["monthly_rent"] == 64_695


def test_commercial_rent_uses_chargeable_area_for_psf_math_and_keeps_sale_fields_out():
    table, row = _item(
        "Fully furnished office, carpet 11350, chargeable 18900, rent 175 PSF chargeable",
        property_category="commercial",
        listing_type="rent",
        carpet_area_sqft=11_350,
        chargeable_area_sqft=18_900,
        price_basis="chargeable_per_sqft",
        broker_rera_number="A51900002370",
        price={"amount": 175, "unit": "per_sqft", "raw_price_text": "Rs 175 per sqft of chargeable area"},
        workstation_count=180,
        director_cabin_count=14,
    )
    assert table == "commercial_rent_listings"
    assert row["rent_per_sqft"] == 175
    assert row["monthly_rent"] == 3_307_500
    assert row["price_math"]["basis"] == "chargeable_area_sqft"
    assert row["broker_rera_number"] == "A51900002370"
    assert "total_asking_price" not in row
    assert "price_per_sqft" not in row


def test_commercial_package_is_monthly_rent_not_deposit_or_cam():
    assert canonical_commercial_rental_price_rupees(145, "k", "AED 145K pkg") == 145_000
    assert canonical_commercial_rental_price_rupees(14.5, "k", "14.5k pkg") == 14_500
    table, row = _item(
        "Commercial office, 460 carpet, Asking 145K pkg neg, no parking",
        property_category="commercial",
        listing_type="rent",
        carpet_area_sqft=460,
        price={"amount": 145, "unit": "k", "raw_price_text": "145K pkg"},
        price_basis="monthly",
        deposit_amount=None,
        cam_amount=None,
    )
    assert table == "commercial_rent_listings"
    assert row["monthly_rent"] == 145_000
    assert "deposit_amount" not in row
    assert "cam_amount" not in row


def test_commercial_sale_keeps_options_and_uses_total_price():
    table, row = _item(
        "Iris Bay office on sale, 1800 sqft, middle floor, 2 parking, furnished, AED 7.20M nego",
        property_category="commercial",
        listing_type="sale",
        building_name="Iris Bay",
        carpet_area_sqft=1800,
        floor_level="middle",
        car_parking_count=2,
        fitout_status="fully_furnished",
        price={"amount": 7.20, "unit": "m", "raw_price_text": "AED 7.20M"},
        price_qualifier="negotiable",
        inspection_notice_minutes=120,
        broker_rera_number="PERMIT-71245",
    )
    assert table == "commercial_sale_listings"
    assert row["total_asking_price"] == 7_200_000
    assert row["carpet_area_sqft"] == 1800
    assert row["floor_level"] == "middle"
    assert row["car_parking_count"] == 2
    assert row["broker_rera_number"] == "PERMIT-71245"
    assert "monthly_rent" not in row
    assert "rent_per_sqft" not in row


def test_commercial_sale_psf_math_requires_explicit_pricing_basis():
    table, row = _item(
        "Commercial office for sale, 4800 sqft, AED 240 PSF",
        property_category="commercial",
        listing_type="sale",
        carpet_area_sqft=4800,
        price_basis="carpet_per_sqft",
        price={"amount": 240, "unit": "per_sqft", "raw_price_text": "AED 240 PSF"},
    )
    assert table == "commercial_sale_listings"
    assert row["price_per_sqft"] == 240
    assert row["total_asking_price"] == 1_152_000
    assert row["price_math"]["basis"] == "carpet_area_sqft"


def test_commercial_rent_requirement_preserves_constraints_and_contacts():
    table, row = _item(
        "Urgent requirement on rent, office, 600 to 1000 carpet, fully furnished, "
        "2 cabins, 1 conference, 10 workstations, washroom and pantry, Al Quoz to Business Bay, 90K to 100K",
        property_category="commercial",
        listing_type="requirement",
        classified_is_requirement=True,
        classified_transaction_type="rent",
        commercial_use_type=["office"],
        area_min_sqft=600,
        area_max_sqft=1000,
        budget_min=90_000,
        budget_max=100_000,
        furnishing_preference="fully_furnished",
        min_cabin_count=2,
        min_workstation_count=10,
        needs_conference_room=True,
        needs_washroom=True,
        needs_pantry=True,
        entrance_requirement="street-facing entrance",
        signage_required=True,
        loading_access_required=True,
        power_requirements="3-phase power",
        floor_count_max=2,
        consecutive_floors_required=True,
        budget_includes_maintenance=True,
        locality_options=["Al Quoz", "Business Bay"],
        urgency="urgent",
        contacts=[{"name": "Mangesh Singh", "phone": "0501234567"}],
    )
    assert table == "commercial_rent_requirements"
    assert row["commercial_use_type"] == ["office"]
    assert row["min_cabin_count"] == 2
    assert row["min_workstation_count"] == 10
    assert row["needs_conference_room"] is True
    assert row["needs_washroom"] is True
    assert row["budget_max"] == 100_000
    assert row["urgency"] == "urgent"
    assert row["entrance_requirement"] == "street-facing entrance"
    assert row["signage_required"] is True
    assert row["loading_access_required"] is True
    assert row["power_requirements"] == "3-phase power"
    assert row["floor_count_max"] == 2
    assert row["consecutive_floors_required"] is True
    assert row["budget_includes_maintenance"] is True
    assert "monthly_rent" not in row


def test_colon_price_and_deposit_shorthand_are_source_grounded():
    table, row = _item(
        "1 BHK sale, 2:25M",
        bhk=1,
        price={"amount": 2.25, "unit": "m", "raw_price_text": "2:25M"},
    )
    assert table == "residential_sale_listings"
    assert row["total_asking_price"] == 2_250_000

    deposit = _parse_deposit("3 BHK rent, 90+2", monthly_rent=90_000)
    assert deposit["deposit_months"] == 2
    assert deposit["deposit_amount"] == 180_000


def test_requirement_routes_to_requirement_table_not_listing_table():
    table, row = _item(
        "Looking for 2 BHK for rent in JVC, budget 60-70K",
        listing_type="requirement",
        classified_is_requirement=True,
        classified_transaction_type="rent",
        bhk=2,
        budget_min=60_000,
        budget_max=70_000,
    )
    assert table == "residential_rent_requirements"
    assert row["bhk_options"] == [2.0]
    assert row["budget_max"] == 70_000
    assert "monthly_rent" not in row


def test_residential_rent_requirement_preserves_tenant_floor_and_lease_preferences():
    table, row = _item(
        "Required 2 BHK on rent, JVC to Dubai Marina, 800-1000 carpet, "
        "fully furnished, higher floor, family/company lease, pets, up to 150K, "
        "3 year lease, 100K deposit",
        listing_type="requirement",
        classified_is_requirement=True,
        classified_transaction_type="rent",
        bhk_options=[2],
        configuration_preference=["2 BHK"],
        budget_min=None,
        budget_max=150_000,
        area_min_sqft=800,
        area_max_sqft=1000,
        locality_options=["JVC", "Dubai Marina"],
        furnishing_preference="fully_furnished",
        floor_preference="higher_floor",
        tenant_type="family/company_lease",
        has_pets=True,
        lease_term_preference="3 years",
        deposit_budget_max=100_000,
        amenity_requirements=["parking"],
    )
    assert table == "residential_rent_requirements"
    assert row["locality_options"] == ["JVC", "Dubai Marina"]
    assert row["floor_preference"] == "higher_floor"
    assert row["tenant_type"] == "family/company_lease"
    assert row["has_pets"] is True
    assert row["lease_term_preference"] == "3 years"
    assert row["deposit_budget_max"] == 100_000
    assert "monthly_rent" not in row
