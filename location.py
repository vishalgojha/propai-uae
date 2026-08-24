"""
Structured location parser for PropAI.

Tokenizes free-text location strings into typed, resolved geographic signals
using the evidence engine's known landmarks, micro markets, and spatial relations.

Input:  "in JBR near Ain Dubai, 500m from metro"
Output: {
    "micro_market": "JBR",
    "locality": "JBR",
    "landmark": "Ain Dubai",
    "spatial_relation": "near",
    "distance_m": 500,
    "transit_landmark": "metro",
    "city": "Dubai",
}
"""

from __future__ import annotations

import logging
import math
import re
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)

# ── Spatial relation patterns (ordered longest-first for greedy match) ──

_SPATIAL_RELATIONS = [
    ("walking distance from", "walking distance"),
    ("walking distance to", "walking distance"),
    ("walking distance", "walking distance"),
    ("around the corner from", "near"),
    ("around the corner", "near"),
    ("close to", "near"),
    ("adjacent to", "adjacent"),
    ("next to", "next to"),
    ("opposite", "opposite"),
    ("facing", "facing"),
    ("behind", "behind"),
    ("beside", "beside"),
    ("across", "across"),
    ("nearby", "near"),
    ("near", "near"),
    ("off", "off"),
    ("above", "above"),
    ("below", "below"),
]

# ── Distance pattern ──

_DISTANCE_RE = re.compile(
    r'(\d+[\d,]*\.?\d*)\s*(km|kms|kilometer|kilometre|kilometers|kilometres|'
    r'm|mtr|metre|meter|metres|meters|min|mins|minutes)\b',
    re.IGNORECASE,
)

# ── Transit landmark keywords ──

_TRANSIT_KEYWORDS = frozenset({
    "station", "railway station", "metro", "metro station", "bus stop",
    "bus stand", "airport", "railway", "rail",
})

# ── Common city names ──

_CITIES = frozenset({
    "dubai", "abu dhabi", "sharjah", "ajman", "ras al khaimah",
    "fujairah", "umm al quwain", "uae", "united arab emirates",
})

# ── Building indicator keywords ──

_BUILDING_INDICATORS = frozenset({
    "tower", "towers", "building", "residency", "heights", "park",
    "enclave", "garden", "villa", "villas", "palace", "court",
    "house", "apartment", "complex", "plaza", "chambers",
    "corporation", "building", "heritage",
})

# ── Stop words to skip in location text ──

_STOP = frozenset({
    "a", "an", "the", "in", "at", "on", "for", "with", "and", "&",
    "of", "to", "is", "are", "this", "that", "it", "its", "has",
    "have", "been", "being", "from", "by",
})

_GENERIC_LOCATION_PHRASES = frozenset({
    "rent", "rental", "on rent", "for rent", "lease", "on lease",
    "sale", "sell", "for sale", "available", "available on rent",
    "available for rent", "available on sale", "available for sale",
    "direct inventory", "inventory", "urgent", "urgently",
})

# ── Property-related noise patterns — never valid locations ──

_FURNISHING_TERMS = frozenset({
    "semi furnished", "semi-furnished", "semi fur", "sf", "fully furnished",
    "fully-furnished", "fully fur", "ff", "unfurnished", "un furn", "uf",
    "partly furnished", "partly furn", "part furn", "furnished",
    "furnish", "furnishing", "newly furnished",
})

_FEATURE_NOISE = frozenset({
    "terrace", "balcony", "washroom", "washrooms", "powder room",
    "carpet", "carpet area", "carpet-", "parking", "car parking",
    "middle floor", "higher floor", "lower floor", "top floor",
    "ground floor", "upper floor", "high floor", "low floor",
    "premium", "luxury", "brand new", "prestigious", "exclusive",
    "sole mandate", "done up", "newly done", "renovated",
    "newly painted", "good location", "prime location",
})

_PROPERTY_TYPE_NOISE = frozenset({
    "apartment", "flat", "villa", "bungalow", "office", "shop",
    "showroom", "space", "property", "unit", "residence",
    "penthouse", "duplex", "studio", "room", "rooms",
})

_LISTING_HEADER_NOISE = frozenset({
    "hot listing", "hot lease", "exclusive listing", "urgent listing",
    "special listing", "direct listing", "fresh listing",
    "residential flat", "commercial space", "open house",
    "location", "area", "sector", "only", "require", "requirement",
})

