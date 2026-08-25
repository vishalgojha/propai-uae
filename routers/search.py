"""
Search routes — text search, raw FTS, sender/group filtered search, market search.
"""
import asyncio
import logging
import os
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from routers.common import storage, require_user, get_tenant_context
from routers.protection import TTLCache, bounded_page
from storage.supabase import _merge_observation_rows

router = APIRouter(tags=["search"])

_logger = logging.getLogger(__name__)
_query_parse_cache = TTLCache(
    max_entries=int(os.getenv("PROPAI_SEARCH_PARSE_CACHE_ENTRIES", "512")),
    ttl_seconds=float(os.getenv("PROPAI_SEARCH_PARSE_CACHE_SECONDS", "60")),
)


class ParsedQuery(BaseModel):
    query: str
    bhk: Optional[int] = None
    intent: Optional[str] = None
    asset: Optional[str] = None
    minPrice: Optional[int] = None
    maxPrice: Optional[int] = None
    furnishing: Optional[str] = None
    locality: Optional[str] = None
    localities: list[str] = []
    building: Optional[str] = None
    broker: Optional[str] = None
    minArea: Optional[int] = None
    maxArea: Optional[int] = None


_LOCALITY_NAMES = [
    "dubai marina", "jumeirah beach residence", "palm jumeirah",
    "jumeirah lakes towers", "jumeirah village circle", "jumeirah village triangle",
    "dubai hills estate", "arabian ranches", "discovery gardens",
    "dubai production city", "dubai sports city", "dubai silicon oasis",
    "business bay", "downtown dubai", "emirates hills", "damac hills",
    "sheikh zayed road", "town square", "motor city", "international city",
    "festival city", "silicon oasis", "bluewaters island",
    "jbr", "jvc", "jvt", "jlt", "difc", "dso", "szr", "impz",
    "marina", "barsha", "furjan", "springs", "meadows", "lakes",
    "greens", "views", "ranches", "hills estate", "sports city",
    "deira", "bur dubai", "karama", "mirdif", "qusais", "nahda",
    "jaddaf", "meydan", "warqa", "khawaneej", "mizhar", "garhoud",
    "dubailand", "jebel ali", "jafza", "remraam", "mudon", "arjan",
    "jumeirah", "suqeim", "sufouh", "wasl", "zabeel", "barari",
]


def _corridor_endpoints(query: str) -> tuple[str, str] | None:
    """Return explicitly stated endpoints; geography is resolved separately."""
    known = sorted(_LOCALITY_NAMES, key=len, reverse=True)
    names = "|".join(re.escape(value) for value in known)
    match = re.search(
        rf"\bbetween\s+({names})\s+(?:and|to)\s+({names})\b",
        query.casefold(),
    )
    return (match.group(1), match.group(2)) if match else None


def _extract_localities(query: str) -> list[str]:
    lower = query.lower()
    localities = []
    endpoints = _corridor_endpoints(lower)
    if endpoints:
        return list(endpoints)
    for loc in sorted(_LOCALITY_NAMES, key=len, reverse=True):
        pattern = rf'\b{loc}\b'
        if re.search(pattern, lower):
            # A specific directional locality already accounts for its bare
            # parent ("Dubai Marina" must not also become "Marina").
            if any(
                existing.startswith(f"{loc} ") or loc.startswith(f"{existing} ")
                or f" {loc}" in f" {existing}"
                for existing in localities
            ):
                continue
            if loc not in localities:
                localities.append(loc)
    return localities[:4]


