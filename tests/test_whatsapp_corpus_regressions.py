"""Corpus-backed regression tests for real WhatsApp export patterns.

The source material comes from the newer ZIP exports in /home/vishal/Downloads/wadata.
These tests pin the agreed parser contract on real broker messages instead of synthetic
toy strings.
"""

import evidence.resolver

from ai_chat_engine import parse_market_search_request
from app import parse_message, resolve_parsed
from location import enrich_parsed_location, parse_location


IRIS_BAY_OFFICE = """\
Available *Office Space on Lease in Business Bay, Marasi Drive*
Building: *Iris Bay*
Carpet Area: *2,742 sq.ft*
Rent: *AED 950K.*
Deposit: *6 Months Rent*
Condition: *Warmshell*

Call *Pratham* Mobile: *0501234567*
"""


PREMIUM_OFFICE_ON_RENT = """\
🔥 *_PREMIUM OFFICE ON RENT – BUSINESS BAY_* 🔥

📍_*Bay Square, Marasi Drive_*

🔹 *1000 Sqft Carpet + 450 Sqft Mezzanine*
🔹 *15 Ft Clear Height*

💼 *_Fully Furnished | Brand New Interior_*
✔ *Spacious Reception*
✔ *1 MD Cabin (Attached Washroom)*
✔ *3 Additional Cabins*
✔ *Meeting Room*
✔️ *+ 9 Seater Conference*
✔ *24 Workstations*
✔ *Pantry + Separate Washrooms*

🚗 *_Unlimited Parking | 24 Hrs Access_*

💰 *Rent: AED 300K*
💰 *_Deposit: AED 150K_*
📃 *5 Years Lock-in Possible*

⚡ *_Possession From 1st June_*

📲 *Call/WhatsApp: 050 123 4567*

_*(Serious Profile Required For Details)_*
"""


RESIDENTIAL_RENTAL_WITH_AVAILABILITY = """\
2 BHK on Rent
Available from 15 Aug
Semi Furnished
Rent: AED 110K
Call/WhatsApp: 0501234567
"""


def test_lodha_supremus_office_card_parses_as_commercial():
    parsed = parse_message(IRIS_BAY_OFFICE)

    assert parsed["intent"] == "COMMERCIAL"
    assert parsed["asset_type"] == "commercial"
    assert parsed["commercial_use_type"] == "office"
    assert parsed["fitout_status"] == "warm_shell"
    assert parsed["bhk"] is None
    assert parsed["configuration"] is None
    assert parsed["building_name"] == "Iris Bay"
    assert parsed["area_sqft"] == 2742.0
    assert parsed["price"] == 950.0
    assert parsed["price_unit"] == "K"
    assert parsed["micro_market"] == "Business Bay"


def test_lodha_supremus_commercial_promote_labels_use_use_type():
    from routers.ai_chat import _promote_headline

    parsed = parse_message(IRIS_BAY_OFFICE)
    headline = _promote_headline(parsed, "whatsapp")
    assert "Office" in headline
    assert "BHK" not in headline


def test_premium_andheri_office_message_parses_as_one_office_card():
    parsed = parse_message(PREMIUM_OFFICE_ON_RENT)

    assert parsed["intent"] == "COMMERCIAL"
    assert parsed["area_sqft"] == 1000.0
    assert parsed["price"] == 300.0
    assert parsed["price_unit"] == "K"
    assert parsed["micro_market"] == "Business Bay"


def test_residential_schema_fields_are_normalized_without_blocking():
    parsed = parse_message(RESIDENTIAL_RENTAL_WITH_AVAILABILITY)

    assert parsed["asset_type"] == "residential"
    assert parsed["property_type"] == "apartment"
    assert parsed["transaction_type"] == "rent"
    assert parsed["configuration"] == "2 BHK"
    assert parsed["furnishing_canonical"] == "semi_furnished"
    assert parsed["availability_status"] == "coming_soon"
    assert parsed["available_from"] == "15 Aug"
    assert parsed["price_model"] == "total"


def test_market_chat_parser_routes_office_space_to_commercial_intent():
    parsed = parse_market_search_request("any office space on rent in business bay?")

    assert parsed is not None
    assert parsed["intent"] == "COMMERCIAL"
    assert parsed["micro_markets"] == ["Business Bay"]


def test_known_locality_is_promoted_to_micro_market():
    location = parse_location("3 BHK for rent in Dubai Marina")

    assert location.locality == "Dubai Marina"
    assert location.micro_market == "Dubai Marina"


def test_numbered_requirement_prefix_is_not_a_building_name():
    location = parse_location("4. Buy: 3 BHK | JVC | AED 1.2-1.5M")

    assert location.micro_market == "JVC"
    assert location.building is None


def test_location_enrichment_uses_only_unambiguous_full_message_fallback():
    enriched = enrich_parsed_location(
        {"intent": "SELL", "building_name": "Gulf Towers"},
        "Gulf Towers",
        fallback_text="3 BHK for sale in JVC",
    )
    ambiguous = enrich_parsed_location(
        {"intent": "BUY"},
        "Requirement",
        fallback_text="Looking in Dubai Marina or JVC",
    )

    assert enriched["micro_market"] == "JVC"
    assert ambiguous.get("micro_market") is None


def test_primary_building_resolution_preserves_registry_micro_market(monkeypatch):
    monkeypatch.setattr(
        evidence.resolver,
        "CACHE",
        {
            "buildings": {
                "marina sail": {
                    "building_id": 99,
                    "canonical_name": "Marina Sail",
                    "area": "Dubai Marina",
                }
            }
        },
    )
    monkeypatch.setattr(evidence.resolver, "_load_registry", lambda: None)
    monkeypatch.setattr(
        evidence.resolver,
        "resolve",
        lambda *_args: (99, 0.95, "exact_name"),
    )

    resolved = resolve_parsed(
        {"building_name": "Marina Sail", "confidence": 0.9},
        "Marina Sail available for sale",
    )

    assert resolved["building_name"] == "Marina Sail"
    assert resolved["micro_market"] == "Dubai Marina"
