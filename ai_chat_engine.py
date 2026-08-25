import os
import json
import datetime
import re
import logging
from pathlib import Path
from urllib.parse import quote, urlparse
from fnmatch import fnmatch
import pandas as pd
from openai import OpenAI
import time
from typing import Any

MODEL = os.getenv("DOUBLEWORD_MODEL", "").strip()
BASE_URL = os.getenv("DOUBLEWORD_API_URL", "https://api.doubleword.ai/v1")
_lab_dir = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
_propai_data = os.path.realpath(os.path.join(_lab_dir, "..", "propai", "data"))
DATA_DIR = _propai_data if os.path.isdir(_propai_data) else os.path.join(_lab_dir, "data")
PROMPT_DIR = Path(_lab_dir) / "prompts"

_logger = logging.getLogger(__name__)

_CACHE_TTL = "1h"
_CACHE_BOUNDARY = "\nCurrent date and time: "


def _cached_system_blocks(system_prompt: str) -> list[dict]:
    """Split a system prompt into cached (static) + dynamic content blocks.

    Everything before ``_CACHE_BOUNDARY`` is static (identity, instructions,
    JSON contract, examples).  Everything after is dynamic (timestamp, broker
    identity, dataset row counts) and must NOT be cached.

    Returns a list of ``{"type": "text", ...}`` content blocks suitable for
    the ``content`` field of a system message.  Each block carries a
    ``cache_control`` marker on the static portion so Doubleword's prompt
    caching can reuse it across requests.
    """
    idx = system_prompt.find(_CACHE_BOUNDARY)
    if idx < 0:
        return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral", "ttl": _CACHE_TTL}}]
    static = system_prompt[:idx]
    dynamic = system_prompt[idx:]  # includes the leading "\nCurrent date..."
    return [
        {"type": "text", "text": static, "cache_control": {"type": "ephemeral", "ttl": _CACHE_TTL}},
        {"type": "text", "text": dynamic},
    ]


def _add_tool_cache_control(tools: list[dict]) -> list[dict]:
    """Add ``cache_control`` to the last tool definition so all tools are cached."""
    if not tools:
        return tools
    cached = [t.copy() for t in tools]
    cached[-1] = {**cached[-1], "cache_control": {"type": "ephemeral", "ttl": _CACHE_TTL}}
    return cached


_client = None
_client_key = ""
_client_base_url = ""
_supabase_storage = None

# ── Conversation Memory ────────────────────────────────────────
# Three-tier memory: working (raw), summaries (compacted topics), domain (persistent facts).

_KNOWN_MARKETS_FOR_MEMORY = [
    "Bandra West", "Bandra East", "Bandra", "Khar West", "Khar", "Santacruz West",
    "Santacruz East", "Santacruz", "Andheri West", "Andheri East", "Andheri",
    "Juhu", "Vile Parle West", "Vile Parle East", "Dadar", "Prabhadevi",
    "Goregaon West", "Goregaon East", "Goregaon", "Malad West", "Malad East",
    "Malad", "Powai", "Chembur", "BKC", "Pali Hill", "Kalina", "Lokhandwala",
    "Lower Parel", "Worli", "Marine Lines", "Nariman Point",
]

_TOPIC_END_SIGNALS = re.compile(
    r"^(now|next|switch|different|instead|forget|ignore|skip|another|other|"
    r"what about|how about|show me|try|search|find|looking for|need|want)\b",
    re.IGNORECASE,
)

_FRESH_CONTEXT_SIGNALS = re.compile(
    r"\b(?:fresh|start over|reset|clear|forget previous|ignore previous|new search)\b",
    re.IGNORECASE,
)


class ConversationMemory:
    def __init__(self, max_working_turns: int = 8, session_id: str | None = None):
        self.working: list[dict] = []
        self.summaries: list[str] = []
        self.domain: dict[str, str] = {}
        self._topic_start: int = 0
        self.max_working_turns = max_working_turns
        self.session_id = session_id
        self._hydrated = False
        self._dirty = False
        self._last_save_at = 0.0

    def hydrate(self) -> None:
        """Load previously saved state from Supabase once per process lifetime.

        Falls back silently if storage is unreachable so chat remains
        functional even if the persistence layer is down."""
        if self._hydrated or not self.session_id:
            return
        self._hydrated = True
        try:
            from storage import supabase as _storage  # type: ignore
            db = _storage.SupabaseStorage().db
            row = db.execute(
                "select json_state from public.conversation_state where session_id = ?",
                (self.session_id,),
            ).fetchone()
            if not row:
                return
            payload = row[0]
            if isinstance(payload, str):
                payload = json.loads(payload)
            self.working = list(payload.get("working") or [])
            self.summaries = list(payload.get("summaries") or [])
            self.domain = dict(payload.get("domain") or {})
            self._topic_start = int(payload.get("topic_start") or 0)
        except Exception:
            return

    def persist(self) -> None:
        """Snapshot working memory to Supabase. Rate-limited to one write per
        1.5 seconds per session so repeated tool-round message appends during
        a single chat turn don't generate a write storm."""
        if not self.session_id or not self._dirty:
            return
        now = time.time()
        if now - self._last_save_at < 1.5:
            return
        snapshot = {
            "working": self.working,
            "summaries": self.summaries,
            "domain": self.domain,
            "topic_start": self._topic_start,
        }
        try:
            from storage import supabase as _storage  # type: ignore
            db = _storage.SupabaseStorage().db
            db.execute(
                """
                insert into public.conversation_state (session_id, json_state, updated_at)
                values (?, ?::jsonb, now())
                on conflict (session_id) do update
                  set json_state = excluded.json_state,
                      updated_at = now()
                """,
                (self.session_id, json.dumps(snapshot)),
            )
            self._last_save_at = now
            self._dirty = False
        except Exception:
            return

    def add(self, role: str, content: str) -> None:
        self.working.append({"role": role, "content": content})
        self._dirty = True

    def reset(self) -> None:
        """Start a genuinely fresh topic without deleting the chat transcript."""
        self.working = []
        self.summaries = []
        self.domain = {}
        self._topic_start = 0
        self._dirty = True

    def requests_fresh_context(self, message: str) -> bool:
        return bool(_FRESH_CONTEXT_SIGNALS.search(message or ""))

    def detect_topic_change(self, message: str) -> bool:
        if not self.working:
            return True
        lowered = message.strip().lower()
        if _TOPIC_END_SIGNALS.match(lowered):
            return True
        current_market = self._current_market()
        if current_market:
            for m in _KNOWN_MARKETS_FOR_MEMORY:
                if m.lower() in lowered and m != current_market:
                    return True
        return False

    def compact_topic(self) -> str:
        topic_msgs = self.working[self._topic_start:]
        if len(topic_msgs) < 2:
            return ""
        entities: list[str] = []
        markets: set[str] = set()
        intents: set[str] = set()
        bhk: str | None = None
        prices: list[str] = []
        brokers: set[str] = set()
        for msg in topic_msgs:
            if msg["role"] != "user":
                continue
            text = msg["content"]
            lowered = text.lower()
            for m in _KNOWN_MARKETS_FOR_MEMORY:
                if m.lower() in lowered:
                    markets.add(m)
            if re.search(r"\b(rent|rental|lease)\b", lowered):
                intents.add("rent")
            if re.search(r"\b(sale|sell|buy|purchase)\b", lowered):
                intents.add("buy/sale")
            bhk_m = re.search(r"\b(\d+)\s*bhk\b", lowered)
            if bhk_m:
                bhk = bhk_m.group(1)
            price_m = re.search(r"(?:under|below|upto|up to|max)?\s*(?:aed|dhs)?\s*(\d+(?:\.\d+)?)\s*(m|mn|million|k)?", lowered)
            if price_m:
                prices.append(price_m.group(0).strip())
            broker_m = re.search(r"\b(call|contact|message|text)\s+(\w+)", lowered)
            if broker_m:
                brokers.add(broker_m.group(2))
        parts = []
        if markets:
            parts.append(f"area={'/'.join(sorted(markets))}")
        if intents:
            parts.append(f"intent={'/'.join(sorted(intents))}")
        if bhk:
            parts.append(f"bhk={bhk}")
        if prices:
            parts.append(f"price={', '.join(prices[:2])}")
        if brokers:
            parts.append(f"contact={'/'.join(brokers)}")
        summary = " | ".join(parts) if parts else "general inquiry"
        self.summaries.append(summary)
        self._topic_start = len(self.working)
        return summary

    def _current_market(self) -> str | None:
        for msg in reversed(self.working[self._topic_start:]):
            if msg["role"] == "user":
                lowered = msg["content"].lower()
                for m in _KNOWN_MARKETS_FOR_MEMORY:
                    if m.lower() in lowered:
                        return m
        return None

    def build_context(self) -> str:
        parts: list[str] = []
        if self.summaries:
            parts.append("Previous topics:")
            for i, s in enumerate(self.summaries, 1):
                parts.append(f"  [{i}] {s}")
            parts.append("")
        current = self.working[self._topic_start:]
        if current:
            parts.append("Current conversation:")
            for msg in current:
                parts.append(f"{msg['role']}: {msg['content']}")
        return "\n".join(parts)

    def prune(self) -> None:
        if len(self.working) - self._topic_start > self.max_working_turns * 2:
            excess = len(self.working) - self._topic_start - self.max_working_turns
            self._topic_start += excess


_memory_store: dict[str, ConversationMemory] = {}


def get_memory(session_id: str) -> ConversationMemory:
    if session_id not in _memory_store:
        mem = ConversationMemory(session_id=session_id)
        mem.hydrate()
        _memory_store[session_id] = mem
    return _memory_store[session_id]


def persist_memory(session_id: str) -> None:
    mem = _memory_store.get(session_id)
    if mem is not None:
        mem.persist()


def get_client(api_key=None, base_url=None):
    global _client, _client_key, _client_base_url
    if api_key or base_url:
        key = api_key or os.environ.get("DOUBLEWORD_API_KEY", "")
        endpoint = (base_url or BASE_URL).rstrip("/")
        if _client is None or _client_key != key or _client_base_url != endpoint:
            _client = OpenAI(api_key=key, base_url=endpoint)
            _client_key = key
            _client_base_url = endpoint
        return _client
    # No explicit key → use provider fallback chain
    from llm import get_client as _fb_client
    return _fb_client()


def load_data():
    sources = {}
    files = {
        "portal_listings": ("Property listings collected from online portals", ["propi_listings.csv", "listings.csv"]),
        "buildings": ("Building and address directory", ["propi_buildings.csv", "buildings.csv"]),
    }
    for key, (desc, candidates) in files.items():
        path = None
        for fn in candidates:
            p = os.path.join(DATA_DIR, fn)
            if os.path.exists(p):
                path = p
                break
        if path is None:
            continue
        if os.path.exists(path):
            df = pd.read_csv(path)
            if not df.empty:
                if key == "portal_listings":
                    df = _prepare_listings(df)
                sources[key] = {"df": df, "description": desc}
    return sources


def _get_supabase_db():
    global _supabase_storage
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not supabase_key:
        return None
    try:
        if _supabase_storage is None:
            from storage import SupabaseStorage
            _supabase_storage = SupabaseStorage(supabase_url, supabase_key)
        return _supabase_storage.db
    except Exception:
        return None


