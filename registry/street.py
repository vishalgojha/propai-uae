"""
Street Registry — location infrastructure for the Evidence Engine.

Streets are a first-class canonical entity alongside buildings.
Every building sits on one or more streets. Every street belongs
to a micro market.

This enables:
  - "Show me buildings on Marina Walk"
  - "Property near Al Wasl Road" (WhatsApp/DLD)
  - "2 BR near Burj Khalifa" (landmark → street → buildings)
  - DLD address resolution (plot number → street → micro market)
"""
import csv
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREETS_PATH = os.path.join(BASE_DIR, "data", "streets.csv")
BUILDING_STREETS_PATH = os.path.join(BASE_DIR, "data", "building_streets.csv")

STREET_SUFFIXES = {"road", "street", "walk", "lane", "avenue", "drive", "highway", "way", "path", "promenade", "boulevard"}

# Known Dubai streets that may not appear in our existing data
# Each: (name, aliases, micro_market, pincodes) — Dubai has no postal codes,
# so pincodes are always empty.
KNOWN_DUBAI_STREETS = [
    # Arterial highways
    ("Sheikh Zayed Road", ["SZR", "E11", "Sheikh Zayed Rd"], "Sheikh Zayed Road", []),
    ("Al Khail Road", ["E44", "Al Khail"], "Al Quoz", []),
    ("Mohammed Bin Zayed Road", ["E311", "MBZ Road", "MBZR"], "Al Barsha", []),
    ("Hessa Street", ["Al Hess Street", "Al Hessa Street"], "Al Barsha", []),
    # Marina / New Dubai
    ("Marina Walk", ["Dubai Marina Walk", "Marina Promenade"], "Dubai Marina", []),
    ("Al Sufouh Road", ["Al Sufouh St"], "Al Sufouh", []),
    ("The Walk", ["JBR Walk", "The Beach Walk"], "JBR", []),
    # Jumeirah belt
    ("Jumeirah Beach Road", ["Jumeirah Rd", "Jumeirah Road"], "Jumeirah", []),
    ("Al Wasl Road", ["Wasl Road", "Al Wasl Rd"], "Al Wasl", []),
    ("Al Safa Street", ["Safa Street"], "Al Barsha", []),
    # Deira / old Dubai
    ("Baniyas Road", ["Baniyas Sq", "Deira Baniyas Rd"], "Deira", []),
    ("Al Maktoum Road", ["Al Maktoum St"], "Deira", []),
    ("Oud Metha Road", ["Oud Metha St"], "Oud Metha", []),
    # Business Bay / Downtown
    ("Marasi Drive", ["Marasi Street"], "Business Bay", []),
    ("Al Asayel Street", ["Asayel St"], "Business Bay", []),
    ("Mohammed Bin Rashid Boulevard", ["MR Boulevard", "Downtown Boulevard"], "Downtown Dubai", []),
]

# Micro market hierarchy
DUBAI_ZONES = {
    "New Dubai": [
        "Dubai Marina", "JBR", "Bluewaters Island", "Palm Jumeirah", "JLT",
        "Al Furjan", "Dubai Production City", "Discovery Gardens", "Jebel Ali",
        "Jumeirah Islands", "Jumeirah Park",
    ],
    "Central Dubai": [
        "Downtown Dubai", "Business Bay", "DIFC", "Za'abeel", "City Walk",
        "Sheikh Zayed Road", "Al Jaddaf", "Dubai Creek Harbour",
    ],
    "Emirates Living": [
        "The Springs", "The Meadows", "The Lakes", "Emirates Hills",
        "The Greens", "The Views", "Dubai Hills Estate",
    ],
    "Jumeirah Belt": [
        "Jumeirah", "Umm Suqeim", "Al Sufouh", "Al Wasl", "Al Barsha", "Al Quoz",
    ],
    "Villages & Dubailand": [
        "Jumeirah Village Circle", "Jumeirah Village Triangle", "Motor City",
        "Sports City", "Studio City", "Arjan", "Remraam", "Mudon", "Town Square",
        "DAMAC Hills", "Dubailand", "Dubai Silicon Oasis", "Academic City",
        "Nad Al Sheba", "Meydan", "Al Barari",
    ],
    "Old Dubai": [
        "Deira", "Bur Dubai", "Al Karama", "Oud Metha", "Umm Hurair", "Al Qusais",
        "Al Nahda", "Al Rashidiya", "Al Garhoud", "Mirdif", "Al Warqaa",
        "International City", "Dubai Festival City", "Ras Al Khor", "Al Khawaneej",
        "Al Mizhar",
    ],
}