def _corridor_from_reference_rows(
    endpoints: tuple[str, str], rows: list[dict]
) -> list[str]:
    """Expand endpoints using persisted locality_reference geography order."""
    ordered: list[tuple[int, str]] = []
    positions: dict[str, int] = {}
    for row in rows:
        # Search inventory is stored at micro-market level. Prefer the
        # canonical parent so street/sub-locality reference rows do not make
        # the corridor narrower than the listing data it is filtering.
        label = str(row.get("parent_locality") or row.get("sub_locality") or "").strip()
        try:
            position = int(row.get("sort_order"))
        except (TypeError, ValueError):
            continue
        if not label:
            continue
        key = re.sub(r"\s+", " ", label.casefold())
        positions.setdefault(key, position)
        ordered.append((position, label))
    def endpoint_positions(endpoint: str) -> list[int]:
        key = endpoint.casefold()
        exact = positions.get(key)
        if exact is not None:
            return [exact]
        # Queries commonly say "between Bandra and Andheri West" while the
        # reference table stores directional parents such as Bandra East and
        # Bandra West. Resolve a bare parent only against those known rows;
        # never invent a locality from free text.
        prefix = f"{key} "
        return [position for position, label in ordered
                if label.casefold().startswith(prefix)]

    first_positions = endpoint_positions(endpoints[0])
    second_positions = endpoint_positions(endpoints[1])
    if not first_positions or not second_positions:
        return []
    lower = min(*first_positions, *second_positions)
    upper = max(*first_positions, *second_positions)
    result: list[str] = []
    seen: set[str] = set()
    for _, label in sorted(ordered):
        key = label.casefold()
        if lower <= positions[key] <= upper and key not in seen:
            seen.add(key)
            result.append(label)
    return result


def _corridor_search_terms(canonical_localities: list[str], rows: list[dict]) -> list[str]:
    """Include every known sub-locality alias belonging to the corridor."""
    canonical = {value.casefold() for value in canonical_localities}
    terms: list[str] = []
    seen: set[str] = set()
    for value in canonical_localities:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            terms.append(value)
    for row in rows:
        parent = str(row.get("parent_locality") or "").strip()
        alias = str(row.get("sub_locality") or "").strip()
        key = alias.casefold()
        if parent.casefold() in canonical and alias and key not in seen:
            seen.add(key)
            terms.append(alias)
    return terms


def _load_locality_corridor(endpoints: tuple[str, str]) -> dict[str, list[str]]:
    try:
        rows = (
            storage.client.table("locality_reference")
            .select("sub_locality,parent_locality,sort_order")
            .order("sort_order")
            .limit(500)
            .execute()
            .data
            or []
        )
    except Exception:
        _logger.warning("Locality corridor lookup failed", exc_info=True)
        return {"localities": [], "search_terms": []}
    localities = _corridor_from_reference_rows(endpoints, rows)
    return {
        "localities": localities,
        "search_terms": _corridor_search_terms(localities, rows),
    }


