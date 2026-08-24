"""
Broker routes — list, summary, feed, find, profile, share-card, hide/unhide, shared card view.
"""
import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from routers.common import storage, require_user, get_tenant_context, require_tenant, _resolve_active_organization_id, _group_jid_to_name

router = APIRouter(tags=["brokers"])


@router.get("/api/brokers")
async def list_brokers(
    user: dict = Depends(require_user),
    tenant_id: str = Depends(require_tenant),
):
    storage.rebuild_broker_graph()
    blocked_keys = storage.get_workspace_blocked_broker_keys(tenant_id)
    rows = storage.db.execute("""
        SELECT id, canonical_name, primary_phone,
               observation_count, listing_count, requirement_count,
               rental_count, commercial_count, group_count, market_count,
               building_count, active_days_30, first_seen_at, last_seen_at
        FROM brokers
        ORDER BY observation_count DESC, last_seen_at DESC
    """).fetchall()
    rows = [
        row for row in rows
        if not storage.broker_is_workspace_blocked(
            phone=str(row["primary_phone"] or ""),
            name=str(row["canonical_name"] or ""),
            blocked_keys=blocked_keys,
        )
    ]
    if not rows:
        # The public market inbox reads the current typed WhatsApp feed. During
        # the broker-graph cutover the legacy profile tables can be empty even
        # though live parsed listings already contain broker identities. Keep
        # the directory useful from that same source instead of showing a
        # misleading zero-profile state.
        feed = storage.get_brokers_feed(limit=200, offset=0, min_observations=1)
        return [
            {
                **item,
                "aliases": [],
                "phones": ([{"phone": item.get("primary_phone"), "observation_count": item.get("observation_count", 0)}]
                           if item.get("primary_phone") else []),
                "markets": [
                    {"micro_market": market, "observation_count": item.get("observation_count", 0),
                     "listing_count": item.get("listing_count", 0), "requirement_count": item.get("requirement_count", 0)}
                    for market in (item.get("specialty_localities") or [])
                ],
                "buildings": [],
                "groups": item.get("channels") or [],
                "recent_observations": [],
                "rental_count": 0,
                "commercial_count": 0,
                "market_count": len(item.get("specialty_localities") or []),
            }
            for item in feed
            if not storage.broker_is_workspace_blocked(
                phone=str(item.get("primary_phone") or ""),
                name=str(item.get("canonical_name") or ""),
                blocked_keys=blocked_keys,
            )
        ]
    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    top_n = {
        "aliases": 8,
        "phones": 5,
        "markets": 5,
        "buildings": 5,
        "observations": 8,
        "groups": 5,
    }

    def _batch(sql: str, params: list[int], n: int):
        # Rows are globally ordered by the same sort key as the original
        # per-broker query, so the first `n` rows per broker are its top-N.
        return storage.db.execute(f"{sql} LIMIT {len(params) * n}", params).fetchall()

    # Single batched query per table (6 total, run in parallel) instead of
    # one query per broker per table (6N+1 sequential round-trips).
    aliases_rows, phones_rows, markets_rows, buildings_rows, obs_rows, groups_rows = await asyncio.gather(
        asyncio.to_thread(_batch, f"""
            SELECT broker_id, alias, observation_count
            FROM broker_aliases
            WHERE broker_id IN ({placeholders})
            ORDER BY observation_count DESC
        """, ids, top_n["aliases"]),
        asyncio.to_thread(_batch, f"""
            SELECT broker_id, phone, observation_count
            FROM broker_phones
            WHERE broker_id IN ({placeholders})
            ORDER BY observation_count DESC
        """, ids, top_n["phones"]),
        asyncio.to_thread(_batch, f"""
            SELECT broker_id, micro_market, observation_count, listing_count, requirement_count
            FROM broker_market_stats
            WHERE broker_id IN ({placeholders})
            ORDER BY observation_count DESC, last_seen_at DESC
        """, ids, top_n["markets"]),
        asyncio.to_thread(_batch, f"""
            SELECT broker_id, building_name, observation_count, listing_count, requirement_count
            FROM broker_building_stats
            WHERE broker_id IN ({placeholders})
            ORDER BY observation_count DESC, last_seen_at DESC
        """, ids, top_n["buildings"]),
        asyncio.to_thread(_batch, f"""
            SELECT bo.broker_id, p.intent, p.message_type, p.bhk, p.furnishing,
                   p.building_name, p.micro_market, p.location_raw,
                   p.summary_title, substr(r.message, 1, 220) AS message
            FROM broker_observations bo
            JOIN parsed_output_unified p ON p.id = bo.parsed_id
            LEFT JOIN raw_messages r ON r.id = p.raw_message_id
            WHERE bo.broker_id IN ({placeholders})
            ORDER BY bo.seen_at DESC
        """, ids, top_n["observations"]),
        asyncio.to_thread(_batch, f"""
            SELECT broker_id, group_name,
                   COUNT(*) AS observation_count,
                   SUM(CASE WHEN role = 'listing' THEN 1 ELSE 0 END) AS listing_count,
                   SUM(CASE WHEN role = 'requirement' THEN 1 ELSE 0 END) AS requirement_count,
                   MAX(seen_at) AS last_seen_at
            FROM broker_observations
            WHERE broker_id IN ({placeholders}) AND group_name IS NOT NULL AND group_name != ''
            GROUP BY broker_id, group_name
            ORDER BY observation_count DESC, last_seen_at DESC
        """, ids, top_n["groups"]),
    )

    def _group_top_n(rows, n):
        grouped: dict[int, list[dict]] = {}
        counts: dict[int, int] = {}
        for r in rows:
            broker_id = r["broker_id"]
            if counts.get(broker_id, 0) >= n:
                continue
            item = dict(r)
            item.pop("broker_id", None)
            grouped.setdefault(broker_id, []).append(item)
            counts[broker_id] = counts.get(broker_id, 0) + 1
        return grouped

    aliases_by_broker = _group_top_n(aliases_rows, top_n["aliases"])
    phones_by_broker = _group_top_n(phones_rows, top_n["phones"])
    markets_by_broker = _group_top_n(markets_rows, top_n["markets"])
    buildings_by_broker = _group_top_n(buildings_rows, top_n["buildings"])
    obs_by_broker = _group_top_n(obs_rows, top_n["observations"])
    groups_by_broker = _group_top_n(groups_rows, top_n["groups"])
    # Resolve group JIDs to display names with a single batched lookup
    # instead of one sync_jobs query per group.
    group_jids = {item["group_name"] for items in groups_by_broker.values() for item in items}
    jid_to_name: dict[str, str] = {}
    if group_jids:
        gid_placeholders = ",".join("?" for _ in group_jids)
        try:
            for row in storage.db.execute(
                f"SELECT group_jid, group_name FROM sync_jobs WHERE group_jid IN ({gid_placeholders})",
                list(group_jids),
            ).fetchall():
                jid_to_name[row["group_jid"]] = row["group_name"]
        except Exception:
            pass
    groups_by_broker = {
        bid: [
            {
                **item,
                "group_name": jid_to_name.get(item["group_name"])
                or (item["group_name"].split("@")[0] if "@" in item["group_name"] else item["group_name"]),
            }
            for item in items
        ]
        for bid, items in groups_by_broker.items()
    }

    brokers = []
    for row in rows:
        broker = dict(row)
        broker_id = broker["id"]
        broker["aliases"] = aliases_by_broker.get(broker_id, [])
        broker["phones"] = phones_by_broker.get(broker_id, [])
        broker["markets"] = markets_by_broker.get(broker_id, [])
        broker["buildings"] = buildings_by_broker.get(broker_id, [])
        broker["recent_observations"] = obs_by_broker.get(broker_id, [])
        broker["groups"] = groups_by_broker.get(broker_id, [])
        search_parts = [
            broker.get("name"),
            broker.get("phone"),
            *(item.get("alias") for item in broker["aliases"]),
            *(item.get("phone") for item in broker["phones"]),
            *(item.get("micro_market") for item in broker["markets"]),
            *(item.get("building_name") for item in broker["buildings"]),
            *(item.get("group_name") for item in broker["groups"]),
        ]
        for item in broker["recent_observations"]:
            search_parts.extend([
                item.get("intent"),
                item.get("message_type"),
                item.get("bhk"),
                item.get("furnishing"),
                item.get("building_name"),
                item.get("micro_market"),
                item.get("location_raw"),
                item.get("summary_title"),
                item.get("message"),
            ])
        broker["search_text"] = " ".join(str(part) for part in search_parts if part).lower()
        brokers.append(broker)
    return brokers