# Area → street mapping (which streets pass through which areas)
# This enriches the building-street mapping from area field
AREA_STREETS = {
    "dubai marina": ["Marina Walk"],
    "marina walk": ["Marina Walk"],
    "jbr": ["The Walk"],
    "jumeirah beach residence": ["The Walk"],
    "the walk": ["The Walk"],
    "palm jumeirah": ["Crescent Road", "Frond"],
    "al sufouh": ["Al Sufouh Road"],
    "al sufouh 1": ["Al Sufouh Road"],
    "al sufouh 2": ["Al Sufouh Road"],
    "jumeirah": ["Jumeirah Beach Road"],
    "jumeirah 1": ["Jumeirah Beach Road"],
    "jumeirah 2": ["Jumeirah Beach Road", "Al Wasl Road"],
    "jumeirah 3": ["Jumeirah Beach Road", "Al Wasl Road"],
    "al wasl": ["Al Wasl Road"],
    "umm suqeim": ["Jumeirah Beach Road", "Al Wasl Road"],
    "al barsha": ["Hessa Street", "Al Safa Street", "Sheikh Zayed Road"],
    "al barsha 1": ["Al Safa Street", "Sheikh Zayed Road"],
    "al barsha 2": ["Hessa Street"],
    "barsha south": ["Hessa Street", "Al Khail Road"],
    "al quoz": ["Al Khail Road"],
    "sheikh zayed road": ["Sheikh Zayed Road"],
    "szr": ["Sheikh Zayed Road"],
    "business bay": ["Marasi Drive", "Al Asayel Street", "Al Khail Road"],
    "downtown dubai": ["Mohammed Bin Rashid Boulevard"],
    "za'abeel": ["Sheikh Zayed Road", "Al Khail Road"],
    "deira": ["Baniyas Road", "Al Maktoum Road"],
    "port saeed": ["Al Maktoum Road", "Baniyas Road"],
    "oud metha": ["Oud Metha Road"],
    "bur dubai": ["Al Khail Road", "Al Fahidi Street"],
    "al karama": ["Oud Metha Road"],
    "al jaddaf": ["Al Khail Road"],
    "dubai creek harbour": ["Al Khail Road"],
    "ras al khor": ["Al Khail Road"],
    "dubai festival city": ["Al Khail Road"],
    "al garhoud": ["Al Khail Road"],
    "al qusais": ["Al Khail Road", "Amman Street"],
    "al nahda": ["Al Khail Road", "Amman Street"],
    "al rashidiya": ["Al Khail Road"],
    "mirdif": ["Al Khail Road"],
    "al warqaa": ["Al Khail Road"],
    "international city": ["Al Khail Road", "Manama Street"],
    "warsan": ["Al Khail Road"],
    "al khawaneej": ["Al Khawaneej Road"],
    "al mizhar": ["Al Khawaneej Road"],
    "meydan": ["Al Khail Road"],
    "nad al sheba": ["Al Khail Road", "Sheikh Zayed Road"],
    "dubai silicon oasis": ["Al Khail Road"],
    "academic city": ["Al Khail Road"],
    "dubailand": ["Sheikh Zayed Road", "Al Khail Road"],
    "arjan": ["Hessa Street", "Mohammed Bin Zayed Road"],
    "motor city": ["Hessa Street", "Mohammed Bin Zayed Road"],
    "sports city": ["Hessa Street", "Mohammed Bin Zayed Road"],
    "studio city": ["Hessa Street", "Al Khail Road"],
    "town square": ["Al Khail Road"],
    "damac hills": ["Mohammed Bin Zayed Road"],
    "remraam": ["Mohammed Bin Zayed Road", "Al Khail Road"],
    "mudon": ["Mohammed Bin Zayed Road"],
    "jvc": ["Al Khail Road", "Hessa Street"],
    "jvt": ["Al Khail Road", "Hessa Street"],
    "jlt": ["Sheikh Zayed Road", "Al Khail Road"],
    "dubai marina ": ["Marina Walk"],
    "discovery gardens": ["Sheikh Zayed Road", "Ibn Battuta Street"],
    "al furjan": ["Sheikh Zayed Road", "Mohammed Bin Zayed Road"],
    "impz": ["Al Khail Road"],
    "production city": ["Al Khail Road"],
    "jebel ali": ["Sheikh Zayed Road", "Mohammed Bin Zayed Road"],
    "emirates hills": ["Sheikh Zayed Road"],
    "dubai hills estate": ["Al Khail Road", "Umm Suqeim Street"],
    "the springs": ["Al Asayel Street"],
    "the meadows": ["Sheikh Zayed Road"],
    "the lakes": ["Sheikh Zayed Road"],
    "the greens": ["Sheikh Zayed Road"],
    "the views": ["Sheikh Zayed Road"],
}

