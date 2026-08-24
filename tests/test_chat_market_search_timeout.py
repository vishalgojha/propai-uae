import json
from unittest.mock import Mock, patch

import ai_chat_engine
from routers import ai_chat as ai_chat_router


def _fake_choice(content: str):
    msg = Mock()
    msg.content = content
    choice = Mock()
    choice.message = msg
    resp = Mock()
    resp.choices = [choice]
    return resp


def test_conversational_reply_strips_think_blocks():
    raw = "Public intro. <think>secret chain of thought</think> Public outro."
    fake_client = Mock()
    fake_client.chat.completions.create.return_value = _fake_choice(raw)
    with patch.object(ai_chat_engine, "get_client", return_value=fake_client):
        with patch.object(ai_chat_engine, "_get_fallback_model", return_value="mock-model"):
            with patch.object(ai_chat_engine, "_log_usage"):
                reply = ai_chat_engine.get_conversational_reply(
                    [{"role": "user", "content": "hey"}],
                    api_key="k", model="m", base_url="u",
                )
    assert "<think>" not in reply.content
    assert "</think>" not in reply.content
    assert "secret chain of thought" not in reply.content
    assert "Public intro" in reply.content
    assert "Public outro" in reply.content


def test_conversational_reply_strips_lone_dangling_close_tag():
    raw = "Hey there! How can I help you today? Any property search on my mind?\n</think>Hey there! How can I help you today? Any property search on my mind?"
    fake_client = Mock()
    fake_client.chat.completions.create.return_value = _fake_choice(raw)
    with patch.object(ai_chat_engine, "get_client", return_value=fake_client):
        with patch.object(ai_chat_engine, "_get_fallback_model", return_value="mock-model"):
            with patch.object(ai_chat_engine, "_log_usage"):
                reply = ai_chat_engine.get_conversational_reply(
                    [{"role": "user", "content": "hey"}],
                    api_key="k", model="m", base_url="u",
                )
    assert "</think>" not in reply.content
    assert reply.content.count("Hey there!") == 1


def test_market_search_llm_call_respects_timeout():
    captured = {}

    class SlowCall:
        def __call__(self, *args, **kwargs):
            captured.update(kwargs)
            return _fake_choice("slow")

    fake_client = Mock()
    slow = SlowCall()
    fake_client.chat.completions.create = slow
    with patch.object(ai_chat_engine, "get_client", return_value=fake_client):
        result = ai_chat_engine._llm_market_search_request(
            "any 3 bhk for rent in dubai marina?",
            api_key="k",
            model="m",
            base_url="u",
        )
    assert result is None
    assert captured.get("timeout") == 20


def test_market_search_llm_timeout_falls_through_to_regex():
    fake_client = Mock()
    fake_client.chat.completions.create.side_effect = TimeoutError("simulated upstream timeout")
    with patch.object(ai_chat_engine, "get_client", return_value=fake_client):
        result = ai_chat_engine.parse_market_search_request(
            "any 3 bhk for rent in dubai marina?",
            api_key="k",
            model="m",
            base_url="u",
        )
    assert result is not None
    assert result.get("bhk") == "3"
    assert result.get("intent") == "RENT"


def test_market_search_allow_llm_false_skips_client():
    fake_client = Mock()
    with patch.object(ai_chat_engine, "get_client", return_value=fake_client) as get_client:
        result = ai_chat_engine.parse_market_search_request(
            "any 3 bhk for rent in dubai marina?",
            api_key="k",
            model="m",
            base_url="u",
            allow_llm=False,
        )
    get_client.assert_not_called()
    assert result is not None
    assert result.get("bhk") == "3"


def test_market_search_recognizes_jvc_and_absolute_aed_range():
    result = ai_chat_engine.parse_market_search_request(
        "Find 3 BHK for rent in JVC between AED 80K and 120K per month.",
        allow_llm=False,
    )

    assert result is not None
    assert result["micro_markets"] == ["JVC"]
    assert result["intent"] == "RENT"
    assert result["price_min"] == 80_000
    assert result["price_max"] == 120_000


def test_requirement_matching_query_uses_requirement_scope():
    result = ai_chat_engine.parse_market_search_request(
        "any 3 bhk for rent in difc any requirements that match?",
        allow_llm=False,
    )
    assert result is not None
    assert result.get("search_scope") == "requirements"
    payload = json.dumps({
        "type": "requirement_results",
        "total": 1,
        "results": [{
            "raw_message_id": 42,
            "intent": "RENT",
            "bhk": "3 BHK",
            "micro_market": "DIFC",
            "property_type": "residential",
            "broker_name": "Broker A",
        }],
    })
    response = ai_chat_engine.deterministic_market_response(result, payload)
    assert response["blocks"][0]["title"] == "Matching broker requirements"
    assert "Active listings" not in response["content"]


