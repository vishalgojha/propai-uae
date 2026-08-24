"""Deterministic pre-LLM filters for the extraction pipeline.

Two independent savings, both applied before any provider call:

1. ``should_skip`` — messages that cannot contain a listing (empty,
   ultra-short, pure chatter, no property signal) never reach an LLM.

2. ``content_hash`` + ``extraction_cache`` — identical message bodies are
   extracted once per tenant and reused. 87.6% of the queue is duplicate
   text: the same listing forwarded across many broker groups.

Neither path guesses at content. A skip is a refusal to extract, not an
inference about what the message meant, and a cache hit reuses a result
derived from byte-identical text. Per ``docs/DATA_QUALITY.md`` every
raw_message still keeps its own row and its own provenance — dedup only
avoids paying twice for the same model output, it never merges evidence.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

# ── Normalization ────────────────────────────────────────────────────

# Collapse the things that differ between forwards of the same listing but
# carry no semantic weight: whitespace runs, zero-width joiners, and the
# "Forwarded"/"Fwd" banners WhatsApp clients prepend.
_WS_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_FORWARD_BANNER_RE = re.compile(
    r"^\s*(?:>+\s*)?(?:forwarded(?:\s+many\s+times)?|fwd|fw)\s*[:\-–]?\s*",
    re.IGNORECASE,
)


def normalize_for_hash(text: str) -> str:
    """Return a canonical form of ``text`` for content-addressed dedup.

    Deliberately conservative: case and punctuation are preserved because
    "2 BHK" vs "2 bhk" is the same listing but "Rs 45,00,000" vs
    "Rs 4,500,000" is not, and we cannot tell those apart after
    aggressive stripping. Only unambiguously-cosmetic differences are
    normalized away.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    out = _ZERO_WIDTH_RE.sub("", out)
    out = _FORWARD_BANNER_RE.sub("", out)
    out = _WS_RE.sub(" ", out)
    return out.strip()


def content_hash(text: str) -> str:
    """sha256 of the normalized body. Stable across processes and runs."""
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()


# ── Pre-LLM skip filter ──────────────────────────────────────────────

MIN_EXTRACTABLE_CHARS = 30

# Pure conversational filler. Anchored and whole-string: a message must be
# *entirely* chatter to match, so "ok 2 BHK available Bandra" is never skipped.
_CHATTER_RE = re.compile(
    r"^(?:"
    r"ok(?:ay)?|thanks?|thank\s+you|ty|tysm|yes|yep|no|nope|hi|hello|hey|"
    r"good\s+(?:morning|afternoon|evening|night)|gm|gn|gud\s+mrng|"
    r"welcome|noted|done|sure|k|kk|hmm+|haan|ha|nahi|theek|thik|ji|"
    r"congrats|congratulations|great|nice|super|perfect|"
    r"please\s+share|share\s+details?|any\s+update|updates?|"
    r"deleted\s+this\s+message|this\s+message\s+was\s+deleted|"
    r"joined\s+using\s+this\s+group|left|removed|added"
    r")"
    r"[\s\.,!?👍🙏😊😄🎉❤️✅➕]*$",
    re.IGNORECASE,
)

# Media-only placeholders written by the ingestor when a message had no text.
_PLACEHOLDER_RE = re.compile(
    r"^\s*\[?\s*(?:image|video|audio|document|sticker|gif|photo|voice|"
    r"media\s+omitted|no\s+text)\s*\]?\s*$",
    re.IGNORECASE,
)

# A real-estate message says *something* about property, money, or intent.
# Broad on purpose — this is a floor, not a classifier. Anything matching
# goes to the LLM; only messages with zero signal are skipped.
_PROPERTY_SIGNAL_RE = re.compile(
    r"(?:"
    r"\d\s*(?:bhk|br\b|rk\b)|\bbhk\b|\bbr\b|\brk\b|"
    r"sq\.?\s?ft|sqft|sq\.?\s?mtr|carpet|built\s*up|saleable|"
    r"\brent\b|\bsale\b|\blease\b|resale|\bbuy\b|\bsell\b|"
    r"aed\b|dhs\b|dirhams?\b|cheques?\b|chqs?\b|ejari|"
    r"\bprice\b|budget|deposit|brokerage|"
    # "wanted"/"need" only count in their property sense — bare "just wanted
    # to say hi" is chatter, "wanted: 2 BHK" is a requirement.
    r"available|require(?:ment|d)?\b|looking\s+for|"
    r"\bwanted\s*[:\-]|\bwanted\s+(?:a|an|\d|one|two|three|flat|shop|office|"
    r"apartment|property|space|bhk)\b|"
    r"\bneed\s+(?:a|an|\d|one|two|three|flat|shop|office|apartment|property|"
    r"space|bhk|urgent)\b|"
    r"\bflat\b|apartment|\bshop\b|office|villa|bungalow|\bplot\b|"
    r"penthouse|studio|duplex|godown|warehouse|showroom|townhouse|"
    # PG / co-living inventory: priced per bed, so it often carries no BHK,
    # no sqft and no currency marker — "Hall triple sharing 10k" is a real listing.
    r"\bpg\b|paying\s+guest|co\s*-?\s*living|hostel|"
    r"(?:single|double|triple|twin|quad)\s+sharing|sharing\s+basis|"
    r"\b\d+\s*[km]\b|"
    r"furnished|unfurnished|vacant|possession|"
    r"floor\b|tower|wing\b|society|building|project"
    r")",
    re.IGNORECASE,
)