# Known buildings with no street mapping → assign via micro market proximity
# Map: micro_market -> likely streets
MICRO_MARKET_STREETS = {
    "Dubai Marina": ["Marina Walk"],
    "JBR": ["The Walk"],
    "Bluewaters Island": ["Bluewaters Walk"],
    "Palm Jumeirah": ["Crescent Road", "Frond", "Palm Jumeirah Road"],
    "JLT": ["Sheikh Zayed Road", "Al Khail Road"],
    "Al Furjan": ["Mohammed Bin Zayed Road"],
    "Discovery Gardens": ["Ibn Battuta Street"],
    "Jebel Ali": ["Sheikh Zayed Road"],
    "Downtown Dubai": ["Mohammed Bin Rashid Boulevard"],
    "Business Bay": ["Marasi Drive", "Al Asayel Street", "Al Khail Road"],
    "DIFC": ["Financial Center Road"],
    "Za'abeel": ["Sheikh Zayed Road", "Al Khail Road"],
    "Sheikh Zayed Road": ["Sheikh Zayed Road"],
    "The Springs": ["Al Asayel Street"],
    "The Meadows": ["Sheikh Zayed Road"],
    "The Lakes": ["Sheikh Zayed Road"],
    "Emirates Hills": ["Sheikh Zayed Road"],
    "The Greens": ["Sheikh Zayed Road"],
    "The Views": ["Sheikh Zayed Road"],
    "Dubai Hills Estate": ["Al Khail Road", "Umm Suqeim Street"],
    "Jumeirah": ["Jumeirah Beach Road", "Al Wasl Road"],
    "Umm Suqeim": ["Jumeirah Beach Road", "Al Wasl Road"],
    "Al Sufouh": ["Al Sufouh Road"],
    "Al Wasl": ["Al Wasl Road"],
    "Jumeirah Village Circle": ["Al Khail Road", "Hessa Street"],
    "Jumeirah Village Triangle": ["Al Khail Road", "Hessa Street"],
    "Al Barsha": ["Hessa Street", "Al Safa Street", "Sheikh Zayed Road"],
    "Al Quoz": ["Al Khail Road"],
    "Motor City": ["Hessa Street", "Mohammed Bin Zayed Road"],
    "Sports City": ["Hessa Street", "Mohammed Bin Zayed Road"],
    "Studio City": ["Hessa Street", "Al Khail Road"],
    "Arjan": ["Hessa Street", "Mohammed Bin Zayed Road"],
    "Remraam": ["Mohammed Bin Zayed Road", "Al Khail Road"],
    "Mudon": ["Mohammed Bin Zayed Road"],
    "Town Square": ["Al Khail Road"],
    "DAMAC Hills": ["Mohammed Bin Zayed Road"],
    "Dubailand": ["Al Khail Road", "Sheikh Zayed Road"],
    "Dubai Silicon Oasis": ["Al Khail Road"],
    "Academic City": ["Al Khail Road"],
    "Mirdif": ["Al Khail Road"],
    "Al Warqaa": ["Al Khail Road"],
    "International City": ["Al Khail Road", "Manama Street"],
    "Nad Al Sheba": ["Al Khail Road", "Sheikh Zayed Road"],
    "Meydan": ["Al Khail Road"],
    "Al Barari": ["Sheikh Mohammed Bin Zayed Road"],
    "Al Khawaneej": ["Al Khawaneej Road"],
    "Al Mizhar": ["Al Khawaneej Road"],
    "Deira": ["Baniyas Road", "Al Maktoum Road"],
    "Bur Dubai": ["Al Fahidi Street", "Al Khail Road"],
    "Al Karama": ["Oud Metha Road"],
    "Oud Metha": ["Oud Metha Road"],
    "Umm Hurair": ["Oud Metha Road"],
    "Al Qusais": ["Al Khail Road", "Amman Street"],
    "Al Nahda": ["Al Khail Road", "Amman Street"],
    "Al Rashidiya": ["Al Khail Road"],
    "Al Garhoud": ["Al Khail Road", "Airport Road"],
    "Dubai Festival City": ["Al Khail Road"],
    "Al Jaddaf": ["Al Khail Road"],
    "Dubai Creek Harbour": ["Al Khail Road"],
    "Ras Al Khor": ["Al Khail Road"],
}



