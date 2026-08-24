"""
Landmark Registry Builder.

Creates a canonical landmark registry and maps buildings to nearby landmarks.

Flow:
  1. Load canonical buildings with coordinates
  2. Build proximity index (buildings within 500m of each landmark)
  3. Assign LandmarkIDs (LM-001 format)
  4. Score landmarks by importance
  5. Write landmarks.csv + building_landmarks.csv

Landmark hierarchy:
  City → Zone → Micro Market → Landmark → Street → Building
"""
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

LANDMARKS_PATH = os.path.join(DATA_DIR, "landmarks.csv")
BUILDING_LANDMARKS_PATH = os.path.join(DATA_DIR, "building_landmarks.csv")


# ── Seed landmarks ───────────────────────────────────────────────
# Curated from broker vocabulary — these are the landmarks brokers
# actually reference in Dubai real estate.
SEED_LANDMARKS = [
    # ── Downtown / Central ───────────────────────────────────
    {"name": "Burj Khalifa", "aliases": ["Burj", "Tallest Tower"], "type": "Monument", "micro_market": "Downtown Dubai", "lat": 25.1972, "lng": 55.2744, "importance": 95},
    {"name": "The Dubai Mall", "aliases": ["Dubai Mall", "DM"], "type": "Mall", "micro_market": "Downtown Dubai", "lat": 25.1975, "lng": 55.2796, "importance": 92},
    {"name": "Dubai Fountain", "aliases": ["The Fountain"], "type": "Attraction", "micro_market": "Downtown Dubai", "lat": 25.1955, "lng": 55.2760, "importance": 85},
    {"name": "Dubai Opera", "aliases": ["Opera District"], "type": "Theatre", "micro_market": "Downtown Dubai", "lat": 25.1920, "lng": 55.2755, "importance": 80},
    {"name": "DIFC Gate Avenue", "aliases": ["DIFC", "Gate Avenue", "Dubai International Financial Centre"], "type": "Office", "micro_market": "DIFC", "lat": 25.2138, "lng": 55.2799, "importance": 86},
    {"name": "Emirates Towers", "aliases": ["Emirates Office Tower", "Jumeirah Emirates Towers"], "type": "Office Tower", "micro_market": "Sheikh Zayed Road", "lat": 25.2168, "lng": 55.2839, "importance": 78},
    {"name": "Museum of the Future", "aliases": ["MOTF"], "type": "Attraction", "micro_market": "Sheikh Zayed Road", "lat": 25.2197, "lng": 55.2823, "importance": 82},
    {"name": "Dubai World Trade Centre", "aliases": ["DWTC", "World Trade Centre"], "type": "Exhibition Centre", "micro_market": "Sheikh Zayed Road", "lat": 25.2276, "lng": 55.2900, "importance": 76},
    {"name": "Dubai Frame", "aliases": ["The Frame"], "type": "Monument", "micro_market": "Za'abeel", "lat": 25.2350, "lng": 55.3004, "importance": 72},
    {"name": "City Walk", "aliases": ["Citywalk"], "type": "Attraction", "micro_market": "City Walk", "lat": 25.2245, "lng": 55.2875, "importance": 74},
    {"name": "Safa Park", "aliases": ["Al Safa Park"], "type": "Park", "micro_market": "Al Wasl", "lat": 25.2090, "lng": 55.2560, "importance": 65},
    # ── Business Bay / Creek ─────────────────────────────────
    {"name": "Marasi Marina", "aliases": ["Marasi Drive", "Marasi Bay"], "type": "Promenade", "micro_market": "Business Bay", "lat": 25.1810, "lng": 55.2660, "importance": 70},
    {"name": "Bay Avenue Mall", "aliases": ["Bay Avenue"], "type": "Mall", "micro_market": "Business Bay", "lat": 25.1845, "lng": 55.2700, "importance": 66},
    {"name": "Dubai Creek Harbour", "aliases": ["Creek Harbour", "Creek Beach"], "type": "Waterfront", "micro_market": "Dubai Creek Harbour", "lat": 25.2020, "lng": 55.3420, "importance": 72},
    # ── Marina / New Dubai ───────────────────────────────────
    {"name": "Dubai Marina Walk", "aliases": ["Marina Walk", "Marina Promenade"], "type": "Promenade", "micro_market": "Dubai Marina", "lat": 25.0790, "lng": 55.1400, "importance": 90},
    {"name": "Dubai Marina Mall", "aliases": ["Marina Mall"], "type": "Mall", "micro_market": "Dubai Marina", "lat": 25.0774, "lng": 55.1359, "importance": 76},
    {"name": "JBR Beach", "aliases": ["The Beach JBR", "Jumeirah Beach Residence"], "type": "Beach", "micro_market": "JBR", "lat": 25.0777, "lng": 55.1290, "importance": 84},
    {"name": "Ain Dubai", "aliases": ["Dubai Eye", "Bluewaters Wheel"], "type": "Attraction", "micro_market": "Bluewaters Island", "lat": 25.0787, "lng": 55.1239, "importance": 74},
    {"name": "Atlantis The Palm", "aliases": ["Atlantis", "Atlantis Palm"], "type": "Hotel", "micro_market": "Palm Jumeirah", "lat": 25.1309, "lng": 55.1171, "importance": 86},
    {"name": "Nakheel Mall", "aliases": ["Palm Jumeirah Mall", "The View at The Palm"], "type": "Mall", "micro_market": "Palm Jumeirah", "lat": 25.1155, "lng": 55.1310, "importance": 74},
    {"name": "Palm West Beach", "aliases": ["West Beach"], "type": "Beach", "micro_market": "Palm Jumeirah", "lat": 25.1124, "lng": 55.1337, "importance": 68},
    {"name": "Ibn Battuta Mall", "aliases": ["Ibn Battuta"], "type": "Mall", "micro_market": "Jebel Ali", "lat": 25.0266, "lng": 55.1141, "importance": 72},
    {"name": "Expo City Dubai", "aliases": ["Expo 2020 Site", "Expo City"], "type": "Attraction", "micro_market": "Jebel Ali", "lat": 24.9880, "lng": 55.1600, "importance": 66},
    # ── Jumeirah belt ────────────────────────────────────────
    {"name": "Burj Al Arab", "aliases": ["Sail of Dubai"], "type": "Hotel", "micro_market": "Umm Suqeim", "lat": 25.1412, "lng": 55.1853, "importance": 85},
    {"name": "Kite Beach", "aliases": ["Kite Beach Umm Suqeim"], "type": "Beach", "micro_market": "Umm Suqeim", "lat": 25.1559, "lng": 55.2100, "importance": 70},
    {"name": "Madinat Jumeirah", "aliases": ["Souk Madinat", "Madinat"], "type": "Hotel", "micro_market": "Umm Suqeim", "lat": 25.1340, "lng": 55.1850, "importance": 72},
    {"name": "La Mer Beach", "aliases": ["La Mer"], "type": "Beach", "micro_market": "Jumeirah", "lat": 25.2580, "lng": 55.2890, "importance": 68},
    {"name": "Etihad Museum", "aliases": ["Union House"], "type": "Monument", "micro_market": "Jumeirah", "lat": 25.2500, "lng": 55.2840, "importance": 60},
    {"name": "Dubai Internet City", "aliases": ["DIC"], "type": "Office", "micro_market": "Al Sufouh", "lat": 25.0940, "lng": 55.1610, "importance": 72},
    {"name": "Knowledge Village", "aliases": ["KV", "Dubai Knowledge Park"], "type": "Institute", "micro_market": "Al Sufouh", "lat": 25.0975, "lng": 55.1615, "importance": 66},
    # ── Al Barsha / Dubailand corridor ───────────────────────
    {"name": "Mall of the Emirates", "aliases": ["MOE", "Mall Of Emirates"], "type": "Mall", "micro_market": "Al Barsha", "lat": 25.1181, "lng": 55.2007, "importance": 88},
    {"name": "Dubai Miracle Garden", "aliases": ["Miracle Garden"], "type": "Attraction", "micro_market": "Arjan", "lat": 25.0600, "lng": 55.2443, "importance": 70},
    {"name": "Global Village", "aliases": ["GV"], "type": "Attraction", "micro_market": "Dubailand", "lat": 25.0699, "lng": 55.3049, "importance": 72},
    {"name": "IMG Worlds of Adventure", "aliases": ["IMG Worlds", "IMG"], "type": "Theme Park", "micro_market": "Dubailand", "lat": 25.1010, "lng": 55.3830, "importance": 66},
    {"name": "Al Barari", "aliases": ["Al Barari Gardens"], "type": "Park", "micro_market": "Al Barari", "lat": 25.0850, "lng": 55.3290, "importance": 58},
    {"name": "Meydan Racecourse", "aliases": ["Meydan Grandstand", "Dubai World Cup"], "type": "Racecourse", "micro_market": "Meydan", "lat": 25.1990, "lng": 55.3310, "importance": 68},
    {"name": "Emirates Golf Club", "aliases": ["Emirates Hills Golf", "EGC"], "type": "Golf Club", "micro_market": "The Greens", "lat": 25.0850, "lng": 55.1680, "importance": 64},
    # ── Airports ─────────────────────────────────────────────
    {"name": "Dubai International Airport", "aliases": ["DXB", "Dubai Airport"], "type": "Airport", "micro_market": "Al Garhoud", "lat": 25.2532, "lng": 55.3657, "importance": 88},
    {"name": "Al Maktoum International Airport", "aliases": ["DWC", "Dubai World Central"], "type": "Airport", "micro_market": "Jebel Ali", "lat": 24.9027, "lng": 55.1637, "importance": 68},
    # ── Old Dubai ────────────────────────────────────────────
    {"name": "Deira City Centre", "aliases": ["DCC", "City Centre Deira"], "type": "Mall", "micro_market": "Deira", "lat": 25.2520, "lng": 55.3340, "importance": 74},
    {"name": "Gold Souk", "aliases": ["Deira Gold Souk", "Gold Souq"], "type": "Market", "micro_market": "Deira", "lat": 25.2680, "lng": 55.2970, "importance": 66},
    {"name": "Al Fahidi Historical District", "aliases": ["Al Fahidi", "Bastakiya"], "type": "Historical District", "micro_market": "Bur Dubai", "lat": 25.2637, "lng": 55.2972, "importance": 62},
    {"name": "Dubai Festival City Mall", "aliases": ["DFC Mall", "Festival City"], "type": "Mall", "micro_market": "Dubai Festival City", "lat": 25.2305, "lng": 55.3445, "importance": 72},
    {"name": "Dubai Creek Golf & Yacht Club", "aliases": ["Creek Golf Club"], "type": "Golf Club", "micro_market": "Al Garhoud", "lat": 25.2400, "lng": 55.3400, "importance": 60},
    {"name": "Mirdif City Centre", "aliases": ["MCC"], "type": "Mall", "micro_market": "Mirdif", "lat": 25.2280, "lng": 55.4060, "importance": 68},
    {"name": "Dragon Mart", "aliases": ["Dragon Mart 2"], "type": "Mall", "micro_market": "International City", "lat": 25.2040, "lng": 55.4170, "importance": 62},
]