_COMMON_DUBAI_LOCALITIES = frozenset({
    "dubai marina", "marina", "marina walk",
    "jbr", "jumeirah beach residence", "the walk",
    "bluewaters", "bluewaters island",
    "palm jumeirah", "frond", "crescent road",
    "jlt", "jumeirah lakes towers", "cluster",
    "al furjan", "furjan",
    "impz", "dubai production city", "production city",
    "discovery gardens",
    "jebel ali", "jafza",
    "jumeirah islands", "jumeirah park",
    "downtown dubai", "downtown", "opera district", "burj khalifa area",
    "business bay", "bbay",
    "difc", "dubai international financial centre",
    "za'abeel", "zabeel", "za'abeel 1", "za'abeel 2",
    "city walk",
    "sheikh zayed road", "szr", "trade centre", "world trade centre",
    "springs", "the springs",
    "meadows", "the meadows",
    "lakes", "the lakes",
    "arabian ranches", "ranches",
    "emirates hills", "montgomerie", "the montgomerie",
    "greens", "the greens",
    "views", "the views",
    "dubai hills estate", "dubai hills", "hills estate",
    "jumeirah", "jumeirah 1", "jumeirah 2", "jumeirah 3",
    "umm suqeim", "umm suqeim 1", "umm suqeim 2", "umm suqeim 3",
    "um suqeim",
    "al sufouh", "al sufouh 1", "al sufouh 2", "sufouh",
    "madina jumeirah living", "mjl", "madinat jumeirah living",
    "al wasl",
    "jvc", "jumeirah village circle",
    "jvt", "jumeirah village triangle",
    "al barsha", "barsha", "al barsha 1", "al barsha 2", "al barsha 3",
    "barsha south", "barsha heights", "tecom",
    "al quoz", "quoz", "al quoz 1", "al quoz 2", "al quoz 3", "al quoz 4",
    "motor city", "sports city", "dubai sports city",
    "studio city", "dubai studio city",
    "arjan", "remraam", "mudon",
    "town square", "nshama town square", "nshama",
    "damac hills", "akoya", "akoya oxygen",
    "dubailand", "majan", "liwan", "the villa", "living legends", "reem", "mira",
    "dubai silicon oasis", "silicon oasis", "dso",
    "academic city", "dubai international academic city",
    "mirdif",
    "al warqaa", "warqaa", "al warqa 1", "al warqa 2", "al warqa 3", "al warqa 4",
    "international city", "warsan",
    "nad al sheba", "nad al sheba 1", "nad al sheba 2", "nad al sheba 3",
    "meydan", "meydan city", "meydan gated community",
    "al barari",
    "al khawaneej", "khawaneej", "al khawaneej 1", "al khawaneej 2",
    "al mizhar", "mizhar", "al mizhar 1", "al mizhar 2",
    "deira", "naif", "port saeed",
    "bur dubai", "al fahidi", "al raffa",
    "karama", "al karama",
    "oud metha",
    "umm hurair", "umm hurair 1", "umm hurair 2",
    "al qusais", "qusais", "al qusais 1", "al qusais 2", "al qusais 3",
    "al nahda", "nahda", "al nahda 1", "al nahda 2",
    "al rashidiya", "rashidiya",
    "garhoud", "al garhoud",
    "festival city", "dubai festival city",
    "al jaddaf", "jadaf", "culture village",
    "dubai creek harbour", "creek harbour",
    "ras al khor",
})

_NON_MARKET_LOCATION_NAMES = frozenset({
    "marina walk", "the walk", "crescent road", "cluster",
})

# Colloquial / abbreviated -> canonical micro_market. Applied as substring
# substitutions inside parse_location so the deterministic resolver maps
# common WhatsApp shorthand without an LLM call. Conservative, high-confidence.
_LOCATION_ALIASES: list[tuple[str, str]] = [
    # Common WhatsApp shorthand for Dubai areas. Keep the raw mention in
    # evidence, but use the corrected locality for search and enrichment.
    ("jumeriah village circle", "jumeirah village circle"),
    ("jumeriah", "jumeirah"),
    ("marina walk", "dubai marina"),
    ("jumeirah beach residence", "jbr"),
    ("businessbay", "business bay"),
    ("buisness bay", "business bay"),
    ("bbay", "business bay"),
    ("burj khalifa area", "downtown dubai"),
    ("opera district", "downtown dubai"),
    ("sheikh zayed road", "szr"),
    ("silicon oasis", "dubai silicon oasis"),
    ("creek harbour", "dubai creek harbour"),
    ("culture village", "al jaddaf"),
    ("akoya oxygen", "damac hills"),
    ("nshama town square", "town square"),
    ("nshama", "town square"),
    ("impz", "dubai production city"),
    ("production city", "dubai production city"),
    ("barsha heights", "al barsha"),
    ("tecom", "al barsha"),
    ("madinat jumeirah living", "madina jumeirah living"),
    ("um suqeim", "umm suqeim"),
    ("khawaneej", "al khawaneej"),
    ("warsan", "international city"),
]


# ── Token match result ──

class LocationToken:
    """A single token extracted from location text."""
    def __init__(self, text: str, kind: str, value: str | None = None,
                 score: float = 1.0, meta: dict | None = None):
        self.text = text
        self.kind = kind          # micro_market, locality, landmark, building,
                                  # spatial_relation, distance, transit_landmark,
                                  # city, street, unknown
        self.value = value or text
        self.score = score
        self.meta = meta or {}

    def __repr__(self) -> str:
        return f"Token({self.text!r}, {self.kind}, val={self.value!r})"


# ── Structured location output ──

class StructuredLocation:
    """Resolved, structured location from a message."""
    def __init__(self):
        self.city: Optional[str] = None
        self.micro_market: Optional[str] = None
        self.locality: Optional[str] = None
        self.building: Optional[str] = None
        self.landmark: Optional[str] = None
        self.transit_landmark: Optional[str] = None
        self.street: Optional[str] = None
        self.spatial_relation: Optional[str] = None
        self.distance_m: Optional[float] = None
        self.distance_text: Optional[str] = None
        self.tokens: list[dict] = []
        self.raw: str = ""

    def to_dict(self) -> dict:
        return {
            "city": self.city,
            "micro_market": self.micro_market,
            "locality": self.locality,
            "building": self.building,
            "landmark": self.landmark,
            "transit_landmark": self.transit_landmark,
            "street": self.street,
            "spatial_relation": self.spatial_relation,
            "distance_m": self.distance_m,
            "distance_text": self.distance_text,
            "tokens": self.tokens,
            "raw": self.raw,
        }

    @staticmethod
    def from_dict(d: dict) -> "StructuredLocation":
        loc = StructuredLocation()
        loc.city = d.get("city")
        loc.micro_market = d.get("micro_market")
        loc.locality = d.get("locality")
        loc.building = d.get("building")
        loc.landmark = d.get("landmark")
        loc.transit_landmark = d.get("transit_landmark")
        loc.street = d.get("street")
        loc.spatial_relation = d.get("spatial_relation")
        loc.distance_m = d.get("distance_m")
        loc.distance_text = d.get("distance_text")
        loc.tokens = d.get("tokens", [])
        loc.raw = d.get("raw", "")
        return loc


