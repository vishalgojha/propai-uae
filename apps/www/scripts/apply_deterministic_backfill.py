"""
Apply the pre-approved deterministic backfill: tag ONLY the rows that
parse_location + aliases resolve to a cleaned-gazetteer canonical locality.
Read-only dry-run confirmed this set = 370 rows. The remaining 4,575 rows are
structurally non-locality text (marketing fragments, floor/deal descriptors,
prepositions, bare building names) and are left PERMANENTLY untagged.

No LLM pass. No UPDATE on unmatched rows.

Run: python3 apps/www/scripts/apply_deterministic_backfill.py
"""
import json
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


def is_canonical(value):
    if not value:
        return None
    v = value.strip().lower()
    return v if v in GAZETTEER else None


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/untagged.json"
    rows = json.load(open(path))

    to_tag = []  # (id, micro_market)
    for row in rows:
        signal = (row.get("location_label") or "").strip() or (row.get("landmark_name") or "").strip()
        if not signal:
            continue
        loc = parse_location(signal)
        mm = is_canonical(loc.micro_market) or is_canonical(loc.locality) or is_canonical(loc.city)
        if mm:
            to_tag.append((row["id"], mm.title()))

    print(f"Rows to tag: {len(to_tag)}")

    # Apply via PostgREST (service role). Use a minimal HTTP client to avoid
    # the broken python supabase package.
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    headers = {
        "apikey": KEY,
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    def patch(row_id_mm):
        row_id, mm = row_id_mm
        body = json.dumps({"micro_market": mm}).encode()
        req = urllib.request.Request(
            f"{URL}/rest/v1/listings?id=eq.{row_id}",
            data=body,
            headers=headers,
            method="PATCH",
        )
        urllib.request.urlopen(req)

    done = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(patch, to_tag))
        done = len(to_tag)

    print(f"Tagged {done} rows. Remaining {len(rows) - done} left permanently untagged.")
    print("No LLM pass. Unmatched rows have no locality signal in source text.")


if __name__ == "__main__":
    main()
