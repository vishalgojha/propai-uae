"""Background enrichment worker.

Polls pending enrichment_jobs where scheduled_after has passed.
For each job:
  - Runs deterministic enrichments (alias resolution, price check)
  - Runs database/cross-reference lookups (building, location)
  - Calls LLM as last resort for building/location
  - Creates ai_suggestions with findings

Runs as: python3 worker.py
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from storage import SupabaseStorage

POLL_INTERVAL = 30
JOB_BATCH = 20


def run_cycle(storage: SupabaseStorage):
    jobs = storage.get_pending_enrichment_jobs(limit=JOB_BATCH)
    if not jobs:
        return

    for job in jobs:
        if not storage.claim_enrichment_job(job["id"]):
            continue
        try:
            enrich_observation(storage, job)
            storage.complete_enrichment_job(job["id"])
        except Exception as e:
            storage.complete_enrichment_job(job["id"], error=str(e))


def enrich_observation(storage: SupabaseStorage, job: dict):
    parsed_id = job["parsed_id"]
    row = storage.db.execute(
        """SELECT p.*, r.sender, r.group_name, r.message, r.timestamp
           FROM parsed_output_unified p
           JOIN raw_messages r ON r.id = p.raw_message_id
           WHERE p.id = ?""",
        (parsed_id,),
    ).fetchone()
    if not row:
        return

    d = dict(row)

    # 1. Building name detection
    try:
        from agents.building_detector import enrich_building
        enrich_building(storage, d)
    except Exception as e:
        pass

    # 2. Location resolution
    try:
        from agents.location_resolver import enrich_location
        enrich_location(storage, d)
    except Exception as e:
        pass

    # 3. Price outlier check
    try:
        _check_price_outlier(storage, d)
    except Exception:
        pass


def _check_price_outlier(storage: SupabaseStorage, d: dict):
    micro_market = d.get("micro_market") or ""
    bhk = d.get("bhk") or ""
    price = d.get("price")
    intent = d.get("intent") or "listing"
    parsed_id = d.get("id")

    if not micro_market or not bhk or not price:
        return
    stats = storage.get_price_stats(micro_market, bhk, intent)
    if not stats or stats["count"] < 5:
        return

    price_f = float(price)
    if stats["p5"] <= price_f <= stats["p95"]:
        return

    outlier_type = "above" if price_f > stats["p95"] else "below"
    title = f"Price outlier: AED {price_f:,.0f} for {bhk} in {micro_market}"
    description = (
        f"{bhk} in {micro_market}: price AED {price_f:,.0f} is {outlier_type} "
        f"the normal range (AED {stats['p5']:,.0f}–{stats['p95']:,.0f}). "
        f"Median: AED {stats['median']:,.0f} ({stats['count']} samples). "
    )

    from lab.storage.base import AISuggestion
    sug = AISuggestion(
        agent="price",
        suggestion_type="flag",
        title=title,
        description=description,
        source_data=json.dumps({
            "parsed_id": parsed_id,
            "micro_market": micro_market,
            "bhk": bhk,
            "price": price_f,
            "median": stats["median"],
            "p5": stats["p5"],
            "p95": stats["p95"],
            "count": stats["count"],
        }),
        proposal_data=json.dumps({"action": "flag_price_outlier", "parsed_id": parsed_id}),
        confidence=0.92 if outlier_type == "below" else 0.85,
    )
    storage.create_suggestion(sug)


def main():
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not supabase_key:
        raise RuntimeError("Supabase is required. Set SUPABASE_URL and SUPABASE_SERVICE_KEY.")
    storage = SupabaseStorage(supabase_url, supabase_key)

    print(f"Worker started — polling every {POLL_INTERVAL}s for enrichment jobs")
    while True:
        try:
            run_cycle(storage)
        except Exception as e:
            print(f"Cycle error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