# ═══════════════════════════════════════════════════════════════════
# Evidence engine integration
# ═══════════════════════════════════════════════════════════════════

_evidence_loaded = False
_landmarks_by_name: dict[str, dict] = {}
_landmarks_by_alias: dict[str, dict] = {}
_landmarks_list: list[dict] = []
_buildings: dict[str, dict] = {}
_buildings_by_alias: dict[str, dict] = {}
_micro_markets: set[str] = set()
_localities: set[str] = set()


def _load_evidence():
    global _evidence_loaded, _landmarks_by_name, _landmarks_by_alias
    global _landmarks_list, _buildings, _buildings_by_alias
    global _micro_markets, _localities
    if _evidence_loaded:
        return
    try:
        from evidence.resolver import CACHE, _load_registry, _load_landmarks
        _load_registry()
        _load_landmarks()
        _landmarks_by_name = CACHE.get("landmarks_by_name", {})
        _landmarks_by_alias = CACHE.get("landmarks_by_alias", {})
        _landmarks_list = CACHE.get("landmarks_list", [])
        _buildings = CACHE.get("buildings", {})
        _buildings_by_alias = CACHE.get("buildings_by_alias", {})
        # Extract micro markets from landmarks
        for lm in _landmarks_list:
            mm = lm.get("micro_market")
            if mm:
                _micro_markets.add(mm.lower())
        # Extract locality names from micro markets
        for mm in _micro_markets:
            for part in mm.split():
                p = part.strip(" -")
                if len(p) > 2:
                    _localities.add(p.lower())
        # Also add micro markets themselves as potential localities
        _localities.update(_micro_markets)
        _localities.update(_COMMON_DUBAI_LOCALITIES)
        _evidence_loaded = True
    except Exception as exc:
        _localities.update(_COMMON_DUBAI_LOCALITIES)
        _evidence_loaded = True
        logger.warning("Location evidence registry could not be loaded: %s", exc)


# Acronym localities that must keep their uppercase display form.
_ACRONYM_LOCALITIES: dict[str, str] = {
    "jvc": "JVC",
    "jvt": "JVT",
    "jbr": "JBR",
    "jlt": "JLT",
    "difc": "DIFC",
    "dso": "DSO",
    "szr": "SZR",
}


def _canonical_micro_market(value: str | None) -> str | None:
    """Return a stable display name for a known Dubai micro-market."""
    normalized = re.sub(r"\s+", " ", (value or "").strip().lower())
    if (
        normalized not in _COMMON_DUBAI_LOCALITIES
        or normalized in _NON_MARKET_LOCATION_NAMES
    ):
        return None
    if normalized in _ACRONYM_LOCALITIES:
        return _ACRONYM_LOCALITIES[normalized]
    if normalized == "za'abeel":
        return "Za'abeel"
    return normalized.title()


# ── Canonical locality slug (mirrors apps/www/src/lib/locality-canon.ts) ──

_HIDDEN_BUCKETS = frozenset({
    "new dubai prime", "central dubai core", "dubai suburbs",
})

_GENERIC_PARENTS = frozenset({
    "jumeirah", "barsha", "qusais", "sufouh", "suqeim", "warqa", "khawaneej",
})

_IMPLIED_DIRECTION: dict[str, str] = {
    "marina": "Dubai Marina",
    "jbr": "JBR",
    "jvc": "JVC",
    "jvt": "JVT",
    "jlt": "JLT",
}

_REDIRECTS: dict[str, str] = {
    "opera district": "Downtown Dubai",
    "burj khalifa area": "Downtown Dubai",
    "old town": "Downtown Dubai",
    "downtown": "Downtown Dubai",
    "impz": "Dubai Production City",
    "production city": "Dubai Production City",
    "akoya": "DAMAC Hills",
    "akoya oxygen": "DAMAC Hills",
    "nshama": "Town Square",
    "nshama town square": "Town Square",
    "town square dubai": "Town Square",
    "culture village": "Al Jaddaf",
    "jadaf": "Al Jaddaf",
    "creek gate": "Dubai Creek Harbour",
    "creek harbour": "Dubai Creek Harbour",
    "silicon oasis": "Dubai Silicon Oasis",
    "dso": "DSO",
    "festival city": "Dubai Festival City",
    "garhoud": "Al Garhoud",
    "karama": "Al Karama",
    "warsan": "International City",
    "madinat jumeirah living": "Madina Jumeirah Living",
    "mjl": "Madina Jumeirah Living",
    "tecom": "Al Barsha",
    "barsha heights": "Al Barsha",
    "barsha south": "Al Barsha",
    "quoz": "Al Quoz",
    "rashidiya": "Al Rashidiya",
    "naif": "Deira",
    "port saeed": "Deira",
    "al fahidi": "Bur Dubai",
    "mira": "Dubailand",
    "reem": "Dubailand",
    "majan": "Dubailand",
    "liwan": "Dubailand",
    "the villa": "Dubailand",
    "living legends": "Dubailand",
    "jafza": "Jebel Ali",
    "montgomerie": "Emirates Hills",
    "the montgomerie": "Emirates Hills",
}