def _parse_price(text: str) -> tuple[Optional[int], Optional[int]]:
    """Parse AED budgets: Dubai quotes K/M shorthand against annual rents.

    Handles ranges ("80k to 1.2M"), single shorthand values ("85K",
    "1.5M") with under/above prefixes, and absolute dirham amounts written
    after an explicit currency word ("AED 95000").  Cheque-count tokens
    ("4 cheques") never parse as prices.
    """
    text = text.lower()
    multipliers = {"k": 1_000, "thousand": 1_000,
                   "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000}
    unit_re = r'(k|thousand|m|mn|million)?'

    def _value(num: str, unit: str | None) -> int | None:
        try:
            val = float(num.replace(",", ""))
        except ValueError:
            return None
        if unit:
            return int(val * multipliers[unit])
        return int(val)

    range_match = re.search(
        rf'(?:aed|dhs)?\s*([\d,.]+)\s*{unit_re}\s*(?:to|and|-)\s*'
        rf'(?:aed|dhs)?\s*([\d,.]+)\s*{unit_re}', text)
    if range_match and (range_match.group(2) or range_match.group(4)
                        or 'aed' in range_match.group(0) or 'dhs' in range_match.group(0)):
        first_unit = range_match.group(2) or range_match.group(4)
        second_unit = range_match.group(4) or range_match.group(2)
        first = _value(range_match.group(1), first_unit)
        second = _value(range_match.group(3), second_unit)
        if first is not None and second is not None:
            return min(first, second), max(first, second)

    km_match = re.search(rf'([\d,.]+)\s*{unit_re}(?![a-z])', text)
    if km_match and km_match.group(2):
        val = _value(km_match.group(1), km_match.group(2))
        if val is not None:
            prefix = text[max(0, km_match.start() - 18):km_match.start()]
            if re.search(r'\b(?:under|below|upto|up\s+to|max(?:imum)?)\s*$', prefix):
                return None, val
            if re.search(r'\b(?:above|over|min(?:imum)?|from)\s*$', prefix):
                return val, None
            return val, val

    abs_match = re.search(r'(?:aed|dhs)\s*([\d,]{4,})', text)
    if abs_match:
        val = _value(abs_match.group(1), None)
        if val is not None:
            prefix = text[max(0, abs_match.start() - 18):abs_match.start()]
            if re.search(r'\b(?:under|below|upto|up\s+to|max(?:imum)?)\s*$', prefix):
                return None, val
            if re.search(r'\b(?:above|over|min(?:imum)?|from)\s*$', prefix):
                return val, None
            return val, val
    return None, None


def _parse_area(text: str) -> tuple[Optional[int], Optional[int]]:
    match = re.search(r"(\d[\d,]*)\s*(?:to|and|-)\s*(\d[\d,]*)\s*(?:sq\.?\s*ft|sqft|sft|square\s*feet)", text.casefold())
    if match:
        values = sorted(int(match.group(i).replace(",", "")) for i in (1, 2))
        return values[0], values[1]
    single = re.search(r"(\d[\d,]*)\s*(?:sq\.?\s*ft|sqft|sft|square\s*feet)", text.casefold())
    if single:
        value = int(single.group(1).replace(",", ""))
        return value, value
    return None, None


def _parse_query_simple(query: str) -> ParsedQuery:
    parsed = ParsedQuery(query=query)
    lower = query.lower()
    bhk_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:bhk|bedrooms?|beds?)', lower)
    if bhk_match:
        parsed.bhk = int(float(bhk_match.group(1)))
    if re.search(r'\b(rent|rental|lease)\b', lower):
        parsed.intent = "rent"
    elif re.search(r'\b(sale|sell|buy|purchase)\b', lower):
        parsed.intent = "sale"
    if re.search(r'\b(commercial|office|shop|showroom|warehouse|retail)\b', lower):
        parsed.asset = "commercial"
    if re.search(r'\b(residential|apartment|flat|house)\b', lower):
        parsed.asset = "residential"
    if re.search(r'\b(unfurnished)\b', lower):
        parsed.furnishing = "unfurnished"
    elif re.search(r'\b(fully\s+furnished|ff|furnished)\b', lower):
        parsed.furnishing = "furnished"
    elif re.search(r'\b(semi\s+furnished|sf)\b', lower):
        parsed.furnishing = "semi_furnished"
    min_p, max_p = _parse_price(query)
    parsed.minPrice = min_p
    parsed.maxPrice = max_p
    parsed.minArea, parsed.maxArea = _parse_area(query)
    localities = _extract_localities(query)
    parsed.localities = localities
    if localities:
        parsed.locality = localities[0]
    return parsed


