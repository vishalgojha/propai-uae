"""
Geocode location names using the free Nominatim (OpenStreetMap) API.

Rate-limited to 1 request/second as per Nominatim usage policy.
"""
import csv
import json
import os
import time
from urllib.parse import quote
import urllib.request
import urllib.error

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "geocode_cache.json")


def _load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def geocode_location(loc_name: str) -> dict | None:
    """
    Geocode a location name via Nominatim.
    
    Returns dict with lat, lon, display_name, pincode or None.
    
    Rate limited: max 1 request per second.
    """
    if not loc_name or loc_name in ("—", "-", ""):
        return None
    
    # Pad with Dubai context
    query = f"{loc_name}, Dubai, United Arab Emirates"
    url = f"https://nominatim.openstreetmap.org/search?q={quote(query)}&format=json&limit=1&addressdetails=1"
    
    headers = {
        "User-Agent": "PropAI/1.0 (building-registry)",
        "Accept": "application/json",
    }
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    
    if not data:
        # Try without Dubai
        url = f"https://nominatim.openstreetmap.org/search?q={quote(loc_name)}&format=json&limit=1&addressdetails=1"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            return None
        if not data:
            return None
    
    result = data[0]
    lat = float(result.get("lat", 0))
    lon = float(result.get("lon", 0))
    display_name = result.get("display_name", "")
    
    # Extract pincode from address details
    addr = result.get("address", {})
    pincode = addr.get("postcode", "")
    
    return {
        "lat": lat,
        "lon": lon,
        "display_name": display_name,
        "pincode": pincode,
    }


def geocode_locations(locations: list[str]) -> dict[str, dict]:
    """
    Geocode a list of unique location names.
    Uses cache to avoid re-fetching.
    
    Returns dict mapping loc_name → {lat, lon, display_name, pincode}
    """
    cache = _load_cache()
    results = {}
    remaining = []
    
    for loc in locations:
        if loc in cache:
            results[loc] = cache[loc]
        else:
            remaining.append(loc)
    
    if remaining:
        print(f"Geocoding {len(remaining)} locations (rate-limited to 1/sec)...")
        for i, loc in enumerate(remaining):
            print(f"  [{i+1}/{len(remaining)}] {loc}", end="\r")
            result = geocode_location(loc)
            if result:
                cache[loc] = result
                results[loc] = result
            else:
                cache[loc] = None
                results[loc] = None
            _save_cache(cache)
            if i < len(remaining) - 1:
                time.sleep(1.1)  # Nominatim: max 1 req/sec
        print()
    
    return results
