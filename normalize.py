"""
WhatsApp Message Normalization
==============================
Pre-processing pipeline for raw WhatsApp messages before parsing/extraction.
Removes noise, standardizes formats, expands abbreviations.
"""

import re
from typing import Optional

# ── Emoji ranges ──────────────────────────────────────────────────
_EMOJI_RANGES = [
    (0x1F600, 0x1F64F),  # Emoticons
    (0x1F300, 0x1F5FF),  # Misc Symbols & Pictographs
    (0x1F680, 0x1F6FF),  # Transport & Map
    (0x1F700, 0x1F77F),  # Alchemical
    (0x1F780, 0x1F7FF),  # Geometric Shapes Extended
    (0x1F800, 0x1F8FF),  # Supplemental Arrows-C
    (0x1F900, 0x1F9FF),  # Supplemental Symbols
    (0x1FA70, 0x1FAFF),  # Symbols & Pictographs Extended-A
    (0x2600, 0x26FF),    # Misc Symbols
    (0x2700, 0x27BF),    # Dingbats
    (0xFE00, 0xFE0F),    # Variation Selectors
]

_EMOJI_PATTERN = re.compile(
    '[' + ''.join(chr(start) + '-' + chr(end) for start, end in _EMOJI_RANGES) + ']'
)

# ── WhatsApp formatting ───────────────────────────────────────────
_BOLD_RE = re.compile(r'\*([^*\n]{1,200}?)\*')
_ITALIC_RE = re.compile(r'_([^_\n]{1,200}?)_')
_STRIKE_RE = re.compile(r'~([^~\n]{1,200}?)~')
_MONO_RE = re.compile(r'`([^`\n]{1,200}?)`')

# ── Noise patterns ────────────────────────────────────────────────
_PHONE_RE = re.compile(
    r'(?:\+?(?:971|91)[\s\-]?)?'
    r'(?:[5]\d{8}|'
    r'[6-9]\d{9}|'
    r'\d{3}[\s\-]?\d{3}[\s\-]?\d{4}|'
    r'\(\d{3}\)\s*\d{3}[\s\-]?\d{4})'
)
_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
_URL_RE = re.compile(r'https?://\S+|www\.\S+')

# Broker signatures / disclaimers
_SIGNATURE_PATTERNS = [
    r'(?i)^\s*(?:regards?|thanks?|thank\s*you|best|cheers|sincerely)[\s,.*_\-]*$',
    r'(?i)^\s*(?:sent\s+from\s+my\s+\w+)[\s,.*_\-]*$',
    r'(?i)^\s*(?:forwarded|fwd)[\s:]*',
    r'(?i)^\s*(?:disclaimer|confidential|privileged)[\s:].*$',
    r'(?i)^\s*(?:broker|agent|dealer|consultant)[\s:]*\w+.*$',
    r'(?i)^\s*(?:RERA|rera|DLD|dld|BRN|brn)[\s:]*\w+.*$',
]