@dataclass
class Street:
    street_id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    micro_market: str = ""
    pincodes: list[str] = field(default_factory=list)
    lat_start: Optional[float] = None
    lng_start: Optional[float] = None
    lat_end: Optional[float] = None
    lng_end: Optional[float] = None
    building_ids: list[int] = field(default_factory=list)
    source: str = ""  # "nominatim", "geocode_area", "manual"

    def to_csv_row(self) -> dict:
        return {
            "street_id": self.street_id,
            "name": self.name,
            "aliases": ";".join(self.aliases),
            "micro_market": self.micro_market,
            "pincodes": ";".join(self.pincodes),
            "lat_start": self.lat_start or "",
            "lng_start": self.lng_start or "",
            "lat_end": self.lat_end or "",
            "lng_end": self.lng_end or "",
            "building_ids": ";".join(str(b) for b in self.building_ids),
            "source": self.source,
        }


STREET_CSV_FIELDS = [
    "street_id", "name", "aliases", "micro_market", "pincodes",
    "lat_start", "lng_start", "lat_end", "lng_end",
    "building_ids", "source",
]


def is_street_name(name: str) -> bool:
    """Check if a name looks like a street/road."""
    lower = name.lower().strip()
    parts = lower.split()
    if not parts:
        return False
    last = parts[-1].rstrip(".")
    # Direct street suffix check
    if last in STREET_SUFFIXES:
        return True
    # "Rd", "St" abbreviations
    if last in ("rd", "st"):
        return True
    return False


