"""
Dubai location hierarchy.

Maps every known area/locality to its parent micro-market.
Based on standard Dubai real estate market segmentation used by
Bayut, Property Finder, Dubizzle, and DLD transaction data.

IMPORTANT: Every micro_market value must be a real, specific locality
that property portals recognize. No fake aggregate buckets like
"New Dubai Prime" or "Central Dubai Core".
"""

# Micro markets → sub-areas they contain
# Each micro_market is a real locality name.
MICRO_MARKETS: dict[str, list[str]] = {
    "Dubai Marina": [
        "Dubai Marina",
        "Marina Walk",
    ],
    "JBR": [
        "Jumeirah Beach Residence",
        "JBR",
        "The Walk",
    ],
    "Bluewaters Island": [
        "Bluewaters",
        "Bluewaters Island",
    ],
    "Palm Jumeirah": [
        "Palm Jumeirah",
        "Frond",
        "Crescent Road",
    ],
    "JLT": [
        "JLT",
        "Jumeirah Lakes Towers",
        "Cluster",
    ],
    "Al Furjan": [
        "Al Furjan",
    ],
    "Dubai Production City": [
        "IMPZ",
        "Dubai Production City",
        "Production City",
    ],
    "Discovery Gardens": [
        "Discovery Gardens",
    ],
    "Jebel Ali": [
        "Jebel Ali",
        "Jafza",
    ],
    "Jumeirah Islands": [
        "Jumeirah Islands",
    ],
    "Jumeirah Park": [
        "Jumeirah Park",
    ],
    "Downtown Dubai": [
        "Downtown Dubai",
        "Opera District",
        "Burj Khalifa Area",
    ],
    "Business Bay": [
        "Business Bay",
    ],
    "DIFC": [
        "DIFC",
        "Dubai International Financial Centre",
    ],
    "Za'abeel": [
        "Za'abeel 1",
        "Za'abeel 2",
        "Zabeel",
    ],
    "City Walk": [
        "City Walk",
    ],
    "Sheikh Zayed Road": [
        "Sheikh Zayed Road",
        "SZR",
        "Trade Centre",
        "World Trade Centre",
    ],
    "The Springs": [
        "Springs",
        "The Springs",
    ],
    "The Meadows": [
        "Meadows",
        "The Meadows",
    ],
    "The Lakes": [
        "Lakes",
        "The Lakes",
    ],
    "Emirates Hills": [
        "Emirates Hills",
        "The Montgomerie",
    ],
    "The Greens": [
        "The Greens",
    ],
    "The Views": [
        "The Views",
    ],
    "Arabian Ranches": [
        "Arabian Ranches",
        "Arabian Ranches 1",
        "Arabian Ranches 2",
        "Arabian Ranches 3",
        "Ranches",
    ],
    "Dubai Hills Estate": [
        "Dubai Hills Estate",
        "Dubai Hills",
    ],
    "Jumeirah": [
        "Jumeirah 1",
        "Jumeirah 2",
        "Jumeirah 3",
    ],
    "Umm Suqeim": [
        "Umm Suqeim 1",
        "Umm Suqeim 2",
        "Umm Suqeim 3",
    ],
    "Al Sufouh": [
        "Al Sufouh 1",
        "Al Sufouh 2",
        "Madina Jumeirah Living",
        "MJL",
    ],
    "Al Wasl": [
        "Al Wasl",
    ],
    "Jumeirah Village Circle": [
        "JVC",
        "Jumeirah Village Circle",
    ],
    "Jumeirah Village Triangle": [
        "JVT",
        "Jumeirah Village Triangle",
    ],
    "Al Barsha": [
        "Al Barsha 1",
        "Al Barsha 2",
        "Al Barsha 3",
        "Barsha South",
        "Barsha Heights",
        "TECOM",
    ],
    "Al Quoz": [
        "Al Quoz 1",
        "Al Quoz 2",
        "Al Quoz 3",
        "Al Quoz 4",
    ],
    "Motor City": [
        "Motor City",
        "Dubai Motor City",
    ],
    "Sports City": [
        "Dubai Sports City",
        "Sports City",
    ],
    "Studio City": [
        "Dubai Studio City",
    ],
    "Arjan": [
        "Arjan",
    ],
    "Remraam": [
        "Remraam",
    ],
    "Mudon": [
        "Mudon",
    ],
    "Town Square": [
        "Town Square",
        "Nshama Town Square",
    ],
    "DAMAC Hills": [
        "DAMAC Hills",
        "Akoya Oxygen",
        "Akoya",
    ],
    "Dubailand": [
        "Dubailand",
        "Majan",
        "Liwan",
        "The Villa",
        "Living Legends",
        "Reem",
        "Mira",
    ],
    "Dubai Silicon Oasis": [
        "Dubai Silicon Oasis",
        "DSO",
    ],
    "Academic City": [
        "Academic City",
        "Dubai International Academic City",
    ],
    "Mirdif": [
        "Mirdif",
    ],
    "Al Warqaa": [
        "Al Warqa 1",
        "Al Warqa 2",
        "Al Warqa 3",
        "Al Warqa 4",
        "Warqaa",
    ],
    "International City": [
        "International City",
        "Dubai International City",
        "Warsan",
    ],
    "Nad Al Sheba": [
        "Nad Al Sheba 1",
        "Nad Al Sheba 2",
        "Nad Al Sheba 3",
        "Nad Al Sheba",
    ],
    "Meydan": [
        "Meydan",
        "Meydan City",
        "Meydan Gated Community",
    ],
    "Al Barari": [
        "Al Barari",
    ],
    "Al Khawaneej": [
        "Al Khawaneej 1",
        "Al Khawaneej 2",
    ],
    "Al Mizhar": [
        "Al Mizhar 1",
        "Al Mizhar 2",
    ],
    "Deira": [
        "Deira",
        "Naif",
        "Port Saeed",
    ],
    "Bur Dubai": [
        "Bur Dubai",
        "Al Fahidi",
        "Al Raffa",
    ],
    "Al Karama": [
        "Karama",
        "Al Karama",
    ],
    "Oud Metha": [
        "Oud Metha",
    ],
    "Umm Hurair": [
        "Umm Hurair 1",
        "Umm Hurair 2",
    ],
    "Al Qusais": [
        "Al Qusais 1",
        "Al Qusais 2",
        "Al Qusais 3",
    ],
    "Al Nahda": [
        "Al Nahda 1",
        "Al Nahda 2",
    ],
    "Al Rashidiya": [
        "Al Rashidiya",
        "Rashidiya",
    ],
    "Al Garhoud": [
        "Garhoud",
        "Al Garhoud",
    ],
    "Dubai Festival City": [
        "Festival City",
        "Dubai Festival City",
    ],
    "Al Jaddaf": [
        "Al Jaddaf",
        "Culture Village",
    ],
    "Dubai Creek Harbour": [
        "Dubai Creek Harbour",
        "Creek Harbour",
    ],
    "Ras Al Khor": [
        "Ras Al Khor",
    ],
}