# Common real estate abbreviations
_ABBREVIATIONS = {
    # Furnishing
    r'\bSF\b': 'Semi Furnished',
    r'\bFF\b': 'Fully Furnished',
    r'\bUF\b': 'Unfurnished',
    r'\bUNF\b': 'Unfurnished',
    r'\bFURN\b': 'Furnished',
    r'\bSEMIF\b': 'Semi Furnished',
    
    # BHK
    r'\b1\s*RK\b': '1 RK',
    r'\b1\s*BHK\b': '1 BHK',
    r'\b2\s*BHK\b': '2 BHK',
    r'\b3\s*BHK\b': '3 BHK',
    r'\b4\s*BHK\b': '4 BHK',
    r'\b5\s*BHK\b': '5 BHK',
    
    # Property types
    r'\bFLAT\b': 'Flat',
    r'\bAPT\b': 'Apartment',
    r'\bAPPT\b': 'Apartment',
    r'\bROW\s*HOUSE\b': 'Row House',
    r'\bRH\b': 'Row House',
    r'\bBUNGALOW\b': 'Bungalow',
    r'\bVILLA\b': 'Villa',
    r'\bPENTHOUSE\b': 'Penthouse',
    r'\bSTUDIO\b': 'Studio',
    r'\bSHOP\b': 'Shop',
    r'\bOFFICE\b': 'Office',
    r'\bCOMM\b': 'Commercial',
    r'\bCOMMERCIAL\b': 'Commercial',
    r'\bRES\b': 'Residential',
    r'\bRESIDENTIAL\b': 'Residential',
    
    # Status
    r'\bRTO\b': 'Ready to Move',
    r'\bRTM\b': 'Ready to Move',
    r'\bUC\b': 'Under Construction',
    r'\bPRE\s*LAUNCH\b': 'Pre Launch',
    r'\bPOSSESSION\b': 'Possession',
    
    # Directions
    r'\bN\b(?=\s|$|[,.])': 'North',
    r'\bS\b(?=\s|$|[,.])': 'South',
    r'\bE\b(?=\s|$|[,.])': 'East',
    r'\bW\b(?=\s|$|[,.])': 'West',
    r'\bNE\b': 'North East',
    r'\bNW\b': 'North West',
    r'\bSE\b': 'South East',
    r'\bSW\b': 'South West',
    
    # Floor
    r'\bGF\b': 'Ground Floor',
    r'\bGR\b': 'Ground Floor',
    r'\bFF\b': 'First Floor',
    r'\bSF\b': 'Second Floor',
    r'\bTF\b': 'Third Floor',
    
    # Area
    r'\bSQFT\b': 'sq ft',
    r'\bSQ\s*FT\b': 'sq ft',
    r'\bSQM\b': 'sq m',
    r'\bSQ\s*M\b': 'sq m',
}

# Price unit normalization (UAE: K = thousand, M = million; no lakh/crore).
# Bare ``m`` is guarded so area strings such as ``1,200 sq m`` are untouched.
_PRICE_UNITS = {
    r'(?i)\b(?:k|thousand)\b': 'K',
    r'(?i)(?<!\bsq\s)\b(?:m|mil|million|mn)\b': 'M',
}

# Common noise words to remove
_NOISE_WORDS = [
    r'(?i)\burgent\b',
    r'(?i)\bimmediate\b',
    r'(?i)\bcall\s+now\b',
    r'(?i)\bwhatsapp\s+only\b',
    r'(?i)\bno\s+brokers?\b',
    r'(?i)\bbroker\s*[s]?\s*welcome\b',
    r'(?i)\bgenuine\b',
    r'(?i)\bverified\b',
    r'(?i)\bauthentic\b',
    r'(?i)\bdirect\s+owner\b',
    r'(?i)\bowner\s+direct\b',
    r'(?i)\ball\s+brokers\b',
    r'(?i)\binterested\s+(?:party|parties|buyers?|tenants?)\s+(?:call|contact|msg|whatsapp)\b',
    r'(?i)\b(?:call|contact|msg|whatsapp|ping)\s+(?:me|us)\s*(?:now|asap)?\b',
]

# ── Public API ────────────────────────────────────────────────────