async def _parse_query_llm(query: str) -> Optional[ParsedQuery]:
    try:
        from llm import get_fast_client, get_fast_model, get_fast_provider_name
        client = get_fast_client()
        model = get_fast_model()
        prompt = f"""
Parse this Dubai real estate search query into structured JSON:

Query: "{query}"

Return JSON with: bhk (number or null), intent ("rent"|"sale"|null), minPrice (rupees or null), maxPrice (rupees or null), locality (city/area or null), localities (array of city/area names, for "between X and Y" patterns), building (complex name or null), furnishing (null|"unfurnished"|"semi_furnished"|"fully_furnished"), asset (null|"residential"|"commercial").

Rules:
- Extract explicit values only, don't infer
- Convert dirhams: 1K = 1,000, 1M = 1,000,000
- Locality: extract Dubai area names like Dubai Marina, JVC, Business Bay, etc.
- For "between X and Y" locality patterns, return both in localities array
- Building: extract society/complex names like "Kalpataru", "Prestige", etc.

Query: "{query}"
JSON:"""
        resp = client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}], max_tokens=500, temperature=0)
        content = (resp.choices[0].message.content or "").strip()
        import json
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
        parsed = json.loads(content)
        result = ParsedQuery(query=query, **parsed)
        # Keep deterministic extraction as a safety net when the provider
        # omits an explicit bedroom/BHK mention from otherwise valid JSON.
        simple = _parse_query_simple(query)
        if result.bhk is None:
            result.bhk = simple.bhk
        if result.intent is None:
            result.intent = simple.intent
        if result.minPrice is None:
            result.minPrice = simple.minPrice
        if result.maxPrice is None:
            result.maxPrice = simple.maxPrice
        if result.minArea is None:
            result.minArea = simple.minArea
        if result.maxArea is None:
            result.maxArea = simple.maxArea
        if result.asset is None:
            result.asset = simple.asset
        if result.intent is None:
            result.intent = simple.intent
        if result.locality is None:
            result.locality = simple.locality
        # Directional locality names are deterministic and must not be
        # replaced by an LLM's broader or incorrect guess.  In particular,
        # ``Bandra East`` must never become ``Bandra West``.
        if simple.localities:
            result.localities = simple.localities
            result.locality = simple.localities[0]
        if not result.localities:
            result.localities = simple.localities
        if not result.localities and result.locality:
            result.localities = [result.locality] if result.locality else []
        if result.localities:
            result.locality = result.localities[0]
        return result
    except Exception as e:
        _logger.warning("LLM query parsing failed: %s", str(e)[:100])
        return None


@router.get("/api/search/parse")
async def parse_query(q: str = ""):
    if not q:
        return ParsedQuery(query="").model_dump()
    q = q.strip()
    cache_key = q.casefold()
    cached = _query_parse_cache.get(cache_key)
    if cached is not None:
        return cached
    parsed = await _parse_query_llm(q)
    if not parsed:
        parsed = _parse_query_simple(q)
    payload = parsed.model_dump()
    _query_parse_cache.set(cache_key, payload)
    return payload


def _search_value(row: dict, *keys: str) -> str:
    return " ".join(str(row.get(key) or "") for key in keys).casefold()