def load_live_data(db_path):
    """Load live tables as additional sources with broker-friendly names."""
    con = None
    if hasattr(db_path, "execute"):
        con = db_path
    elif os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY"):
        con = _get_supabase_db()
    if con is None:
        return {}
    sources = {}

    raw_cnt = con.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]
    parsed_cnt = con.execute("SELECT COUNT(*) FROM parsed_output_unified").fetchone()[0]
    sources["overview"] = {
        "df": pd.DataFrame([{
            "total_messages": raw_cnt,
            "total_properties_posted": parsed_cnt,
            "total_brokers": con.execute("SELECT COUNT(*) FROM brokers").fetchone()[0],
            "unique_properties": con.execute("SELECT COUNT(*) FROM listings_unified").fetchone()[0],
            "building_matches_found": con.execute("SELECT COUNT(*) FROM resolver_decisions WHERE method IS NOT NULL AND method != 'unresolved' AND building_id IS NOT NULL").fetchone()[0],
        }]),
        "description": "Platform overview with total counts of messages, properties, brokers, and matched buildings",
    }

    brokers = con.execute(
        "SELECT canonical_name AS name, primary_phone AS phone, "
        "observation_count AS total_posts, listing_count AS properties_posted, "
        "requirement_count AS requirements_posted, rental_count AS rentals_posted, "
        "commercial_count AS commercial_posted, group_count AS groups_active, "
        "market_count AS markets_served, "
        "avg_ticket AS average_price, first_seen_at AS first_active, last_seen_at AS last_active "
        "FROM brokers ORDER BY observation_count DESC LIMIT 2000"
    ).fetchall()
    if brokers:
        df = pd.DataFrame([dict(r) for r in brokers])
        if "average_price" in df.columns:
            df["average_price"] = pd.to_numeric(df["average_price"], errors="coerce")
        sources["brokers"] = {"df": df, "description": "Brokers with their activity, markets, and average prices"}

    listings = con.execute(
        "SELECT fingerprint, intent, bhk, price, price_unit, area_sqft, furnishing, "
        "location_label AS area, building_name, landmark_name, micro_market, "
        "broker_name, broker_phone, "
        "observation_count AS times_seen, group_count AS groups_seen_in, "
        "first_seen, last_seen FROM listings_unified ORDER BY last_seen DESC LIMIT 5000"
    ).fetchall()
    if listings:
        sources["unique_listings"] = {"df": pd.DataFrame([dict(r) for r in listings]),
                                       "description": "Unique properties posted in WhatsApp groups"}

    obs = con.execute(
        "SELECT p.intent AS purpose, p.bhk, p.price, p.price_unit, p.area_sqft, "
        "p.furnishing, p.building_name, p.micro_market AS locality, "
        "p.broker_name, p.broker_phone, "
        # parsed_output_unified is a live projection over the typed tables;
        # it intentionally does not expose the legacy forwarded column.
        "p.created_at AS posted_at, "
        "r.group_name AS group_name, r.sender AS posted_by, r.timestamp "
        "FROM parsed_output_unified p JOIN raw_messages r ON r.id = p.raw_message_id "
        "ORDER BY p.id DESC LIMIT 10000"
    ).fetchall()
    if obs:
        df = pd.DataFrame([dict(r) for r in obs])
        for c in ["price", "area_sqft"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        sources["market_feed"] = {"df": df, "description": "Recent property posts and requirements posted in WhatsApp groups"}

    resolved = con.execute(
        "SELECT rd.building_name AS matched_building, rd.landmark_name AS matched_landmark, "
        "p.intent AS purpose, p.micro_market AS locality, "
        "rd.method AS match_status, rd.final_confidence AS match_confidence, "
        "rd.failure_category, rd.created_at "
        "FROM resolver_decisions rd JOIN parsed_output_unified p ON p.id = rd.parsed_id "
        "ORDER BY rd.id DESC LIMIT 10000"
    ).fetchall()
    if resolved:
        sources["building_matches"] = {"df": pd.DataFrame([dict(r) for r in resolved]),
                                        "description": "Which properties were matched to known buildings and landmarks"}

    # Unresolved messages (parser gaps)
    unresolved = con.execute("""
        SELECT p.id, p.intent, p.bhk, p.price, p.micro_market,
               p.broker_name, p.created_at,
               r.message, r.group_name, r.timestamp,
               d.method, d.failure_category
        FROM parsed_output_unified p
        JOIN raw_messages r ON r.id = p.raw_message_id
        LEFT JOIN resolver_decisions d ON d.parsed_id = p.id
        WHERE d.method = 'unresolved'
        ORDER BY p.id DESC
        LIMIT 500
    """).fetchall()
    if unresolved:
        sources["unresolved_messages"] = {"df": pd.DataFrame([dict(r) for r in unresolved]),
                                           "description": "Messages the parser couldn't fully understand or resolve — needs human review"}

    # Pending AI suggestions
    suggestions = con.execute("""
        SELECT id, agent, suggestion_type, title, description, confidence, status, created_at
        FROM ai_suggestions
        WHERE status = 'pending'
        ORDER BY created_at DESC
        LIMIT 200
    """).fetchall()
    if suggestions:
        sources["pending_suggestions"] = {"df": pd.DataFrame([dict(r) for r in suggestions]),
                                           "description": "AI suggestions waiting for human review and approval"}

    con.close()
    return sources


def build_overview(sources):
    lines = []
    for key, src in sources.items():
        df = src["df"]
        lines.append(f"-- {src['description']} --")
        lines.append(f"  Rows: {len(df)}, Columns: {len(df.columns)}")
    return "\n".join(lines)


def _read_prompt_file(name: str) -> str:
    try:
        return (PROMPT_DIR / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _broker_context_block(broker=None) -> str:
    """Return private, relevant identity context for the current chat owner."""
    if not broker:
        return ""

    name = str(broker.get("name") or "").strip() or "the current broker"
    city = str(broker.get("city") or "").strip()
    location = f" based in {city}" if city else ""
    lines = [
        "\nCURRENT BROKER CONTEXT (private system context):",
        f"You are assisting {name}{location}.",
        "Use this context only when relevant, especially for identity questions or explicit 'my' queries. ",
        "Do not volunteer it, repeat it, or introduce the broker in unrelated property answers.",
    ]

    labels = (
        ("listing_count", "tracked listings"),
        ("requirement_count", "tracked requirements"),
        ("active_days_30", "active days in the last 30 days"),
        ("avg_ticket", "recorded average ticket"),
        ("market_count", "markets covered"),
        ("building_count", "buildings covered"),
    )
    stats = []
    for key, label in labels:
        value = broker.get(key)
        if value is not None and str(value).strip() != "":
            stats.append(f"{value} {label}")
    if stats:
        lines.append("Broker activity: " + "; ".join(stats) + ".")
    if broker.get("email"):
        lines.append(f"Account email: {broker['email']}.")
    return "\n".join(lines)


def build_conversational_system_prompt(broker=None):
    """Minimal system prompt for pure conversation — no data, no tools, no JSON contract."""
    identity = _read_prompt_file("identity.md")
    now = datetime.datetime.now()
    time_str = now.strftime("%A, %d %B %Y at %I:%M %p GST")
    broker_line = _broker_context_block(broker)
    return f"""{identity or "You are PropAI, a Dubai real-estate broker assistant."}

Current date and time: {time_str}

You are PropAI's conversational mode. Do NOT use any tools or search any databases.

Users may write in English, Arabic, Hindi/Hinglish, Urdu or Russian - always reply in the same language the user used in their latest message.{broker_line}

The user is just chatting. They may ask about buildings, localities, or property types — treat these as queries, not self-introductions. For example, "in Kalpataru Sparkle" means they're asking about that building, not saying their name is Kalpataru Sparkle.

Respond naturally as a helpful broker assistant would — brief, warm, and human. No JSON, no structured output, no markdown unless the user asks for it.

Never say "Ready.", "How can I assist?", or "What would you like to do?" — sound human.

If the user greets you, greet them back naturally. If they ask how you are, say something genuine. If they thank you, acknowledge it. If they say goodbye, wish them well.
"""


def _legacy_build_system_prompt(sources, broker=None):
    overview = build_overview(sources)
    identity = _read_prompt_file("identity.md")
    bootstrap = _read_prompt_file("bootstrap.md")
    now = datetime.datetime.now()
    time_str = now.strftime("%A, %d %B %Y at %I:%M %p GST")
    broker_line = _broker_context_block(broker)
    return f"""{identity or "You are PropAI, a Dubai real-estate broker assistant."}

{bootstrap}

Current date and time: {time_str}{broker_line}

You are also PropAI's Dynamic AI Workspace for structured market database work.

Users may write in English, Arabic, Hindi/Hinglish, Urdu or Russian - always reply in the same language the user used in their latest message.

AVAILABLE DATA:
{overview}

LISTING CARD FORMAT (when showing listings in workspace UI):
Building Name
AED Price / year (or sale)
BHK | Area | Furnishing
Micro Market | Building | Broker
First Seen | Last Seen | Observed (count messages)
Confidence: XX%
Actions: View | Open Inventory | Open Original Messages | Promote | Save | Connect Broker

CONVERSATION-FIRST RULE:
Before calling any tool, ask yourself: "Can I answer this from the conversation context alone?"
If yes, just respond naturally. Do not call tools.
Only call tools when the user explicitly asks for data retrieval, searching, or an action.

CONTRACT TRIGGER — READ BEFORE CHOOSING OUTPUT FORMAT:
If your response surfaces ANY real retrieved value — a price, a listing, a count, a broker name, a locality stat — you are in the data-query path. Always emit the JSON contract below, even if you are also asking a clarifying follow-up question. A clarifying question does NOT downgrade a response to "casual chat." Only skip the contract when NO retrieved data appears anywhere in the response (pure greetings, thanks, identity questions).

INTRODUCTION vs. REQUIREMENT — DO NOT CONFUSE THESE:
"I'm Rahul" / "This is Suresh" = introduction. Acknowledge naturally, no tools.
"I have a client looking for X" / "I have a buyer who wants Y" = a REQUIREMENT, not an introduction,
even though it starts with "I have/I am." If the message contains ANY concrete filter — BHK, locality,
budget, furnishing, intent — you MUST call market_search and use the JSON contract. Never acknowledge
a requirement message the way you'd acknowledge a name introduction.

Example — WRONG:
User: "I have a client looking for a fully furnished 2BR in Dubai Marina, budget up to 120K/year"
Bad: "Nice to meet you! How can I help?"

Example — RIGHT:
User: "I have a client looking for a fully furnished 2BR in Dubai Marina, budget up to 120K/year"
Good: [calls market_search with intent=RENT, bhk=2, building/locality=Dubai Marina,
furnishing=Furnished, price_max=120000] then returns the JSON contract with listing_cards.

FINAL RESPONSE CONTRACT:
- For greetings, casual chat, small talk, introductions, or anything you can answer from conversation: SKIP this contract entirely. Return plain text. No tools. No JSON.
- For actual data queries: Return JSON only. No markdown fences, no prose outside JSON.
- Shape:
  {{
    "content": "Short plain-language summary",
    "blocks": [{{"type": "summary", ...}}],
    "sources": ["overview", "portal_listings"],
    "status_steps": ["Searching listings", "Ranking results", "Rendering"],
    "trace": {{"sources": ["WhatsApp groups", "buildings"], "last_updated": "GST timestamp"}}
  }}
- Use only these block types:
  summary, listing_cards, buyer_cards, broker_cards, building_card, market_card, table, timeline, map, comparison, original_messages, ai_suggestions, charts, export_panel, promotion_preview, property_gallery, related_listings, matching_buyers, suggested_questions, error_state, empty_state, loading

FEW-SHOT — CONTRACT TRIGGER vs. CHAT (memorize this pattern):
BAD (do not do this):
**Recent Activity Snapshot (last 7 days):**
• **Rent — 3 BHK** — 2.2 Lac/month in Dindoshi
To give you actual trends, I'd need to aggregate by locality + BHK. Want me to pull that?

GOOD (do this instead):
{{
  "content": "I found a few recent posts, but I need locality + BHK to make the answer useful. Which ones matter?",
  "blocks": [{{"type": "listing_cards", "items": [
    {{"title": "3 BHK Rent, Dindoshi (Park Altezza)", "price": "2.2L/month", "furnishing": "Fully Furnished"}},
    {{"title": "1 BHK Rent, Andheri West", "price": "50K/month", "area_sqft": 330}}
  ]}}],
  "sources": ["market_feed"],
  "status_steps": ["Checking recent feed", "Grouping relevant matches"],
  "trace": {{"sources": ["WhatsApp groups"], "last_updated": "2026-07-13T10:33:00+05:30"}}
}}

- Never invent property details. If a fact is missing, surface it as missing.
- Keep content short. The UI will render the blocks.

EXPORTS:
Always offer: Export CSV | Export Excel | Export PDF | Copy WhatsApp Summary | Copy Email Summary

PRICE UNIT NORMALIZATION (IMPORTANT):
When user mentions prices, normalize to standard units:
- M = Mn = Million = Millions (same thing)
- K = Thousand (same thing)
- AED or Dhs or Dirhams = Absolute dirhams (e.g., AED 1500000 = 1.5M)
- Rents are ANNUAL totals unless explicitly marked "/month".

When you see a price like "85K", treat it as AED 85,000 per year.
When you see "1.5M" or "1.5 Million", treat it as AED 1,500,000.
When user says "80 to 120K budget", they mean AED 80,000 to AED 120,000 per year.

If you're unsure about a unit, use ask_clarification to ask the user.
When user teaches you a new unit mapping, use save_unit_alias to remember it.

You can also learn from context: if user says "85K rent", it's AED 85,000/year.
Common patterns:
- "2BR for rent in Marina 100-120K" = 2 bedrooms, rent AED 100,000-120,000/year
- "2M apartment" = AED 2,000,000 purchase price
- "85000 yearly" = AED 85,000/year (absolute dirhams)"""


def build_system_prompt(sources, broker=None, workspace_settings=None):
    """Build the shared workspace policy used by the live assistant.

    Search routing happens in code, so the model receives a small, current
    policy rather than a second copy of every product workflow.
    """
    identity = _read_prompt_file("identity.md")
    bootstrap = _read_prompt_file("bootstrap.md")
    now = datetime.datetime.now().strftime("%A, %d %B %Y at %I:%M %p GST")
    broker_line = _broker_context_block(broker)
    browser_enabled = bool(getattr(workspace_settings, "browser_enabled", False)) if workspace_settings else False
    browser_capability = """BROWSER CAPABILITY:
This workspace has an Agent Browser that can inspect internal PropAI pages and
approved external websites. Do not say that you lack access to external
websites when the user asks for a live web check. Ask for browser approval
through the normal permission flow. Once the user approves, honor the user's
safe browsing task rather than limiting the task to prebuilt workflows. The
MahaRERA project/construction and IGR Maharashtra/e-Search workflows are
optimized paths, not the limit of browser capability. Ask the user to complete
login/CAPTCHA or provide missing identifiers when required. Never invent a
status if the portal blocks access.
""" if browser_enabled else """BROWSER CAPABILITY:
Browser actions are disabled for this workspace. Do not claim to have opened
or inspected a website; explain that browser access must be enabled first.
"""
    return f"""{identity or 'You are PropAI, a Dubai real-estate broker assistant.'}

{bootstrap}

Current date and time: {now}{broker_line}

You are in the PropAI workspace.

Users may write in English, Arabic, Hindi/Hinglish, Urdu or Russian - always reply in the same language the user used in their latest message.

Available sources are summarized below; use
only retrieved values as facts. Concrete property and requirement requests are
searched against the live tenant-scoped marketplace before you answer. Never
claim that the database is unavailable when verified search results are present.

PLANNER RULE — READ FIRST:
If the user's latest message contains any concrete filter (BHK, locality,
building name, price range, transaction type, furnishing, parking, pets),
you MUST call a live tool before producing any listing card, count claim, or
list. Prefer `search_listings` for listings and `match_client_to_listings`
for client matching. Never use the legacy CSV/SQLite search path. Never emit
a count like "Found 119 listings" without a matching tool result in this
turn. Never invent listings, broker names, addresses, or phone numbers —
every listing card you render must carry listing_id / message_id / cluster_id
so readers can verify the source.
If a search returns zero rows, say zero. If you want to ask a clarifying
question, do it AFTER the search, not before.

CONFIDENCE RULE:
After you return a result, state how confident you are (0.0-1.0). If the
search returned fuzzy matches (limited rows, locality inferred), say so.
If your own confidence is below 0.6, surface the uncertainty in your
`content` field instead of presenting the matches as certain.

CONVERSATION MEMORY:
A working memory keeps your last known filters (area, BHK, intent, price,
kind of building). When the user says "youve just repeated data" or
"the same as before", do NOT re-run a search — re-use the last shown
listings and respond conversationally. If the user switches topic,
the previous topic is auto-summarized and you will receive it.

NO DUPLICATE OUTPUT RULE:
Do not emit the same paragraph twice in the same response. Do not quote
your own previous turn verbatim unless the user explicitly requested it
("repeat that", "show again"). The renderer now drops duplicated text
automatically, so non-compliance produces an empty response — please
follow this rule so the user sees the right content.

AGENT VOICE:
Write like a sharp property assistant, not like a parser.
Never lead with "Parsed request", "Searched live marketplace", or any
similar pipeline label in the visible `content` field.
If you search, summarize the result in one human sentence first, such as
"I found 12 3 BHK rentals in Bandra West within budget."
Use `status_steps` only for short, human-readable progress hints and keep
them optional.

AVAILABLE DATA:
{build_overview(sources)}

WORKSPACE AGENT POLICY:
Use the workspace's own configured API keys and limits when deciding how many
tool rounds to take or whether browser work is allowed. If browser use is
enabled for this workspace, treat browser actions as allowed only within the
configured routes/actions and keep the browser session traceable. A route of
`*` explicitly permits external URLs as well as internal PropAI pages. If
browser use is disabled, do not invent browser actions. Never claim you opened
a web page, clicked a listing, or inspected a browser page unless a browser
tool actually returned that result in this turn. If browser use is disabled or
unavailable, say that plainly and continue in text-only mode; do not say the
"workspace is not active" or imply hidden browser access you do not have.

{browser_capability}

For workspace data, return valid JSON with a short `content` field and UI
`blocks`. Keep the `content` field as concise GitHub Flavored Markdown.
Use `summary`, `listing_cards`, `broker_cards`, `table`, `empty_state`,
or `error_state`. Every listing_cards item MUST include at least one of:
listing_id, message_id, cluster_id, raw_message_id,
whatsapp_message_id. Items missing these fields are removed by the
renderer and replaced with an error_state block. The chat surface renders
the structured blocks as Markdown tables, so do not rely on card-style UI
for readability."""


WORKSPACE_BLOCK_TYPES = {
    "summary",
    "listing_cards",
    "buyer_cards",
    "broker_cards",
    "building_card",
    "market_card",
    "table",
    "timeline",
    "map",
    "comparison",
    "original_messages",
    "ai_suggestions",
    "charts",
    "export_panel",
    "promotion_preview",
    "property_gallery",
    "related_listings",
    "matching_buyers",
    "suggested_questions",
    "error_state",
    "empty_state",
    "loading",
    "greeting",
    "activity",
}


_THINK_TAG_RE = re.compile(r"<think[^>]*>.*?</think>|</think>", re.DOTALL | re.IGNORECASE)
_LISTING_PROVENANCE_FIELDS = (
    "listing_id",
    "message_id",
    "cluster_id",
    "raw_message_id",
    "whatsapp_message_id",
)


def _collapse_halved_repeat(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) < 40:
        return cleaned
    if len(cleaned) % 2 == 0:
        half = len(cleaned) // 2
        if cleaned[:half] == cleaned[half:]:
            return cleaned[:half].strip()
    # A model that echoes its answer after a dangling "</think>" often leaves a
    # separating newline between the two copies; tolerate that whitespace.
    for split in range(40, len(cleaned) // 2 + 1):
        first, rest = cleaned[:split], cleaned[split:]
        if rest.lstrip() == first:
            return first
    return cleaned


def strip_think_blocks(text) -> str:
    if not text:
        return ""
    cleaned = _THINK_TAG_RE.sub("", text)
    cleaned = _collapse_halved_repeat(cleaned)
    return cleaned.strip()


def _drop_echoed_assistant_content(messages: list[dict]) -> None:
    """If the most recent assistant message was immediately followed by tool
    results and a second assistant turn produced prose that matches the
    earlier prose byte-for-byte, collapse the second copy into an empty
    content string. Stops the LLM from quoting itself when re-prompted."""
    for idx in range(len(messages) - 1, 0, -1):
        msg = messages[idx]
        if msg.get("role") != "assistant":
            continue
        prev_assistant = None
        for j in range(idx - 1, -1, -1):
            if messages[j].get("role") == "assistant":
                prev_assistant = messages[j]
                break
        if prev_assistant is None:
            return
        prev_text = strip_think_blocks(prev_assistant.get("content") or "").strip()
        cur_text = strip_think_blocks(msg.get("content") or "").strip()
        if (
            prev_text
            and cur_text
            and prev_text == cur_text
            and msg.get("tool_calls")
        ):
            msg["content"] = ""
        return


def _has_provenance(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    return any(item.get(field) for field in _LISTING_PROVENANCE_FIELDS)


def _purify_listing_blocks(blocks: list[dict]) -> tuple[list[dict], int]:
    """Drop listing_cards items that carry no provenance field. A single
    item that lacks listing_id / message_id / cluster_id is treated as
    hallucinated inventory and removed. If everything in a block is
    purgable, the block collapses to an error_state explaining why."""
    cleaned: list[dict] = []
    dropped_count = 0
    for block in blocks:
        if not isinstance(block, dict):
            cleaned.append(block)
            continue
        if block.get("type") in ("listing_cards", "buyer_cards", "broker_cards"):
            items = block.get("items")
            if isinstance(items, list):
                kept = [it for it in items if _has_provenance(it) or not isinstance(it, dict)]
                removed = len(items) - len(kept) if all(isinstance(it, dict) for it in items) else 0
                dropped_count += max(removed, 0)
                if not kept:
                    cleaned.append({
                        "type": "error_state",
                        "title": "Listings unavailable",
                        "body": "PropAI didn't return a verifiable source for these listings. Please refresh or rephrase your search.",
                    })
                    continue
                block = {**block, "items": kept}
        cleaned.append(block)
    return cleaned, dropped_count


def _normalize_real_phone(value) -> str:
    """Return the 10-digit real phone number, or '' when the value isn't a real line."""
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 10:
        return digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits[-10:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[-10:]
    return ""


def _strip_json_fences(text: str) -> str:
    cleaned = strip_think_blocks(text)
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _load_json_payload(text: str):
    cleaned = _strip_json_fences(text)
    try:
        return json.loads(cleaned)
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except Exception:
                return None
    return None


def _normalize_block(block):
    if not isinstance(block, dict):
        return None
    block_type = str(block.get("type", "")).strip()
    if block_type not in WORKSPACE_BLOCK_TYPES:
        return None
    normalized = {"type": block_type}
    for key in (
        "title",
        "subtitle",
        "body",
        "summary",
        "description",
        "note",
        "items",
        "results",
        "rows",
        "columns",
        "metrics",
        "bullets",
        "actions",
        "cards",
        "events",
        "questions",
        "trace",
        "sources",
        "status",
        "status_steps",
        "content",
        "prompt",
        "channels",
        "steps",
        "highlights",
        "hashtags",
        "cta",
        "headline",
    ):
        if key in block:
            normalized[key] = block[key]
    return normalized


def normalize_workspace_response(content: str | None, sources: dict):
    raw_text = (content or "").strip()
    parsed = _load_json_payload(raw_text) if raw_text else None
    source_names = list(sources.keys())

    if isinstance(parsed, dict):
        blocks = []
        for block in parsed.get("blocks", []) or []:
            normalized = _normalize_block(block)
            if normalized:
                blocks.append(normalized)
        blocks, dropped_listings = _purify_listing_blocks(blocks)
        response = {
            "content": str(parsed.get("content") or parsed.get("summary") or raw_text).strip(),
            "blocks": blocks,
            "sources": parsed.get("sources") if isinstance(parsed.get("sources"), list) and parsed.get("sources") else source_names,
            "status_steps": parsed.get("status_steps") if isinstance(parsed.get("status_steps"), list) else [],
            "trace": parsed.get("trace") if isinstance(parsed.get("trace"), dict) else {"sources": source_names},
        }
        if not response["blocks"]:
            response["blocks"] = [
                {
                    "type": "summary",
                    "title": "Answer",
                    "body": response["content"] or "The assistant returned no blocks.",
                }
            ]
        if not response["content"]:
            first = response["blocks"][0]
            response["content"] = str(first.get("body") or first.get("summary") or first.get("title") or "").strip()
        return response

    fallback_text = raw_text or "The assistant returned no response."
    return {
        "content": fallback_text,
        "blocks": [
            {
                "type": "greeting",
                "body": fallback_text,
            }
        ],
        "sources": source_names,
        "status_steps": [],
        "trace": {"sources": source_names},
    }


def _suggestion_tool():
    return {
        "type": "function",
        "function": {
            "name": "create_suggestion",
            "description": "Create a Review Center suggestion that needs human approval. Use this when the user asks you to make changes — like creating a building, merging brokers, adding aliases, flagging data issues. The suggestion will appear in the Review Center for approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "enum": ["building", "location", "merge_broker", "duplicate_listing", "alias", "quality", "user_request"],
                        "description": "Which agent category this suggestion belongs to",
                    },
                    "suggestion_type": {
                        "type": "string",
                        "description": "Type of action — e.g. 'create_alias', 'merge', 'flag', 'add_building', 'review'",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title for the suggestion card (e.g. 'Create building: Chandak Unicorn')",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed description explaining what needs to be done and why",
                    },
                    "proposal_data": {
                        "type": "object",
                        "description": "Structured data with the proposed action details",
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence in this suggestion (0.0 to 1.0). Use 0.85 for AI-generated, 0.95 for clear deterministic matches.",
                    },
                },
                "required": ["agent", "suggestion_type", "title", "description", "proposal_data", "confidence"],
            },
        },
    }


def _build_tools(sources, prefer_supabase_agent: bool = False, browser_enabled: bool = False):
    source_keys = sorted(sources.keys())
    tools = [
        _suggestion_tool(),
    ]
    if not prefer_supabase_agent:
        tools.append(_market_search_tool())
    tools.extend([
        _search_jid_memory_tool(),
        {
            "type": "function",
            "function": {
                "name": "query_data",
                "description": "Search, filter, aggregate, or list records from any dataset",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "enum": source_keys,
                            "description": f"Which dataset to query. Available: {', '.join(source_keys)}",
                        },
                        "filters": {
                            "type": "object",
                            "description": "Column->value filters (exact match, case-insensitive)."
                            "For partial text match use {{'col__contains': 'text'}}."
                            "For numeric ranges use {{'col__lt': N, 'col__gt': N, 'col__lte': N, 'col__gte': N}}.",
                        },
                        "aggregate": {
                            "type": "string",
                            "enum": ["count", "list", "avg", "min", "max", "none"],
                            "description": "What to do with matching rows (default: list)",
                        },
                        "group_by": {
                            "type": "string",
                            "description": "Column to group by when using count/avg/min/max aggregate",
                        },
                        "sort_by": {"type": "string", "description": "Column to sort results by"},
                        "ascending": {"type": "boolean", "description": "Sort ascending (default: true)"},
                        "limit": {"type": "integer", "description": "Max rows to return (default 20)"},
                    },
                    "required": ["source"],
                },
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_overview",
                "description": "Get an overview of all available datasets (schema, row counts, sample values)",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "find_parser_gaps",
                "description": "Find messages the parser couldn't understand — unresolved locations, low confidence parses, missing fields. Helps identify what knowledge the system is missing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["unresolved_location", "low_confidence", "missing_bhk", "missing_price", "no_intent", "all"],
                            "description": "Category of parser gaps to find",
                        },
                        "limit": {"type": "integer", "description": "Max results (default 10)"},
                    },
                    "required": ["category"],
                },
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_raw_messages",
                "description": "Search across all raw WhatsApp messages (groups and DMs). Returns matching messages with sender, group, timestamp. Use for finding specific conversations, mentions of buildings/brokers, or any text content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (supports natural language)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 10)",
                        },
                    },
                    "required": ["query"],
                },
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_sender_history",
                "description": "Get message history and profile for a specific sender. Shows their buildings, markets, BHK configs, and recent messages.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sender": {
                            "type": "string",
                            "description": "Sender name or phone number to look up",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max messages to return (default 20)",
                        },
                    },
                    "required": ["sender"],
                },
            }
        },
        {
            "type": "function",
            "function": {
                "name": "ask_clarification",
                "description": "Ask the user for clarification when you're confused about units, terms, or ambiguous input. Use this when you don't understand what the user means (e.g., '1.5M' could mean 1.5 million AED).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The clarification question to ask the user",
                        },
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Possible interpretation options (optional)",
                        },
                    },
                    "required": ["question"],
                },
            }
        },
        {
            "type": "function",
            "function": {
                "name": "save_unit_alias",
                "description": "Save a learned unit alias. Use when the user teaches you that a term means a specific unit (e.g., 'L means Lakhs'). This helps PropAI learn and remember.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "alias": {
                            "type": "string",
                            "description": "The term/alias to remember (e.g., 'M', 'mn', 'K')",
                        },
                        "canonical_unit": {
                            "type": "string",
                            "enum": ["K", "M", "abs"],
                            "description": "What unit this maps to: M=Millions, K=Thousands, abs=Absolute dirhams",
                        },
                    },
                    "required": ["alias", "canonical_unit"],
                },
            }
        },
        {
            "type": "function",
            "function": {
                "name": "send_whatsapp",
                "description": "Send a WhatsApp message to a specific phone number. Use this to proactively message brokers or clients on behalf of the user when requested (e.g. 'send this requirement to broker X').",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to_phone": {
                            "type": "string",
                            "description": "The phone number to send the message to (in 91XXXXXXXXXX format, no +)"
                        },
                        "text": {
                            "type": "string",
                            "description": "The message text to send"
                        }
                    },
                    "required": ["to_phone", "text"]
                }
            }
        },
    ])
    from agent_tools import TOOL_DEFINITIONS, BROWSER_TOOL_DEFINITIONS
    tools.extend(TOOL_DEFINITIONS)
    if browser_enabled:
        tools.extend(BROWSER_TOOL_DEFINITIONS)
    return tools