# Reverse mapping: area → micro market
AREA_TO_MARKET: dict[str, str] = {}
for market, areas in MICRO_MARKETS.items():
    for area in areas:
        AREA_TO_MARKET[area.lower()] = market


# Also add direct mappings for known aliases/variants
AREA_ALIASES: dict[str, str] = {
    "—": "",
    "jvc": "JVC",
    "jvc dubai": "JVC",
    "jumeirah village circle": "JVC",
    "jvt": "JVT",
    "jumeirah village triangle": "JVT",
    "jbr": "JBR",
    "the walk": "The Walk",
    "jlt": "JLT",
    "jumeirah lakes towers": "JLT",
    "dso": "DSO",
    "silicon oasis": "DSO",
    "szr": "SZR",
    "sheikh zayed rd": "SZR",
    "sheikh zayed road": "SZR",
    "difc": "DIFC",
    "dubai international financial centre": "DIFC",
    "buisness bay": "Business Bay",
    "businessbay": "Business Bay",
    "bbay": "Business Bay",
    "downtown": "Downtown Dubai",
    "downtown burj khalifa": "Burj Khalifa Area",
    "burj area": "Burj Khalifa Area",
    "old town": "Downtown Dubai",
    "palm jumeirah": "Palm Jumeirah",
    "the palm": "Palm Jumeirah",
    "marina": "Dubai Marina",
    "dubai marina": "Dubai Marina",
    "marina walk": "Marina Walk",
    "bluewaters": "Bluewaters",
    "blue water island": "Bluewaters Island",
    "creek harbour": "Creek Harbour",
    "creek gate": "Dubai Creek Harbour",
    "dubai hills": "Dubai Hills",
    "hills estate": "Dubai Hills Estate",
    "sports city": "Dubai Sports City",
    "dsc": "Dubai Sports City",
    "motor city": "Motor City",
    "studio city": "Dubai Studio City",
    "town square dubai": "Nshama Town Square",
    "nshama": "Nshama Town Square",
    "damac hills": "DAMAC Hills",
    "akoya": "Akoya",
    "akoya oxygen": "Akoya Oxygen",
    "impz": "IMPZ",
    "production city": "Production City",
    "dubai production city": "Dubai Production City",
    "barsha heights": "Barsha Heights",
    "tecom": "TECOM",
    "barsha south": "Barsha South",
    "al barsha": "Al Barsha 1",
    "barsha": "Al Barsha 1",
    "quoz": "Al Quoz 1",
    "al quoz": "Al Quoz 1",
    "umm suqueim": "Umm Suqeim",
    "um suqeim": "Umm Suqeim",
    "alsufouh": "Al Sufouh 1",
    "sufouh": "Al Sufouh 1",
    "mjl": "Madina Jumeirah Living",
    "madinat jumeirah living": "Madina Jumeirah Living",
    "emirates hills": "Emirates Hills",
    "the montgomerie": "The Montgomerie",
    "greens": "The Greens",
    "views": "The Views",
    "springs": "Springs",
    "meadows": "Meadows",
    "lakes": "Lakes",
    "ranches": "Arabian Ranches",
    "the ranches": "Arabian Ranches",
    "remraam": "Remraam",
    "mudon": "Mudon",
    "arjan": "Arjan",
    "majan": "Majan",
    "liwan": "Liwan",
    "the villa": "The Villa",
    "living legends": "Living Legends",
    "reem dubai": "Reem",
    "mira dubai": "Mira",
    "mirdif": "Mirdif",
    "warqaa": "Al Warqa 1",
    "al warqa": "Al Warqa 1",
    "international city": "International City",
    "warsan": "Warsan",
    "nad al sheba": "Nad Al Sheba",
    "meydan": "Meydan",
    "al barari": "Al Barari",
    "khawaneej": "Al Khawaneej 1",
    "mizhar": "Al Mizhar 1",
    "karama": "Karama",
    "bur dubai": "Bur Dubai",
    "fahidi": "Al Fahidi",
    "oud metha": "Oud Metha",
    "qusais": "Al Qusais 1",
    "nahda": "Al Nahda 1",
    "rashidiya": "Rashidiya",
    "garhoud": "Garhoud",
    "festival city": "Festival City",
    "jadaf": "Al Jaddaf",
    "culture village": "Culture Village",
    "deira": "Deira",
    "port saeed": "Port Saeed",
    "naif": "Naif",
    "ras al khor": "Ras Al Khor",
    "jafza": "Jafza",
    "jebel ali": "Jebel Ali",
    "discovery gardens": "Discovery Gardens",
    "furjan": "Al Furjan",
    "jumeirah park": "Jumeirah Park",
    "jumeirah islands": "Jumeirah Islands",
}


def get_micro_market(area: str) -> str:
    """Get the micro market for a given area name."""
    key = area.strip().lower()

    # First try area alias resolution
    if key in AREA_ALIASES:
        resolved = AREA_ALIASES[key]
        if not resolved:
            return ""
        key = resolved.lower()

    # Then look up in the reverse mapping
    if key in AREA_TO_MARKET:
        return AREA_TO_MARKET[key]

    return ""


def get_canonical_area(area: str) -> str:
    """Resolve an area to its canonical name."""
    key = area.strip().lower()
    if key in AREA_ALIASES:
        return AREA_ALIASES[key]
    return area.strip()
