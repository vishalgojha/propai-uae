from extraction import _rescue_core_fields


def test_rescues_common_broker_shorthand_when_model_omits_core_fields():
    parsed = {"bhk": None, "price": None, "area_sqft": None, "intent": "SELL", "building_name": None}

    result = _rescue_core_fields(
        parsed,
        "2BHK Dubai Marina\nMarina Sail\nCarpet 850 sqft\nPrice 1.25M",
    )

    assert result["bhk"] == "2 BHK"
    assert result["area_sqft"] == 850
    assert result["price"] == 1_250_000
    assert result["total_asking_price"] == 1_250_000
    assert result["building_name"] == "Marina Sail"


def test_media_placeholders_never_become_building_names():
    parsed = {
        "bhk": None,
        "price": None,
        "area_sqft": None,
        "building_name": "[Document]",
        "intent": "SELL",
    }

    result = _rescue_core_fields(parsed, "[Document]")

    assert result["building_name"] is None
    assert result["bhk"] is None
    assert result["price"] is None
    assert result["area_sqft"] is None


def test_rescues_plain_rupee_and_bedroom_variants():
    parsed = {"bhk": None, "price": None, "area_sqft": None, "intent": "RENT"}

    result = _rescue_core_fields(
        parsed,
        "3 bedrooms in Khar West, 1,30,000 per month, 1,420 sft",
    )

    assert result["bhk"] == "3 BHK"
    assert result["price"] == 130000
    assert result["monthly_rent"] == 130000
    assert result["area_sqft"] == 1420