def _parse_price(val):
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace(",", "").strip().lower()
    for token in ("aed", "dhs", "dirham"):
        s = s.replace(token, "").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|mn|millions?|millions|k)?", s)
    if not m:
        try:
            return float(s)
        except ValueError:
            return None
    amount = float(m.group(1))
    multipliers = {"m": 1_000_000, "mn": 1_000_000, "million": 1_000_000, "millions": 1_000_000, "k": 1_000}
    return amount * multipliers.get(m.group(2) or "", 1)


def _prepare_listings(df):
    if "price" in df.columns and "price_numeric" not in df.columns:
        df["price_numeric"] = df["price"].apply(_parse_price)
    return df


_PRICE_COLS = {"price", "price_numeric"}


def _normalize_browser_provider_name(provider_name: str | None) -> str:
    normalized = str(provider_name or "").strip().lower()
    if normalized in {"", "browser-use", "playwright", "browser-use-cli", "browser_use", "agent-browser"}:
        return "agent-browser"
    return normalized


def _browser_settings_row(storage_client, tenant_id: str | None) -> dict:
    if storage_client is None or not tenant_id:
        return {}
    try:
        rows = (
            storage_client.table("workspace_ai_settings")
            .select("tenant_id,browser_enabled,browser_provider,allowed_routes,allowed_actions,max_browser_sessions,max_tool_rounds,notes")
            .eq("tenant_id", tenant_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else {}
    except Exception:
        return {}


def _browser_policy_error(settings: dict, command: str, url: str = "") -> str:
    """Return a policy error, or an empty string when browser work is allowed."""
    allowed_actions = {str(item).strip().lower() for item in (settings.get("allowed_actions") or []) if str(item).strip()}
    action = "state" if command == "state" else command
    # Reading state is required to render a trace and is always safe once the
    # workspace has enabled browser access. Other actions remain configurable.
    if action != "state" and allowed_actions and "*" not in allowed_actions and action not in allowed_actions:
        return f"Browser action '{action}' is not allowed for this workspace"
    if not url or command != "open":
        return ""
    allowed_routes = [str(item).strip() for item in (settings.get("allowed_routes") or []) if str(item).strip()]
    if not allowed_routes or "*" in allowed_routes:
        return ""
    parsed = urlparse(url)
    candidate = f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"
    if any(fnmatch(candidate, route) or fnmatch(url, route) for route in allowed_routes):
        return ""
    return "This website is not in the workspace browser allowlist"


def _upsert_browser_session(storage_client, tenant_id: str | None, session_id: str, values: dict) -> dict | None:
    if storage_client is None or not tenant_id or not session_id:
        return None
    data = {"id": session_id, "tenant_id": tenant_id, **values}
    res = storage_client.table("agent_browser_sessions").upsert(data).execute().data or []
    return res[0] if res else None


def _log_browser_step(storage_client, tenant_id: str | None, session_id: str, step_index: int, action: str, target: str, url: str, status: str, metadata: dict | None = None, screenshot_url: str = "") -> None:
    if storage_client is None or not tenant_id or not session_id:
        return
    payload = {
        "tenant_id": tenant_id,
        "browser_session_id": session_id,
        "step_index": step_index,
        "action": action,
        "target": target,
        "url": url,
        "status": status if status in {"ok", "failed", "skipped"} else "failed",
        "metadata": metadata or {},
        # The database column is NOT NULL; an absent screenshot is an empty
        # string, not SQL NULL.
        "screenshot_url": screenshot_url or "",
    }
    try:
        storage_client.table("agent_browser_steps").insert(payload).execute()
    except Exception:
        _logger.exception("Failed to persist browser step for session %s", session_id)


def _browser_runtime_response(result, *, session_id: str, command: str) -> dict:
    return {
        "status": result.status,
        "tool": command,
        "browser_session_id": session_id,
        "provider": result.provider,
        "url": result.url,
        "title": result.title,
        "summary": result.summary,
        "elements": result.elements,
        "screenshot_path": result.screenshot_path,
        "raw_output": result.raw_output,
        "error": result.error,
    }


def _market_search_tool():
    return {
        "type": "function",
        "function": {
            "name": "market_search",
            "description": "Search PropAI's database for property listings. Returns structured results with building grouping, traceability, match reasons, and pagination info. Use this for ALL listing searches — never use query_data for listings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "enum": ["RENT", "SELL", "BUY", "RENTAL_SEEKER"],
                        "description": "Filter by intent: RENT, SELL, BUY, RENTAL_SEEKER",
                    },
                    "bhk": {
                        "type": "string",
                        "description": "BHK filter: 1, 1.5, 2, 2.5, 3, 4, 5, or 'any'",
                    },
                    "building": {
                        "type": "string",
                        "description": "Building name or alias (supports partial match, aliases like 'X BKC' = 'X One BKC')",
                    },
                    "micro_market": {
                        "type": "string",
                        "description": "Micro market / locality name (e.g. 'Bandra East', 'BKC', 'Andheri West')",
                    },
                    "micro_markets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Alternative micro markets to search together, e.g. ['JVC', 'Dubai Marina']",
                    },
                    "price_max": {
                        "type": "string",
                        "description": "Maximum price filter (in AED per year for rents, e.g. 2000000 for AED 2M)",
                    },
                    "price_min": {
                        "type": "string",
                        "description": "Minimum price filter (in AED per year for rents)",
                    },
                    "furnishing": {
                        "type": "string",
                        "enum": ["Furnished", "Semi Furnished", "Unfurnished", "any"],
                        "description": "Furnishing filter",
                    },
                    "broker": {
                        "type": "string",
                        "description": "Broker name (partial match)",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["price", "last_seen", "observation_count", "confidence"],
                        "description": "Sort results by field (default: last_seen)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results per page (default 10)",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset (default 0)",
                    },
                    "group_by_building": {
                        "type": "boolean",
                        "description": "Group results by building name (default true)",
                    },
                },
                "required": [],
            },
        },
    }


def _search_jid_memory_tool():
    return {
        "type": "function",
        "function": {
            "name": "search_jid_memory",
            "description": "Search PropAI's WhatsApp JID memory. Use this for broker/person history, aliases, frequent localities/buildings, requirements posted, listings posted, and raw message retrieval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Name, phone, locality, building, or natural language text to search in JID memory and raw messages.",
                    },
                    "message_kind": {
                        "type": "string",
                        "enum": ["listing", "requirement", "any"],
                        "description": "Filter by remembered message kind.",
                    },
                    "locality": {"type": "string", "description": "Locality / micro-market filter."},
                    "building": {"type": "string", "description": "Building name filter."},
                    "bhk": {"type": "string", "description": "BHK filter, e.g. 3 BHK."},
                    "limit": {"type": "integer", "description": "Max profiles/messages to return."},
                },
            },
        },
    }


def apply_filters(df, filters):
    if not filters:
        return df
    for key, value in filters.items():
        raw_col = key.replace("__contains", "").replace("__lt", "").replace("__gt", "").replace("__lte", "").replace("__gte", "")
        col = raw_col
        if col in _PRICE_COLS and "price_numeric" in df.columns:
            col = "price_numeric"
        if key.endswith("__contains"):
            df = df[df[col].astype(str).str.contains(str(value), case=False, na=False)]
        elif key.endswith("__lt"):
            df = df[df[col].astype(float) < float(value)]
        elif key.endswith("__gt"):
            df = df[df[col].astype(float) > float(value)]
        elif key.endswith("__lte"):
            col = key.replace("__lte", "")
            df = df[df[col].astype(float) <= float(value)]
        elif key.endswith("__gte"):
            col = key.replace("__gte", "")
            df = df[df[col].astype(float) >= float(value)]
        else:
            df = df[df[key].astype(str).str.lower() == str(value).lower()]
    return df


def fmt_price(val):
    try:
        v = float(val)
        if v >= 1_000_000:
            return f"AED {v / 1_000_000:.2f}M"
        elif v >= 1_000:
            return f"AED {round(v / 1_000):g}K"
        else:
            return f"AED {v:,.0f}"
    except (ValueError, TypeError):
        return str(val)


def fmt_listing_price(val, unit=None, intent=None):
    if val in (None, ""):
        return ""
    try:
        v = float(val)
    except (ValueError, TypeError):
        return str(val)

    normalized_unit = str(unit or "").strip().lower()
    suffix = "/yr" if str(intent or "").upper() == "RENT" else ""
    if normalized_unit in {"m", "mn", "million", "millions"}:
        return f"AED {v:g}M{suffix}"
    if normalized_unit == "k":
        return f"AED {v:g}K{suffix}"
    if normalized_unit in {"abs", "absolute", "aed", "dhs", "dirhams"}:
        return f"{fmt_price(v)}{suffix}"
    return f"{fmt_price(v)}{suffix}"


# Search routing is intentionally deterministic. The chat model may explain
# verified results, but it must never be the component that decides whether a
# concrete property request reaches the live marketplace.
_MARKET_LOCALITIES = (
    "Dubai Marina", "JBR", "Downtown Dubai", "Business Bay", "DIFC",
    "Palm Jumeirah", "JVC", "JVT", "JLT", "Dubai Hills Estate",
    "Arabian Ranches", "The Springs", "The Meadows", "The Greens",
    "Al Barsha", "Al Furjan", "Deira", "Bur Dubai", "Karama", "Mirdif",
    "Silicon Oasis", "Sports City", "Motor City", "Studio City",
    "Emirates Hills", "City Walk", "Al Wasl", "Satwa",
)


def _db_client(db_path):
    client = getattr(db_path, "_client", None)
    if client is not None:
        return client
    client = getattr(db_path, "client", None)
    if client is not None:
        return client
    return db_path


