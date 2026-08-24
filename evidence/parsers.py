"""
Broker Vocabulary Parser.

Interprets spatial relationship patterns from broker language
into structured queries.

Broker grammar patterns:
  - "opposite <landmark>" / "opp. <landmark>" / "opp <landmark>"
  - "behind <landmark>" / "backside of <landmark>"
  - "near <landmark>" / "next to <landmark>" / "adjacent to <landmark>"
  - "off <road>" (lane off main road)
  - "walkable to <landmark>" / "walking distance from <landmark>"
  - "<landmark> lane" / "<landmark> galli" (small street off landmark)
  - "<area> naka" (junction)
  - "<area> circle" (traffic circle)
  - "<area> signal" (traffic signal)
  - "<area> cross" / "<area> crossing"
  - "<name> station" → resolves to railway station
  - "<name> road" / "<name> rd" → resolves to street

Output: {
    "relation": str | None,
    "main_query": str,
    "hint_type": "landmark" | "street" | "station" | "area",
    "confidence": float,
    "raw": str,
}
"""
import re
from typing import Optional

# ── Prefix Pattern: "<relation> <entity>" ─────────────────────────
PREFIX_PATTERNS = [
    # opposite, behind, near
    (r"^(?:opposite|opp\.?|opp|across\s+from)\s+(.+)$", "opposite", 0.95),
    (r"^(?:behind|backside\s+of|back\s+side\s+of)\s+(.+)$", "behind", 0.90),
    (r"^(?:near|next\s+to|adjacent\s+to|close\s+to|by\s+the\s+side\s+of)\s+(.+)$", "near", 0.85),
    (r"^just\s+(?:off|near|opposite)\s+(.+)$", "near", 0.80),
    # walkable / walking distance
    (r"^(?:walkable\s+to|walking\s+distance\s+(?:from|to)|walking\s+from|5\s*min\s+(?:walk\s+)?from)\s+(.+)$", "walkable", 0.80),
    # off <main road>
    (r"^off\s+(.+)$", "off", 0.85),
    # on <road/street>
    (r"^on\s+(.+)$", "on", 0.70),
    # at <location>
    (r"^at\s+(.+)$", "at", 0.60),
]

# ── Suffix/Postfix Patterns: "<entity> <type>" ────────────────────
SUFFIX_PATTERNS = [
    (r"^(.+?)\s+(?:lane|lane|galli|gully)$", "lane", 0.85, "street"),
    (r"^(.+?)\s+(?:naka)$", "junction", 0.90, "area"),
    (r"^(.+?)\s+(?:circle|chowk|square)$", "circle", 0.85, "area"),
    (r"^(.+?)\s+(?:signal)$", "signal", 0.80, "area"),
    (r"^(.+?)\s+(?:cross|crossing)$", "crossing", 0.80, "area"),
    (r"^(.+?)\s+(?:station|stn)$", "station", 0.90, "station"),
    (r"^(.+?)\s+(?:road|rd)$", "road", 0.75, "street"),
]

# ── Keyword hints (weak signals) ──────────────────────────────────
KEYWORD_HINTS = {
    "hospital": ("landmark", 0.70),
    "mall": ("landmark", 0.70),
    "temple": ("landmark", 0.70),
    "mandir": ("landmark", 0.70),
    "church": ("landmark", 0.70),
    "mosque": ("landmark", 0.70),
    "masjid": ("landmark", 0.70),
    "dargah": ("landmark", 0.70),
    "beach": ("landmark", 0.70),
    "lake": ("landmark", 0.65),
    "garden": ("landmark", 0.65),
    "park": ("landmark", 0.65),
    "stadium": ("landmark", 0.70),
    "station": ("landmark", 0.60),
    "airport": ("landmark", 0.75),
    "hotel": ("landmark", 0.65),
    "college": ("landmark", 0.65),
    "school": ("landmark", 0.65),
    "university": ("landmark", 0.65),
    "theatre": ("landmark", 0.65),
    "cinema": ("landmark", 0.65),
    "museum": ("landmark", 0.65),
    "club": ("landmark", 0.60),
}


def parse(text: str) -> dict:
    """
    Parse a broker-style location query into a structured result.

    >>> parse("opposite Lilavati Hospital")
    {'relation': 'opposite', 'main_query': 'lilavati hospital', ...}

    >>> parse("near Linking Road")
    {'relation': 'near', 'main_query': 'linking road', ...}

    >>> parse("Emirates Towers Metro Station")
    {'relation': None, 'main_query': 'marina', 'hint_type': 'metro', ...}
    """
    raw = text.strip()
    lower = raw.lower().strip()

    # 1. Try prefix patterns: "opposite X", "near X", etc.
    for pattern, relation, confidence in PREFIX_PATTERNS:
        m = re.match(pattern, lower)
        if m:
            main_query = m.group(1).strip()
            return {
                "relation": relation,
                "main_query": main_query,
                "hint_type": _infer_type(main_query),
                "confidence": confidence,
                "raw": raw,
            }

    # 2. Try suffix patterns: "X lane", "X station", etc.
    for pattern, relation, confidence, hint_type in SUFFIX_PATTERNS:
        m = re.match(pattern, lower)
        if m:
            main_query = m.group(1).strip()
            return {
                "relation": relation,
                "main_query": main_query,
                "hint_type": hint_type,
                "confidence": confidence,
                "raw": raw,
            }

    # 3. Check for keyword hints in the text
    hint_type = _infer_type(lower)
    if hint_type == "landmark":
        return {
            "relation": None,
            "main_query": lower,
            "hint_type": "landmark",
            "confidence": 0.50,
            "raw": raw,
        }

    # 4. No pattern detected — pass through as raw
    return {
        "relation": None,
        "main_query": lower,
        "hint_type": None,
        "confidence": 0.0,
        "raw": raw,
    }


def _infer_type(query: str) -> Optional[str]:
    """Use keyword hints to infer whether query is a landmark, area, or street."""
    for keyword, (hint_type, _) in KEYWORD_HINTS.items():
        if keyword in query:
            return hint_type
    return None


def strip_relation(text: str) -> str:
    """Remove spatial relation prefix and return the entity name."""
    result = parse(text)
    return result["main_query"]


def relation_confidence(relation: str) -> float:
    """Return confidence weight for a given spatial relation."""
    weights = {
        "opposite": 0.95,
        "behind": 0.90,
        "near": 0.85,
        "off": 0.85,
        "walkable": 0.80,
        "on": 0.70,
        "at": 0.60,
        "lane": 0.85,
        "junction": 0.90,
        "circle": 0.80,
        "signal": 0.70,
        "crossing": 0.75,
    }
    return weights.get(relation, 0.50)
