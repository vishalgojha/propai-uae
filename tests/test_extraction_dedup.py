"""Tests for the deterministic pre-LLM filters in ``extraction_dedup``.

Two guarantees are load-bearing and get the most coverage here:

* A skip must never drop a real listing. Over-eager filtering silently
  destroys inventory, which is worse than paying for an extra LLM call.
* A cache hit must never cross tenants, and must never serve a failed
  extraction as if it succeeded.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extraction_dedup import (  # noqa: E402
    _cache_hash,
    MIN_EXTRACTABLE_CHARS,
    SKIP_CHATTER,
    SKIP_EMPTY,
    SKIP_NO_SIGNAL,
    SKIP_PLACEHOLDER,
    SKIP_TOO_SHORT,
    cache_lookup,
    cache_store,
    content_hash,
    normalize_for_hash,
    should_skip,
)


# ── normalization / hashing ──────────────────────────────────────────


def test_identical_text_hashes_identically():
    a = "3 BHK for sale in Belscot Tower, JVC. Price 5.25 M."
    assert content_hash(a) == content_hash(a)


def test_whitespace_and_forward_banner_do_not_change_hash():
    """The same listing forwarded between groups must hit the cache."""
    original = "3 BHK for sale in Belscot Tower, JVC. Price 5.25 M."
    forwarded = "Forwarded: 3 BHK  for sale  in Belscot Tower,\nJVC. Price 5.25 M."
    assert content_hash(original) == content_hash(forwarded)


def test_zero_width_characters_do_not_change_hash():
    plain = "2 BHK rent Dubai Marina 85000 carpet 950 sqft"
    zwj = "2 BHK rent\u200b Dubai Marina 85000 carpet 950 sqft"
    assert content_hash(plain) == content_hash(zwj)


def test_different_price_produces_different_hash():
    """Near-identical text with a real difference must NOT share a cache entry."""
    a = "3 BHK Belscot Tower JVC. Price 5.25 M. Contact 501234567"
    b = "3 BHK Belscot Tower JVC. Price 6.25 M. Contact 501234567"
    assert content_hash(a) != content_hash(b)


def test_different_building_produces_different_hash():
    a = "2 BHK rent in Aristo Sapphire Al Barsha 250 k semi furnished"
    b = "2 BHK rent in Belscot Tower Al Barsha 250 k semi furnished"
    assert content_hash(a) != content_hash(b)


def test_normalize_collapses_whitespace_but_preserves_case_and_digits():
    out = normalize_for_hash("  2   BHK\n\nRs 45,00,000  ")
    assert out == "2 BHK Rs 45,00,000"


# ── skip filter: must NOT skip real listings ─────────────────────────


REAL_LISTINGS = [
    "FOR SALE - Belscot Tower, JVC. 3 BHK, 1500 carpet, 5.25 M. Suraj 552345678",
    "2 BHK for rent in JVC Cluster, carpet 950 sqft, rent 85000",
    "Office for rent Dubai Marina, Rizvi Chambers, 270 sq.ft, rent 48000 negotiable",
    "*Sole Mandate* Available 3bhk On Lease in Aristo Sapphire, Al Barsha, 250 k",
    "Requirement: client looking for 4 BHK in Juhu, budget 12 cr, ready possession only",
    "Shop available on lease at Hill Road, 400 sqft, deposit 50 k, rent 12 k",
    "Plot for sale 2000 sq mtr in Thane West, clear title, price on request",
    "ok 2 BHK available in Dubai Marina 85000 rent carpet 900 sqft call me",
]


@pytest.mark.parametrize("text", REAL_LISTINGS)
def test_real_listings_are_never_skipped(text):
    assert should_skip(text) is None, f"real listing was skipped: {text!r}"


def test_chatter_prefix_does_not_skip_a_real_listing():
    """'ok' starts the message but it still carries a listing — must not skip."""
    assert should_skip("ok 2 BHK available in Dubai Marina 85000 rent 900 sqft") is None


# ── skip filter: must skip non-listings ──────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        (None, SKIP_EMPTY),
        ("", SKIP_EMPTY),
        ("   ", SKIP_EMPTY),
        ("\n\t ", SKIP_EMPTY),
        ("[Image]", SKIP_PLACEHOLDER),
        ("[Video]", SKIP_PLACEHOLDER),
        ("image", SKIP_PLACEHOLDER),
        ("media omitted", SKIP_PLACEHOLDER),
        ("ok", SKIP_CHATTER),
        ("Thanks!", SKIP_CHATTER),
        ("good morning", SKIP_CHATTER),
        ("noted 👍", SKIP_CHATTER),
        ("haan", SKIP_CHATTER),
        ("This message was deleted", SKIP_CHATTER),
        ("please share", SKIP_CHATTER),
    ],
)
def test_non_listings_are_skipped_with_expected_reason(text, expected):
    assert should_skip(text) == expected


def test_short_message_without_signal_is_skipped_as_too_short():
    text = "call me later today pls"
    assert len(text) < MIN_EXTRACTABLE_CHARS
    assert should_skip(text) == SKIP_TOO_SHORT


def test_long_message_without_property_signal_is_skipped():
    text = (
        "Hi everyone, hope you are all doing well today. "
        "Just wanted to check in and say hello to the group."
    )
    assert len(text) >= MIN_EXTRACTABLE_CHARS
    assert should_skip(text) == SKIP_NO_SIGNAL


def test_skip_order_placeholder_beats_length():
    """'[Image]' is under the length floor but should report as placeholder."""
    assert should_skip("[Image]") == SKIP_PLACEHOLDER


# ── cache: fakes ─────────────────────────────────────────────────────


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filters: dict[str, object] = {}

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def limit(self, _n):
        return self

    def execute(self):
        rows = [
            r
            for r in self._rows
            if all(r.get(k) == v for k, v in self.filters.items())
        ]
        return SimpleNamespace(data=rows)


class _FakeTable:
    def __init__(self, rows, sink):
        self._rows = rows
        self._sink = sink

    def select(self, *a, **k):
        return _FakeQuery(self._rows).select(*a, **k)

    def upsert(self, payload, **kwargs):
        self._sink.append((payload, kwargs))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[payload]))


class _FakeClient:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.upserts: list = []
        self.rpc_calls: list = []

    def table(self, _name):
        return _FakeTable(self.rows, self.upserts)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=None))


class _FakeStorage:
    def __init__(self, rows=None):
        self.client = _FakeClient(rows)


TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"

PAYLOAD = {
    "extraction_source": "ai",
    "provider_used": "grid",
    "extractions": [{"listing_type": "sale", "bhk": 3}],
}


# ── cache: lookup ────────────────────────────────────────────────────


def test_cache_lookup_returns_payload_on_hit():
    text = "3 BHK Belscot Tower JVC 5.25 M carpet 1500 sqft"
    storage = _FakeStorage(
        [{"id": 7, "tenant_id": TENANT_A, "content_hash": _cache_hash(text),
          "extraction": PAYLOAD, "provider_used": "grid", "item_count": 1}]
    )
    assert cache_lookup(storage, TENANT_A, text) == PAYLOAD


def test_cache_lookup_miss_returns_none():
    storage = _FakeStorage([])
    assert cache_lookup(storage, TENANT_A, "2 BHK rent Dubai Marina 85000") is None


def test_cache_lookup_does_not_reuse_pre_versioned_output():
    text = "2 BHK rent Dubai Marina 85000 carpet 900 sqft"
    storage = _FakeStorage(
        [{"id": 9, "tenant_id": TENANT_A, "content_hash": content_hash(text),
          "extraction": PAYLOAD, "provider_used": "grid", "item_count": 1}]
    )

    assert cache_lookup(storage, TENANT_A, text) is None


def test_cache_lookup_is_tenant_scoped():
    """Tenant B must not read an entry cached by tenant A."""
    text = "3 BHK Belscot Tower JVC 5.25 M carpet 1500 sqft"
    storage = _FakeStorage(
        [{"id": 7, "tenant_id": TENANT_A, "content_hash": _cache_hash(text),
          "extraction": PAYLOAD, "provider_used": "grid", "item_count": 1}]
    )
    assert cache_lookup(storage, TENANT_A, text) == PAYLOAD
    assert cache_lookup(storage, TENANT_B, text) is None


def test_cache_lookup_records_hit():
    text = "3 BHK Belscot Tower JVC 5.25 M carpet 1500 sqft"
    storage = _FakeStorage(
        [{"id": 42, "tenant_id": TENANT_A, "content_hash": _cache_hash(text),
          "extraction": PAYLOAD}]
    )
    cache_lookup(storage, TENANT_A, text)
    assert storage.client.rpc_calls == [
        ("increment_extraction_cache_hit", {"p_id": 42})
    ]


def test_cache_lookup_without_tenant_returns_none():
    storage = _FakeStorage([])
    assert cache_lookup(storage, "", "2 BHK rent Dubai Marina 85000 carpet") is None


def test_cache_lookup_survives_backend_error():
    class _Boom:
        def table(self, _n):
            raise RuntimeError("supabase down")

    storage = SimpleNamespace(client=_Boom())
    assert cache_lookup(storage, TENANT_A, "2 BHK rent Dubai Marina 85000") is None


# ── cache: store ─────────────────────────────────────────────────────


def test_cache_store_writes_tenant_scoped_row():
    storage = _FakeStorage([])
    text = "2 BHK rent Dubai Marina 85000 carpet 900 sqft"
    cache_store(storage, TENANT_A, text, PAYLOAD, provider_used="grid")

    assert len(storage.client.upserts) == 1
    payload, kwargs = storage.client.upserts[0]
    assert payload["tenant_id"] == TENANT_A
    assert payload["content_hash"] == _cache_hash(text)
    assert payload["item_count"] == 1
    assert payload["provider_used"] == "grid"
    assert kwargs.get("on_conflict") == "tenant_id,content_hash"


def test_cache_store_ignores_failed_extraction():
    """A provider outage must not be cached as a real result."""
    storage = _FakeStorage([])
    cache_store(
        storage,
        TENANT_A,
        "2 BHK rent Dubai Marina 85000 carpet",
        {"extraction_source": "ai_unavailable", "extractions": [], "needs_review": True},
    )
    assert storage.client.upserts == []


def test_cache_store_ignores_reparse_preview():
    storage = _FakeStorage([])
    cache_store(
        storage,
        TENANT_A,
        "2 BHK rent Dubai Marina 85000 carpet",
        {"extraction_source": "reviewed_reparse_preview", "extractions": []},
    )
    assert storage.client.upserts == []


def test_cache_store_without_tenant_is_noop():
    storage = _FakeStorage([])
    cache_store(storage, "", "2 BHK rent Dubai Marina 85000 carpet", PAYLOAD)
    assert storage.client.upserts == []


def test_cache_roundtrip_forwarded_copy_hits_original():
    """End-to-end: store the original, then a forwarded copy must hit."""
    original = "3 BHK for sale in Belscot Tower, JVC. Price 5.25 M."
    forwarded = "Forwarded: 3 BHK  for sale  in Belscot Tower,\nJVC. Price 5.25 M."

    storage = _FakeStorage([])
    cache_store(storage, TENANT_A, original, PAYLOAD, provider_used="grid")
    stored, _ = storage.client.upserts[0]

    reader = _FakeStorage([{
        "id": 1,
        "tenant_id": stored["tenant_id"],
        "content_hash": stored["content_hash"],
        "extraction": stored["extraction"],
    }])
    assert cache_lookup(reader, TENANT_A, forwarded) == PAYLOAD
