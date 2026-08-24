"""
BuildingID Resolver — multi-path resolution engine.

Resolution strategies (in order):
  1. Exact match on canonical_name
  2. Alias match
  3. Normalized match
  4. Landmark match (name → landmark → nearby buildings)
  5. Broker vocabulary parse + landmark match
  6. Street match (name → street → buildings)
  7. Project name match (name → project → building_ids)
  8. RERA number match (rera → project → building_ids)
  9. Developer-narrowed fuzzy match
 10. Full fuzzy match (name similarity + area)

Output: always (building_id, confidence, method).
If all paths fail: (0, 0.0, "unresolved").
"""
import csv
import os
import re
from difflib import SequenceMatcher
from typing import Optional
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Resolver data lives alongside propai-lab, not inside it
_ALT = os.path.join(os.path.dirname(BASE_DIR), "propai")
if os.path.isdir(os.path.join(_ALT, "data")):
    BASE_DIR = _ALT
CACHE = {}

# Landmark proximity radius for resolution (metres)
LANDMARK_RADIUS = 1000


# ── Registry Loaders ─────────────────────────────────────────────

def _load_registry():
    if CACHE.get("loaded"):
        return
    buildings = {}
    aliases = defaultdict(list)

    path = os.path.join(BASE_DIR, "data", "canonical_buildings.csv")
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                bid = int(row["building_id"])
                buildings[row["canonical_name"].strip().lower()] = {
                    "building_id": bid,
                    "canonical_name": row["canonical_name"].strip(),
                    "area": row.get("area", "").strip().lower(),
                    "developer": row.get("developer", "").strip().lower(),
                    "fingerprint": row.get("fingerprint", ""),
                }

    path = os.path.join(BASE_DIR, "data", "building_aliases.csv")
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                aliases[row["alias"].strip().lower()].append(int(row["building_id"]))

    CACHE["buildings"] = buildings
    CACHE["aliases"] = aliases
    CACHE["loaded"] = True


def _load_streets():
    if CACHE.get("streets_loaded"):
        return
    street_by_name = {}
    path = os.path.join(BASE_DIR, "data", "streets.csv")
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                s = {
                    "street_id": row["street_id"],
                    "name": row["name"],
                    "building_ids": [int(b) for b in row.get("building_ids", "").split(";") if b.strip()],
                }
                street_by_name[s["name"].lower()] = s

    CACHE["street_by_name"] = street_by_name
    CACHE["streets_loaded"] = True


def _load_projects():
    """Load projects.csv as a canonical entity. Maps project_name → project + building_ids."""
    if CACHE.get("projects_loaded"):
        return
    projects_by_name = {}
    projects_by_rera = {}
    path = os.path.join(BASE_DIR, "data", "projects.csv")
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                name_lower = row["project_name"].strip().lower()
                rera = row["rera_no"].strip()
                bids = [int(b) for b in row.get("building_ids", "").split(";") if b.strip()]
                project = {
                    "project_id": int(row["project_id"]),
                    "project_name": row["project_name"],
                    "developer_name": row.get("developer_name", ""),
                    "building_ids": bids,
                    "district": row.get("district", ""),
                    "area": row.get("area", ""),
                }
                projects_by_name[name_lower] = project
                if rera:
                    projects_by_rera[rera] = project

    CACHE["projects_by_name"] = projects_by_name
    CACHE["projects_by_rera"] = projects_by_rera
    CACHE["projects_loaded"] = True


def _load_developer_registry():
    if CACHE.get("dev_loaded"):
        return
    dev_buildings = defaultdict(set)
    path = os.path.join(BASE_DIR, "data", "developer_registry.csv")
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                dev_name = row["developer_name"].strip().lower()
                bids = row.get("building_ids", "").strip()
                for b in bids.split(";"):
                    b = b.strip()
                    if b:
                        dev_buildings[dev_name].add(int(b))
    CACHE["dev_buildings"] = dev_buildings
    CACHE["dev_loaded"] = True