@router.get("/api/search/market-items")
async def search_market_items(
    q: str,
    result_type: str = "all",
    limit: int = 50,
    offset: int = 0,
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    """Search typed listings and requirements using a free-form query.

    The Market Inbox timeline is intentionally bounded, so searching that
    browser-side page can never be complete. This endpoint parses the query
    into real-estate filters and searches a wider typed-data window instead.
    """
    query = str(q or "").strip()
    if len(query) < 2:
        return {"items": [], "total": 0, "query": query, "parsed": {}}
    if result_type not in {"all", "listings", "requirements"}:
        raise HTTPException(422, "result_type must be all, listings, or requirements")

    cache_key = query.casefold()
    parsed_payload = _query_parse_cache.get(cache_key)
    if parsed_payload is None:
        parsed = await _parse_query_llm(query) or _parse_query_simple(query)
        parsed_payload = parsed.model_dump()
        _query_parse_cache.set(cache_key, parsed_payload)
    parsed = ParsedQuery(**parsed_payload)

    corridor_endpoints = _corridor_endpoints(query)
    corridor_localities: list[str] = []
    corridor_search_terms: list[str] = []
    if corridor_endpoints:
        corridor = await asyncio.to_thread(
            _load_locality_corridor, corridor_endpoints
        )
        corridor_localities = corridor["localities"]
        corridor_search_terms = corridor["search_terms"]
        # Never pretend that endpoint-only OR matching is a corridor. If the
        # persisted geography cannot resolve both endpoints, return no rows
        # and expose the unresolved state to the UI instead of guessing.
        if not corridor_localities:
            return {
                "items": [],
                "total": 0,
                "query": query,
                "parsed": parsed.model_dump(),
                "corridor": {
                    "endpoints": list(corridor_endpoints),
                    "localities": [],
                    "resolved": False,
                },
            }

    requirement_filter = (
        True if result_type == "requirements"
        else False if result_type == "listings"
        else None
    )
    rows = await asyncio.to_thread(
        storage._fetch_typed_rows,
        requirements=requirement_filter,
        all_tenants=True,
        limit_per_table=500,
        card_only=True,
    )

    intent = str(parsed.intent or "").casefold()
    asset = str(parsed.asset or "").casefold()
    localities = [
        str(value).casefold()
        for value in (corridor_search_terms or parsed.localities or [])
        if value
    ]
    if parsed.locality and parsed.locality.casefold() not in localities:
        localities.append(parsed.locality.casefold())
    building = str(parsed.building or "").casefold()
    furnishing = str(parsed.furnishing or "").casefold().replace("_", " ")
    ignored_terms = {
        "a", "an", "and", "or", "the", "for", "in", "at", "near", "between",
        "to", "from", "under", "below", "above", "over", "up", "upto", "max",
        "minimum", "maximum", "bhk", "bed", "beds", "bedroom", "bedrooms",
        "rent", "rental", "lease", "sale", "sell", "buy", "purchase", "property",
        "properties", "listing", "listings", "requirement", "requirements",
        "aed", "dhs", "dirham", "dirhams", "cheque", "cheques", "chq",
        "yearly", "annual", "annum", "monthly", "residential", "commercial", "office",
        "br", "bhk", "studio",
        "flat", "apartment", "furnished", "unfurnished", "semi", "fully",
        "sqft", "sq", "ft", "area", "carpet", "built", "chargeable",
        "looking", "look", "want", "wanted", "need", "needs", "seeking", "on", "available", "space",
    }
    structured_values = {
        *(value for locality in localities for value in locality.split()),
        *(building.split() if building else []),
    }
    residual_terms = [
        token for token in re.findall(r"[a-z0-9]+", query.casefold())
        if token not in ignored_terms
        and token not in structured_values
        and not re.fullmatch(r"\d+(?:\.\d+)?", token)
    ]

    matches: list[dict] = []
    for typed in rows:
        table = str(typed.get("_typed_table") or "")
        if str(typed.get("visibility") or "").casefold() == "workspace_private":
            continue
        if asset and not table.startswith(f"{asset}_"):
            continue
        transaction = str(typed.get("transaction_type") or "").casefold()
        if intent and transaction != intent:
            continue
        if parsed.bhk is not None:
            wanted_bhk = float(parsed.bhk)
            options = typed.get("bhk_options")
            if not isinstance(options, (list, tuple)):
                options = []
            candidates = [typed.get("bhk"), *options]
            try:
                if not any(float(value) == wanted_bhk for value in candidates if value is not None):
                    continue
            except (TypeError, ValueError):
                continue
        if parsed.minArea is not None or parsed.maxArea is not None:
            area_values = [
                typed.get(field)
                for field in ("carpet_area_sqft", "built_up_area_sqft", "chargeable_area_sqft", "area_min_sqft", "area_max_sqft")
                if typed.get(field) not in (None, "")
            ]
            numeric_areas = []
            for value in area_values:
                try:
                    numeric_areas.append(float(value))
                except (TypeError, ValueError):
                    continue
            if not numeric_areas:
                continue
            row_min = min(numeric_areas)
            row_max = max(numeric_areas)
            if parsed.minArea is not None and row_max < parsed.minArea:
                continue
            if parsed.maxArea is not None and row_min > parsed.maxArea:
                continue
        locality_text = _search_value(
            typed, "micro_market", "locality_raw", "locality_resolved", "locality_options"
        )
        # A corridor query expands into every persisted locality between the
        # endpoints. A row may belong to any one of those locality buckets.
        if localities and not any(locality in locality_text for locality in localities):
            continue
        searchable = _search_value(
            typed, "summary_title", "building_name", "micro_market", "locality_raw",
            "locality_resolved", "locality_options", "broker_name", "commercial_use_type",
            "property_type", "furnishing", "furnishing_preference",
        )
        if building and building not in searchable:
            continue
        if furnishing and furnishing not in searchable:
            continue
        if residual_terms and not all(term in searchable for term in residual_terms):
            continue

        legacy = storage._typed_row_to_legacy(typed)
        price = legacy.get("price") or typed.get("budget_max") or typed.get("budget_min") or 0
        try:
            numeric_price = float(price or 0)
        except (TypeError, ValueError):
            numeric_price = 0
        if parsed.minPrice is not None and numeric_price < parsed.minPrice:
            continue
        if parsed.maxPrice is not None and numeric_price > parsed.maxPrice:
            continue

        legacy["source_schema"] = table
        legacy["_typed_table"] = table
        legacy["observation_type"] = "REQUIREMENT" if table.endswith("_requirements") else "LISTING"
        legacy["latest_parsed_id"] = typed.get("id")
        legacy["latest_raw_message_id"] = typed.get("raw_message_id")
        legacy["first_seen"] = typed.get("created_at")
        legacy["last_seen"] = typed.get("last_seen_at") or typed.get("updated_at") or typed.get("created_at")
        matches.append(legacy)

    # Search must return the same canonical opportunities as the market feed;
    # otherwise reposts appear as a wall of identical cards only in search.
    matches = _merge_observation_rows(matches)
    matches.sort(key=lambda row: str(row.get("last_seen") or row.get("created_at") or ""), reverse=True)
    safe_limit = min(max(int(limit or 50), 1), 100)
    safe_offset = max(int(offset or 0), 0)
    return {
        "items": matches[safe_offset:safe_offset + safe_limit],
        "total": len(matches),
        "query": query,
        "parsed": parsed.model_dump(),
        "corridor": {
            "endpoints": list(corridor_endpoints or ()),
            "localities": corridor_localities,
            "resolved": bool(corridor_localities),
        },
    }


@router.get("/api/search")
async def search_messages(q: str = "", use_llm: bool = False):
    """Search live Supabase projections used by the dashboard.

    This palette predates the typed-table migration.  The old implementation
    queried local SQLite compatibility tables and swallowed every exception,
    which made the modal silently return no results in production.
    """
    query = str(q or "").strip()
    if not query:
        return {}
    needle = query.casefold()

    def search_live() -> dict[str, list[dict]]:
        rows = storage._fetch_typed_rows(
            requirements=None,
            all_tenants=True,
            limit_per_table=50,
            card_only=True,
            search_text=query,
        )
        matching = []
        for row in rows:
            haystack = " ".join(
                str(row.get(key) or "")
                for key in ("building_name", "micro_market", "locality_raw", "broker_name", "group_name", "summary_title")
            ).casefold()
            if needle not in haystack:
                continue
            matching.append(row)
        matching.sort(key=lambda row: str(row.get("last_seen_at") or row.get("created_at") or ""), reverse=True)
        listings = [
            {
                "building_name": row.get("building_name"),
                "micro_market": row.get("micro_market") or row.get("locality_raw"),
                "broker_name": row.get("broker_name"),
                "transaction_type": row.get("transaction_type"),
                "summary_title": row.get("summary_title"),
                "source_schema": row.get("_typed_table"),
                "id": row.get("id"),
            }
            for row in matching
            if not str(row.get("_typed_table") or "").endswith("_requirements")
        ][:8]
        requirements = [
            {
                "building_name": row.get("building_name"),
                "micro_market": row.get("micro_market") or row.get("locality_raw"),
                "broker_name": row.get("broker_name"),
                "summary_title": row.get("summary_title"),
                "source_schema": row.get("_typed_table"),
                "id": row.get("id"),
            }
            for row in matching
            if str(row.get("_typed_table") or "").endswith("_requirements")
        ][:6]
        brokers = []
        try:
            brokers = [
                {"id": row.get("id"), "name": row.get("canonical_name"), "phone": row.get("primary_phone"),
                 "observation_count": row.get("observation_count")}
                for row in storage.get_brokers(search=query, limit=6)
            ]
        except Exception:
            pass
        buildings = []
        try:
            buildings = [
                {"name": row.get("canonical_name"), "micro_market": row.get("micro_market"),
                 "occurrence_count": row.get("observed_listings") or 0}
                for row in storage.get_buildings(search=query, limit=6)
            ]
        except Exception:
            pass
        return {key: value for key, value in {
            "listings": listings,
            "requirements": requirements,
            "brokers": brokers,
            "buildings": buildings,
        }.items() if value}

    return await asyncio.to_thread(search_live)


@router.get("/api/search/raw")
async def search_raw_messages(q: str = "", limit: int = 20, offset: int = 0):
    if not q:
        return {"results": [], "count": 0}
    q = q.strip()
    limit, offset = bounded_page(limit, offset)

    def _resolve_group_name(group_name: str) -> str:
        if group_name and "@g.us" in group_name:
            resolved = storage.db.execute(
                "SELECT group_name FROM source_sync_jobs WHERE group_id = ? LIMIT 1",
                (group_name,)
            ).fetchone()
            if resolved:
                return resolved[0]
        return group_name

    def _row_to_result(row: Any, snippet: str | None = None) -> dict:
        return {
            "id": row[0],
            "group_name": _resolve_group_name(row[1]),
            "sender": row[2],
            "sender_phone": row[3],
            "message": row[4],
            "timestamp": row[5],
            "source": row[6],
            "snippet": snippet if snippet is not None else (row[4][:200] if row[4] else ""),
        }

    try:
        rows = storage.db.execute("""
            SELECT rm.id, rm.group_name, rm.sender, rm.sender_phone,
                   rm.message, rm.timestamp, rm.source,
                   snippet(raw_messages_fts, 0, '<mark>', '</mark>', '...', 40) as snippet
            FROM raw_messages_fts fts
            JOIN raw_messages rm ON rm.id = fts.rowid
            WHERE raw_messages_fts MATCH ?
            ORDER BY rank
            LIMIT ? OFFSET ?
        """, (q, limit, offset)).fetchall()
        count_row = storage.db.execute("""
            SELECT COUNT(*) FROM raw_messages_fts WHERE raw_messages_fts MATCH ?
        """, (q,)).fetchone()
        total = count_row[0] if count_row else 0
        return {"results": [_row_to_result(r, r[7]) for r in rows], "count": total, "query": q}
    except Exception as e:
        try:
            like_q = f"%{q}%"
            rows = storage.db.execute("""
                SELECT id, group_name, sender, sender_phone, message, timestamp, source
                FROM raw_messages
                WHERE message LIKE ? OR group_name LIKE ? OR sender LIKE ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
            """, (like_q, like_q, like_q, limit, offset)).fetchall()
            count_row = storage.db.execute("""
                SELECT COUNT(*) FROM raw_messages
                WHERE message LIKE ? OR group_name LIKE ? OR sender LIKE ?
            """, (like_q, like_q, like_q)).fetchone()
            total = count_row[0] if count_row else 0
            return {"results": [_row_to_result(r) for r in rows], "count": total, "query": q}
        except Exception as like_error:
            logging.warning("[api/search/raw] FTS and LIKE search failed: %s / %s", e, like_error)
        try:
            # Never recover from a failed indexed query by scanning an
            # arbitrary raw-message page in Python. That made one search
            # request read 1,000 rows and still return incomplete results.
            # Surface an explicit degraded response so the client can retry
            # after the FTS/index problem is fixed.
            return {"results": [], "count": 0, "query": q, "fallback": "indexed_search_unavailable"}
        except Exception as scan_error:
            logging.warning("[api/search/raw] python fallback failed: %s", scan_error)
            return {"results": [], "count": 0, "query": q, "fallback": "empty"}


@router.get("/api/search/raw/sender")
async def search_raw_by_sender(sender: str = "", limit: int = 50, user: dict = Depends(require_user)):
    if not sender:
        return {"results": [], "count": 0}
    limit, _ = bounded_page(limit, 0)
    like_q = f"%{sender}%"
    rows = storage.db.execute("""
        SELECT id, group_name, sender, sender_phone, message, timestamp, source
        FROM raw_messages
        WHERE sender LIKE ? OR sender_phone LIKE ?
        ORDER BY id DESC
        LIMIT ?
    """, (like_q, like_q, limit)).fetchall()
    results = []
    for r in rows:
        group_name = r[1]
        if group_name and '@g.us' in group_name:
            resolved = storage.db.execute(
                "SELECT group_name FROM source_sync_jobs WHERE group_id = ? LIMIT 1",
                (group_name,)
            ).fetchone()
            if resolved:
                group_name = resolved[0]
        results.append({
            "id": r[0],
            "group_name": group_name,
            "sender": r[2],
            "sender_phone": r[3],
            "message": r[4],
            "timestamp": r[5],
            "source": r[6],
        })
    return {"results": results, "count": len(results), "query": sender}


@router.get("/api/search/raw/group")
async def search_raw_by_group(group_jid: str = "", limit: int = 50, user: dict = Depends(require_user)):
    if not group_jid:
        return {"results": [], "count": 0}
    limit, _ = bounded_page(limit, 0)
    rows = storage.db.execute("""
        SELECT id, group_name, sender, sender_phone, message, timestamp, source
        FROM raw_messages
        WHERE group_name = ? OR group_name LIKE ?
        ORDER BY id DESC
        LIMIT ?
    """, (group_jid, f"%{group_jid}%", limit)).fetchall()
    results = []
    for r in rows:
        group_name = r[1]
        if group_name and '@g.us' in group_name:
            resolved = storage.db.execute(
                "SELECT group_name FROM source_sync_jobs WHERE group_id = ? LIMIT 1",
                (group_name,)
            ).fetchone()
            if resolved:
                group_name = resolved[0]
        results.append({
            "id": r[0],
            "group_name": group_name,
            "sender": r[2],
            "sender_phone": r[3],
            "message": r[4],
            "timestamp": r[5],
            "source": r[6],
        })
    return {"results": results, "count": len(results), "query": group_jid}


@router.get("/api/search/market")
async def market_search(
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
    intent: str = "", bhk: str = "", building: str = "", micro_market: str = "",
    q: str = "",
    price_max: float = 0, price_min: float = 0, furnishing: str = "", broker: str = "",
    sort_by: str = "last_seen", limit: int = 10, offset: int = 0,
):
    # Market Map is backed by the shared typed listing tables. Keep this
    # route off the deprecated SQLite ``listings_unified`` compatibility view;
    # that view is not the source of truth and can be absent in production.
    return await asyncio.to_thread(
        storage.get_shared_market_listings,
        limit=limit,
        offset=offset,
        intent=intent,
        bhk=bhk,
        building=building,
        micro_market=micro_market,
        q=q,
        price_max=price_max,
        price_min=price_min,
        furnishing=furnishing,
        broker=broker,
    )
