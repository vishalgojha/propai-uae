import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deterministic_splitters import parse_chunk, parse_message


INLINE_BOLD_REPRO = """*Available Dubai Marina Brand new building*
*Crescent* 3bhk 1342 sqft pali hill price 12.12m
*Parishram* 4bhk - 2046 sqft carpet hiegher floor sea view pali hill price 31.32m
dubai marina *New brand building* *Penthouse*
5188sqft carpet with sea facing marina walk price 76.8m dubai marina
*Available for sale 2Bhk* Building Name: *Pioneer Heights* (al barsha)
Flat is of 950 carpet approx With 1 Car Parking Floor:2nd
Total floors in Building:14
Amenities in building: Gym,Play Room
Price 3.70m Kindly Call
Sunil - contact
Mahi - contact"""


def test_inline_bold_broadcast_splits_four_sale_listings():
    pattern_id, chunks = parse_message(INLINE_BOLD_REPRO)

    assert pattern_id == "inline_bold_header"
    assert len(chunks) == 4
    assert [chunk["building_name"] for chunk in chunks] == [
        "Crescent", "Parishram", "Penthouse", "Pioneer Heights"
    ]
    assert [chunk["bhk"] for chunk in chunks] == ["3 BHK", "4 BHK", None, "2 BHK"]
    assert [chunk["price"] for chunk in chunks] == [12.12, 31.32, 76.8, 3.7]
    assert [chunk["price_unit"] for chunk in chunks] == ["M"] * 4
    assert [chunk["intent"] for chunk in chunks] == ["SELL"] * 4
    assert all("Brand new building" in chunk["raw_payload"]["full_text"] for chunk in chunks)


def test_numbered_template_splits_into_three_chunks():
    text = """1. A Wing
3 BHK
1500 carpet
5.25 M

2. B Wing
4 BHK
1800 carpet
6.25 M

3. C Wing
2 BHK
900 carpet
2.5 M"""

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "numbered"
    assert len(chunks) == 3
    assert [chunk["bhk"] for chunk in chunks] == ["3 BHK", "4 BHK", "2 BHK"]
    assert [chunk["price_unit"] for chunk in chunks] == ["M", "M", "M"]


def test_markdown_numbered_headings_keep_building_and_locality_per_slice():
    text = """*1. Cayan Tower – Dubai Marina*
• 4 BHK
• 1,700 Sq. Ft. Carpet Area
• Fully Furnished
• Rent: AED 750 K/month

*2. Burj Vista – Downtown Dubai*
• 2 BHK
• 1,120 Sq. Ft.
• Rent: AED 325 K/month

*3. Fountain Views Estate – Business Bay*
• 1 BHK
• 650 Sq. Ft.
• Rent: AED 100 K/month"""

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "numbered"
    assert len(chunks) == 3
    assert [chunk["building_name"] for chunk in chunks] == [
        "Cayan Tower", "Burj Vista", "Fountain Views Estate"
    ]
    assert [chunk["location_raw"] for chunk in chunks] == [
        "Dubai Marina", "Downtown Dubai", "Business Bay"
    ]


def test_labelled_bildg_is_extracted_as_building():
    text = """*Avail 2 BHK flat for Rent*
Location : *Al Barsha*
*Bildg : Vardhaman Estate*
*Condition : Unfurnished*
*M. Rent : AED 100 K*"""

    parsed = parse_chunk(text)

    assert parsed["building_name"] == "Vardhaman Estate"
    assert parsed["location_raw"] == "Al Barsha"


def test_dash_separator_template_splits_into_two_chunks():
    text = """*3 BHK*
Rustomjee Paramount
24th floor
1350 sqft
semi furnished
5.25 M
──────────
*4 BHK*
Rustomjee Paramount
17th floor
1800 sqft
fully furnished
6.25 M"""

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "dash_separator"
    assert len(chunks) == 2
    assert chunks[0]["bhk"] == "3 BHK"
    assert chunks[1]["bhk"] == "4 BHK"
    assert chunks[0]["furnishing"] == "semi_furnished"


