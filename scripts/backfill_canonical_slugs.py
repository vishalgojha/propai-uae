#!/usr/bin/env python3
"""Backfill canonical_micro_market_slug on listings and buildings tables.

Reads every row's micro_market, applies the canonical locality mapping
(from locality-canon.ts), and writes the computed slug back.

Run once after the migration adds the column:
    python scripts/backfill_canonical_slugs.py

Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment.
"""

import os
import re
import sys
import time

from supabase import create_client

# ── Canonical locality mapping (mirrors apps/www/src/lib/locality-canon.ts) ──

HIDDEN_BUCKETS = {
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

GENERIC_PARENTS = {
    "dubai",
    "uae",
}

IMPLIED_DIRECTION = {
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

STANDALONE_LOCALITIES = {
    "business bay": "Business Bay",
    "downtown dubai": "Downtown Dubai",
    "dubai marina": "Dubai Marina",
    "jbr": "JBR",
    "difc": "DIFC",
    "palm jumeirah": "Palm Jumeirah",
    "jvc": "JVC",
    "jvt": "JVT",
    "jlt": "JLT",
    "dubai hills estate": "Dubai Hills Estate",
    "damac hills": "Damac Hills",
    "damac hills 2": "Damac Hills 2",
    "arabian ranches": "Arabian Ranches",
    "arabian ranches 2": "Arabian Ranches",
    "arabian ranches 3": "Arabian Ranches",
    "the springs": "The Springs",
    "the meadows": "The Meadows",
    "the greens": "The Greens",
    "the lakes": "The Lakes",
    "the views": "The Views",
    "al barsha": "Al Barsha",
    "al barsha south": "Al Barsha South",
    "al furjan": "Al Furjan",
    "deira": "Deira",
    "karama": "Karama",
    "mirdif": "Mirdif",
    "motor city": "Motor City",
    "sports city": "Sports City",
    "studio city": "Studio City",
    "production city": "Production City",
    "remraam": "Remraam",
    "mudon": "Mudon",
    "arjan": "Arjan",
    "town square": "Town Square",
    "dubailand": "Dubailand",
    "liwan": "Liwan",
    "majan": "Majan",
    "nad al sheba": "Nad Al Sheba",
    "meydan": "Meydan",
    "mbr city": "MBR City",
    "reem": "Reem",
    "city walk": "City Walk",
    "zaabeel": "Zaabeel",
    "al jaddaf": "Al Jaddaf",
    "oud metha": "Oud Metha",
    "bur dubai": "Bur Dubai",
    "satwa": "Satwa",
    "jumeirah": "Jumeirah",
    "umm suqeim": "Umm Suqeim",
    "al sufouh": "Al Sufouh",
    "emirates hills": "Emirates Hills",
    "jumeirah golf estates": "Jumeirah Golf Estates",
    "jumeirah islands": "Jumeirah Islands",
    "green community": "Green Community",
    "dubai investment park": "Dubai Investment Park",
    "jebel ali": "Jebel Ali",
    "academic city": "Academic City",
    "al warqa": "Al Warqa",
    "muhaisnah": "Muhaisnah",
    "international city": "International City",
    "al nahda": "Al Nahda",
    "al qusais": "Al Qusais",
    "rashidiya": "Rashidiya",
    "hatta": "Hatta",
}


def _slugify(value: str) -> str:
    """Mirror apps/www/src/lib/supabase.ts slugify()."""
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", value.strip().lower()))


def canonical_micro_market_slug(raw: str | None) -> str | None:
    """Return the canonical URL slug for a raw micro_market value, or None if hidden/unknown."""
    if not raw:
        return None
    normalised = re.sub(r"\s+", " ", raw.strip().lower())
    if not normalised:
        return None
    if normalised in HIDDEN_BUCKETS:
        return None
    if normalised in REDIRECTS:
        return _slugify(REDIRECTS[normalised])
    if normalised in IMPLIED_DIRECTION:
        return _slugify(IMPLIED_DIRECTION[normalised])
    if normalised in GENERIC_PARENTS:
        return _slugify(raw.strip())
    label = STANDALONE_LOCALITIES.get(normalised)
    if label:
        return _slugify(label)
    # Unknown raw value — not public in the canonical mapping.
    return None


# ── Backfill logic ──────────────────────────────────────────────────────────

BATCH = 1000


def backfill_table(client, table: str) -> int:
    """Backfill canonical_micro_market_slug for every row in *table*."""
    total = 0
    offset = 0
    while True:
        res = (
            client.table(table)
            .select("id, micro_market")
            .not_.is_("micro_market", None)
            .range(offset, offset + BATCH - 1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            break

        updates = []
        for row in rows:
            slug = canonical_micro_market_slug(row.get("micro_market"))
            # Only update if the slug is non-null or the column was previously set.
            updates.append({"id": row["id"], "canonical_micro_market_slug": slug})

        # Batch update in chunks of 100 (Supabase upsert limit).
        for i in range(0, len(updates), 100):
            chunk = updates[i : i + 100]
            client.table(table).upsert(chunk, on_conflict="id").execute()

        total += len(rows)
        if len(rows) < BATCH:
            break
        offset += BATCH
        if total % 5000 == 0:
            print(f"  {table}: {total} rows processed …", flush=True)

    return total


def main():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")

    client = create_client(url, key)

    for table in ("listings", "buildings"):
        print(f"Backfilling {table} …", flush=True)
        t0 = time.time()
        n = backfill_table(client, table)
        elapsed = time.time() - t0
        print(f"  {table}: done — {n} rows in {elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    main()