_STANDALONE_LOCALITIES: dict[str, str] = {
    "bluewaters island": "Bluewaters Island",
    "bluewaters": "Bluewaters",
    "bur dubai": "Bur Dubai",
    "city walk": "City Walk",
    "deira": "Deira",
    "discovery gardens": "Discovery Gardens",
    "downtown dubai": "Downtown Dubai",
    "dubai festival city": "Dubai Festival City",
    "dubai hills estate": "Dubai Hills Estate",
    "dubai hills": "Dubai Hills Estate",
    "dubai marina": "Dubai Marina",
    "dubai production city": "Dubai Production City",
    "dubai silicon oasis": "Dubai Silicon Oasis",
    "al barari": "Al Barari",
    "al barsha": "Al Barsha",
    "al furjan": "Al Furjan",
    "al garhoud": "Al Garhoud",
    "al jaddaf": "Al Jaddaf",
    "al karama": "Al Karama",
    "al khawaneej": "Al Khawaneej",
    "al mizhar": "Al Mizhar",
    "al nahda": "Al Nahda",
    "al quoz": "Al Quoz",
    "al rashidiya": "Al Rashidiya",
    "al sufouh": "Al Sufouh",
    "al warqaa": "Al Warqaa",
    "business bay": "Business Bay",
    "damac hills": "DAMAC Hills",
    "difc": "DIFC",
    "dubailand": "Dubailand",
    "emirates hills": "Emirates Hills",
    "international city": "International City",
    "jbr": "JBR",
    "jebel ali": "Jebel Ali",
    "jumeirah": "Jumeirah",
    "jumeirah islands": "Jumeirah Islands",
    "jumeirah lakes towers": "JLT",
    "jumeirah park": "Jumeirah Park",
    "jvc": "JVC",
    "jvt": "JVT",
    "meydan": "Meydan",
    "mirdif": "Mirdif",
    "motor city": "Motor City",
    "nad al sheba": "Nad Al Sheba",
    "oud metha": "Oud Metha",
    "palm jumeirah": "Palm Jumeirah",
    "ras al khor": "Ras Al Khor",
    "remraam": "Remraam",
    "sheikh zayed road": "Sheikh Zayed Road",
    "sports city": "Sports City",
    "studio city": "Studio City",
    "the greens": "The Greens",
    "the lakes": "The Lakes",
    "the meadows": "The Meadows",
    "the springs": "The Springs",
    "the views": "The Views",
    "arabian ranches": "Arabian Ranches",
    "town square": "Town Square",
    "umm suqeim": "Umm Suqeim",
    "arjan": "Arjan",
    "mudon": "Mudon",
}


def _slugify(value: str) -> str:
    """Mirror apps/www/src/lib/supabase.ts slugify()."""
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", value.strip().lower()))


def canonical_micro_market_slug(raw: str | None) -> str | None:
    """Return the canonical URL slug for a raw micro_market value.

    Returns None for hidden buckets, unknown values, or empty input —
    matching the TypeScript canonicalLocality() behaviour in locality-canon.ts.
    """
    if not raw:
        return None
    norm = re.sub(r"\s+", " ", raw.strip().lower())
    if not norm:
        return None
    if norm in _HIDDEN_BUCKETS:
        return None
    if norm in _REDIRECTS:
        return _slugify(_REDIRECTS[norm])
    if norm in _IMPLIED_DIRECTION:
        return _slugify(_IMPLIED_DIRECTION[norm])
    if norm in _GENERIC_PARENTS:
        return _slugify(raw.strip())
    label = _STANDALONE_LOCALITIES.get(norm)
    if label:
        return _slugify(label)
    return None


def infer_unique_micro_market(text: str | None) -> str | None:
    """Resolve a market only when the text names one unambiguous locality."""
    normalized = f" {(text or '').lower()} "
    for pattern, replacement in _LOCATION_ALIASES:
        normalized = normalized.replace(pattern, replacement)

    matches: set[str] = set()
    for locality in sorted(_COMMON_DUBAI_LOCALITIES, key=len, reverse=True):
        if re.search(rf"(?<!\w){re.escape(locality)}(?!\w)", normalized):
            canonical = _canonical_micro_market(locality)
            if canonical:
                matches.add(canonical)

    # Avoid treating a broad parent as a second market (Bandra + Bandra West).
    specific = {
        match for match in matches
        if not any(other != match and other.lower().startswith(match.lower() + " ") for other in matches)
    }
    return next(iter(specific)) if len(specific) == 1 else None


def enrich_parsed_location(
    parsed: dict,
    source_text: str | None,
    fallback_text: str | None = None,
) -> dict:
    """Fill missing structured location fields without overwriting parser output."""
    result = dict(parsed)
    primary_text = (
        result.get("location_raw")
        or result.get("building_name")
        or source_text
        or ""
    )
    loc = parse_location(primary_text)

    if not result.get("location_raw") and loc.raw:
        result["location_raw"] = loc.raw
    if not result.get("location") and loc.raw:
        result["location"] = loc.to_dict()
    if not result.get("building_name") and loc.building:
        result["building_name"] = loc.building
    if not result.get("landmark_name") and loc.landmark:
        result["landmark_name"] = loc.landmark
    if not result.get("street_name") and loc.street:
        result["street_name"] = loc.street

    market = loc.micro_market or _canonical_micro_market(loc.locality)
    if not market:
        market = infer_unique_micro_market(source_text)
    if not market and fallback_text:
        market = infer_unique_micro_market(fallback_text)
    if not result.get("micro_market") and market:
        result["micro_market"] = market

    return result