def test_independent_building_broadcast_does_not_leak_first_block_into_second():
    text = """*🟡GURUKIRPA REALTORS DUBAI | NEW ARRIVALS*

*INDEPENDENT BUILDING AVAILABLE ON RENT**

*⚪Area – 3 Lakhs sqft*
▪ 10 Floors Building
▪ 30,000 sqft Each Floor
▪ A+ Grade Building
▪ Ground Floor Parking
▪ Rent – AED 6 M (AED 200 psf)
▪ Al Quoz, Near Metro Station
▪ Business Bay

────────────

*⚪Charming Standalone Property*
▪ 1300 sq.ft
▪ Ground +1
▪ +800 sqft Terrace
▪ +500 sqft Open Space
▪ Surrounded By Lush Greenery
▪ Rent: AED 800 K
▪ Marina Gate, Dubai Marina"""

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "dash_separator"
    assert len(chunks) == 2
    assert "Al Quoz" in chunks[0]["raw_payload"]["slice_text"]
    assert "Charming Standalone Property" in chunks[1]["raw_payload"]["slice_text"]
    assert "Al Quoz" not in chunks[1]["raw_payload"]["slice_text"]


def test_emoji_bullet_template_splits_into_two_chunks():
    text = """🏡 2 BHK in DIFC
Tower A
1200 sqft
85 K

🏡 3 BHK in DIFC
Tower B
1500 sqft
1.25 M"""

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "emoji_bullet"
    assert len(chunks) == 2
    assert chunks[0]["bhk"] == "2 BHK"
    assert chunks[1]["price_unit"] == "M"


def test_bare_bhk_template_splits_without_separators():
    text = """3 BHK
Rustomjee Paramount
1350 carpet
5.25 M
4 BHK
Rustomjee Paramount
1800 carpet
6.25 M"""

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "bare_bhk_header"
    assert len(chunks) == 2
    assert [chunk["bhk"] for chunk in chunks] == ["3 BHK", "4 BHK"]


def test_run_on_inventory_splits_only_when_each_listing_has_a_price():
    text = (
        "Large One Bhk Sf Flat Marina Walk Partial Seaview Flat Second floor no Lift Asking 75 K "
        "2 Bhk Sf Flat Amrit Bldg King Salman Rd Pet Friendly Society Rent 40 K Neg "
        "Studio Sf Expat Quality Marina Walk Asking 35 K Ist Floor Open View"
    )

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "run_on_inventory"
    assert len(chunks) == 3
    assert [chunk["price"] for chunk in chunks] == [75.0, 40.0, 35.0]
    assert all("raw_payload" in chunk for chunk in chunks)


def test_run_on_configuration_range_is_not_split_into_fake_listings():
    text = "Need 2 BHK or 3 BHK in Dubai Marina, budget 75 K, family tenant only"

    pattern_id, chunks = parse_message(text)

    assert pattern_id is None
    assert chunks == []


def test_bare_bhk_accepts_house_emoji_between_marker_and_configuration():
    text = """*🏡 2 BHK for Rent*
Matai Mansion
1200 sqft
85 K
*🏡3Bhk's*
Another Mansion
1400 sqft
95 K"""

    pattern_id, chunks = parse_message(text, preferred_pattern="bare_bhk_header")

    assert pattern_id == "bare_bhk_header"
    assert len(chunks) == 2
    assert [chunk["bhk"] for chunk in chunks] == ["2 BHK", "3 BHK"]


def test_pushpin_bullet_template_splits_into_two_chunks():
    text = """RESIDENTIAL LEASE LISTINGS
📍 Rustomjee Paramount
2 BHK
85 K
📍 Another Mansion
3 BHK
1.25 M"""

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "emoji_bullet"
    assert len(chunks) == 2
    assert [chunk["bhk"] for chunk in chunks] == ["2 BHK", "3 BHK"]


def test_shared_bhk_header_is_inherited_but_never_becomes_a_listing():
    text = """_UPDATED 3BHK OUTRIGHT LIST_
📍 Marina Crown, Dubai Marina
1350 sqft
8.5 M
📍 Bluewaters, JBR
2880 sqft
40 M"""

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "emoji_bullet"
    assert len(chunks) == 2
    assert [chunk["bhk"] for chunk in chunks] == ["3 BHK", "3 BHK"]
    assert all("UPDATED 3BHK OUTRIGHT LIST" in chunk["raw_payload"]["full_text"] for chunk in chunks)


def test_markdown_wrapped_house_and_pushpin_markers_are_structural():
    text = """_🏡Matai Mansion_
_📍King Salman Road Marina_
_2 BHK_
85 K
_🏡Another Mansion_
_📍Jumeirah Beach Road Marina_
_3 BHK_
1.25 M"""

    pattern_id, chunks = parse_message(text, preferred_pattern="bare_bhk_header")

    assert pattern_id == "bare_bhk_header"
    assert len(chunks) == 2
    assert [chunk["bhk"] for chunk in chunks] == ["2 BHK", "3 BHK"]