def normalize_street_name(name: str) -> str:
    """Normalize a street name for matching."""
    s = name.strip()
    # Remove common prefixes
    s = re.sub(r'^(near|opposite|behind|above|below)\s+', '', s, flags=re.IGNORECASE)
    # Normalize suffix
    s = re.sub(r'\s+Rd\.?$', ' Road', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+St\.?$', ' Street', s, flags=re.IGNORECASE)
    # Title case
    s = s.title()
    return s


def extract_streets_from_geocode() -> list[Street]:
    """Extract streets from the Nominatim geocode cache display_names."""
    cache_path = os.path.join(BASE_DIR, "data", "geocode_cache.json")
    if not os.path.exists(cache_path):
        return []

    with open(cache_path) as f:
        cache = json.load(f)

    seen = {}
    streets = []

    for area_name, data in cache.items():
        if data is None:
            continue
        parts = [p.strip() for p in data.get("display_name", "").split(",")]
        if len(parts) < 2:
            continue
        road = parts[1]
        if not is_street_name(road):
            continue

        pincode = data.get("pincode", "")
        canonical = normalize_street_name(road)

        if canonical not in seen:
            seen[canonical] = Street(
                street_id="",
                name=canonical,
                micro_market="",
                pincodes=[pincode] if pincode else [],
                source="nominatim",
            )
        else:
            if pincode and pincode not in seen[canonical].pincodes:
                seen[canonical].pincodes.append(pincode)

    for s in seen.values():
        s.street_id = _next_id(streets, seen)
        streets.append(s)

    return streets


def extract_streets_from_areas() -> list[Street]:
    """Extract streets from canonical_buildings.csv area field."""
    buildings_path = os.path.join(BASE_DIR, "data", "canonical_buildings.csv")
    if not os.path.exists(buildings_path):
        return []

    seen = {}
    building_areas = defaultdict(list)
    micro_markets = {}

    with open(buildings_path) as f:
        for row in csv.DictReader(f):
            area = row.get("area", "").strip()
            if is_street_name(area):
                canonical = normalize_street_name(area)
                bid = int(row["building_id"])
                building_areas[canonical].append(bid)
                mm = row.get("micro_market", "").strip()
                if mm:
                    micro_markets[canonical] = mm

    streets = []
    for name, bids in building_areas.items():
        s = Street(
            street_id="",
            name=name,
            aliases=[],
            micro_market=micro_markets.get(name, ""),
            building_ids=bids,
            source="geocode_area",
        )
        streets.append(s)

    return streets


def merge_streets(s1: list[Street], s2: list[Street]) -> list[Street]:
    """Merge two street lists, deduplicating by name."""
    by_name = {}

    for s in s1 + s2:
        if s.name not in by_name:
            by_name[s.name] = s
        else:
            existing = by_name[s.name]
            # Merge building_ids
            existing_bids = set(existing.building_ids)
            for bid in s.building_ids:
                if bid not in existing_bids:
                    existing.building_ids.append(bid)
            # Merge pincodes
            for p in s.pincodes:
                if p and p not in existing.pincodes:
                    existing.pincodes.append(p)
            # Merge aliases
            for a in s.aliases:
                if a and a not in existing.aliases:
                    existing.aliases.append(a)
            # Prefer non-empty micro_market
            if not existing.micro_market and s.micro_market:
                existing.micro_market = s.micro_market
            # Prefer manual source
            if s.source == "manual":
                existing.source = "manual"

    return list(by_name.values())


def assign_ids(streets: list[Street]) -> list[Street]:
    """Assign ST-XXX IDs sorted by name."""
    streets.sort(key=lambda s: s.name.lower())
    for i, s in enumerate(streets, 1):
        s.street_id = f"ST-{i:03d}"
    return streets


def write_streets(streets: list[Street]):
    """Write streets.csv and building_streets.csv."""
    os.makedirs(os.path.dirname(STREETS_PATH), exist_ok=True)

    # streets.csv
    with open(STREETS_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=STREET_CSV_FIELDS)
        writer.writeheader()
        for s in streets:
            writer.writerow(s.to_csv_row())

    # building_streets.csv: one row per building-street pair
    building_rows = []
    for s in streets:
        for bid in s.building_ids:
            building_rows.append({
                "building_id": bid,
                "street_id": s.street_id,
                "street_name": s.name,
            })

    with open(BUILDING_STREETS_PATH, "w", newline="") as f:
        if building_rows:
            writer = csv.DictWriter(f, fieldnames=["building_id", "street_id", "street_name"])
            writer.writeheader()
            writer.writerows(building_rows)

    print(f"  Wrote {len(streets)} streets to {STREETS_PATH}")
    print(f"  Wrote {len(building_rows)} building-street mappings to {BUILDING_STREETS_PATH}")


def build_known_streets() -> list[Street]:
    """Build Street objects from known Dubai streets list."""
    streets = []
    for name, aliases, micro_market, pincodes in KNOWN_DUBAI_STREETS:
        s = Street(
            street_id="",
            name=name,
            aliases=aliases,
            micro_market=micro_market,
            pincodes=pincodes,
            source="manual",
        )
        streets.append(s)
    return streets


def map_buildings_by_micro_market(streets: list[Street]) -> list[Street]:
    """Map buildings to streets by matching area field against street name/aliases
    and the AREA_STREETS lookup table.
    """
    buildings_path = os.path.join(BASE_DIR, "data", "canonical_buildings.csv")
    if not os.path.exists(buildings_path):
        return streets

    # Read all buildings
    buildings = []
    with open(buildings_path) as f:
        for row in csv.DictReader(f):
            bid = int(row["building_id"])
            mm = row.get("micro_market", "").strip()
            area = row.get("area", "").strip()
            buildings.append({"building_id": bid, "micro_market": mm, "area": area})

    # Build a lookup: set of already-mapped building IDs
    building_already_mapped = set()
    for s in streets:
        for bid in s.building_ids:
            building_already_mapped.add(bid)

    # Build a street lookup: for each street, all its names (canonical + aliases)
    street_names = {}  # lowercase name → street
    for s in streets:
        street_names[s.name.lower()] = s
        for alias in s.aliases:
            street_names[alias.lower().strip()] = s

    new_mappings = 0

    for b in buildings:
        bid = b["building_id"]
        if bid in building_already_mapped:
            continue
        area = b["area"].lower() if b["area"] else ""

        if not area:
            continue

        # Try 1: area IS a street name
        if area in street_names:
            s = street_names[area]
            s.building_ids.append(bid)
            new_mappings += 1
            continue

        # Try 2: area contains a street name
        for name, s in street_names.items():
            if name in area or area in name:
                if bid not in s.building_ids:
                    s.building_ids.append(bid)
                    new_mappings += 1
                break
        else:
            # Try 3: use AREA_STREETS lookup table
            if area in AREA_STREETS:
                for street_name in AREA_STREETS[area]:
                    sname = street_name.lower()
                    if sname in street_names:
                        s = street_names[sname]
                        if bid not in s.building_ids:
                            s.building_ids.append(bid)
                            new_mappings += 1
                        break  # Assign to the first matching street

    print(f"  New building-street mappings via area/street matching: {new_mappings}")
    return streets


def build_registry():
    """Build the full street registry from all available sources."""
    print("Building Street Registry...")

    streets_from_geocode = extract_streets_from_geocode()
    print(f"  From geocode cache: {len(streets_from_geocode)} streets")

    streets_from_areas = extract_streets_from_areas()
    print(f"  From building areas: {len(streets_from_areas)} streets")

    streets_known = build_known_streets()
    print(f"  From known Dubai streets: {len(streets_known)} streets")

    merged = merge_streets(
        merge_streets(streets_from_geocode, streets_from_areas),
        streets_known,
    )
    print(f"  After merge: {len(merged)} unique streets")

    mapped = map_buildings_by_micro_market(merged)
    print(f"  After micro market mapping: {sum(len(s.building_ids) for s in mapped)} building-street pairs")

    assigned = assign_ids(mapped)

    write_streets(assigned)

    print(f"\nDone. {len(assigned)} streets in registry.")
    return assigned


def load_registry() -> list[Street]:
    """Load streets from CSV."""
    if not os.path.exists(STREETS_PATH):
        return []

    streets = []
    with open(STREETS_PATH) as f:
        for row in csv.DictReader(f):
            s = Street(
                street_id=row["street_id"],
                name=row["name"],
                aliases=[a.strip() for a in row.get("aliases", "").split(";") if a.strip()],
                micro_market=row.get("micro_market", ""),
                pincodes=[p.strip() for p in row.get("pincodes", "").split(";") if p.strip()],
                lat_start=float(row["lat_start"]) if row.get("lat_start") else None,
                lng_start=float(row["lng_start"]) if row.get("lng_start") else None,
                lat_end=float(row["lat_end"]) if row.get("lat_end") else None,
                lng_end=float(row["lng_end"]) if row.get("lng_end") else None,
                building_ids=[int(b) for b in row.get("building_ids", "").split(";") if b.strip()],
                source=row.get("source", ""),
            )
            streets.append(s)

    return streets


def _next_id(streets, seen):
    """Generate a temporary placeholder ID."""
    return f"ST-TMP-{len(streets) + len(seen) + 1:03d}"


if __name__ == "__main__":
    build_registry()