SKIP_EMPTY = "empty"
SKIP_PLACEHOLDER = "media_placeholder"
SKIP_TOO_SHORT = "under_min_chars"
SKIP_CHATTER = "chatter"
SKIP_NO_SIGNAL = "no_property_signal"


def should_skip(text: str | None) -> str | None:
    """Return a skip reason, or ``None`` when the message deserves an LLM call.

    Order matters: cheapest and most certain checks first. Every reason is
    a distinct string so the worker can report which filter fired and we
    can audit whether any filter is over-eager.
    """
    if text is None:
        return SKIP_EMPTY
    stripped = text.strip()
    if not stripped:
        return SKIP_EMPTY
    if _PLACEHOLDER_RE.match(stripped):
        return SKIP_PLACEHOLDER
    if _CHATTER_RE.match(stripped):
        return SKIP_CHATTER
    if len(stripped) < MIN_EXTRACTABLE_CHARS:
        return SKIP_TOO_SHORT
    if not _PROPERTY_SIGNAL_RE.search(stripped):
        return SKIP_NO_SIGNAL
    return None


# ── Cache access ─────────────────────────────────────────────────────

_CACHE_TABLE = "extraction_cache"
# The cache stores model output, so the key must change when source-boundary
# semantics change. Otherwise identical forwards can keep serving an output
# produced before a parser fix was deployed.
EXTRACTION_CACHE_VERSION = "source-slice-v2"


def _cache_hash(text: str) -> str:
    # Normalize the message before adding the version marker. Adding the
    # marker first would stop ``Forwarded:`` removal from matching at the
    # beginning of the actual WhatsApp text.
    return content_hash(f"{normalize_for_hash(text)}\n{EXTRACTION_CACHE_VERSION}")


def cache_lookup(storage, tenant_id: str, text: str) -> dict[str, Any] | None:
    """Return a cached ``ai_extract`` payload for this exact text, or None.

    Tenant-scoped: a hash collision across tenants must never leak one
    tenant's extracted content into another's pipeline.
    """
    if not storage or not tenant_id:
        return None
    digest = _cache_hash(text)
    try:
        res = (
            storage.client.table(_CACHE_TABLE)
            .select("id, extraction, provider_used, item_count")
            .eq("tenant_id", tenant_id)
            .eq("content_hash", digest)
            .limit(1)
            .execute()
        )
    except Exception:
        # A cache miss and a cache outage are the same thing to the caller:
        # fall through to a real extraction rather than failing the message.
        return None
    rows = getattr(res, "data", None) or []
    if not rows:
        return None
    row = rows[0]
    payload = row.get("extraction")
    if not isinstance(payload, dict):
        return None
    _record_hit(storage, row.get("id"))
    return payload


def cache_store(
    storage,
    tenant_id: str,
    text: str,
    payload: dict[str, Any],
    provider_used: str | None = None,
) -> None:
    """Persist a successful extraction for reuse by identical later messages.

    Only genuine model output is cached. Failures and stubs are skipped so a
    transient provider outage never poisons every future copy of that text.
    """
    if not storage or not tenant_id or not isinstance(payload, dict):
        return
    if payload.get("extraction_source") != "ai":
        return
    items = payload.get("extractions") or []
    try:
        storage.client.table(_CACHE_TABLE).upsert(
            {
                "tenant_id": tenant_id,
                "content_hash": _cache_hash(text),
                "extraction": payload,
                "provider_used": provider_used or payload.get("provider_used"),
                "item_count": len(items) if isinstance(items, list) else 0,
            },
            on_conflict="tenant_id,content_hash",
        ).execute()
    except Exception:
        # Caching is an optimisation. Never let it break extraction.
        pass


def _record_hit(storage, row_id: Any) -> None:
    """Best-effort hit accounting so real dedup savings are measurable."""
    if not row_id:
        return
    try:
        storage.client.rpc(
            "increment_extraction_cache_hit", {"p_id": row_id}
        ).execute()
    except Exception:
        pass
