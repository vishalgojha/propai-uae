from routers.common import _extract_save_requirement_query


def test_chat_save_to_my_deals_is_parsed_as_rental_requirement():
    result = _extract_save_requirement_query([
        {
            "role": "user",
            "content": "3 bhk for rent at X Business Bay for 110K per year save it to my deals",
        }
    ])

    assert result is not None
    assert result["intent"] == "RENT"
    assert result["bhk"] == "3 BHK"
    assert result["micro_market"] == "Business Bay"
    assert result["price_max"] == 110000
    assert result["source_text"].endswith("110K per year")


def test_follow_up_save_uses_previous_property_request():
    result = _extract_save_requirement_query([
        {"role": "user", "content": "3 bhk for rent at X Business Bay for 110K per year"},
        {"role": "user", "content": "save it to my deals"},
    ])

    assert result is not None
    assert result["intent"] == "RENT"
    assert result["bhk"] == "3 BHK"
    assert result["micro_market"] == "Business Bay"
    assert result["price_max"] == 110000