def _load_landmarks():
    """Load landmark registry and building→landmark proximity map."""
    if CACHE.get("lm_loaded"):
        return
    landmarks_by_name = {}
    landmarks_by_alias = {}
    landmarks_list = []

    path = os.path.join(BASE_DIR, "data", "landmarks.csv")
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                lm = {
                    "landmark_id": row["landmark_id"],
                    "name": row["name"],
                    "type": row.get("type", ""),
                    "micro_market": row.get("micro_market", ""),
                    "latitude": float(row.get("latitude", 0) or 0),
                    "longitude": float(row.get("longitude", 0) or 0),
                    "importance": int(row.get("importance", 0) or 0),
                }
                key = row["name"].strip().lower()
                landmarks_by_name[key] = lm
                landmarks_list.append(lm)
                aliases_raw = row.get("aliases", "").strip()
                if aliases_raw:
                    for a in aliases_raw.split(";"):
                        a = a.strip().lower()
                        if a:
                            landmarks_by_alias[a] = lm

    # Load building→landmark proximity links
    bldg_to_lms = defaultdict(list)
    lm_to_bldgs = defaultdict(list)
    path = os.path.join(BASE_DIR, "data", "building_landmarks.csv")
    if os.path.exists(path):
        with open(path) as f:
            for row in csv.DictReader(f):
                bid = int(row["building_id"])
                lid = row["landmark_id"]
                dist = int(row.get("distance_m", 0))
                link = {
                    "building_id": bid,
                    "distance_m": dist,
                    "walking_min": int(row.get("walking_min", 0)),
                }
                bldg_to_lms[bid].append(link)
                lm_to_bldgs[lid].append(link)

    CACHE["landmarks_by_name"] = landmarks_by_name
    CACHE["landmarks_by_alias"] = landmarks_by_alias
    CACHE["landmarks_list"] = landmarks_list
    CACHE["bldg_to_lms"] = bldg_to_lms
    CACHE["lm_to_bldgs"] = lm_to_bldgs
    CACHE["lm_loaded"] = True


def _normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r'[^a-z0-9\s]', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def resolve_by_landmark(landmark_query: str) -> tuple[Optional[int], float, str]:
    """
    Resolve a landmark name to the nearest known BuildingID.
    Matches by exact name, alias, or fuzzy match on landmark name.
    Returns (building_id, confidence, method) or (None, 0.0, "lm_not_found").
    """
    _load_landmarks()
    landmarks_by_name = CACHE.get("landmarks_by_name", {})
    landmarks_by_alias = CACHE.get("landmarks_by_alias", {})
    lm_to_bldgs = CACHE.get("lm_to_bldgs", {})
    landmarks_list = CACHE.get("landmarks_list", [])

    query = landmark_query.strip().lower()

    # Exact landmark name match
    if query in landmarks_by_name:
        lid = landmarks_by_name[query]["landmark_id"]
        neighbors = lm_to_bldgs.get(lid, [])
        if neighbors:
            best = min(neighbors, key=lambda x: x["distance_m"])
            return (best["building_id"], 0.88, f"lm:{lid}")
        return (None, 0.0, "lm_no_buildings")

    # Alias match
    if query in landmarks_by_alias:
        lid = landmarks_by_alias[query]["landmark_id"]
        neighbors = lm_to_bldgs.get(lid, [])
        if neighbors:
            best = min(neighbors, key=lambda x: x["distance_m"])
            return (best["building_id"], 0.85, f"lm_alias:{lid}")
        return (None, 0.0, "lm_no_buildings")

    # Fuzzy match on landmark name
    norm_query = _normalize(query)
    best_lm = None
    best_ratio = 0.0
    for lm in landmarks_list:
        norm_lm = _normalize(lm["name"])
        ratio = SequenceMatcher(None, norm_query, norm_lm).ratio()
        if ratio > best_ratio and ratio >= 0.70:
            best_ratio = ratio
            best_lm = lm

    if best_lm:
        lid = best_lm["landmark_id"]
        neighbors = lm_to_bldgs.get(lid, [])
        if neighbors:
            best = min(neighbors, key=lambda x: x["distance_m"])
            return (best["building_id"], round(best_ratio * 0.90, 2), f"lm_fuzzy:{lid}")

    return (None, 0.0, "lm_not_found")