def _lookup_building_locality(db_path, building_name: str) -> str | None:
    """Resolve a building name to its known micro-market, if available."""
    name = re.sub(r"\s+", " ", (building_name or "").strip())
    if not name or db_path is None:
        return None
    client = _db_client(db_path)
    if client is None or not hasattr(client, "table"):
        return None
    try:
        res = client.table("buildings").select("micro_market").eq(
            "canonical_name", name
        ).limit(1).execute()
        if res.data and res.data[0].get("micro_market"):
            return str(res.data[0]["micro_market"]).strip() or None

        res = client.table("buildings").select("micro_market").ilike(
            "canonical_name", name
        ).limit(1).execute()
        if res.data and res.data[0].get("micro_market"):
            return str(res.data[0]["micro_market"]).strip() or None

        res = client.table("building_name_aliases").select("canonical_name").ilike(
            "alias", name
        ).limit(1).execute()
        if res.data:
            canonical = str(res.data[0].get("canonical_name") or "").strip()
            if canonical:
                res2 = client.table("buildings").select("micro_market").eq(
                    "canonical_name", canonical
                ).limit(1).execute()
                if res2.data and res2.data[0].get("micro_market"):
                    return str(res2.data[0]["micro_market"]).strip() or None
    except Exception:
        _logger.warning("building locality lookup failed for %r", building_name, exc_info=True)
    return None


def _extract_building_candidate(text: str) -> str | None:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return None
    candidates: list[str] = []
    for pattern in (
        r"\b(?:in|at|near|around)\s+([a-z0-9][a-z0-9&'().,\/\-\s]{2,80})",
        r"\b(?:for|from)\s+([a-z0-9][a-z0-9&'().,\/\-\s]{2,80})",
    ):
        for match in re.finditer(pattern, raw, re.IGNORECASE):
            candidate = re.sub(r"\s+", " ", match.group(1)).strip(" ?!.,;:")
            if candidate:
                candidates.append(candidate)
    blocked_terms = re.compile(
        r"\b(?:bhk|rent|rental|lease|sale|sell|buy|purchase|furnished|"
        r"unfurnished|apartment|flat|property|inventory|budget|price|"
        r"office|shop|showroom|warehouse|commercial)\b",
        re.IGNORECASE,
    )
    cleaned = []
    seen = set()
    for candidate in sorted(candidates, key=len, reverse=True):
        if blocked_terms.search(candidate):
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(candidate)
    return cleaned[0] if cleaned else None


def _market_price_to_aed(value: str, unit: str) -> float:
    amount = float(value.replace(",", ""))
    unit = unit.lower()
    if unit in {"m", "mn", "million", "millions"}:
        return amount * 1_000_000
    if unit in {"k", "thousand", "thousands"}:
        return amount * 1_000
    return amount


def _resolve_between_localities(db_path, start: str, end: str) -> list[str]:
    """Resolve between endpoints from persisted locality geography."""
    client = getattr(db_path, "_client", None) if db_path is not None else None
    if client is None or not start or not end:
        return []
    try:
        rows = (
            client.table("locality_reference")
            .select("parent_locality,sub_locality,sort_order")
            .not_.is_("sort_order", "null")
            .order("sort_order")
            .execute()
            .data
            or []
        )
    except Exception:
        return []

    def key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()

    start_key, end_key = key(start), key(end)
    ordered = [(int(row["sort_order"]), str(row.get("sub_locality") or row.get("parent_locality") or "").strip()) for row in rows]
    ordered = [(order, name) for order, name in ordered if name]
    start_index = next((i for i, (_, name) in enumerate(ordered) if key(name) == start_key), None)
    end_index = next((i for i, (_, name) in enumerate(ordered) if key(name) == end_key), None)
    if start_index is None or end_index is None:
        return []
    lo, hi = sorted((start_index, end_index))
    result, seen = [], set()
    for _, name in ordered[lo : hi + 1]:
        normalized = key(name)
        if normalized not in seen:
            seen.add(normalized)
            result.append(name)
    return result


def _llm_market_search_request(text: str, api_key: str = "", model: str = "", base_url: str = "", db_path=None) -> dict | None:
    """Let the selected LLM extract filters; keep geography resolution in Supabase."""
    if not api_key or not model:
        return None
    prompt = (
        "Extract a real-estate marketplace search into JSON only. Return {} "
        "when the user is asking for an opinion, area suitability, advice, "
        "explanation, or general market insight rather than asking to find/show "
        "specific available listings or requirements. For example, 'is Bandra "
        "West good for expats?', 'tell me about Powai', and 'what is this area "
        "like?' must return {} and stay on the conversational AI path. Do not "
        "turn a locality mentioned in an advice question into a listing filter. "
        "For concrete inventory requests, extract the search. If the user asks "
        "for office space, shop, showroom, warehouse, godown, retail, or other "
        "commercial inventory, set intent to COMMERCIAL rather than RENT. Do not "
        "infer intermediate locations. For between A and B, return only "
        "between_start and between_end; the application resolves geography. "
        "Keys: bhk, intent (RENT/SELL/null), furnishing, price_min, price_max, "
        "micro_markets (explicit names only), between_start, between_end. "
        "Prices must be numeric dirhams (AED annual totals for rents). User query: " + text
    )
    try:
        response = get_client(api_key=api_key, base_url=base_url or None).chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You extract search filters. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
            temperature=0,
            timeout=20,
        )
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (response.choices[0].message.content or "").strip(), flags=re.IGNORECASE)
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None
        markets = [str(item).strip() for item in (parsed.get("micro_markets") or []) if str(item).strip()]
        between_start = str(parsed.get("between_start") or "").strip()
        between_end = str(parsed.get("between_end") or "").strip()
        if between_start and between_end:
            markets = _resolve_between_localities(db_path, between_start, between_end) or [between_start, between_end]
        if not markets and not parsed.get("bhk"):
            return None
        result = {"limit": 10, "offset": 0, "sort_by": "last_seen", "group_by_building": False, "micro_markets": markets}
        for field in ("bhk", "intent", "furnishing", "price_min", "price_max"):
            if parsed.get(field) not in (None, ""):
                result[field] = parsed[field]
        if between_start and between_end:
            result["between"] = {"start": between_start, "end": between_end, "resolved": markets}
        return result
    except Exception:
        return None


def parse_market_search_request(
    text: str,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
    db_path=None,
    allow_llm: bool = True,
) -> dict | None:
    """Parse an ordinary broker search message into safe market filters.

    This intentionally recognises only concrete property language. Generic
    messages still go to normal conversational AI rather than accidentally
    searching the entire marketplace.
    """
    raw = (text or "").strip()
    lower = raw.lower()
    if not raw:
        return None

    if allow_llm:
        llm_result = _llm_market_search_request(raw, api_key, model, base_url, db_path)
        if llm_result:
            if re.search(r"\b(?:requirement|requirements|wanted|need|needed|looking\s+for)\b", lower):
                llm_result["search_scope"] = "requirements"
            return llm_result

    bhk_match = re.search(r"\b(\d+(?:\.5)?)\s*(?:bhk|bed(?:room)?s?)\b", lower)
    localities = [
        locality for locality in _MARKET_LOCALITIES
        if re.search(rf"(?<!\w){re.escape(locality.lower())}(?!\w)", lower)
    ]
    localities = [
        locality for locality in localities
        if not any(locality != other and locality.lower() in other.lower() for other in localities)
    ]
    property_words = re.search(
        r"\b(?:flat|apartment|property|listing|listings|inventory|requirement|"
        r"rent|rental|lease|sale|sell|buy|purchase|furnished|unfurnished|"
        r"building|tower|society|project|available|need|looking for|find|search)\b",
        lower,
    )
    commercial_signal = re.search(r"\b(?:commercial|office|shop|showroom|warehouse|godown|retail)\b", lower)
    if not (bhk_match or localities or commercial_signal) or not property_words:
        return None

    args: dict[str, object] = {
        "limit": 10,
        "offset": 0,
        "sort_by": "last_seen",
        "group_by_building": False,
    }
    if re.search(r"\b(?:requirement|requirements|wanted|need|needed|looking\s+for)\b", lower):
        args["search_scope"] = "requirements"
    if bhk_match:
        args["bhk"] = bhk_match.group(1)

    # A buyer's request asks for available sale listings; a tenant request asks
    # for available rent listings. Explicit intent wins over generic wording.
    if re.search(r"\b(?:rent|rental|lease|leave\s*(?:&|and)\s*license|l&l)\b", lower):
        args["intent"] = "RENT"
    elif re.search(r"\b(?:sale|sell|buy|purchase)\b", lower):
        args["intent"] = "SELL"
    if commercial_signal:
        # Commercial queries should not be routed as generic residential rent
        # searches. The downstream search path already understands COMMERCIAL.
        args["intent"] = "COMMERCIAL"
        args["property_type"] = "commercial"
    else:
        args["property_type"] = "residential"

    if localities:
        # Preserve "Bandra East or BKC" rather than silently searching only
        # one half of a broker's requirement.
        args["micro_markets"] = sorted(set(localities), key=len, reverse=True)

    if re.search(r"\bsemi[-\s]?furnished\b", lower):
        args["furnishing"] = "Semi Furnished"
    elif re.search(r"\bunfurnished\b", lower):
        args["furnishing"] = "Unfurnished"
    elif re.search(r"\bfully\s+furnished\b|\bfurnished\b", lower):
        args["furnishing"] = "Furnished"

    amount_pattern = r"(\d+(?:\.\d+)?)\s*(m|mn|millions?|k|thousands?)\b"
    range_match = re.search(
        rf"(?:between\s+)?{amount_pattern}\s*(?:to|[-–])\s*{amount_pattern}", lower
    )
    if not range_match:
        # Accept ordinary user phrasing such as “between 80,000 and 120K”.
        # Units may differ or be omitted when the currency marker and the
        # absolute dirham amount make the value unambiguous.
        flexible_amount = (
            r"(?:aed|dhs\s*)?([\d,]+(?:\.\d+)?)\s*"
            r"(m|mn|millions?|k|"
            r"thousands?)?"
        )
        range_match = re.search(
            rf"(?:between\s+)?{flexible_amount}\s*(?:to|and|[-–])\s*"
            rf"(?:aed|dhs\s*)?([\d,]+(?:\.\d+)?)\s*"
            rf"(m|mn|millions?|k|"
            r"thousands?)?\b",
            lower,
        )
        if range_match and not (range_match.group(2) or range_match.group(4)):
            range_match = None
    if range_match:
        first = _market_price_to_aed(range_match.group(1), range_match.group(2))
        second = _market_price_to_aed(range_match.group(3), range_match.group(4))
        args["price_min"], args["price_max"] = sorted((first, second))
    else:
        # Broker shorthand commonly writes "6 to 8 Cr" (unit only once).
        shared_unit_range = re.search(
            r"(?:between\s+)?(\d+(?:\.\d+)?)\s*(?:to|[-–])\s*"
            r"(\d+(?:\.\d+)?)\s*(m|mn|millions?|k|thousands?)\b",
            lower,
        )
        if shared_unit_range:
            first = _market_price_to_aed(shared_unit_range.group(1), shared_unit_range.group(3))
            second = _market_price_to_aed(shared_unit_range.group(2), shared_unit_range.group(3))
            args["price_min"], args["price_max"] = sorted((first, second))
            return args
        ceiling = re.search(rf"(?:under|below|upto|up to|max(?:imum)?|budget\s*(?:of)?\s*)\s*(?:aed|dhs\s*)?{amount_pattern}", lower)
        if ceiling:
            args["price_max"] = _market_price_to_aed(ceiling.group(1), ceiling.group(2))

    return args


def _md_cell(value) -> str:
    """Escape a value for a single GFM markdown table cell."""
    text = str(value) if value not in (None, "") else "—"
    return re.sub(r"\s+", " ", text).replace("|", "\\|").strip()


_FURNISHING_CANON = {
    "fully_furnished": "Fully Furnished",
    "semi_furnished": "Semi Furnished",
    "partly_furnished": "Partly Furnished",
    "partially_furnished": "Partly Furnished",
    "almost_furnished": "Almost Furnished",
    "well_furnished": "Well Furnished",
    "luxury_fully_furnished": "Luxury Fully Furnished",
    "lavishly_furnished": "Lavishly Furnished",
    "unfurnished": "Unfurnished",
    "furnished": "Furnished",
    "bare_shell": "Bare Shell",
    "bareshell": "Bare Shell",
    "warm_shell": "Warm Shell",
    "builder_furnished": "Builder Furnished",
    "builder_finish": "Builder Finish",
    "empty": "Empty",
    "sf": "Semi Furnished",
}


def _md_furnishing(item: dict) -> str:
    """Human-readable furnishing label from the stored snake_case enum."""
    raw = item.get("furnishing")
    if raw in (None, ""):
        return "—"
    normalized = re.sub(r"[- ]", "_", str(raw)).strip().lower()
    if normalized in ("none", "na", "n/a", "*"):
        return "—"
    if normalized in _FURNISHING_CANON:
        return _FURNISHING_CANON[normalized]
    words = [word for word in re.split(r"[_\s]+", normalized) if word]
    if not words:
        return "—"
    return " ".join(word.capitalize() for word in words)


def _md_broker(item: dict) -> str:
    """Broker name with WhatsApp bold markers, instruction prefixes, and
    trailing separators removed; generic post fragments render as —."""
    raw = item.get("broker_name")
    if raw in (None, ""):
        return "—"
    cleaned = re.sub(r"[*_]+", "", str(raw)).strip()
    match = re.match(r"(?i)^(?:contact|call|pls|please)\s*:?\s*[-–—]*\s*(.+)$", cleaned)
    if match:
        cleaned = match.group(1).strip()
    cleaned = re.sub(r"[\s\-–—/:]+$", "", cleaned).strip()
    if not cleaned:
        return "—"
    if (
        re.fullmatch(r"[a-z\s\-–—/:]+", cleaned)
        and re.search(r"(?i)\b(call|contact|pls|please|inspection|visit|availability)\b", cleaned)
    ):
        return "—"
    return cleaned


def _strip_stars(value) -> str:
    """Drop WhatsApp bold markers from a stored text value."""
    return re.sub(r"\*+", "", str(value) if value not in (None, "") else "").strip()


def _whatsapp_enquiry(item: dict) -> str:
    """Deterministic prefilled WhatsApp enquiry message for a listing row."""
    building = _strip_stars(item.get("building_name")) or "the listing"
    configuration = str(item.get("bhk") or item.get("property_type") or "property").strip()
    price = str(item.get("price_formatted") or "").strip()
    carpet = f"{item.get('area_sqft')} sqft" if item.get("area_sqft") else ""
    furnishing = _md_furnishing(item)
    if furnishing == "—":
        furnishing = "Unspecified"
    locality = _strip_stars(
        item.get("micro_market") or item.get("location_label") or item.get("landmark_name")
    ) or "Unknown locality"
    broker = _md_broker(item)
    if broker == "—":
        broker = "there"
    lines = [
        f"Hi {broker},",
        "",
        "I found your listing through PropAI.",
        "",
        "Property:",
        f"• {building}",
        f"• {configuration}",
    ]
    if carpet:
        lines.append(f"• {carpet}")
    if price:
        lines.append(f"• {price}")
    lines += [
        f"• {furnishing}",
        f"• {locality}",
        "",
        "Is this still available?",
        "",
        "If yes, please share:",
        "• Photos",
        "• Availability",
        "• Inspection timing",
        "• Brokerage",
        "",
        "Sent via PropAI",
    ]
    return "\n".join(lines)


def _whatsapp_link(item: dict) -> str:
    """wa.me deep link with the prefilled enquiry; '' when no real phone."""
    phone = _normalize_real_phone(item.get("broker_phone") or "")
    if not phone:
        return ""
    return f"https://wa.me/91{phone}?text={quote(_whatsapp_enquiry(item), safe='')}"


def _md_last_seen(item: dict) -> str:
    """Absolute local date/time the listing was last seen; falls back to the
    relative age string when no parseable timestamp exists."""
    raw = item.get("last_seen") or item.get("last_seen_text") or ""
    if not raw:
        return "—"
    try:
        parsed = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return parsed.strftime("%d %b %Y, %H:%M")
    except (TypeError, ValueError):
        return str(raw)