def normalize_whatsapp_message(text: str) -> dict:
    """
    Full normalization pipeline for a raw WhatsApp message.
    Returns dict with original, cleaned, and metadata.
    """
    if not text or not text.strip():
        return {"original": text, "cleaned": "", "metadata": {}}
    
    original = text
    cleaned = text
    metadata = {
        "had_emoji": False,
        "had_formatting": False,
        "had_phone": False,
        "had_email": False,
        "had_url": False,
        "abbreviations_expanded": [],
        "noise_removed": [],
    }
    
    # 1. Detect and remove emoji spam (3+ consecutive)
    emoji_matches = list(_EMOJI_PATTERN.finditer(cleaned))
    if emoji_matches:
        metadata["had_emoji"] = True
        # Remove runs of 3+ emojis
        cleaned = re.sub(r'([\U0001f600-\U0001f64F\U0001f300-\U0001f5FF\U0001f680-\U0001f6FF\U0001f900-\U0001f9FF\U0001f700-\U0001f77F\U0001f780-\U0001f7FF\U0001f800-\U0001f8FF\U0001fa70-\U0001faff\U00002600-\U000026FF\U00002700-\U000027BF]){3,}', '', cleaned)
    
    # 2. Strip WhatsApp markdown formatting
    if any(p.search(cleaned) for p in [_BOLD_RE, _ITALIC_RE, _STRIKE_RE, _MONO_RE]):
        metadata["had_formatting"] = True
        cleaned = _BOLD_RE.sub(r'\1', cleaned)
        cleaned = _ITALIC_RE.sub(r'\1', cleaned)
        cleaned = _STRIKE_RE.sub(r'\1', cleaned)
        cleaned = _MONO_RE.sub(r'\1', cleaned)
    
    # 3. Mask phone numbers (keep pattern for extraction, but clean for parsing)
    if _PHONE_RE.search(cleaned):
        metadata["had_phone"] = True
        cleaned = _PHONE_RE.sub(' [PHONE] ', cleaned)
    
    # 4. Mask emails
    if _EMAIL_RE.search(cleaned):
        metadata["had_email"] = True
        cleaned = _EMAIL_RE.sub(' [EMAIL] ', cleaned)
    
    # 5. Mask URLs
    if _URL_RE.search(cleaned):
        metadata["had_url"] = True
        cleaned = _URL_RE.sub(' [URL] ', cleaned)
    
    # 6. Remove signature lines
    for pattern in _SIGNATURE_PATTERNS:
        if re.search(pattern, cleaned, re.MULTILINE):
            metadata["noise_removed"].append("signature")
            cleaned = re.sub(pattern, '', cleaned, flags=re.MULTILINE)
    
    # 7. Remove noise words
    for pattern in _NOISE_WORDS:
        if re.search(pattern, cleaned):
            metadata["noise_removed"].append(pattern)
            cleaned = re.sub(pattern, '', cleaned)
    
    # 8. Expand abbreviations
    for pattern, replacement in _ABBREVIATIONS.items():
        if re.search(pattern, cleaned, re.IGNORECASE):
            metadata["abbreviations_expanded"].append(pattern)
            cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    
    # 9. Normalize price units
    for pattern, replacement in _PRICE_UNITS.items():
        cleaned = re.sub(pattern, replacement, cleaned)
    
    # 10. Clean up whitespace
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)  # Max 2 newlines
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)  # Multiple spaces/tabs
    cleaned = re.sub(r'^\s+|\s+$', '', cleaned, flags=re.MULTILINE)  # Trim lines
    cleaned = cleaned.strip()
    
    return {
        "original": original,
        "cleaned": cleaned,
        "metadata": metadata,
    }


def extract_phones(text: str) -> list[str]:
    """Extract all phone numbers from text."""
    matches = _PHONE_RE.findall(text)
    normalized = []
    for m in matches:
        if isinstance(m, tuple):
            m = ''.join(m)
        # Normalize to local UAE (9-digit mobile) or legacy 10-digit form
        digits = re.sub(r'\D', '', m)
        if len(digits) == 12 and digits.startswith('971'):
            digits = digits[3:]
        if len(digits) == 12 and digits.startswith('91'):
            digits = digits[2:]
        if len(digits) == 10 and digits[0] == '0':
            digits = digits[1:]
        if len(digits) == 9 and digits[0] == '5':
            normalized.append(digits)
        elif len(digits) == 10 and digits[0] in '6789':
            normalized.append(digits)
    return list(set(normalized))


def extract_emails(text: str) -> list[str]:
    """Extract all emails from text."""
    return list(set(_EMAIL_RE.findall(text)))


def normalize_building_name(name: str) -> str:
    """Normalize building name for matching."""
    if not name:
        return ""
    # Remove common suffixes/prefixes
    normalized = name.strip()
    # Remove emoji
    normalized = _EMOJI_PATTERN.sub('', normalized)
    # Title case
    normalized = re.sub(r'\b(\w+)', lambda m: m.group(1).capitalize(), normalized)
    # Clean spaces
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized.strip()


def normalize_location(location: str) -> str:
    """Normalize location string."""
    if not location:
        return ""
    normalized = location.strip()
    # Remove emoji
    normalized = _EMOJI_PATTERN.sub('', normalized)
    # Standardize separators
    normalized = re.sub(r'\s*[,/]\s*', ', ', normalized)
    # Clean multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized.strip()


# ── Convenience function for ingestion pipeline ──────────────────

def preprocess_for_parsing(raw_message: str) -> str:
    """
    Lightweight pre-processor to run before the parser.
    Returns only the cleaned text.
    """
    result = normalize_whatsapp_message(raw_message)
    return result["cleaned"]