# ── Multi-path Resolution ────────────────────────────────────────

def resolve(name: str, area: str = "", developer: str = "") -> tuple[int, float, str]:
    """
    Resolve a building/project name to a BuildingID.

    Tries multiple paths. Returns first match.
    """
    _load_registry()
    buildings = CACHE["buildings"]
    aliases = CACHE["aliases"]
    name_lower = name.strip().lower()
    area_lower = area.strip().lower()
    dev_lower = developer.strip().lower()

    # 1. Exact match on canonical name
    if name_lower in buildings:
        return (buildings[name_lower]["building_id"], 1.0, "exact")

    # 2. Alias match
    if name_lower in aliases:
        return (aliases[name_lower][0], 0.98, "alias")

    # 3. Normalized match
    try:
        from knowledge.normalization import canonicalize
        normed = canonicalize(name, area, developer).strip().lower()
        if normed in buildings:
            return (buildings[normed]["building_id"], 0.95, "normalized")
        if normed in aliases:
            return (aliases[normed][0], 0.93, "normalized_alias")
    except Exception:
        pass

    # 4+5. Broker vocabulary parse → landmark match
    from evidence.parsers import parse
    parsed = parse(name_lower)
    lm_query = parsed["main_query"] if parsed["confidence"] >= 0.60 else name_lower

    # Try exact/alias match on raw name first (e.g. "Emirates Towers" is a landmark)
    _load_landmarks()
    lm_names = CACHE.get("landmarks_by_name", {})
    lm_aliases = CACHE.get("landmarks_by_alias", {})
    if name_lower in lm_names or name_lower in lm_aliases:
        bid, conf, method = resolve_by_landmark(name_lower)
        if bid is not None:
            return (bid, conf, method)

    # Then try parsed main_query (stripped of relation/suffix) with full matching
    bid, conf, method = resolve_by_landmark(lm_query)
    if bid is not None:
        tag = f"lm_broker:{method}" if parsed.get("relation") else method
        relation_bonus = 0.05 if parsed.get("relation") else 0
        return (bid, min(conf + relation_bonus, 0.95), tag)

    # 6. Street match
    _load_streets()
    street_by_name = CACHE.get("street_by_name", {})
    street_query = parsed["main_query"] if parsed["confidence"] >= 0.60 else name_lower

    if street_query in street_by_name:
        s = street_by_name[street_query]
        if s["building_ids"]:
            return (s["building_ids"][0], 0.85, f"street:{s['street_id']}")

    for sname, s in street_by_name.items():
        if street_query in sname or sname in street_query:
            if s["building_ids"]:
                return (s["building_ids"][0], 0.75, f"street_partial:{s['street_id']}")

    # 7. Project name match
    _load_projects()
    projects_by_name = CACHE.get("projects_by_name", {})
    if name_lower in projects_by_name:
        proj = projects_by_name[name_lower]
        if proj["building_ids"]:
            return (proj["building_ids"][0], 0.90, f"project:{proj['project_id']}")

    # Try normalized project name
    norm_name = _normalize(name)
    for pname, proj in projects_by_name.items():
        norm_pname = _normalize(pname)
        if norm_pname == norm_name and proj["building_ids"]:
            return (proj["building_ids"][0], 0.85, f"project_norm:{proj['project_id']}")

    # 9. Developer-narrowed fuzzy match
    if dev_lower:
        _load_developer_registry()
        dev_buildings = CACHE.get("dev_buildings", {})
        known_bids = dev_buildings.get(dev_lower, set())
        if known_bids:
            candidates = []
            norm_name = _normalize(name)
            for canon_name, info in buildings.items():
                if info["building_id"] not in known_bids:
                    continue
                norm_canon = _normalize(canon_name)
                ratio = SequenceMatcher(None, norm_name, norm_canon).ratio()
                area_match = area_lower and info["area"] and area_lower == info["area"]
                score = ratio + (0.3 if area_match else 0)
                if score >= 0.8:
                    candidates.append((score, info["building_id"]))
            if candidates:
                candidates.sort(reverse=True)
                return (candidates[0][1], min(round(candidates[0][0], 2), 1.0), "dev_fuzzy")

    # 10. Full fuzzy match
    candidates = []
    norm_name = _normalize(name)
    for canon_name, info in buildings.items():
        norm_canon = _normalize(canon_name)
        ratio = SequenceMatcher(None, norm_name, norm_canon).ratio()
        area_match = area_lower and info["area"] and area_lower == info["area"]
        dev_match = dev_lower and info["developer"] and dev_lower == info["developer"]
        score = ratio + (0.3 if area_match else 0) + (0.2 if dev_match else 0)
        if score >= 0.8:
            candidates.append((score, info["building_id"]))
    if candidates:
        candidates.sort(reverse=True)
        return (candidates[0][1], min(round(candidates[0][0], 2), 1.0), "fuzzy")

    return (0, 0.0, "unresolved")


