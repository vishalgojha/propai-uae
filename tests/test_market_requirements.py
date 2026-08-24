import json

import ai_chat_engine
from storage.supabase import _is_market_requirement, _price_to_rupees


class _Query:
    def __init__(self, table_name, rows, calls):
        self.table_name = table_name
        self.rows = rows
        self.calls = calls

    def select(self, *args, **kwargs):
        self.calls.append((self.table_name, "select", args, kwargs))
        return self

    def eq(self, *args):
        self.calls.append((self.table_name, "eq", args))
        return self

    def in_(self, *args):
        self.calls.append((self.table_name, "in", args))
        return self

    def ilike(self, *args):
        self.calls.append((self.table_name, "ilike", args))
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args):
        return self

    def execute(self):
        return type("Result", (), {"data": self.rows})()


class _Client:
    def __init__(self):
        self.calls = []
        self.tables = {
            "brokers": [],
            "hidden_brokers": [],
            "hidden_market_items": [],
            "requirements_unified": [{
                "id": 7,
                "fingerprint": "requirement:tenant-a:99",
                "raw_message_id": 99,
                "intent": "BUY",
                "transaction_type": "rent",
                "bhk": "3 BHK",
                "price_min": 200000,
                "price_max": 300000,
                "area_sqft": 1200,
                "location_label": "Bandra West",
                "building_name": None,
                "landmark_name": None,
                "micro_market": "Bandra West",
                "broker_name": "Broker A",
                "broker_phone": "9820056180",
                "confidence": 0.92,
                "first_seen": "2026-08-03T00:00:00+00:00",
                "last_seen": "2026-08-03T00:00:00+00:00",
                "created_at": "2026-08-03T00:00:00+00:00",
            }],
        }

    def table(self, name):
        self.calls.append((name, "table"))
        return _Query(name, self.tables.get(name, []), self.calls)


def test_requirement_projection_classifies_demand_and_normalizes_budget():
    assert _is_market_requirement({"message_type": "requirement"})
    assert _is_market_requirement({"message_type": "buy"})
    assert not _is_market_requirement({"message_type": "sale", "intent": "SELL"})
    assert _price_to_rupees(850, "k") == 850000
    assert _price_to_rupees(1.5, "m") == 1500000


def test_requirement_search_reads_market_requirements_and_returns_rows():
    client = _Client()
    payload = json.loads(ai_chat_engine._rest_requirement_search(
        client,
        {
            "search_scope": "requirements",
            "intent": "RENT",
            "bhk": "3",
            "micro_markets": ["Bandra West"],
            "limit": 10,
        },
        tenant_id="tenant-a",
    ))

    assert payload["type"] == "requirement_results"
    assert payload["total"] == 1
    assert payload["results"][0]["broker_name"] == "Broker A"
    assert payload["results"][0]["price_min"] == 200000
    assert payload["results"][0]["price_max"] == 300000
    assert ("requirements_unified", "table") in client.calls
    assert ("parsed_output_unified", "table") not in client.calls
