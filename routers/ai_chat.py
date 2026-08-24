"""AI chat, query, and promotion routes."""
import base64
import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import json as _json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from routers.common import (
    storage, require_user, get_tenant_context,
    _doubleword_error_response, _workspace_provider_candidates,
    _resolve_active_organization_id, _extract_save_requirement_query,
)
from llm import ProviderConfigurationError
from extraction_quality import building_name_problem

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["ai"])

# Placeholders — wired by app.py after real definitions
get_embedder = None
_today_prefix = None

# ── Lazy-imports ───────────────────────────────────────────────────
from lab import ai_chat_engine as chat_engine
from lab.config import ENABLE_AI_PROMO, ENABLE_META_PUBLISHING


# ── SSE helpers ────────────────────────────────────────────────────

def _sse_event(data: dict | str) -> str:
    """Format a single SSE event."""
    payload = _json.dumps(data, default=str) if isinstance(data, dict) else str(data)
    return f"data: {payload}\n\n"


_RAW_MARKUP_PREFIX = re.compile(r"^\s*(?:<!doctype|<html|<head|<body)", re.IGNORECASE)
_RAW_MARKUP_ERROR = "Something went wrong generating a response. Please try again."


def _guard_against_raw_markup(content: str) -> str:
    """Never render an infra-level error page (e.g. a Cloudflare 524 HTML page)
    as assistant text. If the payload looks like markup rather than a model
    reply, swap in a clean generic error and log the raw payload for debugging.
    """
    if _RAW_MARKUP_PREFIX.match(content or ""):
        _logger.error(
            "Blocked non-LLM markup from reaching chat UI: %.500s",
            content,
        )
        return _RAW_MARKUP_ERROR
    return content


def _to_sse_chunks(response: dict) -> str:
    """Convert a workspace response dict into SSE text for DefaultChatTransport.

    Yields text-start, text-delta, data-*, and text-end events. The chat
    surface now serializes structured blocks into Markdown tables on the
    client, so the data events remain available as transport metadata.
    """
    msg_id = f"msg-{uuid.uuid4().hex[:8]}"
    content = _guard_against_raw_markup(str(response.get("content") or "").strip())
    blocks = response.get("blocks") or []
    source_mode = str(response.get("source_mode") or "").strip()

    # text-start
    yield _sse_event({"type": "text-start", "id": msg_id})

    if source_mode:
        yield _sse_event({
            "type": "data-chat_context",
            "id": f"context-{msg_id}",
            "data": {"source_mode": source_mode},
        })

    status_steps = response.get("status_steps") or []
    if status_steps:
        yield _sse_event({
            "type": "data-agent_status",
            "id": f"status-{msg_id}",
            "data": {"steps": status_steps},
        })

    # Emit activity first so the UI can show progress before the final answer.
    for block in blocks:
        block_type = block.get("type", "")
        if block_type == "activity":
            yield _sse_event({
                "type": "data-activity",
                "id": f"activity-{msg_id}",
                "data": block,
            })

    # text-delta from content string
    if content:
        yield _sse_event({"type": "text-delta", "delta": content, "id": msg_id})

    # Emit each remaining block as appropriate
    for block in blocks:
        block_type = block.get("type", "")
        if block_type in {"listing_cards", "buyer_cards", "broker_cards", "matching_buyers"}:
            yield _sse_event({
                "type": f"data-{block_type}",
                "id": f"cards-{msg_id}",
                "data": block,
            })
        elif block_type == "confirmation":
            yield _sse_event({
                "type": "data-confirmation",
                "id": f"confirmation-{msg_id}",
                "data": block,
            })
        elif block_type in ("summary", "empty_state", "error_state", "greeting"):
            body = block.get("body") or ""
            if body and body != content:
                yield _sse_event({"type": "text-delta", "delta": f"\n\n{body}", "id": msg_id})

    # text-end
    yield _sse_event({"type": "text-end", "id": msg_id})
    yield _sse_event("[DONE]")


def _wrap_sse(response: dict) -> StreamingResponse:
    """Wrap a workspace response as an SSE StreamingResponse."""
    return StreamingResponse(
        _to_sse_chunks(response),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "x-vercel-ai-ui-message-stream": "v1",
            "x-accel-buffering": "no",
        },
    )


def _wrap_chat_response(response: dict, is_inbox: bool = False):
    """Return SSE for /chat, plain JSON for inbox AI panel."""
    if is_inbox:
        guarded = dict(response)
        guarded["content"] = _guard_against_raw_markup(str(guarded.get("content") or "").strip())
        return guarded
    return _wrap_sse(response)


