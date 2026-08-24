#!/usr/bin/env python3
"""Backfill canonical locality normalization for micro_market columns.

The DB accumulated dirty micro_market values before any normaliser existed:
case dupes ("Bandra Bkc" vs "Bandra BKC"), non-place internal buckets
("Western Suburbs Prime"), and ambiguous compound labels ("Bandra BKC").

This script applies the SAME rules as apps/www/src/lib/locality-canon.ts:
  - trim + case-fold for comparison
  - implied direction (Bandra->Bandra West, Khar->Khar West,
    Santacruz/Scuz->Santacruz West)
  - Bandra BKC variants -> Bandra East ; bare BKC -> Bandra Kurla Complex
  - generic parents (Andheri, Dadar, Thane, Malad, Goregaon, Vile Parle,
    Kandivali, Borivali) keep their own bucket, no change
  - non-place buckets are HIDDEN (set to NULL) only with --null-hidden

DEFAULT MODE IS DRY-RUN: prints per-bucket affected counts and the raw->canonical
mapping. Pass --apply to actually UPDATE rows. Pass --null-hidden to also NULL
hidden buckets (off by default to avoid data loss).

Requires env: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.
"""
import os
import sys
from collections import defaultdict

try:
    from supabase import create_client
except ImportError:
    print("ERROR: pip install supabase", file=sys.stderr)
    sys.exit(1)