def resolve_batch(observations: list[dict]) -> list[dict]:
    """Resolve BuildingIDs for a batch of observations."""
    for obs in observations:
        bid, confidence, method = resolve(
            obs.get("building_name", ""),
            obs.get("area", ""),
            obs.get("developer", ""),
        )
        obs["building_id"] = bid
        obs["resolution_confidence"] = confidence
        obs["resolution_method"] = method
    return observations


# ── Standalone Lookup Functions ──────────────────────────────────

def resolve_by_street(street_query: str) -> list[int]:
    """Resolve a street name to all BuildingIDs on that street."""
    _load_streets()
    street_by_name = CACHE.get("street_by_name", {})
    query = street_query.lower().strip()
    for prefix in ["near ", "opposite ", "behind ", "on ", "at "]:
        if query.startswith(prefix):
            query = query[len(prefix):].strip()
            break
    matched = []
    if query in street_by_name:
        matched.append(street_by_name[query])
    else:
        for sname, s in street_by_name.items():
            if query in sname or sname in query:
                matched.append(s)

    all_bids = []
    seen = set()
    for s in matched:
        for bid in s.get("building_ids", []):
            if bid not in seen:
                all_bids.append(bid)
                seen.add(bid)
    return all_bids


def resolve_by_rera(rera_no: str) -> tuple[int, float, str]:
    """
    Resolve RERA number → project_id → first building_id.

    Returns (building_id, 0.95, "rera:PROJECT_ID") or (0, 0.0, "rera_not_found").
    """
    _load_projects()
    projects_by_rera = CACHE.get("projects_by_rera", {})
    key = rera_no.strip()
    if key in projects_by_rera:
        proj = projects_by_rera[key]
        if proj["building_ids"]:
            return (proj["building_ids"][0], 0.95, f"rera:{proj['project_id']}")
        return (0, 0.80, f"rera_no_buildings:{proj['project_id']}")
    return (0, 0.0, "rera_not_found")


def resolve_by_project_name(name: str) -> Optional[dict]:
    """
    Resolve project name → project info (including building_ids).

    Returns None if no match.
    """
    _load_projects()
    projects_by_name = CACHE.get("projects_by_name", {})
    key = name.strip().lower()
    if key in projects_by_name:
        return projects_by_name[key]
    norm_key = _normalize(name)
    for pname, proj in projects_by_name.items():
        if _normalize(pname) == norm_key:
            return proj
    return None


def resolve_by_developer(developer: str) -> list[int]:
    """Resolve developer name to all known BuildingIDs."""
    _load_developer_registry()
    dev_buildings = CACHE.get("dev_buildings", {})
    key = developer.strip().lower()
    if key in dev_buildings:
        return list(dev_buildings[key])
    return []


def clear_cache():
    CACHE.clear()