def haversine(lat1, lng1, lat2, lng2):
    """Haversine distance in meters."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def load_buildings_with_coords() -> list[dict]:
    """Load canonical buildings that have lat/lng coordinates."""
    buildings = []
    path = os.path.join(DATA_DIR, "canonical_buildings.csv")
    with open(path) as f:
        for row in csv.DictReader(f):
            lat = row.get("latitude")
            lng = row.get("longitude")
            if lat and lng:
                try:
                    buildings.append({
                        "building_id": int(row["building_id"]),
                        "name": row["canonical_name"],
                        "lat": float(lat),
                        "lng": float(lng),
                        "micro_market": row.get("micro_market", ""),
                        "area": row.get("area", ""),
                    })
                except (ValueError, TypeError):
                    pass
    return buildings


def assign_landmark_ids(landmarks: list[dict]) -> list[dict]:
    """Assign LM-XXX IDs, preserving across rebuilds."""
    existing = {}
    path = os.path.join(DATA_DIR, "landmarks.csv")
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                existing[row["name"].strip().lower()] = row["landmark_id"]

    next_num = 1
    if existing:
        ids = [int(v.split("-")[1]) for v in existing.values() if v.startswith("LM-")]
        next_num = max(ids) + 1 if ids else 1

    for lm in landmarks:
        key = lm["name"].strip().lower()
        if key in existing:
            lm["landmark_id"] = existing[key]
        else:
            lm["landmark_id"] = f"LM-{next_num:03d}"
            next_num += 1
    return landmarks


def compute_proximity(landmarks: list[dict], buildings: list[dict], max_dist: int = 750):
    """For each landmark, find nearby buildings within max_dist meters."""
    links = []
    for lm in landmarks:
        llat, llng = lm["lat"], lm["lng"]
        for b in buildings:
            dist = haversine(llat, llng, b["lat"], b["lng"])
            if dist <= max_dist:
                walking_min = round(dist / 80)  # ~80m/min walking
                links.append({
                    "building_id": b["building_id"],
                    "landmark_id": lm["landmark_id"],
                    "distance_m": round(dist),
                    "walking_min": max(1, walking_min),
                    "building_name": b["name"],
                    "landmark_name": lm["name"],
                })
    return links


def compute_importance(landmarks: list[dict], links: list[dict]):
    """Compute importance score based on nearby building density."""
    building_count = defaultdict(int)
    for link in links:
        building_count[link["landmark_id"]] += 1

    for lm in landmarks:
        lid = lm["landmark_id"]
        nearby = building_count.get(lid, 0)
        # Blend seed importance with observed density
        seed = lm.get("importance", 50)
        density_score = min(nearby * 2, 50)
        lm["importance"] = min(seed + density_score, 100)


def write_landmarks(landmarks: list[dict]):
    fields = [
        "landmark_id", "name", "aliases", "type", "micro_market",
        "latitude", "longitude", "importance", "source",
    ]
    with open(LANDMARKS_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for lm in sorted(landmarks, key=lambda x: -x["importance"]):
            w.writerow({
                "landmark_id": lm["landmark_id"],
                "name": lm["name"],
                "aliases": "; ".join(lm.get("aliases", [])),
                "type": lm["type"],
                "micro_market": lm["micro_market"],
                "latitude": lm["lat"],
                "longitude": lm["lng"],
                "importance": lm["importance"],
                "source": "seed",
            })
    print(f"  Wrote {len(landmarks)} landmarks to {LANDMARKS_PATH}")


def write_building_landmarks(links: list[dict]):
    fields = [
        "building_id", "landmark_id", "distance_m", "walking_min",
        "building_name", "landmark_name",
    ]
    with open(BUILDING_LANDMARKS_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for link in sorted(links, key=lambda x: x["distance_m"]):
            w.writerow(link)
    print(f"  Wrote {len(links)} building→landmark links to {BUILDING_LANDMARKS_PATH}")


def print_summary(landmarks: list[dict], links: list[dict], buildings: list[dict]):
    """Print a summary of the landmark registry."""
    total_buildings_with_coords = len(buildings)
    linked_buildings = len(set(l["building_id"] for l in links))
    types = defaultdict(int)
    for lm in landmarks:
        types[lm["type"]] += 1

    print()
    print("=" * 60)
    print("  LANDMARK REGISTRY SUMMARY")
    print("=" * 60)
    print(f"  Landmarks:                   {len(landmarks)}")
    print(f"  Landmark types:              {len(types)}")
    print(f"  Buildings with coordinates:  {total_buildings_with_coords}")
    print(f"  Buildings near ≥1 landmark:  {linked_buildings} ({linked_buildings/max(total_buildings_with_coords,1)*100:.1f}%)")
    print(f"  Building→landmark links:     {len(links)}")
    print(f"  Avg links per building:      {len(links)/max(linked_buildings,1):.1f}")
    print()
    print("  Landmark types:")
    for t, c in sorted(types.items(), key=lambda x: -x[1]):
        print(f"    {t:<25} {c}")
    print()
    print("  Top 10 landmarks by importance:")
    for lm in sorted(landmarks, key=lambda x: -x["importance"])[:10]:
        nearby = sum(1 for l in links if l["landmark_id"] == lm["landmark_id"])
        print(f"    {lm['landmark_id']}  {lm['name']:<35} importance={lm['importance']:>3}  nearby={nearby}")


def run():
    print("Building Landmark Registry...")

    seeds = SEED_LANDMARKS
    print(f"  Loaded {len(seeds)} seed landmarks")

    landmarks = assign_landmark_ids(seeds)
    print(f"  Assigned IDs: {[lm['landmark_id'] for lm in landmarks[:5]]}...")

    buildings = load_buildings_with_coords()
    print(f"  Loaded {len(buildings)} buildings with coordinates")

    links = compute_proximity(landmarks, buildings, max_dist=1000)
    print(f"  Computed {len(links)} proximity links (max 1000m)")

    compute_importance(landmarks, links)
    print(f"  Computed importance scores")

    write_landmarks(landmarks)
    write_building_landmarks(links)
    print_summary(landmarks, links, buildings)


if __name__ == "__main__":
    run()
