"""
Apply Option C backfill (read-only dry-run verified: 626 resolvable + 115 mistag corrections).

Pass 1 — Option C tag:
  For listings with building_name populated but micro_market NULL, join to
  buildings.canonical_name and use buildings.address as the locality candidate,
  validated against the cleaned gazetteer (buckets excluded). Write micro_market.

Pass 2 — Mistag correction:
  For listings whose micro_market is already set but REALLY conflicts with their
  building's address (base locality differs, West/East granularity excluded),
  overwrite micro_market with the building address (gazetteer-validated).

NO writes happen to rows that fail gazetteer validation.

Run: python3 apps/www/scripts/apply_option_c.py
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

URL = "https://jsoiuzfwohtfkctlkozw.supabase.co"
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]


DETERMINISTIC = [
    "business bay", "downtown dubai", "dubai marina", "jbr", "difc",
    "palm jumeirah", "jvc", "jvt", "jlt", "dubai hills estate",
    "arabian ranches", "the springs", "the meadows", "the greens",
    "the lakes", "the views", "al barsha", "al furjan", "deira", "karama",
    "mirdif", "motor city", "sports city", "studio city", "production city",
    "remraam", "mudon", "arjan", "town square", "dubailand", "liwan",
    "majan", "nad al sheba", "meydan", "mbr city", "reem", "city walk",
    "zaabeel", "al jaddaf", "oud metha", "bur dubai", "satwa", "jumeirah",
    "umm suqeim", "al sufouh", "emirates hills", "jumeirah golf estates",
    "jumeirah islands", "green community", "dubai investment park",
    "jebel ali", "academic city", "al warqa", "muhaisnah",
    "international city", "al nahda", "al qusais", "rashidiya",
]
LLM_KNOWN = [
    "marina", "downtown", "barsha", "furjan", "ranches", "springs",
    "meadows", "greens", "burj khalifa", "old town", "opera district",
    "palm", "pj", "signature villas", "jumeirah village circle",
    "jumeirah village triangle", "impz", "damac hills", "akoya oxygen",
    "discovery gardens", "silicon oasis", "dubai silicon oasis",
    "al qouz", "al quoz", "district one", "mohammed bin rashid city",
    "jebel ali village", "the villa", "meydan city", "reem dubai",
    "dubai marina", "downtown dubai", "business bay", "jbr", "difc",
    "palm jumeirah", "jvc", "jvt", "jlt", "dubai hills estate",
    "arabian ranches", "the springs", "the meadows", "the greens",
    "al barsha", "al furjan", "deira", "karama", "mirdif", "motor city",
    "sports city", "production city", "arjan", "town square", "meydan",
    "mbr city", "city walk", "al jaddaf", "jumeirah", "umm suqeim",
    "al sufouh", "emirates hills", "international city", "al nahda",
    "al qusais", "al warqa",
]
BUCKETS = {
    "unknown", "not specified", "not available", "n/a", "na", "none",
    "null", "nil", "listing", "requirement", "property", "text",
    "various", "multiple",
}
GAZETTEER = {s.strip().lower() for s in (DETERMINISTIC + LLM_KNOWN) if s.strip().lower() and s.strip().lower() not in BUCKETS}

HEADERS = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "Prefer": "return=minimal"}


def is_canonical(v):
    if not v:
        return None
    v = v.strip().lower()
    return v if v in GAZETTEER else None


def base(v):
    return re.sub(r"\s+(west|east|central|prime|mid|extended)$", "", (v or "").lower()).strip()


def get(path, params):
    url = f"{URL}/rest/v1/{path}{params}"
    req = urllib.request.Request(url, headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})
    return json.loads(urllib.request.urlopen(req).read())


def patch(row_id, value):
    body = json.dumps({"micro_market": value}).encode()
    req = urllib.request.Request(f"{URL}/rest/v1/listings?id=eq.{row_id}", data=body, headers=HEADERS, method="PATCH")
    urllib.request.urlopen(req)


def main() -> None:
    # Buildings map
    buildings = []
    for offset in range(0, 10000, 1000):
        rows = get("buildings", f"?select=canonical_name,address,micro_market&canonical_name=not.is.null&limit=1000&offset={offset}")
        buildings.extend(rows)
        if len(rows) < 1000:
            break
    name_to_address = {}
    for b in buildings:
        cn = (b.get("canonical_name") or "").strip()
        if cn:
            name_to_address[cn.lower()] = (b.get("address") or "").strip()

    # Listings
    listings = []
    for offset in range(0, 20000, 1000):
        rows = get("listings", f"?select=id,building_name,micro_market&building_name=not.is.null&limit=1000&offset={offset}")
        listings.extend(rows)
        if len(rows) < 1000:
            break

    option_c = []   # (id, value) untagged -> tag from address
    mistag = []     # (id, value) mistagged -> overwrite from address
    for r in listings:
        bn = (r.get("building_name") or "").strip()
        addr = name_to_address.get(bn.lower(), "")
        hit = is_canonical(addr)
        mm = (r.get("micro_market") or "").strip()
        if not mm:
            if hit:
                option_c.append((r["id"], hit.title()))
        else:
            if hit and base(mm) != base(addr):
                mistag.append((r["id"], hit.title()))

    print(f"Option C to tag: {len(option_c)}")
    print(f"Mistag to correct: {len(mistag)}")

    def run(label, items):
        done = 0
        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(lambda x: patch(x[0], x[1]), items))
            done = len(items)
        print(f"  applied {label}: {done}")

    run("Option C", option_c)
    run("mistag correction", mistag)
    print("\nNO writes to rows failing gazetteer validation. Done.")


if __name__ == "__main__":
    main()