URL = os.environ.get("SUPABASE_URL")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
if not URL or not KEY:
    print("ERROR: set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr)
    sys.exit(1)

# ---- rules (mirror locality-canon.ts) ----
HIDDEN = {
    "unknown",
    "not specified",
    "not available",
    "n/a",
    "na",
    "none",
    "null",
    "nil",
    "listing",
    "requirement",
    "property",
    "text",
}
GENERIC = {
    "dubai",
    "uae",
}
IMPLIED = {
    "marina": "Dubai Marina",
    "downtown": "Downtown Dubai",
    "barsha": "Al Barsha",
    "furjan": "Al Furjan",
    "ranches": "Arabian Ranches",
    "springs": "The Springs",
    "meadows": "The Meadows",
    "greens": "The Greens",
}
REDIRECTS = {
    "jbr": "JBR",
    "jumeirah beach residence": "JBR",
    "jumeirah beach residences": "JBR",
    "burj khalifa": "Downtown Dubai",
    "old town": "Downtown Dubai",
    "opera district": "Downtown Dubai",
    "difc": "DIFC",
    "dubai international financial centre": "DIFC",
    "palm": "Palm Jumeirah",
    "pj": "Palm Jumeirah",
    "palm jumeriah": "Palm Jumeirah",
    "signature villas": "Palm Jumeirah",
    "jumeirah village circle": "JVC",
    "jumeirah village triangle": "JVT",
    "jumeriah lakes towers": "JLT",
    "jumeirah lakes towers": "JLT",
    "impz": "Production City",
    "production city": "Production City",
    "international media production zone": "Production City",
    "dubai hills": "Dubai Hills Estate",
    "hills estate": "Dubai Hills Estate",
    "damac hills": "Damac Hills",
    "akoya oxygen": "Damac Hills 2",
    "damac hills 2": "Damac Hills 2",
    "the lakes": "The Lakes",
    "the views": "The Views",
    "discovery gardens": "Discovery Gardens",
    "jebel ali village": "Jebel Ali",
    "al khail gate": "Al Quoz",
    "al qouz": "Al Quoz",
    "al quoz industrial": "Al Quoz",
    "meydan city": "Meydan",
    "mbr city": "MBR City",
    "mohammed bin rashid city": "MBR City",
    "district one": "MBR City",
    "district 7": "MBR City",
    "reem dubai": "Reem",
    "the villa": "The Villa",
    "al waha": "Silicon Oasis",
    "dubai silicon oasis": "Silicon Oasis",
    "silicon central": "Silicon Oasis",
}

# Listing locality data now lives in the four typed source tables.  Keep the
# aggregate/support tables here as separate backfill targets; the unified
# listing views are intentionally read-only projections.
TABLES = [
    "residential_sale_listings",
    "residential_rent_listings",
    "commercial_sale_listings",
    "commercial_rent_listings",
    "buildings",
    "broker_market_stats",
    "broker_observations",
]


def canon(raw):
    if raw is None:
        return None, None  # (action, value)
    s = " ".join(str(raw).strip().split()).lower()
    if not s:
        return None, None
    if s in HIDDEN:
        return "hidden", None
    if s in REDIRECTS:
        return "mapped", REDIRECTS[s]
    if s in IMPLIED:
        return "mapped", IMPLIED[s]
    if s in GENERIC:
        return "keep", raw.strip()
    return "keep", raw.strip()


def main():
    apply = "--apply" in sys.argv
    null_hidden = "--null-hidden" in sys.argv
    client = create_client(URL, KEY)

    total_by_action = defaultdict(lambda: defaultdict(int))  # table -> action -> count
    mapping = defaultdict(int)  # "raw -> canonical" -> count
    hidden_count = defaultdict(int)

    for table in TABLES:
        # distinct raw values + counts — paginate, supabase caps a bare
        # select at 1000 rows so we must page to get true counts.
        rows = []
        try:
            for offset in range(0, 1_000_000, 1000):
                res = client.table(table).select("micro_market").range(offset, offset + 999).execute()
                page = res.data or []
                rows.extend(page)
                if len(page) < 1000:
                    break
        except Exception as e:
            print(f"  (skip {table}: {e})")
            continue
        counts = defaultdict(int)
        for r in rows:
            mm = r.get("micro_market")
            if mm is None:
                continue
            counts[" ".join(str(mm).strip().split())] += 1

        for raw, n in sorted(counts.items(), key=lambda x: -x[1]):
            action, value = canon(raw)
            if action == "hidden":
                total_by_action[table]["hidden"] += n
                hidden_count[raw] += n
                if apply and null_hidden:
                    _update_null(client, table, raw)
            elif action == "mapped":
                total_by_action[table]["mapped"] += n
                mapping[f"{raw}  ->  {value}"] += n
                if apply:
                    _update_set(client, table, raw, value)
            else:  # keep
                # still normalize case/whitespace if differs from trimmed
                if raw != raw.strip():
                    total_by_action[table]["trimmed"] += n
                    if apply:
                        _update_set(client, table, raw, raw.strip())

    # ---- report ----
    print("\n=== DRY-RUN REPORT ===" if not apply else "\n=== APPLIED ===")
    for table in TABLES:
        a = total_by_action.get(table)
        if not a:
            continue
        print(f"\n[{table}]")
        for act, c in sorted(a.items(), key=lambda x: -x[1]):
            print(f"  {act}: {c} rows")
    if mapping:
        print("\n-- mapped raw -> canonical --")
        for k, v in sorted(mapping.items(), key=lambda x: -x[1]):
            print(f"  {k}   ({v} rows)")
    if hidden_count:
        print("\n-- HIDDEN buckets (not nulled unless --null-hidden) --")
        for k, v in sorted(hidden_count.items(), key=lambda x: -x[1]):
            print(f"  {k}   ({v} rows)")
    print("\nDone." + ("" if apply else " Re-run with --apply to write changes."))


def _update_set(client, table, old, new):
    try:
        client.table(table).update({"micro_market": new}).eq("micro_market", old).execute()
    except Exception as e:
        print(f"  UPDATE FAIL {table} '{old}'->'{new}': {e}", file=sys.stderr)


def _update_null(client, table, old):
    try:
        client.table(table).update({"micro_market": None}).eq("micro_market", old).execute()
    except Exception as e:
        print(f"  NULL FAIL {table} '{old}': {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
