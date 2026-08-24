"""
Option C dry-run (read-only): for listings with building_name populated but
micro_market NULL, resolve the locality via buildings.canonical_name -> buildings.address
(NOT buildings.micro_market, which is a bucket). Validate the address against the
cleaned gazetteer before counting it as resolvable.

Also reports the Kalpataru Magnus mistag scope: listings whose micro_market is
already set but CONFLICTS with their building's address (likely mistagged).

NO database writes.

Run: python3 apps/www/scripts/backfill_option_c_dryrun.py
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error

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


def is_canonical(v):
    if not v:
        return None
    v = v.strip().lower()
    # address may be "Dubai Marina" -> match; or "JBR" -> match; allow gazetteer or alias-normalized
    return v if v in GAZETTEER else None


def get(path, params):
    url = f"{URL}/rest/v1/{path}{params}"
    req = urllib.request.Request(url, headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})
    return json.loads(urllib.request.urlopen(req).read())


def main() -> None:
    # Load all buildings (canonical_name -> address). Paginate.
    buildings = []
    for offset in range(0, 10000, 1000):
        rows = get("buildings", f"?select=canonical_name,address,micro_market&canonical_name=not.is.null&limit=1000&offset={offset}")
        buildings.extend(rows)
        if len(rows) < 1000:
            break
    name_to_address = {}
    name_to_market = {}
    for b in buildings:
        cn = (b["canonical_name"] or "").strip()
        if cn:
            name_to_address[cn.lower()] = (b.get("address") or "").strip()
            name_to_market[cn.lower()] = (b.get("micro_market") or "").strip()

    # Listings with building_name populated. Paginate.
    listings = []
    for offset in range(0, 20000, 1000):
        rows = get("listings", f"?select=id,building_name,micro_market&building_name=not.is.null&limit=1000&offset={offset}")
        listings.extend(rows)
        if len(rows) < 1000:
            break

    untagged = [r for r in listings if not (r.get("micro_market") or "").strip()]
    print(f"Listings w/ building_name: {len(listings)}")
    print(f"  of which micro_market NULL (Option C target): {len(untagged)}")

    resolved = 0
    unresolved_samples = []
    by_locality = {}
    for r in untagged:
        bn = (r.get("building_name") or "").strip()
        addr = name_to_address.get(bn.lower(), "")
        hit = is_canonical(addr)
        if hit:
            resolved += 1
            by_locality[hit] = by_locality.get(hit, 0) + 1
        elif len(unresolved_samples) < 15:
            unresolved_samples.append(f"[id {r['id']}] bn={bn!r} addr={addr!r}")

    rate = f"{(resolved / len(untagged) * 100):.1f}" if untagged else "0"
    print(f"\nOption C resolvable (via building.address, gazetteer-validated): {resolved} ({rate}%)")
    print("Resolved breakdown:")
    for loc, n in sorted(by_locality.items(), key=lambda x: -x[1]):
        print(f"  {loc}: {n}")
    print(f"\nUnresolved samples (first {len(unresolved_samples)}):")
    for s in unresolved_samples:
        print(f"  - {s}")

    # Mistag scope: listings whose micro_market is set but conflicts with building.address.
    # Normalize away West/East/Central granularity so we only count REAL conflicts.
    def base(v):
        return re.sub(r"\s+(west|east|central|prime|mid|extended)$", "", (v or "").lower()).strip()

    conflicts = []
    benign = 0
    for r in listings:
        mm = (r.get("micro_market") or "").strip()
        if not mm:
            continue
        bn = (r.get("building_name") or "").strip()
        addr = name_to_address.get(bn.lower(), "")
        if addr and is_canonical(addr) and is_canonical(mm):
            if base(mm) != base(addr):
                conflicts.append((r["id"], bn, mm, addr))
            else:
                benign += 1
    print(f"\nMistag candidates (listing micro_market set, REAL conflict with building.address): {len(conflicts)}")
    print(f"  (benign West/East granularity diffs, not counted: {benign})")
    for c in conflicts[:15]:
        print(f"  - id={c[0]} bn={c[1]!r} listing_mm={c[2]!r} building_addr={c[3]!r}")

    print("\nNO UPDATES PERFORMED.")


if __name__ == "__main__":
    main()