def test_execute_tool_json_is_module_scoped_not_local():
    con = Mock()
    con.execute.return_value.fetchone.return_value = (7,)
    con.commit = Mock()
    result = ai_chat_engine.execute_tool(
        "create_suggestion",
        {"agent": "test", "suggestion_type": "review", "title": "T", "description": "D"},
        sources={},
        db_path=con,
    )
    assert isinstance(result, str)
    assert "Suggestion created" in result


def test_execute_tool_market_search_returns_valid_json():
    class FakeCon:
        def execute(self, sql, params=None):
            if "COUNT(*)" in sql:
                class Row:
                    def fetchone(self):
                        return (0,)
                return Row()
            class Rows:
                def fetchall(self):
                    return []
            return Rows()

    result = ai_chat_engine.execute_tool(
        "market_search",
        {"intent": "RENT", "bhk": "3", "micro_markets": ["Dubai Marina"]},
        sources={},
        db_path=FakeCon(),
    )
    payload = json.loads(result)
    assert payload["type"] == "listing_results"
    assert payload["results"] == []


def test_normalize_real_phone_is_defined_and_works():
    assert ai_chat_engine._normalize_real_phone("+919876543210") == "9876543210"
    assert ai_chat_engine._normalize_real_phone("09876543210") == "9876543210"
    assert ai_chat_engine._normalize_real_phone("9876543210") == "9876543210"
    assert ai_chat_engine._normalize_real_phone("+1 555 1234") == ""
    assert ai_chat_engine._normalize_real_phone("") == ""


def test_deterministic_market_response_embeds_gfm_table():
    results = [
        {"building_name": "Marina Gate", "micro_market": "Dubai Marina", "bhk": "3 BHK",
         "price_formatted": "AED 1.8 M", "area_sqft": 1200, "furnishing": "Furnished",
            "broker_name": "Rajesh", "broker_phone": "+971 50 123 4567", "listing_id": 123,
         "last_seen": "2026-08-01T14:30:00+00:00"},
        {"building_name": None, "location_label": "Dubai Marina Walk", "bhk": "3 BHK",
         "price_formatted": "AED 2.1 M", "area_sqft": None, "furnishing": None,
         "broker_name": None, "broker_phone": ""},
    ]
    payload = json.dumps({"type": "listing_results", "total": 2, "results": results})
    resp = ai_chat_engine.deterministic_market_response(
        {"bhk": "3", "intent": "RENT", "micro_markets": ["Dubai Marina"]}, payload
    )
    content = resp["content"]
    assert "Found 2 active matches; showing 2 verified options from the shared broker network." in content
    assert "**Applied filters:** 3 BHK · RENT · Dubai Marina" in content
    assert "| Building | Locality | Type | Rent/Sale | Carpet | Furnishing | Broker | Last seen | WhatsApp |" in content
    assert "| --- | --- | --- | --- | --- | --- | --- | --- | --- |" in content
    assert "| Marina Gate | Dubai Marina | 3 BHK | AED 1.8 M | 1200 sqft | Furnished | Rajesh | 01 Aug 2026, 14:30 | Use the Contact broker button |" in content
    assert "| — | Dubai Marina Walk | 3 BHK | AED 2.1 M | — | — | — | — | — |" in content
    assert "Use the Contact broker button" in content
    assert "971501234567" not in content
    blocks = {b["type"]: b for b in resp["blocks"]}
    assert "listing_cards" in blocks
    assert blocks["listing_cards"]["items"] == results


def test_guard_against_raw_markup_swaps_clean_error():
    raw = "<!DOCTYPE html><html><body>Error 524: A timeout occurred</body></html>"
    assert ai_chat_router._guard_against_raw_markup(raw) == ai_chat_router._RAW_MARKUP_ERROR
    assert ai_chat_router._guard_against_raw_markup("normal reply") == "normal reply"


def test_wrap_chat_response_inbox_guards_markup():
    response = {"content": "<html><body>Gateway timeout</body></html>", "blocks": []}
    guarded = ai_chat_router._wrap_chat_response(response, is_inbox=True)
    assert guarded["content"] == ai_chat_router._RAW_MARKUP_ERROR