def test_real_emoji_bullet_broadcast_keeps_missing_bullet_anchor_as_its_own_chunk():
    text = """🔑 RESIDENTIAL LEASE LISTINGS
━━━━━━━━━━━━━━━━━

📍 Dubai Marina – Vandana Building
(Near Medcare Hospital)
• 3 BHK | 1200 Sq.ft
• Fully Furnished
• 1 Parking
• Ample Storage
• 1 Km from DMCC Metro
💰 Rent: AED 120 K
💰 Deposit: AED 50 K
Only for family

📍 Business Bay – Brindaban Business Bay Tower
• 3 BHK Fully Furnished
• 1 Parking
• Immediate Possession
💰 Rent: AED 120 K
💰 Deposit: AED 30 K
Only for family

Dubai Marina – HDIL Metropolis
• 3 BHK Semi Furnished
• Approx. 1400 Sq.ft
• 28th Floor
• Full Sunlight & Open View
• 3 Bathrooms + Helper’s Bathroom
• Balconies in Hall, Kitchen & Bedrooms
• 2 Car Parks
💰 Rent: AED 210 K (Negotiable)
✅ Pure Veg Families Only

📍 Dubai Marina – Prime Rose Tower
(Murjan, Al Marsa Street)
• 3 BHK Semi Furnished
• 1300 Carpet
• Immediate Possession
💰 Rent: AED 120 K
💰 Deposit: 3 Months"""

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "emoji_bullet"
    assert len(chunks) == 4
    assert chunks[0]["building_name"] == "Dubai Marina – Vandana Building"
    assert chunks[1]["building_name"].startswith("Business Bay")
    assert "HDIL Metropolis" not in chunks[1]["building_name"]
    assert chunks[2]["building_name"] == "HDIL Metropolis"
    assert chunks[2]["location_raw"] == "Dubai Marina"
    assert chunks[3]["building_name"].startswith("Dubai Marina – Prime Rose Tower")


def test_real_dash_separated_anchor_line_populates_building_and_location():
    text = """🔑 PREMIUM RESIDENTIAL LEASE LISTINGS
━━━━━━━━━━━━━━━━━━
📍 Dubai Marina – Raheja Classic
• 3 BHK Lavish Fully Furnished Apartment
• 1150 Sq.ft Carpet
• 1 Parking
• Lower Floor
• Internal Garden View
💰 Rent: ₹1.85 Lac (Final)

📍 Dubai Marina – HDIL Metropolis
• 3 BHK Semi Furnished Apartment
• Approx. 1400 Sq.ft
• 28th Floor
• Full Sunlight & Open View
• 3 Bathrooms + Helper’s Bathroom
• Balconies in Hall, Kitchen & Bedrooms
• 2 Car Parks
💰 Rent: AED 210 K for family
For Bachelor-225 k
✅ Pure Veg Families Only"""

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "emoji_bullet"
    assert len(chunks) == 2
    assert chunks[0]["building_name"] == "Raheja Classic"
    assert chunks[0]["location_raw"] == "Dubai Marina"
    assert chunks[1]["building_name"] == "HDIL Metropolis"
    assert chunks[1]["location_raw"] == "Dubai Marina"


def test_real_611019_third_listing_keeps_full_location_line():
    text = """*🏡 2 BHK for Rent*
Hill Dream, Marina Walk, Dubai Marina
📐 Carpet Area: 750 sq. ft.
🚗 Parking: 1
🛋️ Condition: Semi Furnished
💰 Rent: AED 180 K

*📞 For Inspection & More Details:*
👤 Rajesh: 0501234567
👤 Nandu: 0552345678
📱 0543456789

*2 BHK for Rent*
👉 Building: Bay Central
👉 Location: Murjan, Dubai Marina
👉 Carpet : 850 Sq.Ft.
👉 Condition : Fully Furnished
👉 Rent : 250 K

*📞 For More Details*
👤 Rajesh 0501234567
👤 Nandu : 0553456789
📱 0543456789

*3 BHK for Rent*
👉 Location : Near Marina Mall, Dubai Marina
👉 Carpet : 1000 Sq.Ft.
👉 Condition: Semi Furnished
👉 Parking : 1
👉 Rent : 1.85 Lac"""

    pattern_id, chunks = parse_message(text)

    assert pattern_id == "bare_bhk_header"
    assert len(chunks) == 3
    assert chunks[2]["building_name"] is None
    assert "Near Marina Mall, Dubai Marina" in chunks[2]["location_raw"]