# ═══════════════════════════════════════════════════════════════════
# Location extraction from raw message text
# ═══════════════════════════════════════════════════════════════════

def extract_location_text(raw_text: str) -> str | None:
    """
    Extract the location substring from a raw message.
    Captures everything from the first location trigger keyword until
    price/contact/broker/noise appears.
    Keeps distance phrases (e.g. "500m from station") which contain commas
    but aren't price boundaries.
    """
    text = raw_text.strip()
    lower = text.lower()
    _load_evidence()

    def _is_noise_only(text: str) -> bool:
        """Check if text consists entirely of property noise terms."""
        lower = re.sub(r'[*_`~\U0001F000-\U0001FFFF\s\U0000FE0F]', '', text).strip(" \t,.-:+#").lower()
        lower = re.sub(r'\s+', ' ', lower).strip()
        if not lower or len(lower) < 3:
            return True
        for noise_set in (_FURNISHING_TERMS, _FEATURE_NOISE, _PROPERTY_TYPE_NOISE,
                          _LISTING_HEADER_NOISE, _GENERIC_LOCATION_PHRASES):
            if lower in noise_set or lower.replace("-", " ") in noise_set:
                return True
        # Check if text is a leftover fragment (starts with digit, "no.", "+")
        if re.match(r'^[\d+#]\s*', lower):
            return True
        # Check pure emoji / punctuation
        stripped = re.sub(r'[\w]', '', lower).strip()
        if stripped and not re.search(r'[a-z]', lower):
            return True
        return False

    def usable_candidate(candidate: str | None) -> bool:
        if not candidate:
            return False
        normalized = re.sub(r'[*_`~]', '', candidate).strip(" \t,.-:").lower()
        normalized = re.sub(r'\s+', ' ', normalized)
        if len(normalized) < 3 or normalized in _GENERIC_LOCATION_PHRASES:
            return False
        if re.fullmatch(r'(?:on|for)?\s*(?:rent|sale|lease|sell|buy|requirement)s?', normalized):
            return False
        if re.fullmatch(r'(?:rent|sale|lease|available|direct|inventory|urgent|urgently)[\s\W]*', normalized):
            return False
        if _is_noise_only(normalized):
            return False
        return True

    def clean_candidate(candidate: str) -> str:
        rest = re.sub(r'[*_`~]', '', candidate).strip(" \t:-")
        # Numbered WhatsApp cards commonly begin with a post index and an
        # intent label, e.g. ``4. Buy: 3 BHK | Bandra West``.  The index and
        # label are message structure, never a building or locality.
        rest = re.sub(r'^\s*\d+\s*[.)\]:-]\s*', '', rest)
        rest = re.sub(r'^(?:buy|sell|rent|lease|wanted|requirement)\s*[:\-]\s*', '', rest, flags=re.IGNORECASE)
        boundaries = []
        boundary_patterns = [
            r'\n',
            r'\bcontact\b',
            r'\bcall\b',
            r'\bwhatsapp\b',
            r'\bprice\b',
            r'\bstarting\b',
            r'\bstarts?\b',
            r'\bmax\b',
            r'\baed\b',
            r'\bdhs\b',
            r'\bdirhams?\b',
            r'\bcheques?\b',
            r'\bchqs?\b',
            r'/-',
            r'\bbroker\b',
        ]
        for pat in boundary_patterns:
            m = re.search(pat, rest, re.IGNORECASE)
            if m:
                boundaries.append(m.start())
        # Keep comma-separated distance phrases, but stop before the next clause.
        for ci in [m.start() for m in re.finditer(',', rest)]:
            before = rest[:ci].strip()
            if re.search(r'\d+\s*(?:km|kms|m|mtr|min)\s*$', before, re.IGNORECASE):
                continue
            boundaries.append(ci)

        if boundaries:
            rest = rest[:min(boundaries)].strip()
        rest = re.sub(
            r'\s+\d[\d,.]*(?:\s*(?:mil|million|thousand|aed|dhs)\b|\s*[mk]\b|\s*cheques?\b|\s*chqs?\b|\s*/-).*',
            '', rest, count=1, flags=re.IGNORECASE
        ).strip(" \t,.-")
        for noise in ["distance from ", "distance to ",
                      "walking distance from ", "walking distance to "]:
            if rest.lower().startswith(noise):
                rest = rest[len(noise):].strip()
        rest = re.sub(
            r'^(?:available|direct inventory|inventory|urgent|urgently|hot deals?|hot offers?|best deals?|deals?|offers?|new launch|sale|sell|rent|rental|'
            r'on\s+rent|for\s+rent|on\s+lease|for\s+lease|on\s+sale|for\s+sale|'
            r'apt|flat|office|space|property)\b\s*',
            '',
            rest,
            flags=re.IGNORECASE,
        ).strip(" \t,.-!:")
        rest = re.sub(r'^(?:in|at|near)\s+', '', rest, flags=re.IGNORECASE).strip()
        # Strip furnishing/feature noise from both ends
        for _ in range(3):
            rest_lower = rest.lower()
            for noise_set in (_FURNISHING_TERMS, _FEATURE_NOISE, _PROPERTY_TYPE_NOISE,
                              _LISTING_HEADER_NOISE):
                for noise in sorted(noise_set, key=len, reverse=True):
                    if rest_lower.startswith(noise):
                        rest = rest[len(noise):].strip(" \t,.-")
                        break
                    if rest_lower.endswith(noise):
                        rest = rest[:-len(noise)].strip(" \t,.-")
                        break
            rest_lower = rest.lower()
            # Also strip known location noise prefixes like "location-", "area-", "location:", "area:"
            rest = re.sub(r'^(?:location|area)\s*[-:]\s*', '', rest, flags=re.IGNORECASE).strip().lstrip(" \t,.-:")
        # Strip standalone numbers at end (like "741" from "Carpet- 741")
        rest = re.sub(r'\s*[\d.,]+\s*$', '', rest).strip()
        # If nothing remains, return None so caller treats as missing
        if len(rest) < 2 or _is_noise_only(rest):
            return None
        return rest

    def known_location_candidate() -> str | None:
        candidates = sorted(
            {x for x in (_localities | _micro_markets) if len(x) >= 4},
            key=len,
            reverse=True,
        )
        for name in candidates:
            m = re.search(rf'\b{re.escape(name)}\b', lower)
            if not m:
                continue
            start = max(0, m.start() - 45)
            end = min(len(text), m.end() + 90)
            prefix = text[start:m.start()]
            cut = max(prefix.rfind("\n"), prefix.rfind(","), prefix.rfind("*"))
            if cut >= 0:
                start = start + cut + 1
            suffix = text[m.end():end]
            next_breaks = [idx for idx in (suffix.find("\n"), suffix.find(",")) if idx >= 0]
            if next_breaks:
                end = m.end() + min(next_breaks)
            candidate = clean_candidate(text[start:end])
            if not candidate:
                continue
            candidate = re.sub(
                r'^(?:available|direct inventory|inventory|urgent|urgently|sale|sell|rent|rental|on rent|for rent|on sale|for sale|apt|flat|office|space)\b\s*',
                '',
                candidate,
                flags=re.IGNORECASE,
            ).strip(" \t,.-:")
            if usable_candidate(candidate):
                return candidate
        return None

    # Requirement shorthand should prefer the desired area after BHK over
    # secondary landmarks like "near Metro".
    if re.search(r'\b(?:need|require|requirement|tenant|client|wanted|looking\s+for)\b', lower):
        bhk_loc = re.search(
            r'\b(?:need|require|want|wanted|looking\s+for|client\s+requirement|tenant\s+need)?\s*'
            r'(?:\d+(?:\.\d+)?\s*(?:bhk|br|rk|bedroom)|studio)\s+(.+)$',
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if bhk_loc:
            rest = clean_candidate(bhk_loc.group(1))
            if (
                usable_candidate(rest)
                and not re.match(r'^(?:starting|start(?:s)?|price|budget|from|max)\b', rest, re.IGNORECASE)
            ):
                return rest

    known = known_location_candidate()
    if known:
        return known

    loc_keywords = [
        r'at', r'in', r'near', r'opposite', r'opp\.?', r'behind', r'off',
        r'walkable', r'walking', r'walk', r'location', r'area',
        r'distance\s+from', r'distance\s+to',
    ]
    for kw in loc_keywords:
        m = re.search(rf'(?<![A-Za-z]){kw}\s+', lower)
        if m:
            rest = clean_candidate(text[m.end():])
            if usable_candidate(rest):
                return rest

    # Common requirement shorthand: "Need 2 BR JVC Budget 1.5M".
    bhk_loc = re.search(
        r'\b(?:need|require|want|wanted|looking\s+for|client\s+requirement)?\s*'
        r'(?:\d+(?:\.\d+)?\s*(?:bhk|br|rk|bedroom)|studio)\s+(.+)$',
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if bhk_loc:
        rest = clean_candidate(bhk_loc.group(1))
        if (
            usable_candidate(rest)
            and not re.match(r'^(?:starting|start(?:s)?|price|budget|from|max)\b', rest, re.IGNORECASE)
        ):
            return rest

    # Launch/listing cards often put the project and market on their own line.
    for line in [l.strip() for l in text.splitlines() if l.strip()]:
        if re.search(r'\b(?:bhk|\d\s?br\b|budget|contact|call)\b|\baed\b|\bdhs\b|(?:\+?971\d{8,9})|\d{9}', line, re.IGNORECASE):
            continue
        if re.search(r'\b(?:launch|booking|owner|sale|rent|require(?:ment)?|forwarded)\b', line, re.IGNORECASE):
            continue
        # Skip lines that are purely furnishing/feature/property noise
        line_clean = re.sub(r'[*_`~\U0001F000-\U0001FFFF\U0000FE0F]', '', line).strip().lower()
        if _is_noise_only(line_clean):
            continue
        if usable_candidate(line):
            return clean_candidate(line)
    return None


# ═══════════════════════════════════════════════════════════════════
# Tokenizer
# ═══════════════════════════════════════════════════════════════════

def _tokenize(text: str) -> list[str]:
    """Split location text into segments."""
    segments = []
    for part in re.split(r'\s*,\s*|\s+and\s+|\s*&\s*', text):
        part = part.strip()
        if part and len(part) >= 2:
            segments.append(part)
    return segments


def _match_spatial_relation(text: str) -> tuple[str | None, str | None]:
    """Match a spatial relation at the start of text. Returns (matched_text, relation)."""
    lower = text.lower().strip()
    for pattern, relation in _SPATIAL_RELATIONS:
        if lower.startswith(pattern):
            return pattern, relation
    return None, None


def _match_distance(text: str) -> tuple[float | None, str | None, str | None]:
    """Match distance at the start of text. Returns (meters, distance_text, remaining)."""
    m = _DISTANCE_RE.match(text.strip())
    if m:
        val = float(m.group(1).replace(",", ""))
        unit = m.group(2).lower()
        if unit in ("km", "kms", "kilometer", "kilometre", "kilometers", "kilometres"):
            meters = val * 1000
        else:
            meters = val
        return meters, m.group(0), text[m.end():].strip()
    return None, None, None


def _fuzzy_match(item: str, candidates: dict[str, any],
                 threshold: float = 0.80) -> tuple[str | None, float]:
    """Fuzzy match item against candidate keys."""
    item_lower = item.lower().strip()
    best_key = None
    best_ratio = 0.0
    for key in candidates:
        m = SequenceMatcher(None, item_lower, key.lower().strip()).ratio()
        if m > best_ratio:
            best_ratio = m
            best_key = key
    if best_key and best_ratio >= threshold:
        return best_key, best_ratio
    return None, 0.0


def _match_known_entity(text: str) -> tuple[LocationToken | None, str]:
    """
    Try to match the longest known entity at the start of text.
    Returns (token, remaining_text).
    Priority: micro markets > landmarks > buildings > localities > cities.
    """
    lower = text.lower().strip()

    # 1. Micro market match (highest priority — avoid partial building matches)
    mms_sorted = sorted(_micro_markets, key=len, reverse=True)
    for mm in mms_sorted:
        if lower.startswith(mm):
            rest = lower[len(mm):].strip()
            return (
                LocationToken(
                    text=mm,
                    kind="micro_market",
                    value=mm.title(),
                    meta={"micro_market": mm.title()},
                ),
                rest,
            )

    # 2. Landmark match (by name or alias)
    for name in sorted(_landmarks_by_name, key=len, reverse=True):
        if lower.startswith(name):
            info = _landmarks_by_name[name]
            rest = lower[len(name):].strip()
            return (
                LocationToken(
                    text=name,
                    kind="landmark",
                    value=info.get("name", name),
                    meta={
                        "landmark_id": info.get("landmark_id"),
                        "micro_market": info.get("micro_market"),
                        "zone": info.get("zone"),
                    },
                ),
                rest,
            )
    for alias in sorted(_landmarks_by_alias, key=len, reverse=True):
        if lower.startswith(alias):
            info = _landmarks_by_alias[alias]
            rest = lower[len(alias):].strip()
            return (
                LocationToken(
                    text=alias,
                    kind="landmark",
                    value=info.get("name", alias),
                    meta={
                        "landmark_id": info.get("landmark_id"),
                        "micro_market": info.get("micro_market"),
                        "zone": info.get("zone"),
                    },
                ),
                rest,
            )

    # 3. Building match (skip single-word short names to avoid false positives)
    for name in sorted(_buildings, key=len, reverse=True):
        if len(name.split()) < 2 and len(name) <= 4:
            continue
        if lower.startswith(name):
            info = _buildings[name]
            rest = lower[len(name):].strip()
            return (
                LocationToken(
                    text=name,
                    kind="building",
                    value=info.get("canonical_name", name),
                    meta={
                        "building_id": info.get("building_id"),
                        "area": info.get("area"),
                        "developer": info.get("developer"),
                    },
                ),
                rest,
            )

    # 4. Locality match
    loc_sorted = sorted(_localities, key=len, reverse=True)
    for loc in loc_sorted:
        lower_loc = loc.lower().strip()
        if lower.startswith(lower_loc) and lower_loc not in _STOP and len(lower_loc) > 2:
            rest = lower[len(lower_loc):].strip()
            return (
                LocationToken(
                    text=loc,
                    kind="locality",
                    value=loc.title(),
                ),
                rest,
            )

    # 5. City match
    for city in _CITIES:
        if lower.startswith(city):
            rest = lower[len(city):].strip()
            return (
                LocationToken(
                    text=city,
                    kind="city",
                    value=city.title(),
                ),
                rest,
            )

    return None, text


# ═══════════════════════════════════════════════════════════════════
# Main parser
# ═══════════════════════════════════════════════════════════════════

def parse_location(raw_text: str) -> StructuredLocation:
    """
    Parse a raw message text and return a structured location object.
    """
    _load_evidence()
    loc = StructuredLocation()

    # Apply colloquial/abbreviated -> canonical substitutions BEFORE extraction
    # so localities embedded in the message prelude (e.g. "Yari Road ...",
    # "Naupada Thane") are preserved by extract_location_text.
    normalized = (raw_text or "").lower()
    for pattern, replacement in _LOCATION_ALIASES:
        normalized = normalized.replace(pattern, replacement)

    loc.raw = (extract_location_text(normalized) or "").title()
    if not loc.raw:
        return loc

    segments = _tokenize(loc.raw)
    tokens: list[LocationToken] = []

    for segment in segments:
        remaining = segment
        seg_tokens: list[LocationToken] = []

        # Iteratively consume tokens from remaining text
        while remaining:
            remaining = remaining.strip()
            if not remaining or len(remaining) <= 2:
                break

            # Skip stop words
            skip = re.match(r'^(in|at|on|the|a|an|for|to|of)\s+', remaining, re.IGNORECASE)
            if skip:
                remaining = remaining[skip.end():].strip()
                continue

            # 1. Distance at start
            dist_m, dist_text, dist_rest = _match_distance(remaining)
            if dist_m is not None:
                seg_tokens.append(LocationToken(
                    text=dist_text,
                    kind="distance",
                    value=f"{dist_m:.0f}m",
                    meta={"distance_m": dist_m},
                ))
                loc.distance_m = dist_m
                loc.distance_text = dist_text
                remaining = dist_rest
                continue

            # 2. Spatial relation at start
            rel_text, relation = _match_spatial_relation(remaining)
            if rel_text:
                seg_tokens.append(LocationToken(
                    text=rel_text,
                    kind="spatial_relation",
                    value=relation,
                ))
                loc.spatial_relation = relation
                remaining = remaining[len(rel_text):].strip()
                continue

            # 3. "from [entity]" pattern
            from_m = re.match(r'from\s+(.+)', remaining, re.IGNORECASE)
            if from_m:
                after_from = from_m.group(1).strip()
                # Check if transit keyword
                tl_lower = after_from.lower().rstrip(".,")
                if tl_lower in _TRANSIT_KEYWORDS or \
                   any(kw in tl_lower for kw in _TRANSIT_KEYWORDS):
                    seg_tokens.append(LocationToken(
                        text=after_from,
                        kind="transit_landmark",
                        value=after_from,
                    ))
                    loc.transit_landmark = after_from
                    remaining = ""
                    continue
                else:
                    match_token, rest = _match_known_entity(after_from)
                    if match_token:
                        seg_tokens.append(match_token)
                        remaining = rest
                        continue
                    else:
                        remaining = after_from
                        continue

            # 4. Known entity match
            match_token, remaining_after = _match_known_entity(remaining)
            if match_token:
                seg_tokens.append(match_token)
                remaining = remaining_after
                continue

            # 5. Unknown — extract first word as locality, continue
            words = remaining.split()
            found = False
            for i in range(1, min(len(words) + 1, 4)):
                candidate = " ".join(words[:i]).strip(".,")
                match_token, remaining_after = _match_known_entity(" ".join(words[i:]))
                if match_token:
                    if len(candidate) > 2:
                        seg_tokens.append(LocationToken(
                            text=candidate,
                            kind="locality",
                            value=candidate.title(),
                        ))
                    seg_tokens.append(match_token)
                    remaining = remaining_after
                    found = True
                    break
            if found:
                continue

            # Nothing matched — take first word(s) as locality
            first_word = words[0].strip(".,")
            if len(first_word) > 2 and first_word.lower() not in _STOP:
                seg_tokens.append(LocationToken(
                    text=first_word,
                    kind="locality",
                    value=first_word.title(),
                ))
            remaining = " ".join(words[1:])

        # Filter noise
        seg_tokens = [t for t in seg_tokens
                      if t.text.lower().strip(" ,.") not in ("at", "in", "on", "the", "a", "an")]
        tokens.extend(seg_tokens)

    # ── Resolve tokens into structured fields ──
    for t in tokens:
        if t.kind == "city" and not loc.city:
            loc.city = t.value
        elif t.kind == "micro_market" and not loc.micro_market:
            loc.micro_market = t.value
        elif t.kind == "locality":
            if not loc.locality:
                loc.locality = t.value
            canonical_market = _canonical_micro_market(t.value)
            if canonical_market and not loc.micro_market:
                loc.micro_market = canonical_market
        elif t.kind == "landmark" and not loc.landmark:
            loc.landmark = t.value
            # Enrich micro_market from landmark meta
            mm = t.meta.get("micro_market")
            if mm and not loc.micro_market:
                loc.micro_market = mm
            # If no locality but landmark has micro_market, extract locality
            if not loc.locality and mm:
                loc.locality = mm.split()[0]
        elif t.kind == "building" and not loc.building:
            loc.building = t.value
            if t.meta.get("area") and not loc.micro_market:
                loc.micro_market = t.meta["area"]
        elif t.kind == "transit_landmark" and not loc.transit_landmark:
            loc.transit_landmark = t.value
        elif t.kind == "spatial_relation" and not loc.spatial_relation:
            loc.spatial_relation = t.value

    # Set city default
    if not loc.city:
        micro = (loc.micro_market or "").lower()
        if any(c in micro for c in ["dubai", "marina", "jbr", "jvc", "jvt", "jlt",
                                    "palm", "downtown", "business bay", "difc",
                                    "barsha", "deira", "karama", "qusais", "mirdif",
                                    "furjan", "hills", "spring", "meadow", "lake",
                                    "greens", "views", "silicon oasis", "creek",
                                    "ranches", "motor city", "town square",
                                    "damac", "jaddaf", "garhoud", "metha"]):
            loc.city = "Dubai"
        elif any(c in micro for c in ["yas island", "saadiyat", "al reem", "khalifa city"]):
            loc.city = "Abu Dhabi"
        elif any(c in micro for c in ["al khan", "al majaz"]):
            loc.city = "Sharjah"

    loc.tokens = [{"text": t.text, "kind": t.kind, "value": t.value, "meta": t.meta}
                   for t in tokens]
    return loc