@router.get("/api/broker-summary")
async def broker_summary(name: str = "", phone: str = "", user: dict = Depends(require_user)):
    empty = {"total_listings": 0, "intents": {}, "top_bhk": [], "markets": [], "price_range_sale": "", "price_range_rent": ""}
    if not name and not phone:
        return empty
    q = "SELECT intent, bhk, price, price_unit, micro_market, observation_count FROM listings_unified WHERE "
    params: list[str] = []
    clauses: list[str] = []
    if name:
        clauses.append("broker_name LIKE ?")
        params.append(f"%{name}%")
    if phone:
        clauses.append("broker_phone LIKE ?")
        params.append(f"%{phone}%")
    q += " AND ".join(clauses)
    rows = storage.db.execute(q, params).fetchall()
    total = len(rows)
    intents: dict[str, int] = {}
    bhk_dist: dict[str, int] = {}
    markets: dict[str, int] = {}
    prices_sale: list[float] = []
    prices_rent: list[float] = []
    for r in rows:
        d = dict(r)
        intent = d["intent"] or "UNKNOWN"
        intents[intent] = intents.get(intent, 0) + 1
        bhk = d["bhk"] or "?"
        bhk_dist[bhk] = bhk_dist.get(bhk, 0) + 1
        market = d["micro_market"] or "?"
        markets[market] = markets.get(market, 0) + 1
        if d["price"] and d["price_unit"]:
            p = float(d["price"])
            if intent in ("RENT", "LEASE"):
                prices_rent.append(p)
            else:
                prices_sale.append(p)

    def _fmt_price_range(prices: list[float]) -> str:
        if not prices:
            return ""
        prices.sort()
        if len(prices) == 1:
            return f"AED {prices[0]:,.0f}"
        return f"AED {prices[0]:,.0f} – {prices[-1]:,.0f}"

    top_markets = sorted(markets, key=markets.__getitem__, reverse=True)[:3]
    team_members: list[dict] = []
    seen_tm: set[str] = set()
    tm_query = "SELECT raw_payload FROM parsed_output_unified WHERE"
    tm_params: list[str] = []
    tm_clauses: list[str] = []
    if name:
        tm_clauses.append("broker_name LIKE ?")
        tm_params.append(f"%{name}%")
    if phone:
        tm_clauses.append("broker_phone LIKE ?")
        tm_params.append(f"%{phone}%")
    tm_query += " AND ".join(tm_clauses) + " AND raw_payload LIKE '%team_member%' ORDER BY id DESC LIMIT 50"
    for r in storage.db.execute(tm_query, tm_params).fetchall():
        try:
            rp = json.loads(r["raw_payload"]) if isinstance(r["raw_payload"], str) else r["raw_payload"]
            for tm in (rp.get("team_members") or []):
                if not tm.get("name"):
                    continue
                key = tm.get("name", "") + "|" + tm.get("phone", "")
                if key not in seen_tm and key != "|":
                    seen_tm.add(key)
                    team_members.append(tm)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "total_listings": total,
        "intents": intents,
        "top_bhk": sorted(bhk_dist, key=bhk_dist.__getitem__, reverse=True)[:3],
        "markets": top_markets,
        "price_range_sale": _fmt_price_range(prices_sale),
        "price_range_rent": _fmt_price_range(prices_rent),
        "team_members": team_members,
    }


