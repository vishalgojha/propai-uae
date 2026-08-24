"""
Read-only deterministic backfill dry-run.

Uses location.parse_location (with the alias map in location.py _LOCATION_ALIASES)
against a CLEANED canonical gazetteer (merged DETERMINISTIC + LLM
Known micro_markets, buckets stripped). Reports match rate + unmatched samples.

NO database writes.

Run: python3 apps/www/scripts/backfill_deterministic_dryrun.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from location import parse_location  # noqa: E402

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

GAZETTEER = set()
for l in DETERMINISTIC + LLM_KNOWN:
    s = l.strip().lower()
    if s and s not in BUCKETS:
        GAZETTEER.add(s)


def is_canonical(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    return v if v in GAZETTEER else None


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/untagged.json"
    with open(path) as f:
        rows = __import__("json").load(f)
    print(f"Cleaned canonical gazetteer size: {len(GAZETTEER)} (buckets excluded: {len(BUCKETS)})")
    print(f"Loaded {len(rows)} untagged rows from {path}")

    total = matched = 0
    unmatched: list[str] = []
    by_loc: dict[str, int] = {}

    for row in rows:
        total += 1
        signal = (row.get("location_label") or "").strip() or (row.get("landmark_name") or "").strip()
        if not signal:
            if len(unmatched) < 25:
                unmatched.append(f"[id {row['id']}] (empty signal)")
            continue
        loc = parse_location(signal)
        hit = is_canonical(loc.micro_market) or is_canonical(loc.locality) or is_canonical(loc.city)
        if hit:
            matched += 1
            by_loc[hit] = by_loc.get(hit, 0) + 1
        elif len(unmatched) < 25:
            unmatched.append(f"[id {row['id']}] \"{signal[:70]}\"")

    rate = f"{(matched / total * 100):.1f}" if total else "0"
    print("\n=== DETERMINISTIC DRY-RUN (cleaned gazetteer, parse_location + aliases) ===")
    print(f"Untagged listings scanned : {total}")
    print(f"Matched (would be tagged) : {matched} ({rate}%)")
    print(f"Unmatched (review)        : {total - matched} ({100 - float(rate):.1f}%)")
    print("\nMatched breakdown:")
    for loc, n in sorted(by_loc.items(), key=lambda x: -x[1]):
        print(f"  {loc}: {n}")
    print(f"\nUnmatched samples (first {len(unmatched)}):")
    for s in unmatched:
        print(f"  - {s}")
    print("\nNO UPDATES PERFORMED.")


if __name__ == "__main__":
    main()
