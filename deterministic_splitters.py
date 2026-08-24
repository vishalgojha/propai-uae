"""Deterministic multi-listing splitters for broker broadcast messages.

The goal is intentionally narrow:

- recognize recurring broadcast templates;
- split them into per-listing chunks;
- extract the stable, low-variance fields with regexes;
- leave free-form fallback to the main extraction pipeline.

This module is conservative. If a pattern is not convincing, it returns
``None`` and the caller can fall back to the LLM path.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

PATTERN_DASH_SEPARATOR = "dash_separator"
PATTERN_NUMBERED = "numbered"
PATTERN_EMOJI_BULLET = "emoji_bullet"
PATTERN_BARE_BHK = "bare_bhk_header"
PATTERN_INLINE_BOLD = "inline_bold_header"
PATTERN_RUN_ON_INVENTORY = "run_on_inventory"

PATTERN_ORDER = [
    PATTERN_DASH_SEPARATOR,
    PATTERN_NUMBERED,
    PATTERN_EMOJI_BULLET,
    PATTERN_BARE_BHK,
    PATTERN_RUN_ON_INVENTORY,
    PATTERN_INLINE_BOLD,
]

_BHK_HEADER_PATTERN = (
    r"^\s*(?:[*_~]+\s*)?(?:[🏡▪️▫️•]\s*)?"
    r"(?:\d+(?:\.\d+)?\s*(?:bhk|br|rk)\b|\b(?:br|rk)\b)"
)
_BHK_HEADER_RE = re.compile(r"(?im)" + _BHK_HEADER_PATTERN)
_DASH_LINE_RE = re.compile(r"^\s*(?:[-–—_=]{3,}|[─━]{3,}|•{3,}|·{3,})\s*$")
_NUMBERED_LINE_RE = re.compile(r"^\s*(?:\(\s*\d+\s*\)|\d+[.)](?=\s+))\s*")
_EMOJI_BULLET_GLYPHS = ("🏡", "📍", "▪️", "▫️", "•", "‣", "➤")
# Keep the line matcher structurally identical to the header matcher. The
# former determines split points while the latter determines acceptance.
_BHK_LINE_RE = re.compile(r"(?im)" + _BHK_HEADER_PATTERN)
_LISTING_HEADER_RE = re.compile(
    r"(?i)^\s*(?:[*_~]+\s*)?(?:[🏡▪️▫️•‣➤]\s*)?"
    r"(?:\d+(?:\.\d+)?\s*(?:bhk|br|rk)\b|\b(?:br|rk)\b)"
    r"(?:.*\b(?:for\s+rent|for\s+sale|lease|lease\s+out)\b.*)?\s*$"
)

_FURNISHING_RE = re.compile(
    r"(?i)\b("
    r"fully\s*furnished|semi\s*furnished|semi[-\s]?furnished|unfurnished|furnished"
    r")\b"
)
_PARKING_RE = re.compile(r"(?i)\b(\d+)\s*(?:car\s+)?parking\b")
_FLOOR_RE = re.compile(
    r"(?i)\b(?:floor|flr|level)\s*(?:[:\-]?\s*)?(\d+(?:st|nd|rd|th)?(?:\s*[-/]\s*\d+(?:st|nd|rd|th)?)?)"
)
_AREA_RE = re.compile(
    r"(?i)\b(?:carpet|built\s*up|super\s*built\s*up|usable|area|sq\.?\s*ft|sqft)\b"
    r"[^0-9]{0,12}([\d,]+(?:\.\d+)?)\s*(sqft|sq\.?\s*ft|sft)?"
)
_PRICE_RE = re.compile(
    r"(?i)(?:aed|dhs|dirhams?)?\s*([\d,]+(?:\.\d+)?)\s*(m|mn|million|k)\b"
)
# Some brokers send multiple inventory lines without line breaks, punctuation,
# or bullets: ``One BHK SF flat ... 75 K 2 BHK SF flat ... 40 K``. This is
# deliberately stricter than a generic BHK split: every candidate item must
# carry its own explicit price, otherwise a configuration range such as
# ``2 BHK or 3 BHK`` must remain one message for review.
_RUN_ON_LISTING_START_RE = re.compile(
    r"(?i)\b(?:"
    r"(?:large\s+)?(?:one|two|three|four|five|\d+(?:\.\d+)?)\s*(?:bhk|br|rk)"
    r"|studio"
    r")\b(?=\s+(?:sf|ff|uf|flat|apartment|furnished|unfurnished|semi[-\s]?furnished|"
    r"[a-z]))"
)
_INLINE_BOLD_RE = re.compile(r"\*([^*\n]{2,40})\*")
_DECORATIVE_BOLD_RE = re.compile(
    r"(?i)^\s*(?:available|new\s+brand\s+building|brand\s+new\s+building|"
    r"for\s+sale|for\s+rent|kindly\s+call|urgent|hot\s+deal)\b"
)
_GLOBAL_HEADER_RE = re.compile(
    r"(?is)^\s*(\*[^*\n]{2,120}\*)\s*(?=\*[^*\n]{2,40}\*|[^\n]*\b\d+(?:\.\d+)?\s*(?:bhk|br|rk)\b)"
)
_INTENT_RENT_RE = re.compile(r"(?i)\b(?:rent|rental|lease|lease\s+out|for\s+rent)\b")
_INTENT_SALE_RE = re.compile(r"(?i)\b(?:sale|sell|selling|sel|for\s+sale)\b")
_INTENT_REQ_RE = re.compile(r"(?i)\b(?:requirement|required|wanted|looking\s+for|need)\b")
_LOCATION_HINT_RE = re.compile(
    r"(?i)\b("
    r"marina|jbr|jvc|jvt|jlt|downtown|business\s+bay|difc|palm\s+jumeirah|"
    r"barsha|furjan|springs|meadows|lakes|greens|views|ranches|hills\s+estate|"
    r"sports\s+city|motor\s+city|town\s+square|damac\s+hills|dubailand|"
    r"deira|karama|qusais|nahda|festival\s+city|silicon\s+oasis|dso|jaddaf|"
    r"metha|warqa|khawaneej|mirdif|international\s+city|discovery\s+gardens|"
    r"jebel\s+ali|impz|production\s+city|remraam|mudon|arjan|meydan|"
    r"nad\s+al\s+sheba|barari|bluewaters|suqeim|sufouh|wasl|zabeel|szr|"
    r"sheikh\s+zayed\s+road|walk|promenade|boulevard|metro|station|"
    r"exchange|complex|garden|heights|tower|building|apartment|residency|estate"
    r")\b"
)
_BOILERPLATE_RE = re.compile(
    r"(?i)\b(?:note|notes|inspection|contact|details?|profile|client|family|bachelor|"
    r"veg|non[-\s]?veg|allow|allowed|welcome|thank(?:s| you)?|regards?)\b"
)

def _looks_like_location(text: str) -> bool:
    compact = _compact(text)
    if not compact:
        return False
    return bool(_LOCATION_HINT_RE.search(compact))


def _looks_like_anchor_line(line: str) -> bool:
    cleaned = _normalize_match_line(line).strip()
    if not cleaned or _is_signal_line(cleaned):
        return False
    if _NUMBERED_LINE_RE.match(cleaned) or _DASH_LINE_RE.match(cleaned):
        return False
    if _BHK_LINE_RE.match(cleaned):
        return False
    if re.search(r"(?i)\b(?:building\s*name|location)\b", cleaned):
        return True
    # Broadcasts commonly put the building and locality in a pipe-delimited
    # footer, e.g. ``KALPATARU MAGNUS | Bandra East``.  This is a new
    # listing heading, not a continuation of the preceding BHK block.
    pipe_parts = [part.strip() for part in re.split(r"\s*\|\s*", cleaned, maxsplit=1)]
    if len(pipe_parts) == 2 and all(pipe_parts):
        left_is_location = _looks_like_location(pipe_parts[0])
        right_is_location = _looks_like_location(pipe_parts[1])
        if left_is_location != right_is_location:
            return True
    parts = re.split(r"\s+[–—-]\s+", cleaned, maxsplit=1)
    if len(parts) != 2:
        return False
    left, right = [part.strip() for part in parts]
    if not left or not right:
        return False
    left_is_location = _looks_like_location(left)
    right_is_location = _looks_like_location(right)
    return left_is_location != right_is_location


def _anchor_candidate_text(line: str) -> str | None:
    raw = _normalize_match_line(line).strip()
    if not raw:
        return None
    cleaned = _strip_markers(raw)
    if not cleaned:
        return None
    if re.search(r"(?i)^\s*(?:building\s*name|building)\s*[:\-]\s*", cleaned):
        return cleaned
    if re.search(r"(?i)^\s*(?:location|loc\.?)\s*[:\-]\s*", cleaned):
        return cleaned
    if _looks_like_anchor_line(raw):
        return cleaned
    if not _is_signal_line(raw):
        return None
    if _BHK_LINE_RE.match(_normalize_match_line(cleaned)):
        return None
    if re.search(r"\b(?:building\s*name|location)\b", cleaned):
        return cleaned
    if re.search(r"\s+[–—-]\s+", cleaned):
        return cleaned
    if _BOILERPLATE_RE.search(cleaned):
        return None
    if re.search(r"(?i)\b(?:floor|parking|carpet|sqft|rent|sale|furnished|unfurnished|aed|dhs)\b|[\d,.]+\s*[km]\b", cleaned):
        return None
    if len(cleaned.split()) <= 1:
        return None
    return cleaned


def _listing_anchor_count(text: str) -> int:
    count = 0
    for line in _line_items(text):
        cleaned = _normalize_match_line(line).strip()
        if not cleaned:
            continue
        if re.match(r"^(?:🏡|📍)\s+", cleaned):
            count += 1
            continue
        if re.match(r"^\d+[.)]\s+", cleaned):
            count += 1
            continue
        if re.search(r"(?i)\b(?:building\s*name|location)\b", cleaned):
            count += 1
    return count


def _line_items(text: str) -> list[str]:
    return [line.rstrip() for line in (text or "").replace("\r", "").split("\n")]


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _normalize_match_line(line: str) -> str:
    """Ignore WhatsApp markdown wrappers before testing structural markers."""
    normalized = re.sub(r"^\s*(?:[*_~]+\s*)+", "", line or "")
    return re.sub(r"[*_~]+\s*$", "", normalized)


def _emoji_bullet_re(glyph: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*(?:[*_~]+\s*)?(?:{re.escape(glyph)}\s*)")


def _header_count(text: str) -> int:
    # Count line-by-line. ``^\s*`` is intentionally permissive for WhatsApp
    # indentation, but across a whole document it can consume newlines and
    # make a 15-property broadcast look like one header.
    return sum(
        1 for line in _line_items(text)
        if _BHK_HEADER_RE.match(_normalize_match_line(line))
    )


def _normalize_bhk(text: str) -> str | None:
    if not text:
        return None
    cleaned = str(text).strip().upper()
    if "RK" in cleaned:
        return "1 RK"
    match = re.search(r"(\d+(?:\.\d+)?)", cleaned)
    if not match:
        return None
    value = float(match.group(1))
    if value == 0.5:
        return "1 RK"
    if value.is_integer():
        return f"{int(value)} BHK"
    return f"{value:g} BHK"


def _extract_bhk(text: str) -> str | None:
    match = re.search(r"(?i)\b(\d+(?:\.\d+)?)\s*(bhk|br|rk)\b", text or "")
    if match:
        return _normalize_bhk(f"{match.group(1)} {match.group(2)}")
    if re.search(r"(?i)\brk\b", text or ""):
        return "1 RK"
    return None


def _extract_price(text: str) -> tuple[float | None, str | None]:
    match = _PRICE_RE.search(text or "")
    if not match:
        return None, None
    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None, None
    unit = match.group(2).lower()
    if unit in {"m", "mn", "million"}:
        unit = "M"
    elif unit == "k":
        unit = "K"
    return amount, unit


def _extract_area_sqft(text: str) -> float | None:
    match = _AREA_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_furnishing(text: str) -> str | None:
    match = _FURNISHING_RE.search(text or "")
    if not match:
        return None
    value = match.group(1).lower().replace(" ", "_").replace("-", "_")
    if value == "semi_furnished":
        return "semi_furnished"
    if value == "fully_furnished":
        return "fully_furnished"
    if value == "furnished":
        return "fully_furnished"
    if value == "unfurnished":
        return "unfurnished"
    return None


def _extract_parking(text: str) -> int | None:
    match = _PARKING_RE.search(text or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _extract_floor(text: str) -> str | None:
    match = _FLOOR_RE.search(text or "")
    if not match:
        return None
    return match.group(1).strip()


def _extract_intent(text: str) -> str | None:
    if _INTENT_REQ_RE.search(text or ""):
        return "BUY"
    if _INTENT_RENT_RE.search(text or ""):
        return "RENT"
    if _INTENT_SALE_RE.search(text or ""):
        return "SELL"
    return None


def _is_signal_line(line: str) -> bool:
    cleaned = _normalize_match_line(line).strip()
    if not cleaned:
        return False
    if _DASH_LINE_RE.match(cleaned):
        return True
    if _NUMBERED_LINE_RE.match(cleaned):
        return True
    if any(_emoji_bullet_re(glyph).match(cleaned) for glyph in _EMOJI_BULLET_GLYPHS):
        return True
    if _BHK_LINE_RE.match(cleaned):
        return True
    return False


def _choose_text_line(lines: list[str]) -> str | None:
    for line in lines:
        anchor_candidate = _anchor_candidate_text(line)
        if anchor_candidate:
            return anchor_candidate
    for line in lines:
        raw = _normalize_match_line(line)
        if not raw:
            continue
        if _is_signal_line(raw):
            continue
        cleaned = _strip_markers(line)
        if not cleaned:
            continue
        if _BOILERPLATE_RE.search(cleaned):
            continue
        if re.search(r"(?i)\b(?:floor|parking|carpet|sqft|rent|sale|furnished|unfurnished|aed|dhs)\b|[\d,.]+\s*[km]\b", cleaned):
            continue
        if len(cleaned.split()) <= 1:
            continue
        return cleaned
    return None


def _strip_markers(line: str) -> str:
    cleaned = line.strip()
    cleaned = re.sub(r"^[*_~]+\s*", "", cleaned)
    cleaned = re.sub(r"^\(\s*\d+\s*\)\s*", "", cleaned)
    cleaned = re.sub(r"^\d+[.)]\s+", "", cleaned)
    cleaned = re.sub(r"^(?:🏡|📍|▪️|▫️|•|‣|➤|👉|👈|➡️|➡|➤|↪️)\s*", "", cleaned)
    cleaned = re.sub(r"[*_~]+$", "", cleaned)
    return cleaned.strip()


def _split_on_predicate(lines: list[str], predicate) -> list[str]:
    chunks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if predicate(line) and current:
            chunks.append(current)
            current = [line]
            continue
        current.append(line)
    if current:
        chunks.append(current)
    return ["\n".join(chunk).strip() for chunk in chunks if "\n".join(chunk).strip()]


def _split_dash_separator(text: str) -> list[str] | None:
    lines = _line_items(text)
    if not any(_DASH_LINE_RE.match(line or "") for line in lines):
      return None
    chunks: list[str] = []
    current: list[str] = []
    for line in lines:
        if _DASH_LINE_RE.match(line or ""):
            if current:
                chunks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    chunks = [chunk for chunk in chunks if _extract_bhk(chunk) or _PRICE_RE.search(chunk) or _AREA_RE.search(chunk)]
    return chunks or None


def _split_numbered(text: str) -> list[str] | None:
    lines = _line_items(text)
    marker_indices = [
        index for index, line in enumerate(lines)
        if _NUMBERED_LINE_RE.match(_normalize_match_line(line or ""))
    ]
    if not marker_indices:
        return None
    # Text before the first numbered item is broadcast context, not part of
    # the first listing. Keeping it in the first chunk shifts trailing names
    # such as ``(2) Brand new`` into the preceding listing.
    body_lines = lines[marker_indices[0]:]
    chunks = _split_on_predicate(
        body_lines,
        lambda line: bool(_NUMBERED_LINE_RE.match(_normalize_match_line(line or ""))),
    )
    chunks = [chunk for chunk in chunks if _extract_bhk(chunk) or _PRICE_RE.search(chunk) or _AREA_RE.search(chunk)]
    return chunks or None


def _split_emoji_bullet(text: str) -> list[str] | None:
    lines = _line_items(text)
    for glyph in _EMOJI_BULLET_GLYPHS:
        glyph_re = _emoji_bullet_re(glyph)
        marker_indices = [
            index for index, line in enumerate(lines)
            if glyph_re.match(_normalize_match_line(line))
        ]
        if len(marker_indices) < 2:
            continue

        # Text before the first property marker is broadcast-level context,
        # not a standalone listing. Carry it into every child block so facts
        # such as "UPDATED 3BHK OUTRIGHT LIST" are inherited without ever
        # becoming their own database row.
        first_marker = marker_indices[0]
        preamble = "\n".join(lines[:first_marker]).strip()
        body_lines = lines[first_marker:]
        chunks = _split_on_predicate(
            body_lines,
            lambda line, glyph_re=glyph_re: bool(glyph_re.match(_normalize_match_line(line)))
            or bool(_LISTING_HEADER_RE.match(_normalize_match_line(line)))
            or _looks_like_anchor_line(line),
        )
        # A building heading can sit between two emoji property rows. The
        # predicate correctly starts a new chunk at that heading, but the
        # following emoji row would otherwise flush the heading into a
        # heading-only chunk that is later discarded. Attach that heading to
        # the next property instead of leaking it into the previous one.
        repaired_chunks: list[str] = []
        index = 0
        while index < len(chunks):
            current_chunk = chunks[index]
            if (
                index + 1 < len(chunks)
                and not (_extract_bhk(current_chunk) or _PRICE_RE.search(current_chunk) or _AREA_RE.search(current_chunk))
                and _looks_like_anchor_line(current_chunk.splitlines()[0] if current_chunk.splitlines() else "")
            ):
                repaired_chunks.append(f"{current_chunk}\n{chunks[index + 1]}".strip())
                index += 2
                continue
            repaired_chunks.append(current_chunk)
            index += 1
        chunks = repaired_chunks
        chunks = [
            chunk
            for chunk in chunks
            if _extract_bhk(chunk) or _PRICE_RE.search(chunk) or _AREA_RE.search(chunk)
        ]
        if len(chunks) >= 2:
            if preamble and (
                _extract_bhk(preamble)
                or _INTENT_RENT_RE.search(preamble)
                or _INTENT_SALE_RE.search(preamble)
                or _INTENT_REQ_RE.search(preamble)
            ):
                chunks = [f"{preamble}\n{chunk}" for chunk in chunks]
            return chunks
    return None


def _split_bare_bhk(text: str) -> list[str] | None:
    lines = _line_items(text)
    start_indices = [
        idx
        for idx, line in enumerate(lines)
        if _BHK_LINE_RE.match(_normalize_match_line(line))
    ]
    if len(start_indices) < 2:
        return None
    chunks: list[str] = []
    for pos, start in enumerate(start_indices):
        end = start_indices[pos + 1] if pos + 1 < len(start_indices) else len(lines)
        chunk = "\n".join(lines[start:end]).strip()
        if chunk:
            chunks.append(chunk)
    chunks = [chunk for chunk in chunks if _extract_bhk(chunk) or _PRICE_RE.search(chunk) or _AREA_RE.search(chunk)]
    if any(_listing_anchor_count(chunk) > 1 for chunk in chunks):
        return None
    return chunks or None


def _split_run_on_inventory(text: str) -> list[str] | None:
    """Split a compact, single-line inventory only when every item is priced.

    This recovers a common WhatsApp formatting failure without pretending that
    arbitrary prose has reliable listing boundaries. The original message is
    retained as parent evidence; these chunks become the child source units.
    """
    value = _compact(text)
    if not value or "\n" in str(text or ""):
        return None
    starts = list(_RUN_ON_LISTING_START_RE.finditer(value))
    if len(starts) < 2:
        return None
    chunks = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(value)
        chunk = value[start.start():end].strip(" -–—,;:|")
        if not chunk or not _PRICE_RE.search(chunk):
            return None
        chunks.append(chunk)
    return chunks if len(chunks) >= 2 else None


def _split_inline_bold(text: str) -> list[str] | None:
    """Split compact broadcasts whose listing names are inline *bold* spans.

    This deliberately requires multiple explicit prices. A single bold
    building name is not enough evidence to split a message. A bold building
    name immediately following a bold BHK header belongs to that same listing
    (for example ``*Available for sale 2Bhk* Building Name: *Pioneer*``).
    """
    value = text or ""
    prices = list(_PRICE_RE.finditer(value))
    if len(prices) < 2:
        return None

    spans = []
    for match in _INLINE_BOLD_RE.finditer(value):
        label = match.group(1).strip()
        has_config = bool(re.search(r"(?i)\b\d+(?:\.\d+)?\s*(?:bhk|br|rk)\b", label))
        if _DECORATIVE_BOLD_RE.match(label) and not has_config:
            continue
        if len(label.split()) <= 6:
            spans.append(match)
    if len(spans) < 2:
        return None

    # A building name after an inline BHK header is part of that listing, not
    # a new boundary. Only suppress it when no price occurs between the pair.
    boundaries = []
    for match in spans:
        if boundaries:
            previous = boundaries[-1]
            previous_label = previous.group(1)
            between = value[previous.end():match.start()]
            previous_has_config = bool(re.search(
                r"(?i)\b\d+(?:\.\d+)?\s*(?:bhk|br|rk)\b", previous_label
            ))
            if previous_has_config and not _PRICE_RE.search(between):
                continue
        boundaries.append(match)

    if len(boundaries) < 2:
        return None
    chunks = []
    for index, start_match in enumerate(boundaries):
        end = boundaries[index + 1].start() if index + 1 < len(boundaries) else len(value)
        chunk = value[start_match.start():end].strip()
        if chunk:
            chunks.append(chunk)
    chunks = [chunk for chunk in chunks if _extract_bhk(chunk) or _PRICE_RE.search(chunk) or _AREA_RE.search(chunk)]
    if len(chunks) < 2 or len(chunks) != len(prices):
        return None

    # A leading bold broadcast header is shared context, not a listing. Keep
    # it in every block so the extractor can retain facts such as
    # "brand new building" instead of losing them when the decorative span is
    # excluded from the boundary list above.
    header = _GLOBAL_HEADER_RE.match(value)
    if header:
        context = header.group(1).strip()
        chunks = [f"{chunk}\n{context}" for chunk in chunks]
    return chunks


def split_message_into_chunks(text: str, preferred_pattern: str | None = None) -> tuple[str | None, list[str]]:
    """Return the best splitter pattern and the resulting chunks.

    The first accepted pattern in :data:`PATTERN_ORDER` wins. A pattern is
    accepted when it yields at least two chunks and the chunk count matches
    the number of BHK-style headers in the message.
    """
    if not text or len(text.strip()) < 10:
        return None, []

    pattern_to_splitter = {
        PATTERN_DASH_SEPARATOR: _split_dash_separator,
        PATTERN_NUMBERED: _split_numbered,
        PATTERN_EMOJI_BULLET: _split_emoji_bullet,
        PATTERN_BARE_BHK: _split_bare_bhk,
        PATTERN_RUN_ON_INVENTORY: _split_run_on_inventory,
        PATTERN_INLINE_BOLD: _split_inline_bold,
    }
    headers = _header_count(text)
    pattern_ids = [preferred_pattern] if preferred_pattern in PATTERN_ORDER else []
    pattern_ids.extend(pid for pid in PATTERN_ORDER if pid != preferred_pattern)
    has_anchor_like_boundary = any(_looks_like_anchor_line(line) for line in _line_items(text))
    for pattern_id in pattern_ids:
        splitter = pattern_to_splitter[pattern_id]
        chunks = splitter(text) or []
        if pattern_id == PATTERN_INLINE_BOLD:
            if len(chunks) >= 2 and len(chunks) == len(list(_PRICE_RE.finditer(text))):
                return pattern_id, chunks
            continue
        if len(chunks) >= 2 and (
            headers == 0
            or len(chunks) == headers
            or (
                pattern_id == PATTERN_EMOJI_BULLET
                and has_anchor_like_boundary
                and len(chunks) == headers + 1
            )
        ):
            if pattern_id == PATTERN_BARE_BHK and any(_listing_anchor_count(chunk) > 1 for chunk in chunks):
                continue
            return pattern_id, chunks
    return None, []


def parse_chunk(chunk: str) -> dict:
    """Extract stable fields from one listing chunk."""
    lines = [line.strip() for line in _line_items(chunk) if line.strip()]
    first_text_line = _choose_text_line(lines)
    # Remove the header marker before scanning the rest of the chunk.
    body = "\n".join(_strip_markers(line) for line in lines)
    bhk = _extract_bhk(body)
    price, price_unit = _extract_price(body)
    intent = _extract_intent(body)
    area_sqft = _extract_area_sqft(body)
    furnishing = _extract_furnishing(body)
    parking = _extract_parking(body)
    floor = _extract_floor(body)

    building_name = None
    location_raw = None
    numbered_marker = bool(lines and _NUMBERED_LINE_RE.match(_normalize_match_line(lines[0])))
    if numbered_marker:
        heading = _strip_markers(lines[0]).strip()
        heading = re.sub(r"^\s*(?:\(\s*\d+\s*\)|\d+[.)])\s*", "", heading).strip()
        heading_parts = re.split(r"\s+[–—-]\s+", heading, maxsplit=1)
        if len(heading_parts) == 2 and all(part.strip() for part in heading_parts):
            left, right = [part.strip() for part in heading_parts]
            if _looks_like_location(right) and (
                not _looks_like_location(left)
                or re.search(r"(?i)\b(?:apartment|residency|estate|tower|mansion|building|heights|society)\b", left)
            ):
                building_name, location_raw = left, right
        elif heading and not _is_signal_line(heading) and not _BHK_LINE_RE.match(heading):
            building_name = heading
        numbered_candidate = next((
            _strip_markers(line).strip()
            for line in lines[1:]
            if _strip_markers(line).strip()
        ), None)
        if numbered_candidate and not re.match(
            r"(?i)^(?:brand\s+new|new|available|modern\s+amenities)$",
            numbered_candidate,
        ) and not _is_signal_line(numbered_candidate) and not _BHK_LINE_RE.match(numbered_candidate):
            if not building_name:
                building_name = numbered_candidate
    labelled_inline_building = re.search(
        r"(?im)\b(?:bildg|bldg|building)\s*(?:name)?\s*[:\-]\s*\*?([^()\n*]+?)\s*\*?(?:\s*\([^\n)]*\))?(?:\n|$)",
        body,
    )
    if labelled_inline_building:
        building_name = labelled_inline_building.group(1).strip()
    elif not numbered_marker:
        first_bold = _INLINE_BOLD_RE.search(lines[0] if lines else "")
        if first_bold:
            label = first_bold.group(1).strip()
            has_config = bool(re.search(r"(?i)\b\d+(?:\.\d+)?\s*(?:bhk|br|rk)\b", label))
            if not _DECORATIVE_BOLD_RE.match(label) and not has_config:
                building_name = label
    labelled_inline_location = re.search(
        r"(?im)^\s*(?:location|loc\.?)\s*[:\-]\s*\*?([^()\n*]+?)\s*\*?(?:\n|$)",
        body,
    )
    if labelled_inline_location and not location_raw:
        location_raw = labelled_inline_location.group(1).strip()
    if first_text_line and not numbered_marker:
        labelled_building = re.search(r"(?i)^\s*(?:building\s*name|building)\s*[:\-]\s*(.+)$", first_text_line)
        labelled_location = re.search(r"(?i)^\s*(?:location|loc\.?)\s*[:\-]\s*(.+)$", first_text_line)
        if labelled_building:
            building_name = labelled_building.group(1).strip()
        elif building_name:
            pass
        elif labelled_location:
            location_raw = first_text_line
        elif re.search(r"(?i)\b(?:near|at|location|loc\.?)\b", first_text_line):
            location_raw = first_text_line
        elif "," in first_text_line:
            left, right = [part.strip() for part in first_text_line.split(",", 1)]
            if left and len(left.split()) >= 2:
                building_name = left
            if right:
                location_raw = right
        else:
            dash_match = re.split(r"\s+[–—-]\s+", first_text_line, maxsplit=1)
            if len(dash_match) == 2:
                left, right = [part.strip() for part in dash_match]
                left_is_location = _looks_like_location(left)
                right_is_location = _looks_like_location(right)
                if left and right and left_is_location != right_is_location:
                    if left_is_location:
                        location_raw = left
                        building_name = right
                    else:
                        building_name = left
                        location_raw = right
                elif left and right:
                    building_name = first_text_line
                elif first_text_line:
                    building_name = first_text_line
            else:
                building_name = first_text_line

    result = {
        "intent": intent,
        "bhk": bhk,
        "price": price,
        "price_unit": price_unit,
        "area_sqft": area_sqft,
        "furnishing": furnishing,
        "car_parking_count": parking,
        "floor_range": floor,
        "building_name": building_name,
        "location_raw": location_raw,
        "micro_market": location_raw,
        "message_type": intent.lower() if intent else "listing",
        "raw_payload": {"full_text": chunk, "slice_text": chunk},
        "normalized_message": _compact(chunk),
        "confidence": 1.0,
        "summary_title": first_text_line or bhk or "Listing",
        "monthly_rent": price if intent == "RENT" else None,
        "total_asking_price": price if intent == "SELL" else None,
    }
    return result


def parse_message(text: str, preferred_pattern: str | None = None) -> tuple[str | None, list[dict]]:
    """Parse a text into structured chunks, or return ``(None, [])``."""
    pattern_id, chunks = split_message_into_chunks(text, preferred_pattern=preferred_pattern)
    if not pattern_id:
        return None, []
    parsed = [parse_chunk(chunk) for chunk in chunks]
    # Inline bold broadcasts often put the only transaction cue in a
    # different chunk (for example a later ``for sale`` header). Propagate a
    # document-level sale cue only when rent language is absent; dual-mode
    # messages remain for AI/contextual handling.
    document_intent = _extract_intent(text)
    if document_intent == "SELL" and not _INTENT_RENT_RE.search(text or ""):
        for item in parsed:
            if not item.get("intent"):
                item["intent"] = "SELL"
                item["message_type"] = "sell"
                item["total_asking_price"] = item.get("price")
    return pattern_id, parsed
