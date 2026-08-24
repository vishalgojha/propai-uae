"""
Known Dubai real estate developers.
Maps developer names and their known name prefixes/suffixes.
"""
import re

# Known developers with canonical name and matching patterns
DEVELOPERS = [
    # Tier 1: Major developers
    {"name": "Emaar Properties", "patterns": ["emaar"]},
    {"name": "DAMAC Properties", "patterns": ["damac"]},
    {"name": "Nakheel", "patterns": ["nakheel"]},
    {"name": "Sobha Realty", "patterns": ["sobha"]},
    {"name": "Meraas", "patterns": ["meraas"]},
    {"name": "Dubai Properties", "patterns": ["dubai properties", "dubai prop "]},
    {"name": "Select Group", "patterns": ["select group"]},
    {"name": "Aldar Properties", "patterns": ["aldar"]},
    {"name": "Nshama", "patterns": ["nshama"]},
    {"name": "Omniyat", "patterns": ["omniyat"]},
    # Tier 2: Active mid-sized developers
    {"name": "Azizi Developments", "patterns": ["azizi"]},
    {"name": "Danube Properties", "patterns": ["danube"]},
    {"name": "Binghatti", "patterns": ["binghatti"]},
    {"name": "Ellington Properties", "patterns": ["ellington"]},
    {"name": "Deyaar Developments", "patterns": ["deyaar"]},
    {"name": "Union Properties", "patterns": ["union properties", "upi "]},
    {"name": "Wasl Properties", "patterns": ["wasl properties", "wasl "]},
    {"name": "MAG Property Development", "patterns": ["mag property", "mag 5", "mag city", "mag pdp"]},
    {"name": "Tiger Group", "patterns": ["tiger properties", "tiger group"]},
    {"name": "Imtiaz Developments", "patterns": ["imtiaz developments", "imtiaz "]},
    {"name": "Meydan Group", "patterns": ["meydan group", "meydan sobha"]},
    {"name": "Dubai South Properties", "patterns": ["dubai south"]},
    {"name": "Aeon & Triteles", "patterns": ["aeon & triteles", "aeon triteles"]},
    {"name": "Peace Homes Group", "patterns": ["peace homes"]},
    {"name": "Samana Developers", "patterns": ["samana"]},
    {"name": "Leos Developments", "patterns": ["leos developments", "leos "]},
    {"name": "Object 1", "patterns": ["object 1"]},
    {"name": "Reportage Properties", "patterns": ["reportage"]},
    {"name": "Eagle Hills", "patterns": ["eagle hills"]},
    {"name": "Dubai Developers", "patterns": []},  # fallback
]


def extract_developer(building_name: str) -> str | None:
    """Try to identify the developer from a building name."""
    name_lower = building_name.lower().strip()
    
    # Remove common suffixes that might interfere
    cleaned = name_lower.replace("'s", "").replace("s'", "")
    
    best_match = None
    best_len = 0
    
    for dev in DEVELOPERS:
        for pat in dev["patterns"]:
            if pat in cleaned:
                # Prefer longer, more specific patterns
                if len(pat) > best_len:
                    best_match = dev["name"]
                    best_len = len(pat)
    
    return best_match