def _coerce_activity_entries(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        item: dict[str, Any] = {}
        for key in ("tool", "status", "summary", "provider", "url", "title", "detail", "browser_session_id"):
            value = entry.get(key)
            if value not in (None, ""):
                item[key] = value
        if item:
            normalized.append(item)
    return normalized


def _build_activity_block(response: dict) -> dict | None:
    status_steps = [str(step).strip() for step in (response.get("status_steps") or []) if str(step).strip()]
    trace = response.get("trace") if isinstance(response.get("trace"), dict) else {}
    trace_actions = _coerce_activity_entries(trace.get("actions"))
    route = str(trace.get("route") or "").strip()
    if not status_steps and not trace_actions and route not in {"supabase_agent", "browser_permission_prompt"}:
        return None

    title = "Agent activity"
    if route == "browser_permission_prompt":
        title = "Browser check"
    elif route == "supabase_agent":
        title = "Live agent work"

    body = response.get("content") or ""
    if route == "supabase_agent" and status_steps:
        body = status_steps[0]
    elif route == "browser_permission_prompt":
        body = "Waiting for browser permission before continuing."
    elif not body and status_steps:
        body = status_steps[0]

    block: dict[str, Any] = {
        "type": "activity",
        "title": title,
        "body": str(body).strip(),
    }
    if status_steps:
        block["steps"] = status_steps
    if trace_actions:
        block["events"] = trace_actions

    trace_payload: dict[str, Any] = {}
    if isinstance(trace.get("sources"), list) and trace.get("sources"):
        trace_payload["sources"] = trace.get("sources")
    if trace.get("last_updated"):
        trace_payload["last_updated"] = trace.get("last_updated")
    if isinstance(trace.get("notes"), list) and trace.get("notes"):
        trace_payload["notes"] = trace.get("notes")
    if trace_actions:
        trace_payload["actions"] = trace_actions
    def _latest_action_value(key: str) -> Any:
        for action in reversed(trace_actions):
            value = action.get(key)
            if value not in (None, ""):
                return value
        return None

    for key in ("route", "browser_session_id", "browser_provider", "browser_url", "browser_title"):
        value = trace.get(key)
        if value in (None, ""):
            value = _latest_action_value(key)
        if value not in (None, ""):
            trace_payload[key] = value
    if trace_payload:
        block["trace"] = trace_payload
    return block


async def _market_search_with_summary(
    query: dict,
    sources: dict,
    providers: list[dict],
    user_text: str,
    tenant_id: str | None,
) -> dict:
    """Run relaxed market queries through the verified listings index.

    Relaxed parsing is still a market search, not a conversational fallback.
    Keep it deterministic and use the same parsed-listings search and response
    renderer as strict inventory queries.
    """
    search_result = await asyncio.to_thread(
        chat_engine.execute_tool,
        "market_search",
        query,
        sources,
        getattr(storage, "db", None),
        tenant_id,
    )
    return chat_engine.deterministic_market_response(query, search_result, sources)


async def _current_listing_search(query: dict, tenant_id: str | None, user_id: str | None) -> dict:
    """Run a parsed inventory query through the current Supabase agent tool.

    The old ``market_search`` implementation depends on the legacy CSV/SQLite
    source bundle. The workspace agent's inventory contract is instead
    ``agent_tools.search_listings`` against the tenant-scoped typed tables.
    """
    from agent_tools import execute_tool as execute_agent_tool

    markets = [str(value).strip() for value in (query.get("micro_markets") or []) if str(value).strip()]
    locality = markets[0] if markets else ""
    intent = str(query.get("intent") or "RENT").upper()
    listing_type = "sale" if intent in {"SELL", "SALE", "BUY", "PURCHASE"} else "rent"
    property_type = "commercial" if intent == "COMMERCIAL" else "residential"
    page_offset = max(0, int(query.get("offset") or 0))
    tool_args = {
        "locality": locality,
        "listing_type": listing_type,
        "property_type": property_type,
        "limit": 11,
        "offset": page_offset,
    }
    if query.get("bhk") not in (None, ""):
        tool_args["bhk"] = query["bhk"]
    if query.get("price_min") is not None:
        tool_args["price_min"] = query["price_min"]
    if query.get("price_max") is not None:
        tool_args["price_max"] = query["price_max"]

    result = await asyncio.to_thread(
        execute_agent_tool,
        "search_listings",
        tool_args,
        storage.client,
        tenant_id,
        user_id=user_id,
    )
    if result.get("status") != "ok":
        raise RuntimeError(result.get("error") or "Supabase listing search failed")

    requested_localities = [" ".join(str(value).casefold().split()) for value in markets]
    requested_bhk = str(query.get("bhk") or "").strip()
    maximum_price = query.get("price_max")
    minimum_price = query.get("price_min")

    def row_matches_filters(row: dict) -> bool:
        """Reject fuzzy SQL hits that do not satisfy the user's actual filters."""
        if requested_localities:
            location_values = [
                row.get("micro_market"),
                row.get("locality_resolved"),
                row.get("locality_raw"),
            ]
            location_text = " ".join(
                " ".join(str(value).casefold().split())
                for value in location_values
                if value not in (None, "")
            )
            if not any(locality in location_text for locality in requested_localities):
                return False
        if requested_bhk not in ("", "None"):
            try:
                if float(row.get("bhk")) != float(requested_bhk):
                    return False
            except (TypeError, ValueError):
                return False
        try:
            price_value = float(row.get("price"))
            if minimum_price is not None and price_value < float(minimum_price):
                return False
            if maximum_price is not None and price_value > float(maximum_price):
                return False
        except (TypeError, ValueError):
            return False
        return True

    fetched_rows = result.get("results") or []
    has_more = len(fetched_rows) > 10
    normalized = []
    for row in fetched_rows[:10]:
        if not row_matches_filters(row):
            continue
        price = row.get("price")
        listing_intent = "SELL" if listing_type == "sale" else "RENT"
        normalized.append({
            "listing_id": row.get("listing_id") or row.get("id"),
            "fingerprint": row.get("fingerprint"),
            "intent": listing_intent,
            "property_type": property_type,
            "asset_type": property_type,
            "bhk": row.get("bhk"),
            "price": price,
            "price_unit": "AED",
            "price_formatted": chat_engine.fmt_listing_price(price, "AED", listing_intent),
            "area_sqft": row.get("carpet_area_sqft"),
            "furnishing": row.get("furnishing"),
            "location_label": row.get("micro_market"),
            "building_name": row.get("building_name") or "On Request",
            "landmark_name": row.get("landmark_name"),
            "micro_market": row.get("micro_market"),
            "broker_name": row.get("broker_name"),
            "broker_phone": row.get("broker_phone"),
            "confidence": row.get("extraction_confidence"),
            "market_scope": "shared",
            "first_seen": row.get("created_at"),
            "last_seen": row.get("created_at"),
            "raw_message_id": row.get("raw_message_id"),
            "source": f"{property_type}_{listing_type}_listings",
            "source_schema": f"{property_type}_{listing_type}_listings",
        })

    payload = _json.dumps({
        "type": "listing_results",
        "total": len(normalized),
        "results": normalized,
        "showing": len(normalized),
        "offset": 0,
        "has_more": has_more,
        "remaining": 1 if has_more else 0,
    }, default=str)
    shared_query = dict(query)
    shared_query["market_scope"] = "shared"
    shared_query["property_type"] = property_type
    response = chat_engine.deterministic_market_response(
        shared_query,
        payload,
        {"shared_marketplace": True},
    )
    response["trace"] = {
        **(response.get("trace") or {}),
        "inventory_scope": "shared_network",
        "tenant_filter": False,
    }
    return response


def _preferred_workspace_provider(tenant_id: str | None) -> dict:
    """Return the deployment-managed provider shown by the AI config API."""
    return (_workspace_provider_candidates(tenant_id) or [{
        "api_key": "", "model": "", "base_url": "", "provider": "none"
    }])[0]


async def _run_with_provider_failover(call_factory, providers: list[dict], timeout: float = 90):
    """Run one synchronous LLM operation against the rotating provider pool."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_error = None
    for index, provider in enumerate(providers):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda p=provider: call_factory(p)),
                timeout=min(25, remaining),
            )
            result_content = result.get("content", "") if isinstance(result, dict) else getattr(result, "content", "")
            if result is None or not result_content:
                raise RuntimeError("provider returned an empty response")
            return result
        except Exception as exc:
            last_error = exc
            _logger.warning(
                "AI provider attempt %d/%d failed (%s): %s",
                index + 1, len(providers), provider.get("provider", "unknown"), exc,
            )
    if last_error:
        raise last_error
    raise ProviderConfigurationError("No complete LLM provider is configured")


# ═══════════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════════

class QueryRequest(BaseModel):
    query: str
    k: int = 10


class PromoteRequest(BaseModel):
    observation_id: int
    channel: str = "whatsapp"
    use_ai: bool = False
    fields: dict | None = None
    api_key: str = ""


class ChatRequest(BaseModel):
    messages: list[dict]
    api_key: str = ""
    model: str = ""
    session_id: str = ""
    broker_phone: str = ""
    source: str = ""
    browser_approval_token: str = ""
    persist_user_turn: bool = True


class ConfirmAgentActionRequest(BaseModel):
    confirmation_token: str


class ConfirmBrowserActionRequest(BaseModel):
    confirmation_token: str


def _normalize_chat_source(source: str) -> str:
    source = (source or "").strip().lower()
    if source in {"parsed", "inbox"}:
        return source
    return "parsed"


def _chat_session_slug(session: dict) -> str:
    """Create a readable, stable history URL without storing a second key."""
    title = re.sub(r"[^a-z0-9]+", "-", str(session.get("title") or "chat").lower()).strip("-")
    title = title[:60].strip("-") or "chat"
    return f"{title}--{session.get('id')}"


def _session_response(session: dict) -> dict:
    """Keep UUID ownership internal while giving the client a shareable slug."""
    result = dict(session)
    result["slug"] = _chat_session_slug(result)
    return result


def _annotate_chat_response(response: dict, source_mode: str) -> dict:
    annotated = dict(response or {})
    annotated["source_mode"] = source_mode
    activity_block = _build_activity_block(annotated)
    if activity_block:
        blocks = list(annotated.get("blocks") or [])
        if not any(isinstance(block, dict) and block.get("type") == "activity" for block in blocks):
            blocks.insert(0, activity_block)
        annotated["blocks"] = blocks
    return annotated


# ═══════════════════════════════════════════════════════════════════
# Intent router helpers
# ═══════════════════════════════════════════════════════════════════

_CAPABILITY_SIGNALS = re.compile(
    r"\b(what (?:can|do) you (?:access|see)|"
    r"do you have (?:access to (?:the )?(?:database|data|system)|(?:a )?database (?:access|connection))|"
    r"what (?:data|datasets?|tools?) (?:do you have|can you use|are available)|"
    r"your (?:capabilities?|abilities|features?)|"
    r"what are you able to|"
    r"can you (?:access|write to|modify|delete) (?:the )?(?:database|data|system|tables?))\b",
    re.IGNORECASE,
)

_ANALYTICS_ACTION_SIGNALS = re.compile(
    r"\b("
    r"how many (?:listings?|properties?|brokers?|messages?|posts?)\b|"
    r"(?:average|avg|mean|median|total|count|volume|trend|trends?)\s+(?:rent|rents|price|prices|listings?|properties?|brokers?|messages?|posts?)\b|"
    r"top brokers?(?:\s+(?:in|by|for)\b|\b)|"
    r"market stats?\b|broker stats?\b|inventory stats?\b|"
    r"(?:analytics?|reports?|summary|summaries|dashboard|metrics?)\b|"
    r"(?:admin|ops?|operations?|system health|uptime|quota|billing|errors?|logs?|jobs?|pipeline|ingest(?:ion)?)\s+(?:status|stats?|metrics?|health)\b"
    r")",
    re.IGNORECASE,
)

_CONVERSATIONAL_EXPLANATION_SIGNALS = re.compile(
    r"\b(?:what is|what are|what's|explain|difference between|different between|meaning of|"
    r"how does|how do|can you explain|do you know the difference|tell me about)\b",
    re.IGNORECASE,
)

_AGENT_ACTION_SIGNALS = re.compile(
    r"\b(?:client|requirement|candidate|lead|internal\s+note|broker\s+profile|"
    r"match\s+(?:this|the|client)|save\s+(?:this|that)|add\s+(?:a\s+)?note|"
    r"create\s+(?:a\s+)?(?:lead|candidate)|log\s+(?:an\s+)?internal\s+note)\b",
    re.IGNORECASE,
)

_BROWSER_ACTION_SIGNALS = re.compile(
    r"\b(?:browser|browser-use|browse|browsing|click|clicked|tap|open(?:\s+(?:the|a))?\s+(?:page|site|website|listing|result)|"
    r"open\s+(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[^\s]*)?|"
    r"check\s+(?:the\s+)?(?:ai\s+provider|provider|settings?)\s+page|"
    r"dubai\s+land\s+department|dubailand|\bdld\b|ejari|trakheesi|mollak|"
    r"(?:construction|project)\s+(?:status|progress)|"
    r"navigate|navigate(?:s|d)?|scroll|type into|fill(?:\s+in)?|select(?:\s+an)?|"
    r"use\s+browser(?:\s+actions?)?|go to|open propai|propai page|website)\b",
    re.IGNORECASE,
)

_BROWSER_SITE_ALIASES = {
    "dld": "https://dubailand.gov.ae/en/",
    "dubai land department": "https://dubailand.gov.ae/en/",
    "dubailand": "https://dubailand.gov.ae/en/",
    "ejari": "https://dubailand.gov.ae/en/",
    "trakheesi": "https://dubailand.gov.ae/en/",
}


def _browser_approval_secret() -> bytes:
    secret = os.getenv("PROPAI_AGENT_CONFIRMATION_SECRET") or os.getenv("SUPABASE_JWT_SECRET")
    if not secret:
        raise RuntimeError("PROPAI_AGENT_CONFIRMATION_SECRET is required for browser approvals")
    return secret.encode()


def make_browser_approval_token(session_id: str, tenant_id: str | None, user_id: str | None) -> str:
    payload = {
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "exp": int(time.time()) + 900,
    }
    body = base64.urlsafe_b64encode(_json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(_browser_approval_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _read_browser_approval_token(token: str, tenant_id: str | None, user_id: str | None) -> dict:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(_browser_approval_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid browser approval signature")
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = _json.loads(decoded)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid browser approval token") from exc
    if int(payload.get("exp") or 0) < int(time.time()):
        raise ValueError("browser approval expired")
    if str(payload.get("tenant_id") or "") != str(tenant_id or "") or str(payload.get("user_id") or "") != str(user_id or ""):
        raise ValueError("browser approval does not belong to this user/workspace")
    return dict(payload)


def _is_analytics_or_ops_query(text: str) -> bool:
    try:
        return bool(_ANALYTICS_ACTION_SIGNALS.search(text or ""))
    except Exception:
        _logger.exception("Analytics intent check failed for text=%r", (text or "")[:200])
        return False


def _has_query_signals(text: str) -> bool:
    lowered = text.lower()
    query_keywords = [
        "bhk", "rent", "rental", "rentals", "buy", "sale", "lease", "price", "budget", "area", "sqft",
        "broker", "agent", "dealer", "builder", "owner",
        "building", "complex", "tower", "society", "project",
        "locality", "market", "area", "neighbourhood", "neighborhood",
        "flat", "apartment", "office", "shop", "property", "commercial",
        "listing", "listings", "properties", "deal", "requirement", "requirements",
        "show", "find", "search", "look", "need", "want",
        "cr", "lakh", "lac", "thousand", "crore",
        "aed", "dhs", "dirham", "cheque",
        "marina", "jbr", "jvc", "jlt", "business bay", "downtown", "difc",
        "palm jumeirah", "barsha", "furjan", "springs", "meadows", "greens",
        "arabian ranches", "dubai hills", "deira", "karama", "mirdif",
        "duplicate", "merge", "alias",
        "how many", "how much", "count ", "list ", "top ",
        "compare", "versus", "vs",
    ]
    return any(kw in lowered for kw in query_keywords)


def _is_search_followup(text: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:more|next|next\s+10|show\s+more|more\s+options)\s*[.!?]*\s*", text or "", re.IGNORECASE))


def _is_simple_greeting(text: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:hi|hey|hello|namaste|good\s+(?:morning|afternoon|evening))\s*[.!?]*\s*", text or "", re.IGNORECASE))


def _is_conversational_explanation(text: str) -> bool:
    """Keep conceptual questions out of the inventory/tool router.

    A question such as “what is the difference between sale and rent?”
    contains legitimate search keywords, but it asks for an explanation and
    must not be turned into a zero-result market search.
    """
    return bool(_CONVERSATIONAL_EXPLANATION_SIGNALS.search(text or ""))


def _looks_like_browser_followup(text: str) -> bool:
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", (text or "").lower()).strip()
    if not cleaned:
        return False
    tokens = cleaned.split()
    if len(tokens) > 3:
        return False
    return cleaned in {
        "show",
        "open",
        "go",
        "click",
        "there",
        "that",
        "do it",
        "continue",
        "next",
        "proceed",
    }


# ═══════════════════════════════════════════════════════════════════
# Promote helpers
# ═══════════════════════════════════════════════════════════════════

def _parsed_display_label(parsed: dict) -> str:
    if (parsed.get("asset_type") or "").lower() == "commercial":
        label = parsed.get("commercial_use_type") or parsed.get("property_type") or "Commercial"
        return str(label).replace("_", " ").title()
    bhk = parsed.get("bhk")
    if bhk:
        return bhk
    prop_type = parsed.get("property_type")
    if prop_type:
        return str(prop_type).replace("_", " ").title()
    return "Property"


def _promote_highlights(parsed: dict) -> list[str]:
    highlights = []
    if parsed.get("bhk"):
        highlights.append(f"{parsed['bhk']} configuration")
    if parsed.get("area_sqft"):
        highlights.append(f"{parsed['area_sqft']:,} sqft built-up area")
    if parsed.get("furnishing"):
        highlights.append(f"{parsed['furnishing']}")
    if parsed.get("building_name"):
        highlights.append(f"Located at {parsed['building_name']}")
    if parsed.get("landmark_name"):
        highlights.append(f"Near {parsed['landmark_name']}")
    if parsed.get("micro_market"):
        highlights.append(f"Prime location: {parsed['micro_market']}")
    if parsed.get("location_raw") and parsed["location_raw"] not in (parsed.get("micro_market") or "", parsed.get("building_name") or ""):
        highlights.append(f"Area: {parsed['location_raw']}")
    return highlights[:5]


def _promote_price(parsed: dict) -> str:
    price = parsed.get("price")
    unit = parsed.get("price_unit")
    if price and unit == "M":
        return f"AED {(price):,.2f}M"
    if price and unit == "K":
        return f"AED {(price):,.0f}K"
    if price:
        return f"AED {price:,.0f}"
    return ""


def _promote_headline(parsed: dict, channel: str) -> str:
    label = _parsed_display_label(parsed)
    building = parsed.get("building_name", "")
    market = parsed.get("micro_market", "")
    price = _promote_price(parsed)
    location = market or parsed.get("location_raw", "")
    if channel == "whatsapp":
        parts = [f"\U0001f3d7\ufe0f {label}"]
        if building:
            parts.append(f"at {building}")
        if location:
            parts.append(f"in {location}")
        if price:
            parts.append(f"| {price}")
        return " ".join(parts)
    if channel in ("facebook", "instagram"):
        parts = [f"{label}"]
        if building:
            parts.append(f"at {building}")
        if location:
            parts.append(f"in {location}")
        if price:
            parts.append(f"\u2014 {price}")
        return " ".join(parts)
    return ""


def _promote_whatsapp(parsed: dict, highlights: list[str]) -> str:
    label = _parsed_display_label(parsed)
    building = parsed.get("building_name", "")
    market = parsed.get("micro_market", "")
    price = _promote_price(parsed)
    area = f"{parsed['area_sqft']:,} sqft" if parsed.get("area_sqft") else ""
    furnish = parsed.get("furnishing", "")
    broker = parsed.get("broker_name", "")
    phone = re.sub(r"[^0-9]", "", parsed.get("broker_phone") or "")[-10:]
    lines = ["\U0001f3d7\ufe0f *" + _promote_headline(parsed, "whatsapp") + "*", ""]
    if building:
        lines.append(f"\U0001f4cd {building}")
    if market:
        lines.append(f"\U0001f4cd {market}")
    detail_parts = [p for p in [label, area, furnish] if p]
    if detail_parts:
        lines.append(" | ".join(detail_parts))
    if price:
        lines.append(f"\U0001f4b0 {price}")
    lines.append("")
    lines.append("\u2728 Highlights:")
    for h in highlights[:4]:
        lines.append(f"  \u2705 {h}")
    lines.append("")
    if broker:
        lines.append(f"\U0001f4de {broker}")
    if phone and len(phone) == 10:
        lines.append(f"   wa.me/91{phone}")
    return "\n".join(lines)


def _promote_instagram(parsed: dict, highlights: list[str]) -> str:
    label = _parsed_display_label(parsed)
    building = parsed.get("building_name", "")
    market = parsed.get("micro_market", "")
    price = _promote_price(parsed)
    area = f"{parsed['area_sqft']:,} sqft" if parsed.get("area_sqft") else ""
    furnish = parsed.get("furnishing", "")
    lines = [f"\u2728 {label}" + (f" at {building}" if building else "")]
    if market:
        lines.append(f"\U0001f4cd {market}")
    if price:
        lines.append(f"\U0001f4b0 {price}")
    lines.append("")
    if area or furnish:
        detail_parts = [p for p in [area, furnish] if p]
        lines.append(" | ".join(detail_parts))
    lines.append("")
    lines.append("What you get:")
    for h in highlights[:4]:
        lines.append(f"\u2705 {h}")
    lines.append("")
    lines.append("\U0001f4f2 DM for more details or site visit!")
    return "\n".join(lines)


def _promote_facebook(parsed: dict, highlights: list[str]) -> str:
    insta = _promote_instagram(parsed, highlights)
    return insta + "\n\nAvailable for sale/rent. Serious inquiries only."


def _identify_channel_emoji(channel: str) -> str:
    return {"whatsapp": "\U0001f4ac", "facebook": "\U0001f44d", "instagram": "\U0001f4f8"}.get(channel, "\U0001f4e2")


def _ai_promote(system: str, prompt: str) -> str | None:
    try:
        from llm import get_client, get_model
        client = get_client()
        model = get_model()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            max_tokens=300,
        )
        usage = getattr(resp, "usage", None)
        try:
            from usage_logger import log_ai_usage
            log_ai_usage(
                agent="promote",
                model=model,
                tokens_input=getattr(usage, "prompt_tokens", 0) or 0,
                tokens_output=getattr(usage, "completion_tokens", 0) or 0,
            )
        except Exception:
            pass
        return resp.choices[0].message.content
    except Exception:
        return None


def _ai_promote_with_key(system: str, prompt: str, api_key: str) -> str | None:
    try:
        from llm import get_client, get_model
        from openai import OpenAI
        client = get_client() if not api_key else OpenAI(api_key=api_key, base_url="https://api.doubleword.ai/v1")
        model = get_model()
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            max_tokens=300,
        )
        usage = getattr(resp, "usage", None)
        try:
            from usage_logger import log_ai_usage
            log_ai_usage(
                agent="promote",
                model=model,
                tokens_input=getattr(usage, "prompt_tokens", 0) or 0,
                tokens_output=getattr(usage, "completion_tokens", 0) or 0,
            )
        except Exception:
            pass
        return resp.choices[0].message.content
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# Block 1 — Semantic search, explain, summary, broker/building lookup
# ═══════════════════════════════════════════════════════════════════

@router.post("/api/ai/query")
async def ai_query(
    req: QueryRequest,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    from semantic_embeddings import semantic_search
    tenant_id = _resolve_active_organization_id(user, tenant_id)
    results = await asyncio.to_thread(
        semantic_search, storage, req.query,
        tenant_id=tenant_id, limit=req.k,
    )
    return {"query": req.query, "count": len(results), "results": results}


@router.get("/api/ai/similar/{observation_id}")
async def ai_similar(
    observation_id: int,
    k: int = 10,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    detail = storage.get_inbox_evidence_detail(observation_id)
    parsed = detail.get("parsed", {})
    query = str(parsed.get("summary_title") or parsed.get("normalized_message") or "").strip()
    if not query:
        raise HTTPException(404, "Observation has no semantic text")
    from semantic_embeddings import semantic_search
    tenant_id = _resolve_active_organization_id(user, tenant_id)
    results = await asyncio.to_thread(
        semantic_search, storage, query,
        entity_types=["listing", "requirement"], tenant_id=tenant_id, limit=k + 1,
    )
    filtered = [r for r in results if int(r.get("source_id") or 0) != observation_id][:k]
    return {"observation_id": observation_id, "count": len(filtered), "results": filtered}


@router.get("/api/ai/explain/{observation_id}")
async def ai_explain(observation_id: int, user: dict = Depends(require_user)):
    detail = storage.get_inbox_evidence_detail(observation_id)
    parsed = detail.get("parsed", {})
    raw = detail.get("raw", {})
    if not parsed:
        raise HTTPException(404, "Observation not found")

    raw_text = raw.get("message", "")
    lower = raw_text.lower()

    rules = []

    if parsed.get("intent") == "PRE-LAUNCH":
        rules.append("intent=PRE-LAUNCH: matched pre-launch/new-launch keywords")
    elif parsed.get("intent") == "COMMERCIAL":
        rules.append("intent=COMMERCIAL: matched commercial keywords (office/shop/warehouse)")
    elif parsed.get("intent") == "RENT":
        rules.append("intent=RENT: matched rental keywords (rent/lease)")
    elif parsed.get("intent") == "SELL":
        if any(x in lower for x in ["sale", "sell", "selling", "available", "ready to move", "resale", "for sale"]):
            rules.append("intent=SELL: matched sale keywords")
        else:
            rules.append("intent=SELL: default (no buy/rent/pre-launch keywords detected)")
    elif parsed.get("intent") == "BUY":
        rules.append("intent=BUY: matched buy/requirement keywords")

    if parsed.get("principal") == "Owner":
        rules.append("principal=Owner: matched owner-sale/direct-owner pattern")
    elif parsed.get("principal") == "Buyer Client":
        rules.append("principal=Buyer Client: matched client-requirement/buyer-need pattern")
    else:
        rules.append("principal=Unknown: no owner or buyer-client pattern detected")

    broker = parsed.get("broker_name")
    profile = parsed.get("profile_name")
    if profile:
        rules.append(f"broker_name='{broker}' from WhatsApp profile name")
    elif broker:
        rules.append(f"broker_name='{broker}' from signature block (bottom-up extraction)")

    if parsed.get("forwarded"):
        rules.append("forwarded=1: message contains forwarded indicator")

    if parsed.get("bhk"):
        rules.append(f"bhk='{parsed['bhk']}': matched BHK pattern")
    if parsed.get("price"):
        rules.append(f"price={parsed['price']} {parsed.get('price_unit','')}: matched price pattern")
    if parsed.get("area_sqft"):
        rules.append(f"area_sqft={parsed['area_sqft']}: matched area pattern")
    if parsed.get("furnishing"):
        rules.append(f"furnishing='{parsed['furnishing']}': matched furnishing keyword")
    if parsed.get("building_name"):
        rules.append(f"building_name='{parsed['building_name']}': extracted from location")
    if parsed.get("landmark_name"):
        rules.append(f"landmark_name='{parsed['landmark_name']}': extracted from location")
    if parsed.get("micro_market"):
        rules.append(f"micro_market='{parsed['micro_market']}': extracted from location")

    resolver = detail.get("resolver", {})
    if resolver.get("building_id") and resolver.get("method") != "unresolved":
        rules.append(f"resolver={resolver['method']}: matched building #{resolver.get('building_id')} "
                      f"({resolver.get('building_name', 'unknown')}) with confidence {resolver.get('resolver_confidence', 0)}")
    elif resolver.get("failure_category"):
        rules.append(f"resolver={resolver['method']}: {resolver['failure_category']}")

    return {
        "observation_id": observation_id,
        "parsed": {k: v for k, v in parsed.items() if v is not None and k != "embedding"},
        "rules": rules,
    }


@router.get("/api/ai/summary")
async def ai_summary(user: dict = Depends(require_user)):
    today = _today_prefix()
    activity = storage.dashboard_activity(today)
    types = storage.dashboard_message_types_today(today)
    type_map = {t["intent"]: t["c"] for t in types}

    growth = storage.dashboard_growth(today)
    today_timeline = growth["timeline"][-1] if growth["timeline"] else None

    top_brokers = storage.get_top_brokers_today(today)
    heat = storage.dashboard_heatmap()
    top_markets = [h for h in heat if h.get("c", 0) > 0][:10]

    return {
        "date": today,
        "messages_today": activity.get("messages_today", 0),
        "message_types": type_map,
        "growth": {
            "new_buildings": today_timeline.get("new_buildings", 0) if today_timeline else 0,
            "new_landmarks": today_timeline.get("new_landmarks", 0) if today_timeline else 0,
            "new_developers": today_timeline.get("new_developers", 0) if today_timeline else 0,
        },
        "top_brokers": top_brokers,
        "hot_markets": top_markets,
    }


@router.get("/api/ai/broker/{broker_name:path}")
async def ai_broker(broker_name: str, user: dict = Depends(require_user)):
    observations = storage.get_observations_by_broker(broker_name)
    if not observations:
        raise HTTPException(404, f"No observations for broker: {broker_name}")
    total = len(observations)
    intents = {}
    buildings = set()
    markets = set()
    prices = []
    for o in observations:
        i = o.get("intent")
        if i:
            intents[i] = intents.get(i, 0) + 1
        b = o.get("building_name")
        if b:
            buildings.add(b)
        m = o.get("micro_market")
        if m:
            markets.add(m)
        p = o.get("price")
        if p:
            prices.append(p)
    avg_price = round(sum(prices) / len(prices), 2) if prices else None
    last_5 = observations[:5]
    for o in last_5:
        o.pop("embedding", None)
    return {
        "broker_name": broker_name,
        "total_observations": total,
        "intent_breakdown": intents,
        "unique_buildings": list(buildings),
        "unique_markets": list(markets),
        "avg_price": avg_price,
        "last_observations": last_5,
    }


@router.get("/api/ai/building/{building_name:path}")
async def ai_building(building_name: str, user: dict = Depends(require_user)):
    observations = storage.get_observations_by_building(building_name)
    if not observations:
        raise HTTPException(404, f"No observations for building: {building_name}")
    total = len(observations)
    intents = {}
    prices = []
    brokers = set()
    for o in observations:
        i = o.get("intent")
        if i:
            intents[i] = intents.get(i, 0) + 1
        p = o.get("price")
        if p:
            prices.append(p)
        b = o.get("broker_name")
        if b:
            brokers.add(b)
    avg_price = round(sum(prices) / len(prices), 2) if prices else None
    last_5 = observations[:5]
    for o in last_5:
        o.pop("embedding", None)
    return {
        "building_name": building_name,
        "total_observations": total,
        "intent_breakdown": intents,
        "unique_brokers": list(brokers),
        "avg_price": avg_price,
        "last_observations": last_5,
    }


# ═══════════════════════════════════════════════════════════════════
# Block 2 — Promote Listing (ad copy generation)
# ═══════════════════════════════════════════════════════════════════

@router.get("/api/promote/config")
async def promote_config(user: dict = Depends(require_user)):
    has_meta_credentials = bool(
        os.getenv("META_ACCESS_TOKEN")
        and (os.getenv("META_PAGE_ID") or os.getenv("META_INSTAGRAM_BUSINESS_ID"))
    )
    return {
        "enable_ai_promo": ENABLE_AI_PROMO,
        "enable_meta_publishing": ENABLE_META_PUBLISHING,
        "meta_publish_available": ENABLE_META_PUBLISHING and has_meta_credentials,
    }


@router.post("/api/promote/generate")
async def promote_generate(req: PromoteRequest, user: dict = Depends(require_user)):
    detail = storage.get_inbox_evidence_detail(req.observation_id)
    if not detail.get("parsed"):
        raise HTTPException(404, "Observation not found")
    parsed = dict(detail["parsed"])
    if req.fields:
        allowed_fields = {
            "bhk", "price", "price_unit", "area_sqft", "furnishing", "location_raw",
            "building_name", "landmark_name", "micro_market", "broker_name", "broker_phone",
        }
        for key, value in req.fields.items():
            if key in allowed_fields and value not in (None, ""):
                parsed[key] = value
    highlights = _promote_highlights(parsed)
    headline = _promote_headline(parsed, req.channel)

    if req.channel == "whatsapp":
        body = _promote_whatsapp(parsed, highlights)
    elif req.channel == "instagram":
        body = _promote_instagram(parsed, highlights)
    elif req.channel == "facebook":
        body = _promote_facebook(parsed, highlights)
    else:
        raise HTTPException(400, f"Unknown channel: {req.channel}")

    result = {
        "channel": req.channel,
        "emoji": _identify_channel_emoji(req.channel),
        "headline": headline,
        "body": body,
        "highlights": highlights,
        "ai_enhanced": False,
    }

    promo_api_key = req.api_key or ""
    if req.use_ai and ENABLE_AI_PROMO:
        try:
            system = "You are a Dubai real estate marketing assistant. Given property details, write a short promotional ad for the specified channel. Keep it under 120 words. Return only the ad body, no preamble."
            price_str = _promote_price(parsed)
            detail_parts = [v for v in [_parsed_display_label(parsed), parsed.get("furnishing"), f"{parsed.get('area_sqft', '')} sqft" if parsed.get('area_sqft') else ""] if v]
            prompt = f"Channel: {req.channel}\nBuilding: {parsed.get('building_name', 'N/A')}\nLocation: {parsed.get('micro_market', parsed.get('location_raw', 'N/A'))}\nDetails: {' | '.join(detail_parts)}\nPrice: {price_str}\nBroker: {parsed.get('broker_name', 'N/A')}"
            loop = asyncio.get_running_loop()
            ai_body = await loop.run_in_executor(None, lambda: _ai_promote_with_key(system, prompt, promo_api_key))
            if ai_body:
                result["body"] = ai_body
                result["ai_enhanced"] = True
        except Exception:
            pass

    return result


# ═══════════════════════════════════════════════════════════════════
# Block 3 — AI config, chat sessions, chat
# ═══════════════════════════════════════════════════════════════════

@router.get("/api/ai/config")
async def ai_config(user: dict = Depends(require_user), tenant_id: str | None = Depends(get_tenant_context)):
    tenant_id = await asyncio.to_thread(_resolve_active_organization_id, user, tenant_id)
    info = _preferred_workspace_provider(tenant_id)
    return {
        "has_server_key": info.get("provider") != "none",
        "base_url": info.get("base_url", ""),
        "model": info.get("model", ""),
        "provider": info.get("provider", "none"),
    }


async def _chat_owner_context(user: dict, tenant_id: str | None) -> tuple[str, list[str]]:
    """Return one durable owner plus legacy aliases created by older clients."""
    user_id = str(user.get("id") or "").strip()
    if not user_id:
        raise HTTPException(401, "Authenticated user id is missing")
    canonical = f"user:{user_id}"
    aliases = [canonical, user_id, str(user.get("phone") or "").strip()]
    try:
        profile = await asyncio.to_thread(
            storage.get_user_profile,
            auth_user_id=user_id,
            tenant_id=tenant_id,
        )
        aliases.extend([
            str((profile or {}).get("phone") or "").strip(),
            str((profile or {}).get("whatsapp_number") or "").strip(),
        ])
    except Exception:
        pass
    return canonical, list(dict.fromkeys(alias for alias in aliases if alias))


async def _owned_chat_session(session_id: str, user: dict, tenant_id: str | None) -> dict:
    session = await asyncio.to_thread(storage.get_chat_session, session_id, tenant_id)
    if not session:
        raise HTTPException(404, "Session not found")
    canonical, aliases = await _chat_owner_context(user, tenant_id)
    if str(session.get("broker_phone") or "") not in aliases:
        raise HTTPException(403, "Session does not belong to this user")
    if session.get("broker_phone") != canonical:
        await asyncio.to_thread(storage.adopt_chat_session_owners, aliases, canonical, tenant_id)
        session["broker_phone"] = canonical
    return session


def _chat_phone(value: object) -> str:
    """Return the canonical Indian phone suffix used by broker identities."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else ""


def _load_chat_broker_context(
    session: dict | None,
    user: dict,
    requested_phone: str,
    tenant_id: str | None,
) -> dict | None:
    """Load identity and tenant-scoped broker stats for the chat system prompt.

    Chat sessions may contain a legacy phone, an authenticated owner key
    (``user:<uuid>``), or the request's phone. Resolve the profile by the
    authenticated user first, then use the profile phone to find the broker
    row. Every query is scoped to the active tenant.
    """
    try:
        auth_user_id = str(user.get("id") or "").strip()
        profile = storage.get_user_profile(
            auth_user_id=auth_user_id,
            tenant_id=tenant_id,
        ) if auth_user_id else None

        session_phone = _chat_phone((session or {}).get("broker_phone"))
        requested = _chat_phone(requested_phone)
        profile_phone = _chat_phone((profile or {}).get("phone"))
        phone = profile_phone or session_phone or requested

        # Older sessions can be phone-owned even when the auth profile lookup
        # has not been populated yet.
        if not profile and phone:
            profile = storage.get_user_profile(phone=phone, tenant_id=tenant_id)
            profile_phone = _chat_phone((profile or {}).get("phone"))
            phone = profile_phone or phone

        broker_row: dict = {}
        client = getattr(storage, "client", None)
        if client is not None and phone:
            query = client.table("brokers").select(
                "primary_phone,canonical_name,listing_count,requirement_count,"
                "rental_count,commercial_count,avg_ticket,active_days_30,"
                "market_count,building_count"
            ).eq("primary_phone", phone)
            if tenant_id:
                query = query.eq("tenant_id", tenant_id)
            rows = query.limit(1).execute().data or []
            if rows:
                broker_row = dict(rows[0])

        first_name = str((profile or {}).get("first_name") or "").strip()
        last_name = str((profile or {}).get("last_name") or "").strip()
        name = " ".join(part for part in (first_name, last_name) if part).strip()
        if not name:
            name = str(broker_row.get("canonical_name") or "").strip()
        if not name and not phone and not broker_row:
            return None

        return {
            "name": name,
            "phone": phone,
            "city": str((profile or {}).get("city") or "").strip(),
            "email": str((profile or {}).get("email") or "").strip(),
            "listing_count": broker_row.get("listing_count"),
            "requirement_count": broker_row.get("requirement_count"),
            "rental_count": broker_row.get("rental_count"),
            "commercial_count": broker_row.get("commercial_count"),
            "avg_ticket": broker_row.get("avg_ticket"),
            "active_days_30": broker_row.get("active_days_30"),
            "market_count": broker_row.get("market_count"),
            "building_count": broker_row.get("building_count"),
        }
    except Exception:
        _logger.exception("Could not load tenant-scoped broker context for AI chat")
        return None


@router.get("/api/ai/chat/sessions")
async def list_chat_sessions(broker_phone: str = "", limit: int = 50, user: dict = Depends(require_user), tenant_id: str | None = Depends(get_tenant_context)):
    tenant_id = tenant_id or await asyncio.to_thread(_resolve_active_organization_id, user, None)
    owner_key, aliases = await _chat_owner_context(user, tenant_id)
    await asyncio.to_thread(storage.adopt_chat_session_owners, aliases, owner_key, tenant_id)
    sessions = await asyncio.to_thread(storage.list_chat_sessions, owner_key, limit=limit, tenant_id=tenant_id)
    return [_session_response(session) for session in sessions]


@router.post("/api/ai/chat/sessions")
async def create_chat_session(broker_phone: str = "", title: str = "New chat", source: str = "parsed", user: dict = Depends(require_user), tenant_id: str | None = Depends(get_tenant_context)):
    tenant_id = tenant_id or await asyncio.to_thread(_resolve_active_organization_id, user, None)
    owner_key, aliases = await _chat_owner_context(user, tenant_id)
    await asyncio.to_thread(storage.adopt_chat_session_owners, aliases, owner_key, tenant_id)
    session = await asyncio.to_thread(storage.create_chat_session, owner_key, title, source, tenant_id)
    if not session:
        raise HTTPException(500, "Could not create chat session")
    return _session_response(session)


class RenameChatSessionRequest(BaseModel):
    title: str


@router.patch("/api/ai/chat/sessions/{session_id}")
async def rename_chat_session(
    session_id: str,
    body: RenameChatSessionRequest,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    tenant_id = tenant_id or await asyncio.to_thread(_resolve_active_organization_id, user, None)
    session = await _owned_chat_session(session_id, user, tenant_id)
    title = re.sub(r"\s+", " ", body.title or "").strip()
    if not title:
        raise HTTPException(400, "Chat name cannot be empty")
    if len(title) > 120:
        raise HTTPException(400, "Chat name must be 120 characters or fewer")
    await asyncio.to_thread(storage.update_chat_session_title, session_id, title, tenant_id=tenant_id)
    session["title"] = title
    return _session_response(session)


@router.get("/api/ai/chat/sessions/{session_id}/messages")
async def get_chat_session_messages(session_id: str, user: dict = Depends(require_user), tenant_id: str | None = Depends(get_tenant_context)):
    tenant_id = tenant_id or await asyncio.to_thread(_resolve_active_organization_id, user, None)
    await _owned_chat_session(session_id, user, tenant_id)
    return await asyncio.to_thread(storage.get_ai_chat_messages, session_id, tenant_id=tenant_id)


@router.post("/api/ai/chat/confirm")
async def confirm_agent_action(
    req: ConfirmAgentActionRequest,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    """Execute one write tool after the authenticated user confirms it."""
    tenant_id = await asyncio.to_thread(_resolve_active_organization_id, user, tenant_id)
    from agent_tools import confirm_tool

    try:
        result = await asyncio.to_thread(
            confirm_tool,
            req.confirmation_token,
            storage.client,
            tenant_id,
            str(user.get("id") or ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        _logger.exception("AI agent confirmation failed for user=%s", user.get("id"))
        raise HTTPException(500, "Could not execute the confirmed action")
    return result


@router.post("/api/ai/chat/browser/confirm")
async def confirm_browser_action(
    req: ConfirmBrowserActionRequest,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    try:
        tenant_id = await asyncio.to_thread(_resolve_active_organization_id, user, tenant_id)
        payload = _read_browser_approval_token(req.confirmation_token, tenant_id, str(user.get("id") or ""))
        session_id = str(payload.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(400, "Browser approval token is missing a session id")
        session = await _owned_chat_session(session_id, user, tenant_id)
        messages = await asyncio.to_thread(storage.get_ai_chat_messages, session_id, tenant_id=tenant_id)

        # Opening a URL after explicit approval is deterministic. Do it
        # directly so a provider that cannot complete a second tool-call
        # round cannot block the browser itself.
        last_user = next(
            (str(row.get("content") or "").strip() for row in reversed(messages) if row.get("role") == "user"),
            "",
        )
        url_match = re.search(
            r"(?:https?://|www\.)[^\s<>]+|\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[^\s<>]*)?",
            last_user,
            re.IGNORECASE,
        )
        had_explicit_url = bool(url_match)
        from ai_chat_engine import execute_tool

        # Portal tasks use deterministic workflows. The model-driven browser
        # loop is intentionally not used for regulated Maharashtra portals: a
        # portal can change form labels, require CAPTCHA, or show paid/login
        # gates, and we must surface those states instead of guessing.
        portal_text = last_user.lower()
        if any(term in portal_text for term in ("project status", "construction status", "rera", "permit")) or "dld" in portal_text:
            from browser_workflows import run_dld_project_status

            project_match = re.search(
                r"(?:status|progress|construction|project)\s+(?:for|of|about)\s+(.+?)(?:\s+(?:on|in)\s+maha\s*rera|$)",
                last_user,
                re.IGNORECASE,
            )
            project_name = (project_match.group(1) if project_match else "").strip(" .?!")
            if not project_name:
                project_name = re.sub(
                    r".*?maha\s*rera(?:\s+(?:website|site))?\s*",
                    "",
                    last_user,
                    flags=re.IGNORECASE,
                ).strip(" .?!")
            browser_session_id = str(uuid.uuid4())

            def _portal_execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
                tool_args = dict(args)
                tool_args["chat_session_id"] = session_id
                return execute_tool(
                    name,
                    tool_args,
                    {},
                    tenant_id=tenant_id,
                    storage_client=storage.client,
                    user_id=str(user.get("id") or ""),
                    browser_enabled=True,
                    browser_provider="agent-browser",
                )

            workflow = await asyncio.to_thread(
                run_dld_project_status,
                _portal_execute,
                browser_session_id,
                project_name,
            )
            activity = workflow.activity(browser_session_id, "dld_project_status")
            activity["trace"]["confirmation_token"] = req.confirmation_token
            content = workflow.content
            if workflow.source_url:
                content += f"\nOfficial source: {workflow.source_url}"
            await asyncio.to_thread(storage.add_chat_message, session_id, "assistant", content, tenant_id, [activity])
            return _wrap_chat_response({"content": content, "blocks": [activity], "sources": [workflow.source_url], "trace": activity["trace"]}, True)

        if any(term in portal_text for term in ("dld", "dubai land department", "dubailand", "title deed", "ejari", "trakheesi", "transaction search")):
            from browser_workflows import run_dld_transaction_search

            browser_session_id = str(uuid.uuid4())

            def _dld_execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
                tool_args = dict(args)
                tool_args["chat_session_id"] = session_id
                return execute_tool(
                    name,
                    tool_args,
                    {},
                    tenant_id=tenant_id,
                    storage_client=storage.client,
                    user_id=str(user.get("id") or ""),
                    browser_enabled=True,
                    browser_provider="agent-browser",
                )

            workflow = await asyncio.to_thread(run_dld_transaction_search, _dld_execute, browser_session_id, {})
            activity = workflow.activity(browser_session_id, "dld_property_search")
            activity["trace"]["confirmation_token"] = req.confirmation_token
            content = workflow.content + f"\nOfficial source: {workflow.source_url}"
            await asyncio.to_thread(storage.add_chat_message, session_id, "assistant", content, tenant_id, [activity])
            return _wrap_chat_response({"content": content, "blocks": [activity], "sources": [workflow.source_url], "trace": activity["trace"]}, True)

        # A bare browser request (for example, "find the latest RERA notice
        # online") should go to the approved model-driven browser loop. The
        # URL-only fast path is reserved for simple open/title checks; it must
        # not prevent brokers from inventing useful new browsing tasks.
        browser_followup_action = re.search(
            r"\b(?:search|find|look\s+for|check|click|fill|scroll|extract|compare|read|tell\s+me)\b",
            last_user,
            re.IGNORECASE,
        )
        direct_open = bool(url_match) and not browser_followup_action

        if not url_match:
            lowered_user = last_user.lower()
            for alias, alias_url in _BROWSER_SITE_ALIASES.items():
                if alias in lowered_user:
                    url_match = re.search(re.escape(alias), last_user, re.IGNORECASE)
                    break
        alias_url = "" if had_explicit_url else next(
            (value for alias, value in _BROWSER_SITE_ALIASES.items() if alias in last_user.lower()),
            "",
        )
        if direct_open and url_match:
            url = alias_url or url_match.group(0).rstrip(".,!?;:)").strip()
            if not url.lower().startswith(("http://", "https://")):
                url = f"https://{url}"
            # agent_browser_sessions.id is a UUID and agent_browser_steps
            # references it directly. Keep the runtime session identifier
            # UUID-shaped so the existing persistence contract remains valid.
            browser_session_id = str(uuid.uuid4())
            browser_args = {"url": url, "browser_session_id": browser_session_id, "session_label": "Approved browser task", "chat_session_id": session_id}
            opened = await asyncio.to_thread(
                execute_tool,
                "browser_open",
                browser_args,
                {},
                tenant_id=tenant_id,
                storage_client=storage.client,
                user_id=str(user.get("id") or ""),
                browser_enabled=True,
                browser_provider="agent-browser",
            )
            if opened.get("status") != "ok":
                message = str(opened.get("error") or opened.get("raw_output") or "Agent Browser could not open the page")
                raise HTTPException(502, f"Browser runtime failed: {message}")
            state = await asyncio.to_thread(
                execute_tool,
                "browser_state",
                {"browser_session_id": browser_session_id},
                {},
                tenant_id=tenant_id,
                storage_client=storage.client,
                user_id=str(user.get("id") or ""),
                browser_enabled=True,
                browser_provider="agent-browser",
            )
            title = str(state.get("title") or "").strip()
            current_url = str(state.get("url") or url).strip()
            content = f"I opened {current_url} in PropAI’s browser. This checks the page on the server; it does not open a new tab in your computer’s browser."
            if title:
                content += f"\nPage title: {title}"
            host = (urlparse(current_url).hostname or "").lower().removeprefix("www.")
            if host in {"facebook.com", "m.facebook.com"}:
                content += "\nYou can now ask me to run a multi-step Facebook task, such as searching for a person, opening a profile or page, and summarizing the visible information."
            elif host:
                content += f"\nYou can now ask me to run a multi-step task on {host}, such as navigating to a section, searching within the site, and summarizing visible information."
            blocks = [{
                "type": "activity",
                "title": "Browser task complete",
                "body": content,
                "steps": [f"Opened {current_url}"] + ([f"Read page title: {title}"] if title else []),
                "trace": {"route": "direct_browser_open", "browser_provider": "agent-browser", "browser_session_id": browser_session_id, "source_url": current_url},
            }]
            blocks[0]["trace"]["confirmation_token"] = req.confirmation_token
            await asyncio.to_thread(storage.add_chat_message, session_id, "assistant", content, tenant_id, blocks)
            return _wrap_chat_response({"content": content, "blocks": blocks, "sources": [], "trace": blocks[0]["trace"]}, True)

        forwarded = ChatRequest(
            messages=messages,
            api_key="",
            model="",
            session_id=session_id,
            broker_phone=str(session.get("broker_phone") or ""),
            source="inbox",
            browser_approval_token=req.confirmation_token,
            persist_user_turn=False,
        )
        return await ai_chat(forwarded, user=user, tenant_id=tenant_id)
    except HTTPException:
        raise
    except Exception as exc:
        _logger.exception("Browser approval failed before execution for user=%s", user.get("id"))
        # Keep the UI actionable when a deployment has a provider/schema
        # mismatch. The full traceback remains in server logs, while this
        # short detail prevents a useless generic 500 for the operator.
        detail = str(exc).strip().replace("\n", " ")[:240]
        suffix = f": {detail}" if detail else ""
        raise HTTPException(500, f"Browser approval could not start: {type(exc).__name__}{suffix}") from exc


@router.post("/api/ai/chat/browser/decline")
async def decline_browser_action(
    req: ConfirmBrowserActionRequest,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    tenant_id = await asyncio.to_thread(_resolve_active_organization_id, user, tenant_id)
    payload = _read_browser_approval_token(req.confirmation_token, tenant_id, str(user.get("id") or ""))
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(400, "Browser approval token is missing a session id")
    session = await _owned_chat_session(session_id, user, tenant_id)
    broker = await asyncio.to_thread(
        _load_chat_broker_context,
        session,
        user,
        str(session.get("broker_phone") or ""),
        tenant_id,
    )
    messages = await asyncio.to_thread(storage.get_ai_chat_messages, session_id, tenant_id=tenant_id)
    effective_messages = [
        {"role": row.get("role"), "content": str(row.get("content") or "")}
        for row in messages
        if row.get("role") in {"user", "assistant", "system"} and str(row.get("content") or "").strip()
    ]
    providers = _workspace_provider_candidates(tenant_id, "")
    try:
        reply = await _run_with_provider_failover(
            lambda provider: chat_engine.get_conversational_reply(
                effective_messages,
                api_key=provider["api_key"],
                model=provider["model"] or None,
                base_url=provider["base_url"] or None,
                broker=broker,
            ),
            providers,
            timeout=60,
        )
        text = (reply.content or "").strip() or "I can help with that."
        await asyncio.to_thread(storage.add_chat_message, session_id, "assistant", text, tenant_id=tenant_id, blocks=[{"type": "greeting", "body": text}])
        return {
            "content": text,
            "blocks": [{"type": "greeting", "body": text}],
            "sources": [],
            "status_steps": [],
            "trace": {"route": "browser_decline_conversational"},
        }
    except Exception:
        _logger.exception("Browser decline conversational reply failed for session=%s", session_id)
        raise HTTPException(500, "Could not generate the conversational fallback")


@router.delete("/api/ai/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str, user: dict = Depends(require_user), tenant_id: str | None = Depends(get_tenant_context)):
    tenant_id = tenant_id or await asyncio.to_thread(_resolve_active_organization_id, user, None)
    await _owned_chat_session(session_id, user, tenant_id)
    await asyncio.to_thread(storage.delete_chat_session, session_id, tenant_id=tenant_id)
    return {"ok": True}


class BrokerContactRequest(BaseModel):
    source_schema: str | None = None
    raw_message_id: int | None = None
    contact_index: int | None = None
    list_contacts: bool = False


@router.post("/api/contact-broker/{listing_id}")
async def resolve_broker_contact(
    listing_id: int,
    request: BrokerContactRequest | None = None,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    """Resolve a broker's WhatsApp link only after an authenticated click."""
    tenant_id = tenant_id or await asyncio.to_thread(_resolve_active_organization_id, user, None)
    listing = None
    source_schema = str(request.source_schema or "").strip() if request else ""
    if source_schema:
        try:
            listing = await asyncio.to_thread(
                storage.get_market_item_detail,
                listing_id,
                source_schema,
                request.raw_message_id if request else None,
                tenant_id,
            )
        except Exception as exc:
            _logger.exception(
                "Could not resolve typed broker contact for listing=%s schema=%s: %s",
                listing_id, source_schema, exc,
            )
            raise HTTPException(503, "Broker contact is temporarily unavailable")
        if not listing:
            raise HTTPException(404, "Listing not found")
        # Shared/legacy observations are contactable across the parsed market.
        # Explicit workspace-private MCP rows remain visible only to their owner.
        if (
            str(listing.get("visibility") or "").lower() == "workspace_private"
            and str(listing.get("tenant_id") or "") != str(tenant_id or "")
        ):
            raise HTTPException(404, "Listing not found")

    # Compatibility fallback for older chat cards that still carry the legacy
    # listings_unified id but no typed source identity.
    if listing is None:
        try:
            query = storage.client.table("listings_unified").select(
                "id,broker_phone,bhk,building_name,micro_market,intent"
            ).eq("id", listing_id)
            if tenant_id:
                query = query.eq("tenant_id", tenant_id)
            rows = await asyncio.to_thread(lambda: query.limit(1).execute().data or [])
        except Exception as exc:
            _logger.exception("Could not resolve broker contact for listing=%s: %s", listing_id, exc)
            raise HTTPException(503, "Broker contact is temporarily unavailable")
        if not rows:
            raise HTTPException(404, "Listing not found")
        listing = rows[0]
    # Build contact choices from the authenticated listing's source evidence.
    # Numbers stay server-side; the client receives only indexes and labels.
    evidence_text = str(
        listing.get("raw_message")
        or listing.get("source_message")
        or listing.get("normalized_message")
        or ""
    )
    raw_id = int(
        (request.raw_message_id if request else None)
        or listing.get("latest_raw_message_id")
        or listing.get("raw_message_id")
        or 0
    )
    if raw_id:
        try:
            raw_query = storage.client.table("raw_messages").select("message").eq("id", raw_id).limit(1)
            if tenant_id:
                raw_query = raw_query.eq("tenant_id", tenant_id)
            raw_rows = await asyncio.to_thread(lambda: raw_query.execute().data or [])
            if raw_rows and raw_rows[0].get("message"):
                evidence_text = str(raw_rows[0]["message"])
        except Exception:
            _logger.debug("Could not load raw contact evidence for listing=%s", listing_id, exc_info=True)
    contact_numbers: list[str] = []
    for candidate in [listing.get("broker_phone")] + re.findall(
        r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"
        r"|(?<!\d)(?:\+?971[-\s]?[2-7]\d{7,8}|0?5\d[-\s]?\d{3}[-\s]?\d{4})(?!\d)",
        evidence_text,
    ):
        phone_candidate = re.sub(r"\D", "", str(candidate or ""))
        if len(phone_candidate) == 12 and phone_candidate.startswith("971"):
            pass
        elif len(phone_candidate) == 11 and phone_candidate.startswith("0"):
            # Local UAE format 05XXXXXXXX → E.164 without the trunk zero.
            phone_candidate = "971" + phone_candidate[1:]
        elif len(phone_candidate) == 9 and phone_candidate[0] in "234567":
            # Bare UAE subscriber number → assume the platform market code.
            phone_candidate = "971" + phone_candidate
        else:
            phone_candidate = phone_candidate[-10:]
        if len(phone_candidate) >= 9 and phone_candidate not in contact_numbers:
            contact_numbers.append(phone_candidate)
    if request and request.list_contacts:
        return {
            "contacts": [
                {"index": index, "label": f"WhatsApp contact {index + 1}"}
                for index in range(min(len(contact_numbers), 6))
            ]
        }
    selected_index = int(request.contact_index or 0) if request else 0
    if selected_index < 0 or selected_index >= len(contact_numbers):
        raise HTTPException(410, "This listing does not have a contactable broker")
    phone = contact_numbers[selected_index]
    bhk = str(listing.get("bhk") or "").strip()
    try:
        bhk_number = float(bhk)
        bhk = str(int(bhk_number)) if bhk_number.is_integer() else f"{bhk_number:g}"
    except ValueError:
        pass
    building = str(listing.get("building_name") or "").strip()
    locality = str(listing.get("micro_market") or "").strip()
    if building_name_problem(building, locality=locality):
        building = ""
    is_requirement = str(listing.get("message_type") or "").lower() == "requirement"
    asset = str(listing.get("asset_type") or "").lower()
    area = listing.get("carpet_area_sqft") or listing.get("chargeable_area_sqft") or listing.get("built_up_area_sqft")
    try:
        area_label = f"{float(area):g} sq ft" if area else ""
    except (TypeError, ValueError):
        area_label = ""
    if asset == "commercial":
        use_type = listing.get("commercial_use_type") or listing.get("property_type") or "commercial space"
        if isinstance(use_type, list):
            use_type = next((str(item) for item in use_type if item), "commercial space")
        subject_parts = [area_label, str(use_type).strip(), "requirement" if is_requirement else "rental"]
    else:
        subject_parts = [f"{bhk} BHK" if bhk else "", "requirement" if is_requirement else "listing"]
    subject_parts.extend([f"at {building}" if building else "", f"in {locality}" if locality and not building else ""])
    subject = " ".join(value for value in subject_parts if value).strip() or "this property"
    source = str(
        listing.get("source_message")
        or listing.get("normalized_message")
        or ((listing.get("raw_payload") or {}).get("slice_text") if isinstance(listing.get("raw_payload"), dict) else "")
        or ""
    ).strip()[:900]
    recall = (
        f"Hi, I found your {subject} on PropAI. Is it still active?"
        if is_requirement
        else f"Hi, I found {subject} on PropAI. Is it still available?"
    )
    if source:
        recall += f"\n\nOriginal {'requirement' if is_requirement else 'listing'} details:\n{source}"
    message = quote(recall)
    wa_number = f"91{phone}" if len(phone) == 10 else phone
    return {"contact_url": f"https://wa.me/{wa_number}?text={message}"}


@router.post("/api/ai/chat")
async def ai_chat(req: ChatRequest, user: dict = Depends(require_user), tenant_id: str | None = Depends(get_tenant_context)):
    from ai_chat_engine import get_memory

    # The profile.tenant_id column is legacy data and can point at the default
    # org even after the user has switched to their active workspace. Resolve
    # from the authenticated organization membership for every chat request so
    # workspace-saved provider keys are actually visible to the router.
    tenant_id = await asyncio.to_thread(_resolve_active_organization_id, user, tenant_id)
    chat_session = None
    if req.session_id:
        chat_session = await _owned_chat_session(req.session_id, user, tenant_id)
    session_id = req.session_id or "default"
    persisted_messages = (
        await asyncio.to_thread(storage.get_ai_chat_messages, req.session_id, 200, tenant_id)
        if req.session_id
        else []
    )
    effective_messages = [
        {"role": row.get("role"), "content": str(row.get("content") or "")}
        for row in persisted_messages
        if row.get("role") in {"user", "assistant", "system"} and str(row.get("content") or "").strip()
    ]
    for incoming in req.messages:
        content = str(incoming.get("content") or "").strip()
        if not content:
            parts = incoming.get("parts") or []
            for p in parts:
                if p.get("type") == "text":
                    content = str(p.get("text") or "").strip()
                    if content:
                        break
        role = incoming.get("role")
        if role not in {"user", "assistant", "system"} or not content:
            continue
        if not effective_messages or effective_messages[-1] != {"role": role, "content": content}:
            effective_messages.append({"role": role, "content": content})
    # The database transcript is authoritative. Use a fresh in-process memory
    # key for each durable turn so a long-lived API worker cannot append the
    # same restored history repeatedly after refreshes or retries.
    memory_revision = (
        str(persisted_messages[-1].get("id") or len(persisted_messages))
        if persisted_messages
        else "new"
    )
    memory = get_memory(f"{session_id}:{memory_revision}:{len(effective_messages)}")
    effective_model = (req.model or "").strip()
    providers = _workspace_provider_candidates(tenant_id, effective_model)
    workspace_ai_settings = await asyncio.to_thread(storage.get_workspace_ai_settings, tenant_id)
    # Runtime AI is deployment-controlled. Ignore request/workspace API keys;
    # this keeps all broker traffic on the Coolify-managed route and gives us
    # one place to add usage limits later.
    if not providers:
        providers = [{"api_key": "", "model": effective_model, "base_url": "", "provider": "none"}]

    for msg in effective_messages:
        role = msg.get("role", "")
        content = str(msg.get("content", "")).strip()
        if content:
            if not memory.working or memory.working[-1].get("content") != content:
                memory.add(role, content)

    def _persist(role: str, content: str, blocks: list | None = None) -> None:
        if not req.session_id or not content:
            return
        try:
            storage.add_chat_message_if_new(req.session_id, role, content, tenant_id=tenant_id, blocks=blocks)
            storage.touch_chat_session(req.session_id, tenant_id=tenant_id)
        except Exception as exc:
            _logger.exception("Could not persist AI chat message session=%s role=%s: %s", req.session_id, role, exc)

    def _maybe_title(text: str) -> None:
        if not req.session_id or not text:
            return
        try:
            msgs = storage.get_ai_chat_messages(req.session_id, limit=3, tenant_id=tenant_id)
            user_msgs = [m for m in msgs if m.get("role") == "user"]
            if len(user_msgs) <= 1:
                title = text[:80].strip()
                storage.update_chat_session_title(req.session_id, title, tenant_id=tenant_id)
        except Exception:
            pass

    broker = await asyncio.to_thread(
        _load_chat_broker_context,
        chat_session,
        user,
        req.broker_phone,
        tenant_id,
    )

    last_user = ""
    for msg in reversed(effective_messages):
        if msg.get("role") == "user":
            last_user = str(msg.get("content", "")).strip()
            break
    # Save the user turn before any provider/search work. A failed provider
    # must not make the conversation disappear on refresh.
    if last_user and req.persist_user_turn:
        _persist("user", last_user)
        _maybe_title(last_user)

    source_mode = _normalize_chat_source(req.source)
    _is_inbox = source_mode == "inbox"

    # Explicit save commands are broker CRM actions, not prompts for the
    # model. Handle them before inventory search and provider fallback so a
    # transient model failure cannot turn a successful save into a chat reply.
    save_requirement = _extract_save_requirement_query(effective_messages) if last_user else None
    # A clarification response carries the unresolved intake in its structured
    # block. Read that state back from the durable transcript so a later
    # confirmation does not fall through to stale search memory.
    pending_building_save = None
    for persisted in reversed(persisted_messages):
        if persisted.get("role") != "assistant":
            continue
        for block in reversed(persisted.get("blocks") or []):
            if block.get("type") == "clarification" and block.get("pending_save"):
                pending_building_save = block.get("pending_save")
                break
        if pending_building_save:
            break
    pending_confirmation = bool(
        pending_building_save
        and last_user
        and re.fullmatch(
            r"(?:yes|yeah|yep|correct|confirm|confirmed|save(?: it| this)?|go ahead|use that|that's right)[.! ]*",
            last_user.strip(),
            flags=re.IGNORECASE,
        )
    )
    if pending_confirmation:
        save_requirement = dict(pending_building_save.get("save_requirement") or {})
    # Continue an unresolved listing intake deterministically. Do not send a
    # building/locality clarification back through the general conversation
    # model, where stale search filters can leak into the answer.
    building_followup = None
    if last_user and not save_requirement:
        latest_lower = last_user.lower()
        if (
            (re.search(r"\b(?:building\s+name|building)\b", latest_lower) and re.search(r"\b(?:is|called)\b", latest_lower))
            or re.search(r"\b(?:also\s+call(?:ed)?|aka|also\s+known\s+as)\b", latest_lower)
        ):
            for previous in reversed(effective_messages[:-1]):
                if previous.get("role") != "user":
                    continue
                previous_save = _extract_save_requirement_query([previous])
                if previous_save:
                    save_requirement = previous_save
                    building_match = re.search(
                        r"(?:building\s+name\s+is|is\s+the\s+building\s+name|also\s+call(?:ed)?|aka|also\s+known\s+as)\s+(.+?)(?:\s+and\s+locality\s+is\s+|\s+locality\s+is\s+|$)",
                        last_user,
                        flags=re.IGNORECASE,
                    )
                    locality_match = re.search(r"\blocality\s+is\s+(.+?)(?:[.!?]|$)", last_user, flags=re.IGNORECASE)
                    building_followup = {
                        "building_name": building_match.group(1).strip(" .,-") if building_match else "",
                        "locality": locality_match.group(1).strip(" .,-") if locality_match else "",
                    }
                    break
    if save_requirement:
        save_label = "listing"
        try:
            from agent_tools import execute_tool as execute_agent_tool

            latest_lower = last_user.lower()
            explicit_requirement = bool(re.search(
                r"\b(requirement|requirements|client|buyer|tenant|looking\s+for|need(?:s)?|seeking|want(?:s)?)\b",
                latest_lower,
            ))
            message_type = "requirement" if explicit_requirement else "listing"
            save_label = message_type
            transaction_type = "rent" if save_requirement.get("intent") == "RENT" else "sale"
            source_text = str(save_requirement.get("source_text") or last_user).strip()
            # The quick-action chip is UI guidance, not broker evidence. Strip
            # the legacy prefix defensively so older clients cannot persist it
            # into the raw requirement or title.
            source_text = re.sub(r"^\s*add\s+(?:a\s+)?(?:listing|requirement)\s*:\s*", "", source_text, flags=re.IGNORECASE).strip()
            building_name = ""
            listing_locality = str(save_requirement.get("micro_market") or "").strip()
            if message_type == "listing":
                locality = str(
                    (building_followup or {}).get("locality")
                    or (pending_building_save or {}).get("locality")
                    or save_requirement.get("micro_market")
                    or ""
                ).strip()
                explicit_building = str(
                    (building_followup or {}).get("building_name")
                    or (pending_building_save or {}).get("building_name")
                    or save_requirement.get("building_name")
                    or ""
                ).strip()
                if explicit_building and re.search(r"\bdifc\b|dubai\s+international\s+financial\s+centre", explicit_building, re.IGNORECASE):
                    # DIFC is a distinct real-estate micro-market even when a
                    # caller supplies a broader Dubai geography.
                    locality = "DIFC"
                listing_locality = locality
                if locality:
                    candidate = explicit_building
                    if not candidate:
                        candidate_match = re.search(
                            rf"\b(?:at|in)\s+(.+?)\s+(?:in\s+)?{re.escape(locality)}\b",
                            source_text,
                            flags=re.IGNORECASE,
                        )
                        candidate = candidate_match.group(1).strip(" .,-") if candidate_match else ""
                    if candidate:
                        resolved = await asyncio.to_thread(storage.resolve_building, candidate)
                        building_name = str(resolved or "").strip()
                if not building_name and pending_confirmation and explicit_building:
                    # The broker explicitly confirmed the exact observed name
                    # after verification failed. Preserve it as user-supplied
                    # evidence; do not invent a canonical building identity.
                    building_name = explicit_building
                if not building_name:
                    response = {
                        "content": (
                            f"I couldn't verify \"{explicit_building or 'that building'}\" in the building registry. "
                            "Reply ‘yes’ to save it under that exact name, or provide a different verified building name."
                        ),
                        "blocks": [{
                            "type": "clarification",
                            "title": "Building name needed",
                            "body": "I will not silently treat a locality or abbreviation as a building. Confirm the exact observed name before saving.",
                            "pending_save": {
                                "save_requirement": save_requirement,
                                "building_name": explicit_building,
                                "locality": listing_locality,
                            },
                        }],
                        "sources": ["ai_chat"],
                        "status_steps": ["Parsed save request", "Building verification required"],
                        "trace": {"route": "deterministic_save_listing_clarification", "reason": "building_unresolved"},
                    }
                    _persist("assistant", response["content"], blocks=response["blocks"])
                    return _wrap_chat_response(response, _is_inbox)
            tool_args = {
                "source_text": source_text,
                "message_type": message_type,
                "transaction_type": transaction_type,
                "asset_type": "residential",
                "locality": listing_locality or save_requirement.get("micro_market") or "",
                "building_name": building_name,
                "bhk": save_requirement.get("bhk") or "",
                "price": save_requirement.get("price_max") if message_type == "listing" else None,
                "budget_max": save_requirement.get("price_max") if message_type == "requirement" else None,
                "budget_min": save_requirement.get("price_min") if message_type == "requirement" else None,
                "price_unit": "abs",
                "furnishing": save_requirement.get("furnishing") or "",
                "summary_title": source_text[:160],
            }
            tool_result = await asyncio.to_thread(
                execute_agent_tool,
                "save_my_deal",
                tool_args,
                storage.client,
                tenant_id,
                user_id=str(user.get("id") or ""),
                confirmed=True,
            )
            if tool_result.get("status") != "ok":
                raise RuntimeError(tool_result.get("error") or "save_my_deal failed")
            label = "requirement" if message_type == "requirement" else "listing"
            response = {
                "content": f"Saved your {label} to My Deals.",
                "blocks": [{
                    "type": "summary",
                    "title": f"{label.title()} Saved",
                    "body": f"My Deals record #{tool_result.get('typed_id')} · source evidence preserved.",
                }],
                "sources": ["ai_chat", "my_deals"],
                "status_steps": ["Parsed save request", f"Saved {label} to My Deals"],
                "trace": {"route": "deterministic_save_my_deal", "message_type": message_type, "typed_id": tool_result.get("typed_id")},
            }
            _persist("assistant", response.get("content", ""), blocks=response.get("blocks"))
            _maybe_title(last_user)
            return _wrap_chat_response(response, _is_inbox)
        except Exception:
            _logger.exception("Deterministic save requirement failed")
            error_text = f"I couldn't save that {save_label} right now. Please try again; it will appear in My Deals only after the save succeeds."
            response = {
                "content": error_text,
                "blocks": [{"type": "error_state", "title": "Save failed", "body": error_text}],
                "sources": [],
                "status_steps": ["Save request received", "Save failed"],
                "trace": {"route": "deterministic_save_requirement_error"},
            }
            _persist("assistant", error_text, blocks=response["blocks"])
            return _wrap_chat_response(response, _is_inbox)

    # Fully specified inventory requests are deterministic marketplace queries.
    # Route them before capability/conversational/provider handling so prior
    # browser activity or stale working-memory filters cannot hijack the turn.
    deterministic_query = (
        chat_engine.parse_market_search_request(last_user, allow_llm=False)
        if last_user else None
    )
    if last_user and _is_search_followup(last_user):
        for previous in reversed(effective_messages[:-1]):
            if previous.get("role") != "user":
                continue
            previous_query = chat_engine.parse_market_search_request(str(previous.get("content") or ""), allow_llm=False)
            if (
                previous_query
                and previous_query.get("bhk") not in (None, "")
                and previous_query.get("micro_markets")
                and previous_query.get("intent") in {"RENT", "SELL", "COMMERCIAL"}
            ):
                deterministic_query = dict(previous_query)
                deterministic_query["offset"] = 10
                break
    concrete_inventory_query = bool(
        deterministic_query
        and deterministic_query.get("bhk") not in (None, "")
        and deterministic_query.get("micro_markets")
        and deterministic_query.get("intent") in {"RENT", "SELL", "COMMERCIAL"}
    )
    if concrete_inventory_query:
        inventory_query = dict(deterministic_query)
        # “Looking for a 3 BHK” is supply search language in this route. The
        # requirements table is private workspace demand and must not be used
        # for a shared inventory answer.
        inventory_query.pop("search_scope", None)
        try:
            response = await _current_listing_search(inventory_query, tenant_id, str(user.get("id") or ""))
            _persist("assistant", response.get("content", ""), blocks=response.get("blocks"))
            _maybe_title(last_user)
            return _wrap_chat_response(response, _is_inbox)
        except Exception:
            _logger.exception("Deterministic inventory search failed")
            error_text = "I couldn't search the shared PropAI inventory right now. Please try again shortly."
            _persist("assistant", error_text, blocks=[{
                "type": "error_state",
                "title": "Market search unavailable",
                "body": error_text,
            }])
            return _wrap_chat_response({
                "content": error_text,
                "blocks": [{"type": "error_state", "title": "Market search unavailable", "body": error_text}],
                "sources": [],
                "status_steps": ["Shared inventory search failed"],
                "trace": {"route": "deterministic_market_search_error"},
            }, _is_inbox)

    if last_user and _CAPABILITY_SIGNALS.search(last_user):
        try:
            cap_sources = chat_engine.load_data()
            cap_live = chat_engine.load_live_data(getattr(storage, "db", None))
            cap_sources.update(cap_live)
            if cap_sources:
                cap_msgs = [
                    {"role": "system", "content": chat_engine.build_system_prompt(cap_sources, broker=broker, workspace_settings=workspace_ai_settings)},
                    {"role": "user", "content": last_user},
                ]
                cap_reply = await _run_with_provider_failover(
                lambda provider: chat_engine.get_model_reply(
                    cap_msgs, cap_sources, api_key=provider["api_key"],
                    model=provider["model"] or None, base_url=provider["base_url"] or None, max_tool_rounds=0,
                    prefer_supabase_agent=True,
                    browser_enabled=bool(getattr(workspace_ai_settings, "browser_enabled", False)),
                    browser_provider=getattr(workspace_ai_settings, "browser_provider", "agent-browser"),
                ),
                providers,
                timeout=60,
                )
                text = (cap_reply.content or "").strip() or "I can help with that."
                _persist("user", last_user)
                _persist("assistant", text)
                _maybe_title(last_user)
                return _wrap_chat_response({
                    "content": text,
                    "blocks": [{"type": "summary", "body": text}],
                    "sources": list(cap_sources.keys()),
                    "status_steps": [],
                    "trace": {"route": "capability_llm"},
                }, _is_inbox)
        except Exception:
            pass

    if last_user and _is_simple_greeting(last_user):
        greeting = "Hi — I’m here to search the shared PropAI broker network. Tell me the area, rent or sale, BHK, and budget, and I’ll bring back verified options with a WhatsApp contact button."
        _persist("assistant", greeting, blocks=[{"type": "greeting", "body": greeting}])
        _maybe_title(last_user)
        return _wrap_chat_response({
            "content": greeting,
            "blocks": [{"type": "greeting", "body": greeting}],
            "sources": [],
            "status_steps": [],
            "trace": {"route": "deterministic_greeting"},
        }, _is_inbox)

    if last_user and (
        (_is_conversational_explanation(last_user) or not _has_query_signals(last_user))
        and not _AGENT_ACTION_SIGNALS.search(last_user)
        and not (_BROWSER_ACTION_SIGNALS.search(last_user) and bool(workspace_ai_settings and getattr(workspace_ai_settings, "browser_enabled", False)))
    ):
        try:
            reply = await _run_with_provider_failover(
                lambda provider: chat_engine.get_conversational_reply(
                    effective_messages, api_key=provider["api_key"], model=provider["model"] or None,
                    base_url=provider["base_url"] or None, broker=broker
                ),
                providers,
                timeout=60,
            )
            text = (reply.content or "").strip()
            if text:
                _persist("user", last_user)
                _persist("assistant", text)
                _maybe_title(last_user)
                return _wrap_chat_response({
                    "content": text,
                    "blocks": [{"type": "greeting", "body": text}],
                    "sources": [],
                    "status_steps": [],
                    "trace": {"route": "conversational_llm"},
                }, _is_inbox)
            else:
                return _wrap_chat_response({
                    "content": "AI returned an empty response. Please try again.",
                    "blocks": [{"type": "error", "body": "AI returned an empty response. Please try again."}],
                    "sources": [],
                    "status_steps": [],
                    "trace": {"route": "conversational_empty"},
                }, _is_inbox)
        except ProviderConfigurationError:
            _logger.exception("LLM provider configuration error")
            error_text = "LLM provider is not configured. Please check workspace API keys."
            _persist("user", last_user)
            _persist("assistant", error_text)
            _maybe_title(last_user)
            return _wrap_chat_response({
                "content": error_text,
                "blocks": [{"type": "error", "body": error_text}],
                "sources": [],
                "trace": {"route": "conversational_error"},
            }, _is_inbox)
        except Exception:
            _logger.exception("AI chat failed during conversational fallback")
            error_text = "I’m still here, but the conversation model is temporarily busy. You can ask for listings directly with area, BHK, rent or sale, and budget, and I’ll search the live PropAI network without waiting for the model."
            _persist("assistant", error_text, blocks=[{"type": "greeting", "body": error_text}])
            return _wrap_chat_response({
                "content": error_text,
                "blocks": [{"type": "greeting", "body": error_text}],
                "sources": [],
                "trace": {"route": "conversational_error"},
            }, _is_inbox)

    browser_enabled = bool(workspace_ai_settings and getattr(workspace_ai_settings, "browser_enabled", False))
    recent_text = " ".join(str(msg.get("content") or "") for msg in effective_messages[-8:])
    if last_user and _BROWSER_ACTION_SIGNALS.search(last_user) and not browser_enabled:
        prompt_text = "Browser actions are not enabled for this workspace yet. I can still search and summarize listings in chat, but I can't open web pages or click around sites."
        _persist("assistant", prompt_text, blocks=[{
            "type": "error_state",
            "title": "Browser actions unavailable",
            "body": "Enable browser actions in Workspace AI controls to let the agent open internal or external web pages. Until then I can only answer in text.",
        }])
        return _wrap_chat_response({
            "content": prompt_text,
            "blocks": [{
                "type": "error_state",
                "title": "Browser actions unavailable",
                "body": "Enable browser actions in Workspace AI controls to let the agent open internal or external web pages. Until then I can only answer in text.",
            }],
            "sources": [],
            "status_steps": ["Browser actions are disabled"],
            "trace": {"route": "browser_disabled_text_only"},
        }, _is_inbox)

    if last_user and not browser_enabled and _looks_like_browser_followup(last_user) and _BROWSER_ACTION_SIGNALS.search(recent_text):
        prompt_text = "I can’t open web pages in this chat. I can still search listings here if you give me the filters, or I can keep it text-only."
        _persist("assistant", prompt_text, blocks=[{
            "type": "error_state",
            "title": "Browser actions unavailable",
            "body": "This follow-up is asking for page interaction, but browser actions are not enabled in this workspace.",
        }])
        return _wrap_chat_response({
            "content": prompt_text,
            "blocks": [{
                "type": "error_state",
                "title": "Browser actions unavailable",
                "body": "This follow-up is asking for page interaction, but browser actions are not enabled in this workspace.",
            }],
            "sources": [],
            "status_steps": ["Browser actions are disabled"],
            "trace": {"route": "browser_followup_text_only"},
        }, _is_inbox)

    if last_user and _BROWSER_ACTION_SIGNALS.search(last_user) and browser_enabled and not str(req.browser_approval_token or "").strip():
        prompt_text = "I can browse this website and follow the steps you requested. Start the browser task?"
        browser_token = make_browser_approval_token(session_id, tenant_id, str(user.get("id") or ""))
        target_match = re.search(r"(?:https?://|www\.)[^\s<>]+|\b[a-z0-9][a-z0-9.-]*\.(?:com|in|org|net)(?:/[^\s<>]*)?", last_user, re.IGNORECASE)
        target_url = ""
        if target_match:
            target_url = target_match.group(0).rstrip(".,!?;:)")
            if not target_url.lower().startswith(("http://", "https://")):
                target_url = f"https://{target_url}"
        _persist("assistant", prompt_text, blocks=[{
            "type": "confirmation",
            "title": "Ready to browse?",
            "body": "I’ll open the site and follow your instructions. You can keep chatting instead if you prefer.",
            "tool": "browser",
            "mode": "browser",
            "confirmation_token": browser_token,
            "url": target_url,
        }])
        return _wrap_chat_response({
            "content": prompt_text,
            "blocks": [{
                "type": "confirmation",
                "title": "Ready to browse?",
                "body": "I’ll open the site and follow your instructions. You can keep chatting instead if you prefer.",
                "tool": "browser",
                "mode": "browser",
                "confirmation_token": browser_token,
                "url": target_url,
            }],
            "sources": [],
            "status_steps": ["Browser approval required"],
            "trace": {"route": "browser_permission_prompt"},
        }, _is_inbox)

    if last_user and memory.requests_fresh_context(last_user):
        # “Fresh search” must not inherit the previous locality, budget, or
        # intent. Keep the durable transcript, but reset only working memory
        # used to plan this turn.
        memory.reset()
        memory.add("user", last_user)
    elif last_user and memory.detect_topic_change(last_user) and len(memory.working) > 2:
        memory.compact_topic()
    memory.prune()

    # A fresh rental request without filters should ask for the minimum useful
    # search inputs instead of reusing the previous search or depending on an
    # LLM provider just to ask a clarification.
    fresh_rental = bool(last_user and memory.requests_fresh_context(last_user) and re.search(r"\b(rental?|rentals?)\b", last_user, re.IGNORECASE))
    has_search_filter = bool(re.search(r"\b\d+\s*(?:bhk|br)\b|\b(?:in|near|around)\s+[a-z][a-z\s-]{2,}|(?:aed|dhs|rs\.?|\d+\s*(?:k|m|mn|million))", last_user, re.IGNORECASE)) if last_user else False
    if fresh_rental and not has_search_filter:
        clarification = "Sure — starting a fresh rental search. Which area and BHK should I search for? You can also add a monthly budget."
        _persist("assistant", clarification)
        _maybe_title(last_user)
        return _wrap_chat_response({
            "content": clarification,
            "blocks": [{"type": "summary", "body": clarification}],
            "sources": [],
            "status_steps": ["Fresh search started", "Waiting for area and BHK"],
            "trace": {"route": "fresh_rental_clarification"},
        }, _is_inbox)

    sources = chat_engine.load_data()
    try:
        live = chat_engine.load_live_data(getattr(storage, "db", None))
        sources.update(live)
    except Exception:
        pass
    # The legacy CSV/SQLite bundle is optional context for the conversational
    # agent. Inventory search must not fail just because that bundle is empty;
    # strict searches below use the tenant-scoped Supabase agent tools.
    memory.persist()

    active_sources = sources
    if source_mode == "parsed":
        active_sources = {
            key: value
            for key, value in sources.items()
            if key in {"overview", "unique_listings", "buildings", "brokers", "building_matches"}
        } or sources

    def _call(provider):
        system_prompt = chat_engine.build_system_prompt(active_sources, broker=broker, workspace_settings=workspace_ai_settings)
        context = memory.build_context()
        workspace_policy_lines = []
        if workspace_ai_settings:
            workspace_policy_lines.extend([
                f"Workspace browser enabled: {bool(getattr(workspace_ai_settings, 'browser_enabled', False))}",
                f"Workspace browser provider: {getattr(workspace_ai_settings, 'browser_provider', 'agent-browser')}",
                f"Max tool rounds: {int(getattr(workspace_ai_settings, 'max_tool_rounds', 8) or 8)}",
                f"Max concurrent calls: {int(getattr(workspace_ai_settings, 'max_concurrent_calls', 8) or 8)}",
                f"Allowed routes: {', '.join(getattr(workspace_ai_settings, 'allowed_routes', []) or [])}",
                f"Allowed actions: {', '.join(getattr(workspace_ai_settings, 'allowed_actions', []) or [])}",
            ])
        if workspace_policy_lines:
            system_prompt = f"{system_prompt}\n\nWORKSPACE LIMITS:\n" + "\n".join(f"- {line}" for line in workspace_policy_lines)
        msgs = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context},
        ]
        max_tool_rounds = int(getattr(workspace_ai_settings, "max_tool_rounds", 8) or 8) if workspace_ai_settings else 8
        activity_sink: list[dict[str, Any]] = []
        reply = chat_engine.get_model_reply(
            msgs,
            active_sources,
            api_key=provider["api_key"],
            model=provider["model"] or None,
            base_url=provider["base_url"] or None,
            max_tool_rounds=max(1, min(max_tool_rounds, 16)),
            prefer_supabase_agent=True,
            browser_enabled=bool(getattr(workspace_ai_settings, "browser_enabled", False)),
            browser_provider=getattr(workspace_ai_settings, "browser_provider", "agent-browser"),
            storage_client=storage.client,
            user_id=str(user.get("id") or ""),
            activity_sink=activity_sink,
        )
        if isinstance(reply, dict):
            response = dict(reply)
        else:
            if not (reply.content or "").strip():
                raise RuntimeError("provider returned an empty response")
            response = chat_engine.normalize_workspace_response(reply.content or "", active_sources)
        trace = dict(response.get("trace") or {}) if isinstance(response.get("trace"), dict) else {}
        if activity_sink:
            trace["actions"] = list(trace.get("actions") or []) + activity_sink
            response["trace"] = trace
        return response

    if last_user and not _ANALYTICS_ACTION_SIGNALS.search(last_user):
        try:
            response = await _run_with_provider_failover(lambda provider: _call(provider), providers, timeout=90)
            response = _annotate_chat_response(response, source_mode)
            _persist("user", last_user)
            _persist("assistant", response.get("content", ""), blocks=response.get("blocks"))
            _maybe_title(last_user)
            return _wrap_chat_response(response, _is_inbox)
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=504,
                content={"error": "timeout", "message": "Request timed out. Try a simpler query."},
            )
        except Exception as exc:
            err_str = str(exc)
            if "budget_exhausted" in err_str or "402" in err_str:
                return JSONResponse(
                    status_code=503,
                    content={"error": "ai_unavailable", "message": "AI service credits exhausted. Extraction and chat will resume once credits are added."},
                )
            _logger.exception("AI chat failed during provider failover")
            return _doubleword_error_response(exc)

    return JSONResponse(
        status_code=400,
        content={"error": "empty", "message": "No user message provided."},
    )


@router.get("/api/ai/chat/overview")
async def ai_chat_overview(user: dict = Depends(require_user)):
    sources = chat_engine.load_data()
    live = chat_engine.load_live_data(getattr(storage, "db", None))
    sources.update(live)
    if not sources:
        return {"error": "no_data"}
    return {"overview": chat_engine.build_overview(sources), "sources": list(sources.keys())}