def listing_table_from_items(results: list[dict]) -> str:
    """Render verified search rows as a GFM markdown table.

    Deterministic and portable across web, WhatsApp, email, and plain
    markdown viewers. Phone numbers are never exposed in plain text. Contact
    is resolved by the authenticated UI through the server-side endpoint."""
    if not results:
        return ""
    lines = [
        "| Building | Locality | Type | Rent/Sale | Carpet | Furnishing | Broker | Last seen | WhatsApp |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in results:
        cells = [
            _md_cell(_strip_stars(item.get("building_name")) or "—"),
            _md_cell(_strip_stars(item.get("micro_market") or item.get("location_label") or item.get("landmark_name")) or "—"),
            _md_cell(item.get("bhk") or item.get("property_type") or "—"),
            _md_cell(item.get("price_formatted") or "—"),
            _md_cell(f"{item.get('area_sqft')} sqft" if item.get("area_sqft") else "—"),
            _md_furnishing(item),
            _md_broker(item),
            _md_cell(_md_last_seen(item)),
            "Use the Contact broker button" if item.get("listing_id") else "—",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def listing_table_markdown(result: str) -> str:
    """Build the GFM table from a market-search result payload."""
    try:
        payload = json.loads(result)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    return listing_table_from_items(payload.get("results") or [])


def _strict_market_result_matches(row: dict, query: dict) -> bool:
    """Reject rows that cannot satisfy a concrete inventory request."""
    requested_bhk = query.get("bhk")
    if requested_bhk not in (None, ""):
        try:
            requested_value = float(re.search(r"\d+(?:\.\d+)?", str(requested_bhk)).group(0))
            row_value = float(re.search(r"\d+(?:\.\d+)?", str(row.get("bhk"))).group(0))
            if row_value != requested_value:
                return False
        except (AttributeError, TypeError, ValueError):
            return False

    markets = [" ".join(str(value).casefold().split()) for value in (query.get("micro_markets") or []) if str(value).strip()]
    if markets:
        location_text = " ".join(
            " ".join(str(row.get(field) or "").casefold().split())
            for field in ("micro_market", "locality_resolved", "locality_raw", "location_label", "landmark_name")
        )
        if not any(market in location_text for market in markets):
            return False

    maximum = query.get("price_max")
    minimum = query.get("price_min")
    if maximum is not None or minimum is not None:
        try:
            price = float(row.get("price"))
        except (TypeError, ValueError):
            return False
        if maximum is not None and price > float(maximum):
            return False
        if minimum is not None and price < float(minimum):
            return False

    requested_intent = str(query.get("intent") or "").upper()
    row_intent = str(row.get("intent") or row.get("transaction_type") or row.get("listing_type") or "").upper()
    if requested_intent in {"RENT", "SELL", "SALE", "BUY", "PURCHASE"} and row_intent:
        expected = "RENT" if requested_intent == "RENT" else "SELL"
        actual = "RENT" if row_intent in {"RENT", "LEASE"} else "SELL" if row_intent in {"SELL", "SALE", "BUY", "PURCHASE"} else row_intent
        if actual != expected:
            return False

    requested_property_type = str(query.get("property_type") or "").casefold()
    if requested_property_type:
        row_property_type = str(row.get("property_type") or row.get("asset_type") or "").casefold()
        if row_property_type and row_property_type != requested_property_type:
            return False
        if not row_property_type:
            return False
    return True


def deterministic_market_response(query: dict, result: str, sources: dict | None = None) -> dict:
    """Convert the verified market search output into a workspace response."""
    source_names = list((sources or {}).keys())
    is_shared_market = query.get("market_scope") == "shared"
    try:
        payload = json.loads(result)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {
            "content": "I couldn't fetch the latest market listings right now.",
            "blocks": [{"type": "error_state", "title": "Market search unavailable", "body": "Please try again shortly."}],
            "sources": source_names,
            "status_steps": ["Searching live marketplace"],
            "trace": {"route": "deterministic_market_search", "sources": source_names},
        }

    if payload.get("type") == "market_search_error" or payload.get("error"):
        return {
            "content": "I couldn't fetch the latest market listings right now.",
            "blocks": [{
                "type": "error_state",
                "title": "Market search unavailable",
                "body": "Please try again shortly.",
            }],
            "sources": source_names,
            "status_steps": ["Searching live marketplace"],
            "trace": {
                "route": "deterministic_market_search",
                "sources": source_names,
                "error": "market_search_failed",
            },
        }

    results = [
        row for row in (payload.get("results") or [])
        if isinstance(row, dict) and _strict_market_result_matches(row, query)
    ]
    is_requirement_search = payload.get("type") == "requirement_results" or query.get("search_scope") == "requirements"
    # Never trust a producer-supplied total after strict filtering. The count
    # shown to the broker must equal the rows that can actually be displayed.
    total = len(results)
    if not results:
        if query.get("micro_markets"):
            locality_text = ", ".join(str(market) for market in query.get("micro_markets") or [] if str(market).strip())
            fallback_body = (
                f"No exact matches found for {locality_text}."
                if locality_text
                else "No exact matches found."
            )
        else:
            fallback_body = "No exact matches found."
        return {
            "content": "No matching broker requirements were found yet." if is_requirement_search else "No active listings match those filters yet.",
            "blocks": [{
                "type": "empty_state",
                "title": "No exact market matches",
                "body": payload.get("suggestion") or f"{fallback_body} Try a different locality, a wider budget, or the latest listings.",
            }],
            "sources": ["global marketplace"],
            "status_steps": ["Searched live marketplace"],
            "trace": {"route": "deterministic_market_search", "filters": query},
        }

    shown = len(results)
    applied_filters = []
    if query.get("bhk"):
        applied_filters.append(f"{query['bhk']} BHK")
    if query.get("intent"):
        applied_filters.append(str(query["intent"]).upper())
    applied_filters.extend(str(market) for market in (query.get("micro_markets") or []))
    filter_text = " · ".join(applied_filters)

    if is_requirement_search:
        result_label = "matching broker requirement" if total == 1 else "matching broker requirements"
    else:
        result_label = "active match" if total == 1 else "active matches"
    parts = [f"Found {total} {result_label}; showing {shown} verified options from the shared broker network."]
    if filter_text:
        parts.append(f"**Applied filters:** {filter_text}")
    table = listing_table_from_items(results)
    if table:
        parts.append(table)

    return {
        "content": "\n\n".join(parts),
        "blocks": [
            {
                "type": "listing_cards",
                "title": "Matching broker requirements" if is_requirement_search else "Active listings",
                "subtitle": "Parsed demand records from WhatsApp broker posts" if is_requirement_search else (
                    "Shared PropAI broker network — contact details resolved securely"
                    if is_shared_market else "Global PropAI marketplace"
                ),
                "items": results,
                "total": total,
                "sources": ["Shared PropAI broker network" if is_shared_market else "WhatsApp broker posts"],
                "has_more": bool(payload.get("has_more")),
            },
        ],
        "sources": [
            "shared PropAI marketplace" if is_shared_market else "global marketplace",
            "WhatsApp broker posts",
        ],
        "status_steps": ["Parsed request", "Searched live marketplace", "Ranked by recent evidence"],
        "trace": {"route": "deterministic_market_search", "filters": query, "total": total},
    }


_PROPERTY_INTENT_RE = re.compile(
    r"\b(flat|apartment|property|properties|listing|listings|inventory|"
    r"rent|rental|rentals|lease|sale|sell|buy|purchase|furnished|unfurnished|"
    r"building|tower|society|project|office|shop|commercial|requirement|"
    r"available|looking for|find|search|price|budget|area|sqft|bhk|"
    r"thousand|million|k)\b",
    re.IGNORECASE,
)


def has_property_intent(text: str) -> bool:
    """True when the message is about property inventory in any concrete way."""
    lower = (text or "").lower()
    if re.search(r"\b\d+(?:\.5)?\s*(?:bhk|bed(?:room)?s?)\b", lower):
        return True
    if _PROPERTY_INTENT_RE.search(lower):
        return True
    return any(
        re.search(rf"(?<!\w){re.escape(locality.lower())}(?!\w)", lower)
        for locality in _MARKET_LOCALITIES
    )


def relaxed_market_query(text: str) -> dict:
    """Extract any market filters from text without requiring strict property
    language, so a DB-first answer still searches when the strict parser finds
    no inventory intent. Always returns a searchable query — hints only, or a
    plain recent-listings query when nothing concrete is mentioned."""
    raw = (text or "").strip()
    lower = raw.lower()
    args: dict[str, object] = {
        "limit": 10,
        "offset": 0,
        "sort_by": "last_seen",
        "group_by_building": False,
    }
    if re.search(r"\b(?:requirement|requirements|wanted|need|needed|looking\s+for)\b", lower):
        args["search_scope"] = "requirements"
    bhk_match = re.search(r"\b(\d+(?:\.5)?)\s*(?:bhk|bed(?:room)?s?)\b", lower)
    if bhk_match:
        args["bhk"] = bhk_match.group(1)

    localities = [
        locality for locality in _MARKET_LOCALITIES
        if re.search(rf"(?<!\w){re.escape(locality.lower())}(?!\w)", lower)
    ]
    localities = [
        locality for locality in localities
        if not any(locality != other and locality.lower() in other.lower() for other in localities)
    ]
    if localities:
        args["micro_markets"] = sorted(set(localities), key=len, reverse=True)
    else:
        building_candidate = _extract_building_candidate(raw)
        if building_candidate:
            building_market = _lookup_building_locality(db_path, building_candidate)
            if building_market:
                args["micro_markets"] = [building_market]

    if re.search(r"\b(?:rent|rental|lease|leave\s*(?:&|and)\s*license|l&l)\b", lower):
        args["intent"] = "RENT"
    elif re.search(r"\b(?:sale|sell|buy|purchase)\b", lower):
        args["intent"] = "SELL"

    if re.search(r"\bsemi[-\s]?furnished\b", lower):
        args["furnishing"] = "Semi Furnished"
    elif re.search(r"\bunfurnished\b", lower):
        args["furnishing"] = "Unfurnished"
    elif re.search(r"\bfully\s+furnished\b|\bfurnished\b", lower):
        args["furnishing"] = "Furnished"

    amount_pattern = r"(\d+(?:\.\d+)?)\s*(m|mn|millions?|k|thousands?)\b"
    range_match = re.search(
        rf"(?:between\s+)?{amount_pattern}\s*(?:to|[-–])\s*{amount_pattern}", lower
    )
    if range_match:
        first = _market_price_to_aed(range_match.group(1), range_match.group(2))
        second = _market_price_to_aed(range_match.group(3), range_match.group(4))
        args["price_min"], args["price_max"] = sorted((first, second))
    else:
        shared_unit_range = re.search(
            r"(?:between\s+)?(\d+(?:\.\d+)?)\s*(?:to|[-–])\s*"
            r"(\d+(?:\.\d+)?)\s*(m|mn|millions?|k|thousands?)\b",
            lower,
        )
        if shared_unit_range:
            first = _market_price_to_aed(shared_unit_range.group(1), shared_unit_range.group(3))
            second = _market_price_to_aed(shared_unit_range.group(2), shared_unit_range.group(3))
            args["price_min"], args["price_max"] = sorted((first, second))
        else:
            ceiling = re.search(
                rf"(?:under|below|upto|up to|max(?:imum)?|budget\s*(?:of)?\s*)\s*(?:aed|dhs\s*)?{amount_pattern}",
                lower,
            )
            if ceiling:
                args["price_max"] = _market_price_to_aed(ceiling.group(1), ceiling.group(2))

    return args


def llm_summarize_search(text: str, query: dict, result_json: str, api_key: str = "", model: str = "", base_url: str = "") -> str | None:
    """Have the LLM write a short answer strictly grounded in the verified
    search rows. Returns None (caller keeps the canned response) if there are
    no rows or the provider fails — never a fabricated summary."""
    if not api_key or not model:
        return None
    try:
        payload = json.loads(result_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    results = payload.get("results") or []
    if not results:
        return None
    total = int(payload.get("total") or 0)
    digest_lines = []
    for row in results[:10]:
        name = str(row.get("building_name") or "On Request").strip()
        locality = str(row.get("micro_market") or row.get("location_label") or "").strip()
        price = str(row.get("price_formatted") or "").strip()
        bhk = str(row.get("bhk") or "").strip()
        intent = str(row.get("intent") or "").upper().strip()
        parts = [part for part in (name, bhk, intent, locality, price) if part]
        digest_lines.append("- " + " · ".join(parts))
    digest = "\n".join(digest_lines)
    prompt = (
        f"The user asked: {(text or '').strip()}\n\n"
        f"A verified live search returned {total} matches. Here are up to the 10 most recent rows:\n"
        f"{digest}\n\n"
        "Write a short answer (2-4 sentences) that directly addresses the user's question using ONLY these rows. "
        "Summarize what is actually available: localities, BHKs, price range, and sale/rent mix. Do not invent "
        "buildings, brokers, prices, or counts that are not in the rows above. If the rows do not answer the "
        "question, say that plainly. Never mention a listing that is not in the rows."
    )
    try:
        response = get_client(api_key=api_key, base_url=base_url or None).chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You summarize verified property search results. You never invent data."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
            temperature=0.2,
        )
        return (response.choices[0].message.content or "").strip() or None
    except Exception:
        return None


def live_overview_counts(db_path=None) -> dict:
    """Deterministic live database counts for capability answers. Returns {}
    (unavailable) when the database cannot be reached, never a guess."""
    con = db_path if hasattr(db_path, "execute") else _open_db()
    if con is None:
        return {}
    try:
        return {
            "total_messages": con.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0],
            "total_properties_posted": con.execute("SELECT COUNT(*) FROM parsed_output_unified").fetchone()[0],
            "unique_properties": con.execute("SELECT COUNT(*) FROM listings_unified").fetchone()[0],
            "total_brokers": con.execute("SELECT COUNT(*) FROM brokers").fetchone()[0],
        }
    except Exception:
        return {}
    finally:
        if con is not db_path:
            con.close()


def capability_response(db_path=None) -> dict:
    """Answer capability questions with real, live database numbers. No LLM,
    no room to invent access claims."""
    counts = live_overview_counts(db_path)
    if counts:
        metrics = [
            {"label": "Listings", "value": f"{counts['unique_properties']:,}", "tone": "success"},
            {"label": "Property posts", "value": f"{counts['total_properties_posted']:,}", "tone": "neutral"},
            {"label": "Brokers", "value": f"{counts['total_brokers']:,}", "tone": "neutral"},
            {"label": "Messages", "value": f"{counts['total_messages']:,}", "tone": "neutral"},
        ]
        content = (
            "I work directly on PropAI's live database of WhatsApp property posts — "
            f"{counts['unique_properties']:,} unique listings, "
            f"{counts['total_properties_posted']:,} property posts from "
            f"{counts['total_brokers']:,} brokers across {counts['total_messages']:,} messages. "
            "Every listing, count, and comparison I show is fetched from that database at the moment you ask. "
            "I can search listings by BHK, price, locality, and sale/rent; query broker activity; look up raw "
            "WhatsApp messages; and create Review Center suggestions for changes like new buildings or broker merges."
        )
    else:
        metrics = [{"label": "Database", "value": "unavailable", "tone": "neutral"}]
        content = "I search PropAI's live property database, but the database is temporarily unreachable right now."
    return {
        "content": content,
        "blocks": [{"type": "summary", "title": "What I can access", "body": content, "metrics": metrics}],
        "sources": ["live database"],
        "status_steps": ["Read live database counts"],
        "trace": {"route": "deterministic_capability", "counts": counts or None},
    }


def _open_db():
    """Open a Supabase-backed connection for operational queries."""
    supabase_db = _get_supabase_db()
    if supabase_db is not None:
        return supabase_db
    return None


def _listing_price_in_aed(row: dict) -> float | None:
    """Normalize a stored listing price without depending on SQL CASE logic."""
    try:
        value = float(row.get("price"))
    except (TypeError, ValueError):
        return None
    unit = str(row.get("price_unit") or "").strip().lower()
    if unit in {"m", "mn", "million", "millions"}:
        return value * 1_000_000
    if unit == "k":
        return value * 1_000
    return value


def _hidden_broker_phones(client, tenant_id: str | None = None) -> set[str]:
    try:
        query = client.table("brokers").select("primary_phone, phone").eq("is_hidden", True)
        if tenant_id:
            query = query.eq("tenant_id", tenant_id)
        rows = query.execute().data or []
    except Exception:
        return set()
    phones: set[str] = set()
    for row in rows:
        phone = _normalize_real_phone((row.get("primary_phone") if isinstance(row, dict) else None) or (row.get("phone") if isinstance(row, dict) else None) or "")
        if phone:
            phones.add(phone)
    return phones


def _hidden_market_item_ids(client, tenant_id: str | None = None) -> tuple[set[int], set[int]]:
    try:
        query = client.table("hidden_market_items").select("tenant_id, listing_id, raw_message_id")
        rows = query.execute().data or []
    except Exception:
        return set(), set()
    listing_ids: set[int] = set()
    raw_message_ids: set[int] = set()
    for row in rows:
        row_tenant = str(row.get("tenant_id") or "").strip()
        if tenant_id and row_tenant and row_tenant != tenant_id:
            continue
        if row.get("listing_id") is not None:
            try:
                listing_ids.add(int(row["listing_id"]))
            except Exception:
                pass
        if row.get("raw_message_id") is not None:
            try:
                raw_message_ids.add(int(row["raw_message_id"]))
            except Exception:
                pass
    return listing_ids, raw_message_ids


def _rest_requirement_search(client, args: dict, tenant_id: str | None = None) -> str:
    """Search the market-wide demand projection, never the supply table."""
    bhk = str(args.get("bhk") or "").strip()
    bhk_match = re.search(r"\d+(?:\.5)?", bhk)
    requested_bhk = float(bhk_match.group(0)) if bhk_match else None
    broker = str(args.get("broker") or "").strip()
    markets = [str(value).strip() for value in (args.get("micro_markets") or []) if str(value).strip()]
    if not markets and args.get("micro_market"):
        markets = [str(args["micro_market"]).strip()]
    limit = max(1, min(int(args.get("limit") or 10), 50))
    offset = max(0, int(args.get("offset") or 0))
    hidden_brokers = _hidden_broker_phones(client, tenant_id)
    _, hidden_raw_message_ids = _hidden_market_item_ids(client, tenant_id)
    columns = (
        "id,fingerprint,raw_message_id,intent,transaction_type,bhk,price_min,price_max,"
        "area_sqft,location_label,building_name,landmark_name,micro_market,"
        "broker_name,broker_phone,confidence,first_seen,last_seen,created_at"
    )
    query = client.table("requirements_unified").select(columns, count="exact")
    if tenant_id:
        query = query.eq("tenant_id", tenant_id)
    if broker:
        for word in broker.split():
            query = query.ilike("broker_name", f"%{word}%")
    response = query.order("created_at", desc=True).limit(max(limit * 20, 100)).execute()
    rows = []
    for row in response.data or []:
        row_bhk = re.search(r"\d+(?:\.5)?", str(row.get("bhk") or ""))
        if requested_bhk is not None and (row_bhk is None or float(row_bhk.group(0)) != requested_bhk):
            continue
        if markets:
            haystack = " ".join(str(row.get(key) or "") for key in ("micro_market", "location_label", "landmark_name", "building_name")).lower()
            if not any(all(word.lower() in haystack for word in market.split()) for market in markets):
                continue
        phone = _normalize_real_phone(row.get("broker_phone") or "")
        if phone and phone in hidden_brokers:
            continue
        raw_id = row.get("raw_message_id")
        if raw_id is not None and int(raw_id) in hidden_raw_message_ids:
            continue
        rows.append(row)
    total = len(rows)
    results = []
    for row in rows[offset:offset + limit]:
        price_min = row.get("price_min")
        price_max = row.get("price_max")
        price_intent = str(row.get("transaction_type") or "RENT").upper()
        if price_min is not None and price_max is not None and float(price_min) != float(price_max):
            price_formatted = f"{fmt_listing_price(price_min, 'abs', price_intent)} - {fmt_listing_price(price_max, 'abs', price_intent)}"
        else:
            price_formatted = fmt_listing_price(price_min, "abs", price_intent)
        results.append({
            "listing_id": None,
            "requirement_id": row.get("id"),
            "raw_message_id": row.get("raw_message_id"),
            "fingerprint": row.get("fingerprint"),
            "intent": row.get("intent") or "REQUIREMENT",
            "transaction_type": row.get("transaction_type"),
            "bhk": row.get("bhk"),
            "price": price_min,
            "price_min": price_min,
            "price_max": price_max,
            "price_unit": "abs",
            "price_formatted": price_formatted,
            "area_sqft": row.get("area_sqft"),
            "furnishing": None,
            "location_label": row.get("location_label"),
            "building_name": row.get("building_name") or "Requirement",
            "landmark_name": row.get("landmark_name"),
            "micro_market": row.get("micro_market"),
            "broker_name": row.get("broker_name"),
            "broker_phone": row.get("broker_phone"),
            "first_seen": row.get("first_seen") or row.get("created_at"),
            "last_seen": row.get("last_seen") or row.get("created_at"),
            "last_seen_text": row.get("last_seen") or row.get("created_at") or "",
            "observation_count": 1,
            "group_count": 1,
            "confidence": row.get("confidence") or 0,
        })
    return json.dumps({
        "type": "requirement_results",
        "total": total,
        "results": results,
        "showing": len(results),
        "offset": offset,
        "has_more": total > offset + limit,
        "remaining": max(0, total - offset - limit),
    }, default=str)


def _rest_market_search(client, args: dict, tenant_id: str | None = None) -> str:
    """Read global listings through Supabase's table API, not the SQL bridge.

    The SQL bridge is useful for internal diagnostics but has a history of
    statement timeouts under marketplace load. This path is simple indexed
    filters plus a small in-process price normalization step.
    """
    if str(args.get("search_scope") or "").lower() == "requirements":
        return _rest_requirement_search(client, args, tenant_id=tenant_id)
    intent = str(args.get("intent") or "").upper().strip()
    bhk = str(args.get("bhk") or "").strip()
    bhk_match = re.search(r"\d+(?:\.5)?", bhk)
    requested_bhk = float(bhk_match.group(0)) if bhk_match else None
    building = str(args.get("building") or "").strip()
    broker = str(args.get("broker") or "").strip()
    furnishing = str(args.get("furnishing") or "").strip()
    markets = [str(value).strip() for value in (args.get("micro_markets") or []) if str(value).strip()]
    if not markets and args.get("micro_market"):
        markets = [str(args["micro_market"]).strip()]
    price_min = args.get("price_min")
    price_max = args.get("price_max")
    limit = max(1, min(int(args.get("limit") or 10), 50))
    offset = max(0, int(args.get("offset") or 0))
    hidden_brokers = _hidden_broker_phones(client, tenant_id)
    hidden_listing_ids, hidden_raw_message_ids = _hidden_market_item_ids(client, tenant_id)

    columns = (
        "id,fingerprint,intent,bhk,price,price_unit,area_sqft,furnishing,location_label,"
        "building_name,landmark_name,micro_market,broker_name,broker_phone,first_seen,"
        "last_seen,observation_count,group_count,latest_raw_message_id"
    )

    def fetch_one_market(market: str | None):
        query = client.table("listings_unified").select(columns, count="exact")
        if furnishing and furnishing.lower() != "any":
            query = query.ilike("furnishing", furnishing)
        if building:
            for word in building.split():
                query = query.ilike("building_name", f"%{word}%")
        if broker:
            for word in broker.split():
                query = query.ilike("broker_name", f"%{word}%")
        if market:
            for word in market.split():
                query = query.ilike("micro_market", f"%{word}%")
        return query.order("last_seen", desc=True).limit(max(limit * 15, 100)).execute()

    responses = [fetch_one_market(market) for market in (markets or [None])]
    seen: set[str] = set()
    rows: list[dict] = []
    for response in responses:
        for row in response.data or []:
            key = str(row.get("fingerprint") or row.get("id") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            row_intent = str(row.get("intent") or "").upper().strip()
            if intent and row_intent != intent:
                continue
            if requested_bhk is not None:
                row_bhk_match = re.search(r"\d+(?:\.5)?", str(row.get("bhk") or ""))
                if row_bhk_match is None or float(row_bhk_match.group(0)) != requested_bhk:
                    continue
            broker_phone = _normalize_real_phone(row.get("broker_phone") or "")
            if broker_phone and broker_phone in hidden_brokers:
                continue
            if row.get("id") is not None and int(row["id"]) in hidden_listing_ids:
                continue
            if row.get("latest_raw_message_id") is not None and int(row["latest_raw_message_id"]) in hidden_raw_message_ids:
                continue
            normalized_price = _listing_price_in_aed(row)
            if price_min is not None and (normalized_price is None or normalized_price < float(price_min)):
                continue
            if price_max is not None and (normalized_price is None or normalized_price > float(price_max)):
                continue
            rows.append(row)

    rows.sort(key=lambda row: str(row.get("last_seen") or ""), reverse=True)
    total = len(rows)
    result_rows = rows[offset : offset + limit]
    photo_counts: dict[int, int] = {}
    listing_ids = [int(row["id"]) for row in result_rows if row.get("id") is not None]
    if listing_ids:
        try:
            photo_query = client.table("listing_photos").select("listing_id").in_("listing_id", listing_ids)
            if tenant_id:
                photo_query = photo_query.eq("tenant_id", tenant_id)
            for photo in photo_query.execute().data or []:
                listing_id = photo.get("listing_id")
                if listing_id is not None:
                    photo_counts[int(listing_id)] = photo_counts.get(int(listing_id), 0) + 1
        except Exception:
            # Photo availability is enrichment; a schema rollout or transient
            # storage error must never make a listing search disappear.
            photo_counts = {}
    results = []
    for row in result_rows:
        results.append({
            "listing_id": row.get("id"),
            "fingerprint": row.get("fingerprint"),
            "intent": row.get("intent"),
            "bhk": row.get("bhk"),
            "price": row.get("price"),
            "price_unit": row.get("price_unit"),
            "price_formatted": fmt_listing_price(row.get("price"), row.get("price_unit"), row.get("intent")),
            "area_sqft": row.get("area_sqft"),
            "furnishing": row.get("furnishing"),
            "location_label": row.get("location_label"),
            "building_name": row.get("building_name") or "On Request",
            "landmark_name": row.get("landmark_name"),
            "micro_market": row.get("micro_market"),
            "broker_name": row.get("broker_name"),
            "broker_phone": row.get("broker_phone"),
            "first_seen": row.get("first_seen"),
            "last_seen": row.get("last_seen"),
            "last_seen_text": row.get("last_seen") or "",
            "observation_count": row.get("observation_count") or 0,
            "group_count": row.get("group_count") or 0,
            # listings has no confidence column; confidence belongs to the
            # parsed observation layer. Keep the card contract stable.
            "confidence": 0,
            "raw_message_id": row.get("latest_raw_message_id"),
            "photo_count": photo_counts.get(int(row["id"]), 0) if row.get("id") is not None else 0,
            "has_images": bool(photo_counts.get(int(row["id"]), 0)) if row.get("id") is not None else False,
        })

    return json.dumps({
        "type": "listing_results",
        "total": total,
        "results": results,
        "showing": len(results),
        "offset": offset,
        "has_more": total > offset + limit,
        "remaining": max(0, total - offset - limit),
    }, default=str)


def execute_tool(
    name,
    args,
    sources,
    db_path=None,
    tenant_id: str | None = None,
    storage_client=None,
    user_id: str | None = None,
    browser_enabled: bool = False,
    browser_provider: str | None = None,
):
    from agent_tools import READ_TOOL_NAMES, WRITE_TOOL_NAMES, BROWSER_TOOL_NAMES, execute_tool as execute_supabase_tool
    from browser_runtime import run_browser_command

    if name in READ_TOOL_NAMES or name in WRITE_TOOL_NAMES:
        if storage_client is None:
            return {"status": "error", "error": "Supabase agent client is not available"}
        return execute_supabase_tool(name, args, storage_client, tenant_id, user_id=user_id)

    if name in BROWSER_TOOL_NAMES:
        if not browser_enabled:
            return {"status": "error", "tool": name, "error": "Browser actions are disabled for this workspace"}
        if storage_client is None or not tenant_id:
            return {"status": "error", "tool": name, "error": "Browser actions require a tenant-scoped database client"}

        session_id = str(args.get("browser_session_id") or "").strip()
        if not session_id and name == "browser_open":
            import uuid
            session_id = str(uuid.uuid4())
        if not session_id:
            return {"status": "error", "tool": name, "error": "browser_session_id is required"}

        browser_settings = _browser_settings_row(storage_client, tenant_id)
        effective_provider = _normalize_browser_provider_name(
            browser_provider or browser_settings.get("browser_provider") or "agent-browser"
        )
        # Allow workspace overrides to disable browser use even if the LLM tries.
        if not browser_settings.get("browser_enabled", browser_enabled):
            return {"status": "error", "tool": name, "error": "Browser actions are disabled for this workspace"}

        command_map = {
            "browser_open": "open",
            "browser_state": "state",
            "browser_click": "click",
            "browser_fill": "fill",
            "browser_type": "type",
            "browser_select": "select",
            "browser_scroll": "scroll",
            "browser_screenshot": "screenshot",
            "browser_close": "close",
        }
        policy_error = _browser_policy_error(browser_settings, command_map.get(name, name), str(args.get("url") or ""))
        if policy_error:
            return {"status": "error", "tool": name, "browser_session_id": session_id, "error": policy_error}

        if name == "browser_open":
            _upsert_browser_session(
                storage_client,
                tenant_id,
                session_id,
                {
                    "user_id": user_id,
                    "session_id": str(args.get("chat_session_id") or "") or None,
                    "browser_provider": effective_provider,
                    "status": "open",
                    "task_label": str(args.get("session_label") or "").strip(),
                    "start_url": str(args.get("url") or "").strip(),
                    "current_url": str(args.get("url") or "").strip(),
                    "context": {"source": "agent_tool"},
                },
            )

        command = command_map[name]
        runtime_kwargs = {
            "url": args.get("url"),
            "index": args.get("index"),
            "text": args.get("text"),
            "value": args.get("value"),
            "amount": args.get("amount"),
            "output_path": args.get("output_path"),
        }
        try:
            result = run_browser_command(effective_provider, command, session_id, **runtime_kwargs)
        except Exception as exc:
            _logger.exception("Browser runtime failed for session %s", session_id)
            _log_browser_step(
                storage_client,
                tenant_id,
                session_id,
                int(args.get("step_index") or 0),
                command,
                str(args.get("url") or args.get("index") or args.get("text") or ""),
                "",
                "error",
                {"error": str(exc)},
            )
            _upsert_browser_session(
                storage_client,
                tenant_id,
                session_id,
                {
                    "user_id": user_id,
                    "browser_provider": effective_provider,
                    "status": "failed",
                    "current_url": str(args.get("url") or ""),
                    "last_error": str(exc),
                    "context": {
                        "last_command": command,
                        "last_error": str(exc),
                    },
                },
            )
            return {"status": "error", "tool": name, "browser_session_id": session_id, "error": str(exc)}

        step_index = int(args.get("step_index") or 0)
        target = str(args.get("url") or args.get("index") or args.get("text") or args.get("value") or "")
        metadata = {
            "provider": result.provider,
            "command": result.command,
            "summary": result.summary,
            "elements": result.elements,
            "raw_output": result.raw_output,
            "error": result.error,
            "screenshot_path": result.screenshot_path,
        }
        _log_browser_step(
            storage_client,
            tenant_id,
            session_id,
            step_index,
            command,
            target,
            result.url,
            result.status,
            metadata,
            result.screenshot_path,
        )
        if result.status == "ok":
            _upsert_browser_session(
                storage_client,
                tenant_id,
                session_id,
                {
                    "user_id": user_id,
                    "browser_provider": result.provider,
                    "status": "closed" if name == "browser_close" else "running",
                    "current_url": result.url or str(args.get("url") or ""),
                    "context": {
                        "last_command": command,
                        "last_summary": result.summary,
                        "last_error": result.error,
                    },
                },
            )
        else:
            _upsert_browser_session(
                storage_client,
                tenant_id,
                session_id,
                {
                    "user_id": user_id,
                    "browser_provider": result.provider,
                    "status": "failed",
                    "current_url": result.url or str(args.get("url") or ""),
                    "last_error": result.error or result.raw_output or "browser action failed",
                    "context": {
                        "last_command": command,
                        "last_summary": result.summary,
                        "last_error": result.error or result.raw_output or "browser action failed",
                    },
                },
            )
        return _browser_runtime_response(result, session_id=session_id, command=name)

    if name == "get_overview":
        return build_overview(sources)

    if name == "create_suggestion":
        try:
            con = db_path if hasattr(db_path, "execute") else _open_db()
            if con is None:
                return "❌ Failed to create suggestion: Database not available"
            agent = args.get("agent", "user_request")
            sug_type = args.get("suggestion_type", "review")
            title = args.get("title", "")
            description = args.get("description", "")
            proposal = json.dumps(args.get("proposal_data", {}))
            confidence = args.get("confidence", 0.85)
            cursor = con.execute("""
                INSERT INTO ai_suggestions
                    (agent, suggestion_type, title, description, source_data, proposal_data, confidence, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, '{}', ?, ?, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id
            """, (agent, sug_type, title, description, proposal, confidence))
            row = cursor.fetchone() if hasattr(cursor, "fetchone") else None
            if hasattr(con, "commit"):
                con.commit()
            sug_id = row[0] if row else 0
            con.close()
            return f"✅ Suggestion created (ID {sug_id}): \"{title}\". It's now in the Review Center waiting for approval."
        except Exception as e:
            return f"❌ Failed to create suggestion: {e}"

    if name == "find_parser_gaps":
        con = _open_db()
        if not con:
            return "Database not available"
        try:
            category = args.get("category", "all")
            limit = args.get("limit", 10)
            if category == "unresolved_location":
                rows = con.execute("""
                    SELECT p.id, p.intent, p.micro_market, p.broker_name, p.confidence,
                           p.location_raw, r.message, r.group_name
                    FROM parsed_output_unified p
                    JOIN raw_messages r ON r.id = p.raw_message_id
                    LEFT JOIN resolver_decisions d ON d.parsed_id = p.id
                    WHERE d.method = 'unresolved'
                    ORDER BY p.id DESC LIMIT ?
                """, (limit,)).fetchall()
            elif category == "low_confidence":
                rows = con.execute("""
                    SELECT p.id, p.intent, p.micro_market, p.broker_name,
                           p.location_raw, r.message, r.group_name
                    FROM parsed_output_unified p
                    JOIN raw_messages r ON r.id = p.raw_message_id
                    WHERE 1 = 0
                    LIMIT ?
                """, (limit,)).fetchall()
            elif category == "missing_bhk":
                rows = con.execute("""
                    SELECT p.id, p.intent, p.price, p.micro_market, p.broker_name,
                           r.message, r.group_name
                    FROM parsed_output_unified p
                    JOIN raw_messages r ON r.id = p.raw_message_id
                    WHERE (p.bhk IS NULL OR p.bhk = '') AND p.intent IN ('SELL','RENT')
                    ORDER BY p.id DESC LIMIT ?
                """, (limit,)).fetchall()
            elif category == "missing_price":
                rows = con.execute("""
                    SELECT p.id, p.intent, p.bhk, p.micro_market, p.broker_name,
                           r.message, r.group_name
                    FROM parsed_output_unified p
                    JOIN raw_messages r ON r.id = p.raw_message_id
                    WHERE (p.price IS NULL OR p.price = 0) AND p.intent IN ('SELL','RENT')
                    ORDER BY p.id DESC LIMIT ?
                """, (limit,)).fetchall()
            else:
                rows = con.execute("""
                    SELECT p.id, p.intent, p.micro_market, p.broker_name,
                           d.method, d.failure_category,
                           r.message, r.group_name
                    FROM parsed_output_unified p
                    JOIN raw_messages r ON r.id = p.raw_message_id
                    LEFT JOIN resolver_decisions d ON d.parsed_id = p.id
                    WHERE d.method = 'unresolved'
                    ORDER BY p.id DESC LIMIT ?
                """, (limit,)).fetchall()
            if not rows:
                return f"No {category} issues found. The parser is doing well!"
            lines = [f"Found {len(rows)} {'parser gap' if len(rows)==1 else 'parser gaps'}:"]
            for r in rows:
                d = dict(r)
                msg = (d.get("message") or "")[:80]
                lines.append(f"• [ID {d['id']}] {d.get('intent','?')} | {d.get('broker_name','?')} | {d.get('micro_market','?')} | conf={d.get('confidence',0)}")
                lines.append(f"  {msg}")
            return "\n".join(lines)
        finally:
            con.close()

    if name == "search_jid_memory":
        con = _open_db()
        if not con:
            return "Database not available"
        try:
            query = (args.get("query") or "").strip()
            message_kind = args.get("message_kind") or "any"
            locality = (args.get("locality") or "").strip()
            building = (args.get("building") or "").strip()
            bhk = (args.get("bhk") or "").strip()
            limit = int(args.get("limit") or 10)

            where = []
            params = []
            if query:
                like = f"%{query}%"
                where.append("""(
                    jp.display_name LIKE ? OR jp.phone LIKE ? OR jp.jid LIKE ?
                    OR jp.top_localities::text LIKE ? OR jp.top_buildings::text LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM jid_aliases ja
                        WHERE ja.jid_key = jp.jid_key AND ja.alias LIKE ?
                    )
                    OR EXISTS (
                        SELECT 1 FROM jid_message_index jmi
                        JOIN raw_messages r ON r.id = jmi.raw_message_id
                        WHERE jmi.jid_key = jp.jid_key AND r.message LIKE ?
                    )
                )""")
                params.extend([like, like, like, like, like, like, like])
            if locality:
                where.append("jp.top_localities::text LIKE ?")
                params.append(f"%{locality}%")
            if building:
                where.append("jp.top_buildings::text LIKE ?")
                params.append(f"%{building}%")
            where_sql = " AND ".join(where) if where else "1=1"

            profiles = [dict(r) for r in con.execute(f"""
                SELECT jp.*, string_agg(ja.alias, ' | ') AS aliases
                FROM jid_profiles jp
                LEFT JOIN jid_aliases ja ON ja.jid_key = jp.jid_key
                WHERE {where_sql}
                GROUP BY jp.id
                ORDER BY jp.message_count DESC, jp.last_seen_at DESC
                LIMIT ?
            """, params + [limit]).fetchall()]

            messages_where = []
            message_params = []
            if query:
                messages_where.append("(r.message LIKE ? OR r.sender LIKE ? OR r.sender_phone LIKE ?)")
                message_params.extend([f"%{query}%", f"%{query}%", f"%{query}%"])
            if message_kind and message_kind != "any":
                messages_where.append("jmi.message_kind = ?")
                message_params.append(message_kind)
            if locality:
                messages_where.append("jmi.locality LIKE ?")
                message_params.append(f"%{locality}%")
            if building:
                messages_where.append("jmi.building_name LIKE ?")
                message_params.append(f"%{building}%")
            if bhk:
                messages_where.append("jmi.bhk LIKE ?")
                message_params.append(f"%{bhk}%")
            message_sql = " AND ".join(messages_where) if messages_where else "1=1"
            messages = [dict(r) for r in con.execute(f"""
                SELECT jmi.jid_key, jmi.message_kind, jmi.residential_commercial,
                       jmi.transaction_type, jmi.bhk, jmi.budget, jmi.budget_unit,
                       jmi.locality, jmi.building_name, jmi.confidence,
                       r.id AS raw_message_id, r.sender, r.sender_phone, r.group_name,
                       r.timestamp, r.message
                FROM jid_message_index jmi
                JOIN raw_messages r ON r.id = jmi.raw_message_id
                WHERE {message_sql}
                ORDER BY r.timestamp DESC, r.id DESC
                LIMIT ?
            """, message_params + [limit]).fetchall()]

            return json.dumps({
                "type": "jid_memory_results",
                "profiles": profiles,
                "messages": messages,
                "traceability": {
                    "source": "raw_messages + jid_message_index",
                    "raw_messages_returned": len(messages),
                    "profiles_returned": len(profiles),
                },
            }, default=str)
        finally:
            con.close()

    if name == "market_search":
        # The API already owns the Supabase-backed connection. Reuse it when
        # supplied so global market search is not silently pointed at a stale
        # local fallback database.
        con = db_path if hasattr(db_path, "execute") else _open_db()
        close_con = con is not db_path
        if not con:
            return json.dumps({
                "type": "market_search_error",
                "error": "Database not available",
                "detail": "Search backend is not connected to a database.",
            })
        try:
            rest_client = getattr(con, "_client", None)
            if rest_client is not None and hasattr(rest_client, "table"):
                try:
                    return _rest_market_search(rest_client, args, tenant_id=tenant_id)
                except Exception as exc:
                    _logger.exception("REST market search failed")
                    return json.dumps({
                        "type": "market_search_error",
                        "error": "market_search_failed",
                        "detail": str(exc)[:400],
                    })

            import math
            from datetime import datetime, timezone, timedelta

            intent = args.get("intent")
            bhk = args.get("bhk")
            building = args.get("building")
            micro_market = args.get("micro_market")
            micro_markets = [str(value).strip() for value in (args.get("micro_markets") or []) if str(value).strip()]
            price_max = args.get("price_max")
            price_min = args.get("price_min")
            furnishing = args.get("furnishing")
            broker = args.get("broker")
            sort_by = args.get("sort_by", "last_seen")
            limit = args.get("limit", 10)
            offset = args.get("offset", 0)
            group_by_building = args.get("group_by_building", True)

            hidden_brokers: set[str] = set()
            hidden_listing_ids: set[int] = set()
            hidden_raw_message_ids: set[int] = set()
            try:
                hidden_broker_sql = """
                    SELECT DISTINCT COALESCE(NULLIF(primary_phone, ''), NULLIF(phone, '')) AS phone
                    FROM brokers
                    WHERE is_hidden = true
                      AND COALESCE(NULLIF(primary_phone, ''), NULLIF(phone, '')) IS NOT NULL
                """
                hidden_broker_params: tuple[object, ...] = ()
                if tenant_id:
                    hidden_broker_sql += " AND (tenant_id IS NULL OR tenant_id = ?)"
                    hidden_broker_params = (tenant_id,)
                broker_rows = con.execute(
                    hidden_broker_sql,
                    hidden_broker_params,
                ).fetchall()
                for row in broker_rows:
                    phone = _normalize_real_phone(row[0])
                    if phone:
                        hidden_brokers.add(phone)
            except Exception:
                pass
            try:
                hidden_market_sql = "SELECT listing_id, raw_message_id FROM hidden_market_items"
                hidden_market_params: tuple[object, ...] = ()
                if tenant_id:
                    hidden_market_sql += " WHERE tenant_id IS NULL OR tenant_id = ?"
                    hidden_market_params = (tenant_id,)
                hidden_rows = con.execute(
                    hidden_market_sql,
                    hidden_market_params,
                ).fetchall()
                for row in hidden_rows:
                    if row[0] is not None:
                        try:
                            hidden_listing_ids.add(int(row[0]))
                        except Exception:
                            pass
                    if row[1] is not None:
                        try:
                            hidden_raw_message_ids.add(int(row[1]))
                        except Exception:
                            pass
            except Exception:
                pass

            where_clauses = []
            params = []

            if intent and intent != "any":
                where_clauses.append("l.intent = ?")
                params.append(intent.upper())

            if bhk and bhk != "any":
                # DB stores "3 BHK", AI may send "3" or "3 BHK"
                bhk_str = str(bhk).strip()
                if not bhk_str.upper().endswith("BHK") and not bhk_str.upper().endswith("STUDIO"):
                    bhk_str = f"{bhk_str} BHK"
                where_clauses.append("l.bhk = ?")
                params.append(bhk_str)

            if building:
                where_clauses.append("""(
                    l.building_name LIKE ? OR
                    l.building_name IN (SELECT canonical FROM building_aliases WHERE alias LIKE ?) OR
                    l.building_name IN (SELECT alias FROM building_aliases WHERE canonical LIKE ?) OR
                    l.building_name IN (SELECT canonical FROM building_aliases WHERE alias LIKE ?)
                )""")
                bpattern = f"%{building}%"
                params.extend([bpattern, bpattern, bpattern, bpattern])

            if micro_markets:
                where_clauses.append("(" + " OR ".join("l.micro_market LIKE ?" for _ in micro_markets) + ")")
                params.extend(f"%{market}%" for market in micro_markets)
            elif micro_market:
                where_clauses.append("l.micro_market LIKE ?")
                params.append(f"%{micro_market}%")

            if price_max:
                # Normalize price to raw AED for comparison
                # AI sends prices in raw dirhams (e.g. 1500000 = AED 1.5M)
                # DB stores: abs=raw, M=value*1000000, K=value*1000
                where_clauses.append("""(CASE 
                    WHEN l.price_unit = 'M' OR l.price_unit = 'Mn' THEN l.price * 1000000
                    WHEN l.price_unit = 'K' THEN l.price * 1000
                    ELSE l.price END) <= ?""")
                params.append(float(price_max))

            if price_min:
                where_clauses.append("""(CASE 
                    WHEN l.price_unit = 'M' OR l.price_unit = 'Mn' THEN l.price * 1000000
                    WHEN l.price_unit = 'K' THEN l.price * 1000
                    ELSE l.price END) >= ?""")
                params.append(float(price_min))

            if furnishing and furnishing != "any":
                where_clauses.append("l.furnishing = ?")
                params.append(furnishing)

            if broker:
                where_clauses.append("l.broker_name LIKE ?")
                params.append(f"%{broker}%")

            if hidden_brokers:
                where_clauses.append("COALESCE(l.broker_phone, '') NOT IN (" + ",".join("?" for _ in hidden_brokers) + ")")
                params.extend(hidden_brokers)
            if hidden_listing_ids:
                where_clauses.append("l.id NOT IN (" + ",".join("?" for _ in hidden_listing_ids) + ")")
                params.extend(hidden_listing_ids)
            if hidden_raw_message_ids:
                where_clauses.append("COALESCE(l.latest_raw_message_id, -1) NOT IN (" + ",".join("?" for _ in hidden_raw_message_ids) + ")")
                params.extend(hidden_raw_message_ids)

            where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

            sort_map = {
                "price": "l.price DESC",
                "last_seen": "l.last_seen DESC",
                "observation_count": "l.observation_count DESC",
                "confidence": "l.confidence DESC",
            }
            order_sql = sort_map.get(sort_by, "l.last_seen DESC")

            total_query = f"SELECT COUNT(*) FROM listings_unified l WHERE {where_sql}"
            total_count = con.execute(total_query, params).fetchone()[0]

            listing_query = f"""
                SELECT l.id AS listing_id, l.fingerprint, l.intent, l.bhk, l.price, l.price_unit, l.area_sqft,
                       l.furnishing, l.location_label, l.building_name, l.landmark_name,
                       l.micro_market, l.broker_name, l.broker_phone,
                       l.first_seen, l.last_seen, l.observation_count, l.group_count,
                       l.latest_raw_message_id
                FROM listings_unified l
                WHERE {where_sql}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
            """
            params.extend([limit + 50, offset])
            rows = con.execute(listing_query, params).fetchall()

            if not rows:
                return json.dumps({
                    "type": "listing_results",
                    "total": total_count,
                    "results": [],
                    "grouped": {},
                    "showing": 0,
                    "offset": offset,
                    "has_more": False,
                    "suggestion": "No exact matches found. Try: Nearby markets | Similar buildings | Different budget | Different BHK | Latest listings",
                })

            now = datetime.now(timezone.utc)
            results = []
            for r in rows:
                d = dict(r)
                match_reasons = []
                if bhk and bhk != "any" and d.get("bhk"):
                    match_reasons.append(f"✓ {d['bhk']} BHK")
                if intent and d.get("intent"):
                    match_reasons.append(f"✓ {d['intent']}")
                if (micro_market or micro_markets) and d.get("micro_market"):
                    match_reasons.append(f"✓ {d['micro_market']}")
                if building and d.get("building_name"):
                    match_reasons.append(f"✓ Building match: {d['building_name']}")
                if furnishing and d.get("furnishing"):
                    match_reasons.append(f"✓ {d['furnishing']}")

                last_seen = d.get("last_seen")
                age = ""
                if last_seen:
                    try:
                        last_dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                        diff = now - last_dt
                        if diff.days == 0:
                            hours = diff.seconds // 3600
                            age = f"Seen {hours}h ago" if hours > 0 else "Seen just now"
                        elif diff.days == 1:
                            age = "Seen yesterday"
                        elif diff.days < 7:
                            age = f"Seen {diff.days}d ago"
                        else:
                            age = f"Seen {diff.days // 7}w ago"
                    except:
                        age = ""

                first_seen = d.get("first_seen")
                first_age = ""
                if first_seen:
                    try:
                        first_dt = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                        diff = now - first_dt
                        if diff.days == 0:
                            first_age = "First seen today"
                        elif diff.days == 1:
                            first_age = "First seen yesterday"
                        elif diff.days < 7:
                            first_age = f"First seen {diff.days}d ago"
                        else:
                            first_age = f"First seen {diff.days // 7}w ago"
                    except:
                        first_age = ""

                price_formatted = fmt_listing_price(d.get("price"), d.get("price_unit"), d.get("intent"))

                confidence_pct = round((d.get("confidence") or 0) * 100) if d.get("confidence") else 0

                results.append({
                    "listing_id": d.get("listing_id"),
                    "fingerprint": d.get("fingerprint"),
                    "intent": d.get("intent"),
                    "bhk": d.get("bhk"),
                    "price": d.get("price"),
                    "price_unit": d.get("price_unit"),
                    "price_formatted": price_formatted,
                    "area_sqft": d.get("area_sqft"),
                    "furnishing": d.get("furnishing"),
                    "location_label": d.get("location_label"),
                    "building_name": d.get("building_name") or "On Request",
                    "landmark_name": d.get("landmark_name"),
                    "micro_market": d.get("micro_market"),
                    "broker_name": d.get("broker_name"),
                    "broker_phone": d.get("broker_phone"),
                    "first_seen": d.get("first_seen"),
                    "first_seen_text": first_age,
                    "last_seen": d.get("last_seen"),
                    "last_seen_text": age,
                    "observation_count": d.get("observation_count", 0),
                    "group_count": d.get("group_count", 0),
                    "confidence": confidence_pct,
                    "raw_message_id": d.get("latest_raw_message_id"),
                    "match_reasons": match_reasons,
                })

            grouped = {}
            if group_by_building:
                for r in results:
                    bname = r["building_name"] or "On Request"
                    if bname not in grouped:
                        grouped[bname] = {"rentals": 0, "sales": 0, "listings": []}
                    if r["intent"] == "RENT":
                        grouped[bname]["rentals"] += 1
                    elif r["intent"] == "SELL":
                        grouped[bname]["sales"] += 1
                    grouped[bname]["listings"].append(r)

            brokers_found = len(set(r["broker_name"] for r in results if r["broker_name"]))
            buildings_found = len(set(r["building_name"] for r in results if r["building_name"]))

            return json.dumps({
                "type": "listing_results",
                "total": total_count,
                "results": results[:limit],
                "grouped": grouped,
                "showing": len(results[:limit]),
                "offset": offset,
                "has_more": total_count > offset + limit,
                "remaining": max(0, total_count - offset - limit),
                "search_summary": {
                    "total": total_count,
                    "brokers": brokers_found,
                    "buildings": buildings_found,
                },
            }, default=str)
        except Exception as exc:
            _logger.exception("Market search query failed")
            return json.dumps({
                "type": "market_search_error",
                "error": "market_search_failed",
                "detail": str(exc)[:400],
            })
        finally:
            if close_con:
                con.close()

    if name == "query_data":
        source = args.get("source")
        if source not in sources:
            return f"Dataset '{source}' not found. Available: {', '.join(sources.keys())}"
        df = sources[source]["df"].copy()
        df = apply_filters(df, args.get("filters"))
        aggregate = args.get("aggregate", "list")
        group_by = args.get("group_by")
        sort_by = args.get("sort_by")
        ascending = args.get("ascending", True)
        limit = args.get("limit", 20)

        if df.empty:
            return "No records match the given filters."

        if aggregate == "count":
            if group_by:
                result = df.groupby(group_by).size().reset_index(name="count")
                result = result.sort_values("count", ascending=False)
                return result.to_string(index=False)
            return f"Count: {len(df)}"

        if aggregate in ("avg", "min", "max"):
            if not group_by:
                return f"{aggregate} requires a group_by column"
            num_cols = df.select_dtypes(include="number").columns
            if len(num_cols) == 0:
                return "No numeric columns to aggregate"
            agg_col = num_cols[0]
            func_map = {"avg": "mean", "min": "min", "max": "max"}
            result = df.groupby(group_by)[agg_col].agg(func_map[aggregate]).reset_index()
            result = result.sort_values(agg_col, ascending=False)
            return result.to_string(index=False)

        if sort_by and sort_by in df.columns:
            df = df.sort_values(sort_by, ascending=ascending)
        elif sort_by in _PRICE_COLS and "price_numeric" in df.columns:
            df = df.sort_values("price_numeric", ascending=ascending)

        if limit:
            df = df.head(limit)

        rows = df.to_dict("records")
        lines = []
        for i, row in enumerate(rows, 1):
            parts = []
            for col, val in row.items():
                if "price" in col.lower() or "value" in col.lower() or "amount" in col.lower():
                    val = fmt_price(val)
                parts.append(f"{col}={val}")
            lines.append(f"{i}. {' | '.join(parts)}")
        return "\n".join(lines)

    if name == "search_raw_messages":
        con = _open_db()
        if not con:
            return "Database not available"
        try:
            query = (args.get("query") or "").strip()
            limit = int(args.get("limit") or 10)

            if not query:
                return "Please provide a search query."

            # Try FTS5 first
            try:
                rows = con.execute("""
                    SELECT rm.id, rm.group_name, rm.sender, rm.sender_phone,
                           rm.message, rm.timestamp,
                           snippet(raw_messages_fts, 0, '<mark>', '</mark>', '...', 40) as snippet
                    FROM raw_messages_fts fts
                    JOIN raw_messages rm ON rm.id = fts.rowid
                    WHERE raw_messages_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (query, limit)).fetchall()

                if rows:
                    lines = [f"Found {len(rows)} raw messages matching '{query}':"]
                    for r in rows:
                        group = r[1] or "Direct Message"
                        if '@g.us' in group:
                            resolved = con.execute(
                                "SELECT group_name FROM source_sync_jobs WHERE group_id = ? LIMIT 1",
                                (group,)
                            ).fetchone()
                            if resolved:
                                group = resolved[0]
                        lines.append(f"• [{group}] {r[2]}: {r[4][:100]}...")
                    return "\n".join(lines)
            except Exception:
                pass

            # Fallback to LIKE
            like_q = f"%{query}%"
            rows = con.execute("""
                SELECT id, group_name, sender, message, timestamp
                FROM raw_messages
                WHERE message LIKE ? OR sender LIKE ?
                ORDER BY id DESC
                LIMIT ?
            """, (like_q, like_q, limit)).fetchall()

            if rows:
                lines = [f"Found {len(rows)} raw messages matching '{query}':"]
                for r in rows:
                    group = r[1] or "Direct Message"
                    if '@g.us' in group:
                        resolved = con.execute(
                            "SELECT group_name FROM source_sync_jobs WHERE group_id = ? LIMIT 1",
                            (group,)
                        ).fetchone()
                        if resolved:
                            group = resolved[0]
                    lines.append(f"• [{group}] {r[2]}: {(r[3] or '')[:100]}...")
                return "\n".join(lines)

            return f"No raw messages found matching '{query}'."
        finally:
            con.close()

    if name == "get_sender_history":
        con = _open_db()
        if not con:
            return "Database not available"
        try:
            sender = (args.get("sender") or "").strip()
            limit = int(args.get("limit") or 20)

            if not sender:
                return "Please provide a sender name or phone number."

            # Find sender
            like_q = f"%{sender}%"
            senders = con.execute("""
                SELECT DISTINCT sender FROM raw_messages
                WHERE sender LIKE ? OR sender_phone LIKE ?
                LIMIT 5
            """, (like_q, like_q)).fetchall()

            if not senders:
                return f"No sender found matching '{sender}'."

            results = []
            for s in senders:
                sender_name = s[0]
                messages = con.execute("""
                    SELECT id, message, group_name, timestamp
                    FROM raw_messages
                    WHERE sender = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (sender_name, limit)).fetchall()

                if messages:
                    # Extract knowledge
                    import re
                    buildings = set()
                    bhk_configs = set()
                    markets = set()
                    groups = set()

                    building_pattern = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:Bil|Bldg|Building|Apt|Complex|Tower|Heights|Park|Residency|Enclave|Villa|Society)\b', re.IGNORECASE)
                    bhk_pattern = re.compile(r'(\d+)\s*(?:BHK|bhk|Bhk|RK|rk)', re.IGNORECASE)
                    market_keywords = {'Bandra', 'Andheri', 'Santacruz', 'Khar', 'Juhu', 'Goregaon', 'Malad', 'Worli', 'Powai', 'BKC', 'Lokhandwala', 'Versova'}

                    for msg in messages:
                        text = msg[1] or ""
                        groups.add(msg[2])
                        for match in building_pattern.finditer(text):
                            buildings.add(match.group(1))
                        for match in bhk_pattern.finditer(text):
                            bhk_configs.add(f"{match.group(1)} BHK")
                        for market in market_keywords:
                            if market.lower() in text.lower():
                                markets.add(market)

                    results.append({
                        "sender": sender_name,
                        "message_count": len(messages),
                        "groups": list(groups),
                        "buildings": list(buildings)[:10],
                        "bhk_configs": list(bhk_configs),
                        "markets": list(markets),
                        "recent_messages": [(m[0], (m[1] or "")[:100], m[3]) for m in messages[:5]],
                    })

            return json.dumps({"senders": results}, default=str)
        finally:
            con.close()

    if name == "ask_clarification":
        question = args.get("question", "")
        options = args.get("options", [])
        if options:
            return f"CLARIFICATION_NEEDED: {question}\nOptions: {', '.join(options)}"
        return f"CLARIFICATION_NEEDED: {question}"

    if name == "save_unit_alias":
        alias = (args.get("alias") or "").strip()
        canonical = (args.get("canonical_unit") or "").strip()
        if not alias or not canonical:
            return "Please provide both alias and canonical_unit."
        try:
            con = _open_db()
            if con:
                con.execute(
                    "INSERT INTO price_unit_aliases (alias, canonical_unit) VALUES (?, ?) "
                    "ON CONFLICT (alias) DO UPDATE SET canonical_unit = EXCLUDED.canonical_unit",
                    (alias.lower(), canonical)
                )
                con.commit()
                return f"Learned: '{alias}' = {canonical} unit. I'll remember this."
        except Exception as e:
            return f"Error saving alias: {str(e)}"
        return f"Learned: '{alias}' = {canonical} unit. I'll remember this."

    if name == "send_whatsapp":
        to_phone = (args.get("to_phone") or "").strip()
        text = args.get("text", "")
        if not to_phone or not text:
            return "Error: to_phone and text are required"
        try:
            from config import SUPABASE_URL, SUPABASE_SERVICE_KEY
            phone_number_id = None
            access_token = None
            
            # Query the business_api_config table directly via Supabase REST
            # We'll need to use the storage module
            from storage.supabase import SupabaseStorage
            storage = SupabaseStorage()
            config = storage.db.execute(
                "SELECT * FROM business_api_config WHERE key IN ('phone_number_id', 'access_token')"
            ).fetchall()
            for row in config:
                if row["key"] == "phone_number_id":
                    phone_number_id = row["value"]
                elif row["key"] == "access_token":
                    access_token = row["value"]
            
            if not phone_number_id or not access_token:
                return "Error: WABA not configured (phone_number_id or access_token missing)"

            # Normalize phone
            digits = to_phone.replace("+", "").replace(" ", "").replace("-", "").strip()
            if digits.startswith("0"):
                digits = digits[1:]
            if not digits.isdigit() or len(digits) < 10:
                return f"Error: Invalid phone number: {to_phone}"

            url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            body = {
                "messaging_product": "whatsapp",
                "to": digits,
                "type": "text",
                "text": {"body": text},
            }

            # Use httpx synchronously
            resp = httpx.post(url, json=body, headers=headers, timeout=30)
            data = resp.json() if resp.text else {}
            
            if resp.status_code == 200 and data.get("messages"):
                msg_id = data["messages"][0].get("id", "")
                return f"✅ Message sent successfully (ID: {msg_id}) to {digits}"
            else:
                error_msg = data.get("error", {}).get("message", resp.text[:500])
                return f"❌ Failed to send: {error_msg}"
        except Exception as e:
            return f"Error sending WhatsApp: {str(e)}"

    return f"Unknown tool: {name}"


def _default_db_path():
    supabase_db = _get_supabase_db()
    if supabase_db is not None:
        return supabase_db
    return None


def _log_usage(resp, agent: str, model: str, tenant_id: str | None = None) -> None:
    """Fire-and-forget: log token usage from an OpenAI-compatible response."""
    try:
        from usage_logger import log_ai_usage
        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        if tokens_in or tokens_out:
            log_ai_usage(
                agent=agent,
                model=model,
                tokens_input=tokens_in,
                tokens_output=tokens_out,
                tenant_id=tenant_id,
            )
    except Exception:
        pass


def get_conversational_reply(messages, api_key=None, model=None, base_url=None, broker=None):
    """Call the LLM purely conversationally — no tools, no data, no JSON contract."""
    client = get_client(api_key=api_key, base_url=base_url)
    system_prompt = build_conversational_system_prompt(broker=broker)
    msgs = [{"role": "system", "content": _cached_system_blocks(system_prompt)}] + [
        m for m in messages if m.get("role") in ("user", "assistant")
    ]
    used_model = model or _get_fallback_model()
    resp = client.chat.completions.create(
        model=used_model,
        messages=msgs,
        max_tokens=1000,
    )
    _log_usage(resp, "ai_chat", used_model)
    msg = resp.choices[0].message
    msg.content = strip_think_blocks(msg.content or "")
    return msg


def _get_fallback_model() -> str:
    """Return the model name from the active provider chain."""
    from llm import get_model as _fb_model
    return _fb_model()


def get_model_reply(
    messages,
    sources,
    api_key=None,
    db_path=None,
    model=None,
    base_url=None,
    max_tool_rounds=5,
    _depth=0,
    prefer_supabase_agent: bool = False,
    browser_enabled: bool = False,
    browser_provider: str | None = None,
    tenant_id: str | None = None,
    storage_client=None,
    user_id: str | None = None,
    activity_sink: list[dict[str, Any]] | None = None,
):
    client = get_client(api_key=api_key, base_url=base_url)
    tools = _build_tools(
        sources,
        prefer_supabase_agent=prefer_supabase_agent,
        browser_enabled=browser_enabled,
    )
    db_path = db_path or _default_db_path()

    # Apply prompt caching: cache tool definitions + static system prompt
    cached_tools = _add_tool_cache_control(tools)
    cached_msgs = []
    for m in messages:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            cached_msgs.append({**m, "content": _cached_system_blocks(m["content"])})
        else:
            cached_msgs.append(m)

    # Limit recursion depth
    if _depth >= max_tool_rounds:
        # Force a text-only response — no tools, but still cache the system prompt
        _used_model = model or _get_fallback_model()
        resp = client.chat.completions.create(
            model=_used_model,
            messages=cached_msgs,
            max_tokens=2000,
        )
        _log_usage(resp, "ai_chat", _used_model, tenant_id=tenant_id)
        return resp.choices[0].message

    _used_model = model or _get_fallback_model()
    resp = client.chat.completions.create(
        model=_used_model,
        messages=cached_msgs,
        tools=cached_tools,
        tool_choice="auto",
    )
    _log_usage(resp, "ai_chat", _used_model, tenant_id=tenant_id)
    msg = resp.choices[0].message

    # Append as dict, not as raw object; strip leaked chain-of-thought so it
    # neither reaches the user nor gets echoed back into the next LLM round.
    sanitized_content = strip_think_blocks(msg.content or "")
    msg_dict = {"role": "assistant", "content": sanitized_content}
    if msg.tool_calls:
        cleaned_calls = []
        for tc in msg.tool_calls:
            args = tc.function.arguments
            # Fix common JSON issues: double closing braces, trailing commas
            args = args.rstrip()
            while args.endswith("}}"):
                args = args[:-1]
            if args.endswith(",}"):
                args = args[:-2] + "}"
            cleaned_calls.append({
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": args}
            })
            tc.function.arguments = args  # Update for execute_tool
        msg_dict["tool_calls"] = cleaned_calls
    messages.append(msg_dict)

    if msg.tool_calls:
        _drop_echoed_assistant_content(messages)
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                # Try to extract first complete JSON object from truncated output
                try:
                    decoder = json.JSONDecoder()
                    fn_args, _ = decoder.raw_decode(tc.function.arguments)
                except (json.JSONDecodeError, ValueError):
                    fn_args = {}
            _logger.info("AI agent tool call name=%s args=%s tenant_id=%s", fn_name, fn_args, tenant_id)
            result = execute_tool(
                fn_name,
                fn_args,
                sources,
                db_path=db_path,
                tenant_id=tenant_id,
                storage_client=storage_client,
                user_id=user_id,
                browser_enabled=browser_enabled,
                browser_provider=browser_provider,
            )
            _logger.info("AI agent tool result name=%s status=%s", fn_name, result.get("status") if isinstance(result, dict) else "ok")
            if activity_sink is not None:
                activity_entry: dict[str, Any] = {
                    "tool": fn_name,
                    "status": result.get("status") if isinstance(result, dict) else "ok",
                    "summary": "",
                }
                if fn_name in READ_TOOL_NAMES:
                    if fn_name == "search_listings" and isinstance(result, dict):
                        count = len(result.get("results") or [])
                        locality = str(fn_args.get("locality") or "").strip()
                        listing_type = str(fn_args.get("listing_type") or "rent").strip().lower()
                        bhk = fn_args.get("bhk")
                        pieces = []
                        if count:
                            pieces.append(f"Found {count} matching listings")
                        else:
                            pieces.append("No matches found")
                        if locality:
                            pieces.append(f"in {locality}")
                        if bhk not in (None, ""):
                            pieces.append(f"for {bhk} BHK")
                        if listing_type:
                            pieces.append(listing_type)
                        activity_entry["summary"] = " ".join(pieces).strip()
                    elif fn_name == "match_client_to_listings" and isinstance(result, dict):
                        count = len(result.get("matches") or [])
                        activity_entry["summary"] = f"Matched client to {count} listings"
                    elif fn_name == "get_client_requirements":
                        activity_entry["summary"] = "Loaded client requirements"
                    elif fn_name == "get_broker_profile":
                        activity_entry["summary"] = "Loaded broker profile"
                elif fn_name in BROWSER_TOOL_NAMES:
                    command_name = str(result.get("tool") or fn_name).replace("browser_", "").replace("_", " ")
                    activity_entry["summary"] = f"{command_name.capitalize()} browser session"
                    if isinstance(result, dict):
                        activity_entry["browser_session_id"] = result.get("browser_session_id")
                        if result.get("provider"):
                            activity_entry["provider"] = result.get("provider")
                        if result.get("url"):
                            activity_entry["url"] = result.get("url")
                        if result.get("title"):
                            activity_entry["title"] = result.get("title")
                        if result.get("summary"):
                            activity_entry["detail"] = result.get("summary")
                elif fn_name in WRITE_TOOL_NAMES:
                    activity_entry["summary"] = f"Prepared {fn_name.replace('_', ' ')}"
                if not activity_entry.get("summary"):
                    activity_entry["summary"] = f"Ran {fn_name.replace('_', ' ')}"
                activity_sink.append(activity_entry)
            if isinstance(result, dict) and result.get("status") == "pending_confirmation":
                return {
                    "content": result.get("message") or "Confirmation is required before changing workspace data.",
                    "blocks": [{
                        "type": "confirmation",
                        "title": "Confirmation required",
                        "body": result.get("message"),
                        "tool": result.get("tool"),
                        "confirmation_token": result.get("confirmation_token"),
                    }],
                    "status_steps": [f"Prepared {fn_name.replace('_', ' ')}"],
                    "trace": {"route": "supabase_agent", "pending_tool": fn_name},
                }
            result_str = str(result) if isinstance(result, str) else json.dumps(result, default=str)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_str,
            })
        return get_model_reply(
            messages,
            sources,
            api_key=api_key,
            db_path=db_path,
            model=model,
            base_url=base_url,
            max_tool_rounds=max_tool_rounds,
            _depth=_depth + 1,
            prefer_supabase_agent=prefer_supabase_agent,
            browser_enabled=browser_enabled,
            browser_provider=browser_provider,
            tenant_id=tenant_id,
            storage_client=storage_client,
            user_id=user_id,
            activity_sink=activity_sink,
        )

    return msg