@router.get("/api/brokers/hidden")
async def get_hidden_brokers(
    user: dict = Depends(require_user),
    tenant_id: str | None = Depends(get_tenant_context),
):
    try:
        params: list[object] = []
        where = "WHERE is_hidden = true"
        if tenant_id:
            where += " AND (tenant_id IS NULL OR tenant_id = ?)"
            params.append(tenant_id)
        rows = storage.db.execute(
            f"""
            SELECT id, canonical_name AS name, primary_phone, phone, observation_count,
                   listing_count, requirement_count, last_seen_at
            FROM brokers
            {where}
            ORDER BY last_seen_at DESC, observation_count DESC
            """,
            tuple(params),
        ).fetchall()
        brokers = []
        for row in rows:
            broker = dict(row)
            broker["primary_phone"] = broker.get("primary_phone") or broker.get("phone") or ""
            brokers.append(broker)
        return {"brokers": brokers}
    except Exception as exc:
        return {"brokers": []}


@router.get("/api/brokers/blocked")
async def list_blocked_brokers(
    user: dict = Depends(require_user),
    tenant_id: str = Depends(require_tenant),
):
    return {"brokers": storage.get_workspace_blocked_brokers(tenant_id)}


@router.post("/api/brokers/block")
async def block_broker(
    payload: dict,
    user: dict = Depends(require_user),
    tenant_id: str = Depends(require_tenant),
):
    phone = str(payload.get("phone") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not phone and not name:
        raise HTTPException(400, "Broker phone or name is required")
    try:
        rows = storage.block_broker_for_workspace(
            tenant_id,
            phone=phone,
            name=name,
            reason=str(payload.get("reason") or "").strip(),
            created_by=user.get("id"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"success": True, "blocked": rows}


@router.delete("/api/brokers/block")
async def unblock_broker(
    payload: dict,
    user: dict = Depends(require_user),
    tenant_id: str = Depends(require_tenant),
):
    key = str(payload.get("broker_key") or "").strip()
    if not key:
        phone = str(payload.get("phone") or "").strip()
        name = str(payload.get("name") or "").strip()
        key = storage._workspace_broker_key(phone=phone, name=name)
    if not storage.unblock_broker_for_workspace(tenant_id, key):
        raise HTTPException(404, "Blocked broker not found")
    return {"success": True}


@router.get("/api/brokers/feed")
async def get_brokers_feed(
    user: dict = Depends(require_user),
    limit: int = 50, offset: int = 0,
    min_observations: int = 2,
    include_total: bool = False,
    tenant_id: str | None = Depends(get_tenant_context),
):
    items = storage.get_brokers_feed(
        limit,
        offset,
        min_observations=min_observations,
        tenant_id=tenant_id,
    )
    if include_total:
        return {
            "items": items,
            "total": storage.get_brokers_feed_total(
                min_observations=min_observations,
                tenant_id=tenant_id,
            ),
        }
    return items


@router.get("/api/brokers/find")
async def find_broker(name: str = "", phone: str = "", user: dict = Depends(require_user)):
    from storage.supabase import _normalize_india_phone, _market_name_key
    if not name and not phone:
        raise HTTPException(400, "name or phone is required")
    norm_phone = _normalize_india_phone(phone)
    if norm_phone:
        key = f"phone:{norm_phone}"
    else:
        normalized_name = _market_name_key(name)
        key = f"name:{normalized_name}" if normalized_name else None
    if not key:
        raise HTTPException(404, "Broker identity key could not be resolved")
    row = storage.db.execute(
        "SELECT id FROM brokers WHERE identity_key = ?", (key,)
    ).fetchone()
    if not row:
        storage.rebuild_broker_graph()
        row = storage.db.execute(
            "SELECT id FROM brokers WHERE identity_key = ?", (key,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Broker not found")
    return {"broker_id": row["id"]}


@router.get("/api/brokers/{broker_id}")
async def get_broker_profile(
    broker_id: int,
    user: dict = Depends(require_user),
    tenant_id: str = Depends(require_tenant),
):
    storage.rebuild_broker_graph()
    row = storage.db.execute("""
        SELECT id, canonical_name AS name, primary_phone AS phone,
               observation_count, listing_count, requirement_count,
               rental_count, commercial_count, group_count, market_count,
               building_count, active_days_30, first_seen_at, last_seen_at
        FROM brokers
        WHERE id = ?
    """, (broker_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Broker not found")
    if storage.broker_is_workspace_blocked(
        phone=str(row["phone"] or ""),
        name=str(row["name"] or ""),
    ):
        raise HTTPException(404, "Broker not found")
    broker = dict(row)
    broker["aliases"] = [dict(r) for r in storage.db.execute("""
        SELECT alias, observation_count, first_seen_at, last_seen_at
        FROM broker_aliases
        WHERE broker_id = ?
        ORDER BY observation_count DESC
        LIMIT 20
    """, (broker_id,)).fetchall()]
    broker["phones"] = [dict(r) for r in storage.db.execute("""
        SELECT phone, observation_count, first_seen_at, last_seen_at
        FROM broker_phones
        WHERE broker_id = ?
        ORDER BY observation_count DESC
        LIMIT 10
    """, (broker_id,)).fetchall()]
    broker["markets"] = [dict(r) for r in storage.db.execute("""
        SELECT micro_market, observation_count, listing_count, requirement_count
        FROM broker_market_stats
        WHERE broker_id = ?
        ORDER BY observation_count DESC
        LIMIT 20
    """, (broker_id,)).fetchall()]
    broker["buildings"] = [dict(r) for r in storage.db.execute("""
        SELECT b.building_name, b.observation_count, b.listing_count, b.requirement_count,
               b.last_seen_at
        FROM broker_building_stats b
        WHERE b.broker_id = ?
        ORDER BY b.observation_count DESC
        LIMIT 50
    """, (broker_id,)).fetchall()]
    broker["groups"] = [
        {
            "group_name": _group_jid_to_name(r["group_name"]),
            "observation_count": r["observation_count"],
            "listing_count": r["listing_count"],
            "requirement_count": r["requirement_count"],
            "last_seen_at": r["last_seen_at"],
        }
        for r in storage.db.execute("""
            SELECT group_name,
                   COUNT(*) AS observation_count,
                   SUM(CASE WHEN role = 'listing' THEN 1 ELSE 0 END) AS listing_count,
                   SUM(CASE WHEN role = 'requirement' THEN 1 ELSE 0 END) AS requirement_count,
                   MAX(seen_at) AS last_seen_at
            FROM broker_observations
            WHERE broker_id = ? AND group_name IS NOT NULL AND group_name != ''
            GROUP BY group_name
            ORDER BY observation_count DESC, last_seen_at DESC
            LIMIT 30
        """, (broker_id,)).fetchall()
    ]
    broker["observations"] = [dict(r) for r in storage.db.execute("""
        SELECT p.id AS parsed_id, p.intent, p.message_type, p.bhk, p.price, p.price_unit,
               p.furnishing, p.building_name, p.micro_market, p.broker_name,
               p.confidence, p.created_at, bo.role, bo.group_name, bo.seen_at
        FROM broker_observations bo
        JOIN parsed_output_unified p ON p.id = bo.parsed_id
        WHERE bo.broker_id = ?
        ORDER BY bo.seen_at DESC
        LIMIT 100
    """, (broker_id,)).fetchall()]
    try:
        timeline = storage.db.execute("""
            SELECT DATE(seen_at) AS day, COUNT(*) AS count
            FROM broker_observations
            WHERE broker_id = ? AND seen_at IS NOT NULL
              AND seen_at >= DATE('now', '-60 days')
            GROUP BY DATE(seen_at)
            ORDER BY day ASC
        """, (broker_id,)).fetchall()
        broker["timeline"] = [{"day": r[0], "count": r[1]} for r in timeline]
    except Exception:
        broker["timeline"] = []
    try:
        highlights = storage.db.execute("""
            SELECT
                b.building_name,
                b.observation_count AS broker_obs,
                (SELECT COUNT(*) FROM broker_observations WHERE building_name = b.building_name) AS total_obs
            FROM broker_building_stats b
            WHERE b.broker_id = ?
              AND b.observation_count > 0
            ORDER BY b.observation_count DESC
            LIMIT 50
        """, (broker_id,)).fetchall()
        contribution = []
        for h in highlights:
            bldg = h["building_name"]
            bo = h["broker_obs"]
            to = h["total_obs"]
            if to > 0:
                pct = round(bo / to * 100)
                if pct >= 70:
                    contribution.append({
                        "building_name": bldg,
                        "broker_obs": bo,
                        "total_obs": to,
                        "share_pct": pct,
                        "is_exclusive": pct == 100,
                    })
        broker["contribution_highlights"] = contribution[:10]
    except Exception:
        broker["contribution_highlights"] = []
    return broker


@router.get("/api/brokers/{broker_id}/share-card")
async def get_broker_share_card(broker_id: int, user: dict = Depends(require_user)):
    storage.rebuild_broker_graph()
    row = storage.db.execute("""
        SELECT id, canonical_name AS name, primary_phone AS phone,
               observation_count, listing_count, requirement_count,
               rental_count, commercial_count, group_count, market_count,
               building_count, active_days_30, first_seen_at, last_seen_at
        FROM brokers
        WHERE id = ?
    """, (broker_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Broker not found")
    broker = dict(row)
    markets = [dict(r) for r in storage.db.execute("""
        SELECT micro_market, observation_count, listing_count, requirement_count
        FROM broker_market_stats
        WHERE broker_id = ?
        ORDER BY observation_count DESC
        LIMIT 3
    """, (broker_id,)).fetchall()]
    groups = [
        {
            "group_name": _group_jid_to_name(r["group_name"]),
            "observation_count": r["observation_count"],
        }
        for r in storage.db.execute("""
            SELECT group_name, COUNT(*) AS observation_count
            FROM broker_observations
            WHERE broker_id = ? AND group_name IS NOT NULL AND group_name != ''
            GROUP BY group_name
            ORDER BY observation_count DESC
            LIMIT 5
        """, (broker_id,)).fetchall()
    ]

    def _is_masked(name):
        if not name:
            return False
        return name.startswith("+") or "XXX" in name

    def _disp_phone(phone):
        if not phone:
            return ""
        digits = re.sub(r"\D+", "", phone)
        local = digits[-10:] if len(digits) >= 10 else digits
        if len(local) != 10:
            return ""
        return f"+91 {local[:5]} {local[5:]}"

    card_data = {
        "broker_name": _disp_phone(broker["phone"]) if _is_masked(broker["name"]) else (broker["name"] or "Unknown Broker"),
        "is_masked": _is_masked(broker["name"]),
        "phone_display": _disp_phone(broker["phone"]),
        "total_observations": broker["observation_count"] or 0,
        "supply_count": broker["listing_count"] or 0,
        "demand_count": broker["requirement_count"] or 0,
        "top_markets": markets,
        "top_groups": groups,
        "first_seen": broker["first_seen_at"],
        "last_active": broker["last_seen_at"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    }
    return card_data


@router.post("/api/brokers/{broker_id}/share-card/snapshot")
async def save_broker_share_card_snapshot(broker_id: int, user: dict = Depends(require_user)):
    storage.rebuild_broker_graph()
    row = storage.db.execute("""
        SELECT id, canonical_name AS name, primary_phone AS phone,
               observation_count, listing_count, requirement_count,
               rental_count, commercial_count, group_count, market_count,
               building_count, active_days_30, first_seen_at, last_seen_at
        FROM brokers
        WHERE id = ?
    """, (broker_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Broker not found")
    token = hashlib.sha256(f"{broker_id}:{datetime.now(timezone.utc).isoformat()}:{uuid.uuid4()}".encode()).hexdigest()[:32]
    broker = dict(row)
    markets = [dict(r) for r in storage.db.execute("""
        SELECT micro_market, observation_count, listing_count, requirement_count
        FROM broker_market_stats
        WHERE broker_id = ?
        ORDER BY observation_count DESC
        LIMIT 3
    """, (broker_id,)).fetchall()]
    groups = [
        {
            "group_name": _group_jid_to_name(r["group_name"]),
            "observation_count": r["observation_count"],
        }
        for r in storage.db.execute("""
            SELECT group_name, COUNT(*) AS observation_count
            FROM broker_observations
            WHERE broker_id = ? AND group_name IS NOT NULL AND group_name != ''
            GROUP BY group_name
            ORDER BY observation_count DESC
            LIMIT 5
        """, (broker_id,)).fetchall()
    ]

    def _is_masked(name):
        if not name:
            return False
        return name.startswith("+") or "XXX" in name

    def _disp_phone(phone):
        if not phone:
            return ""
        digits = re.sub(r"\D+", "", phone)
        local = digits[-10:] if len(digits) >= 10 else digits
        if len(local) != 10:
            return ""
        return f"+91 {local[:5]} {local[5:]}"

    card_data = {
        "broker_name": _disp_phone(broker["phone"]) if _is_masked(broker["name"]) else (broker["name"] or "Unknown Broker"),
        "is_masked": _is_masked(broker["name"]),
        "phone_display": _disp_phone(broker["phone"]),
        "total_observations": broker["observation_count"] or 0,
        "supply_count": broker["listing_count"] or 0,
        "demand_count": broker["requirement_count"] or 0,
        "top_markets": markets,
        "top_groups": groups,
        "first_seen": broker["first_seen_at"],
        "last_active": broker["last_seen_at"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    }
    storage.db.execute(
        "INSERT INTO share_cards (token, broker_id, card_data, created_at) VALUES (?, ?, ?, ?)",
        (token, broker_id, json.dumps(card_data), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    storage.db.commit()
    return {"token": token, "url": f"/api/share/brokers/{token}"}


@router.post("/api/brokers/{phone}/hide")
async def hide_broker(
    phone: str,
    user: dict = Depends(require_user),
    tenant_id: str = Depends(require_tenant),
):
    rows = storage.block_broker_for_workspace(tenant_id, phone=phone, created_by=user.get("id"))
    return {"success": True, "blocked": rows}


@router.post("/api/brokers/{phone}/unhide")
async def unhide_broker(
    phone: str,
    user: dict = Depends(require_user),
    tenant_id: str = Depends(require_tenant),
):
    from storage.supabase import _normalize_india_phone
    key = f"phone:{_normalize_india_phone(phone)}"
    if not storage.unblock_broker_for_workspace(tenant_id, key):
        raise HTTPException(404, "Blocked broker not found")
    return {"success": True}


@router.get("/api/share/brokers/{token}")
async def get_shared_broker_card(token: str):
    row = storage.db.execute(
        "SELECT card_data, created_at FROM share_cards WHERE token = ?",
        (token,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Share card not found or expired")
    card = json.loads(row["card_data"])
    card["token"] = token
    return card
