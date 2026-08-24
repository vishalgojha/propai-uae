"""Webhook receiver, ingest endpoints, parsing pipeline, webhook helpers.

Imports from routers.common for storage, require_user, _resolve_active_organization_id, etc.
"""
import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Security
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from routers.common import (
    storage,
    require_user,
    _resolve_active_organization_id,
    _resolve_user_organization_id,
    _normalize_real_phone,
    _group_jid_to_name,
    _today_count,
    _status_file,
    _connection_details,
    _broker_live_statuses,
    _first_ingestor_response,
    _ingestor_auth_headers,
    _table_exists,
    _display_phone_from_whatsapp_id,
    _digits_from_whatsapp_id,
    get_tenant_id,
    set_tenant_id,
)
from storage import (
    RawMessage,
    ParsedObservation,
    ResolverDecision,
    Evaluation,
)
from routers.whatsapp_group_controls import extraction_allowed_for_group
from lab.events import get_bus
from lab.config import FRONTEND_URL

_logger = logging.getLogger(__name__)

router = APIRouter(tags=["infra"])

# ── WhatsApp text sanitizer ────────────────────────────────────────
_UNICODE_DIGIT_MAP = str.maketrans({
    "\u0660": "0", "\u0661": "1", "\u0662": "2", "\u0663": "3", "\u0664": "4",
    "\u0665": "5", "\u0666": "6", "\u0667": "7", "\u0668": "8", "\u0669": "9",
    "\u06F0": "0", "\u06F1": "1", "\u06F2": "2", "\u06F3": "3", "\u06F4": "4",
    "\u06F5": "5", "\u06F6": "6", "\u06F7": "7", "\u06F8": "8", "\u06F9": "9",
    "\u066B": ".", "\u066C": ",",
    # Arabic diacritics (tashkeel) - delete
    "\u064B": None, "\u064C": None, "\u064D": None, "\u064E": None,
    "\u064F": None, "\u0650": None, "\u0651": None, "\u0652": None,
    "\u0653": None, "\u0654": None, "\u0655": None, "\u0670": None,
})

_ARABIC_TOKEN_REWRITES = [
    ("\u0645\u0644\u064a\u0648\u0646(?:\u064a\u0646)?", " mn "),   # million/millions
    ("(?:\u0622\u0644\u0627\u0641|\u0627\u0644\u0627\u0641|\u0623\u0644\u0641|\u0627\u0644\u0641)", " k "),  # thousand(s)
    ("\u062f\u0631\u0647\u0645", " AED "),                              # dirham
    (r"\u062f\s*\.\s*\u0625", " AED "),                                 # d.E abbreviation
]


def normalize_multilingual(text: str) -> str:
    """Normalise Arabic-script digits and currency/unit words so downstream
    regexes can treat English, Arabic and Hinglish messages uniformly."""
    if not isinstance(text, str) or not text:
        return text or ""
    text = text.translate(_UNICODE_DIGIT_MAP)
    for pattern, replacement in _ARABIC_TOKEN_REWRITES:
        text = re.sub(pattern, replacement, text)
    return text


def sanitize_whatsapp_text(text: str) -> str:
    if not isinstance(text, str):
        return str(text) if text is not None else ""
    text = normalize_multilingual(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"^[•\-]\s+", "", text, flags=re.M)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    return text.strip()

# ── Extraction worker pool ─────────────────────────────────────────
_EXTRACTION_WORKERS = max(1, int(os.getenv("WEBHOOK_EXTRACTION_WORKERS", "8")))
_EXTRACTION_PENDING_LIMIT = max(
    _EXTRACTION_WORKERS,
    int(os.getenv("WEBHOOK_EXTRACTION_PENDING_LIMIT", "64")),
)
_EXTRACTION_EXECUTOR = ThreadPoolExecutor(
    max_workers=_EXTRACTION_WORKERS,
    thread_name_prefix="propai-extract",
)
_EXTRACTION_SLOTS = threading.BoundedSemaphore(_EXTRACTION_PENDING_LIMIT)

_RE = __import__("re")

# ── Embedding engine ───────────────────────────────────────────────
_embedder = None

def get_embedder():
    global _embedder
    if _embedder is None:
        from lab.embedding import create_engine
        _embedder = create_engine(prefer_fastembed=False)
    return _embedder

def compute_embedding(parsed: dict) -> bytes | None:
    text = observation_text(parsed)
    if text:
        eng = get_embedder()
        eng.partial_fit([text])
        emb = eng.embed(text)
        from lab.embedding import pack_embedding
        return pack_embedding(emb)
    return None

def observation_text(parsed: dict) -> str:
    parts = []
    for key in ("intent", "bhk", "price", "furnishing", "location_raw", "building_name", "landmark_name", "broker_name"):
        v = parsed.get(key)
        if v:
            parts.append(str(v))
    return " ".join(parts)

# ═══════════════════════════════════════════════════════════════════
# Parsing pipeline helpers
# ═══════════════════════════════════════════════════════════════════

_BUILDING_WORDS = {"building", "tower", "wing", "phase", "house", "apartment", "residency", "enclave", "park", "heights", "vista", "gardens", "court", "plaza", "square", "manor", "estate", "villas", "towers", "complex", "society", "chsl", "co-operative"}
_LOCALITY_KEYWORDS = {"west", "east", "road", "market", "station", "colony", "village", "nagar", "gaon", "pada", "village", "junction", "cross", "link", "avenue", "street", "lane", "marg", "chardis", "circus"}
_FURNISHING_CANONICAL_PATTERNS = [
    (re.compile(r'\b(fully\s+furnished|full\s+furnished|fully\s+fur|f\s*/\s*f|ff)\b', re.I), "fully_furnished"),
    (re.compile(r'\b(semi\s+furnished|semi\s+fur|s\s*/\s*f|sf)\b', re.I), "semi_furnished"),
    (re.compile(r'\b(unfurnished|un\s*furnished|u\s*/\s*f|uf)\b', re.I), "unfurnished"),
    (re.compile(r'\b(plug\s*&\s*play|plug\s+and\s+play)\b', re.I), "plug_and_play"),
    (re.compile(r'\b(bare\s+shell)\b', re.I), "bare_shell"),
]
_CONTACT_RE = re.compile(
    r'(?:(?:contact|call|whatsapp)\s*[*]?)?'
    r'([A-Z][a-zA-Z]+(?: +[A-Z][a-zA-Z]+)*)'
    r'\s*[-:–]+\s*[*]?\+?\s*(\d{5}\s?\d{5}|\d{10})'
)
_EMOJI_CONTACT_RE = re.compile(
    r'📞\s*[*]?'
    r'(?:([A-Z][a-zA-Z]+(?: +[A-Z][a-zA-Z]+)*)\s*[-:–]?\s*[*]?\+?\s*)?'
    r'(\d{5}\s?\d{5}|\d{10})'
)
_NON_NAME_RE = re.compile(r'(?i)\b(?:for|inspection|details|contact|call|whatsapp|more|and|our|the|this|any|all|visit|connect|available|connect|price|rent|sale|bhk|sqft|floor)\b')
_CONTACT_NAME_BLACKLIST = {"for", "inspection", "details", "call", "contact", "whatsapp", "available", "price", "rent", "sale", "bhk"}

def _is_valid_contact_name(name: str) -> bool:
    if len(name) < 2 or len(name) > 40:
        return False
    if _NON_NAME_RE.search(name):
        return False
    return True

def _extract_broker_from_signature(text: str) -> tuple[str | None, str | None]:
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        return None, None
    bad_name_terms = (
        "location", "inspection", "carpet", "config", "configuration", "building",
        "available", "rent", "rental", "sale", "sell", "lease", "price", "budget",
        "deposit", "possession", "notice", "parking", "furnished", "unfurnished",
        "semi", "call", "contact", "whatsapp", "site visit", "client", "requirement",
        "direct inventory", "mandate", "note", "landmark", "road", "sqft", "floor",
    )
    def valid_signature_name(line: str) -> bool:
        cleaned = re.sub(r'[*_`~]', '', line).strip(" -:")
        if len(cleaned) < 3 or len(cleaned) > 45:
            return False
        low = cleaned.lower()
        if any(term in low for term in bad_name_terms):
            return False
        if re.search(r'\d{3,}|@|http|\.com|www|(?:aed|dhs)|\b(?:bhk|br|rk|m|mn|million|k|sqft|sft)\b', low):
            return False
        if cleaned.count(",") or cleaned.count(":"):
            return False
        return bool(_RE.match(r'^[A-Z][A-Za-z .&-]{2,}$', cleaned))
    phone_candidate_re = re.compile(r'(?:\+?971[\s-]*)?(?:50|52|54|55|56|58)(?:[\s-]*\d){7}')
    def normalize_phone_candidate(value: str | None) -> str | None:
        digits = re.sub(r'\D+', '', value or '')
        if len(digits) == 12 and digits.startswith('91'):
            digits = digits[-10:]
        elif len(digits) == 11 and digits.startswith('0'):
            digits = digits[-10:]
        if len(digits) == 10 and re.match(r'^[6-9]\d{9}$', digits):
            return digits
        return None
    name = None
    phone = None
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        phone_match = phone_candidate_re.search(line)
        normalized_phone = normalize_phone_candidate(phone_match.group(0) if phone_match else None)
        if normalized_phone and not phone:
            phone = normalized_phone
            continue
        if not name and valid_signature_name(line):
            if not any(kw in line.lower() for kw in ["realty", "property", "estate", "realtors", "consultancy", "enterprises", "ventures"]):
                name = re.sub(r'[*_`~]', '', line).strip(" -:")
    return name, phone

def _extract_all_contacts(text: str) -> list[dict]:
    contacts: list[dict] = []
    seen_phones: set[str] = set()
    for m in _CONTACT_RE.finditer(text):
        name = m.group(1).strip()
        phone = m.group(2).replace(" ", "")
        if len(phone) != 10:
            continue
        if phone in seen_phones:
            continue
        if not _is_valid_contact_name(name):
            continue
        seen_phones.add(phone)
        contacts.append({"name": name, "phone": phone})
    for m in _EMOJI_CONTACT_RE.finditer(text):
        phone = m.group(2).replace(" ", "")
        if len(phone) != 10:
            continue
        if phone in seen_phones:
            continue
        seen_phones.add(phone)
        name = m.group(1).strip() if m.group(1) else ""
        if not _is_valid_contact_name(name):
            continue
        contacts.append({"name": name, "phone": phone})
    return contacts

def _compute_parser_confidence(parsed: dict) -> float:
    weights = {
        "intent": 0.15, "principal": 0.08, "bhk": 0.14, "price": 0.16,
        "location_raw": 0.16, "micro_market": 0.10, "building_name": 0.08,
        "landmark_name": 0.08, "broker_name": 0.08, "broker_phone": 0.07, "furnishing": 0.05, "area_sqft": 0.05,
    }
    score = 0.0
    for field, weight in weights.items():
        value = parsed.get(field)
        if value and value != "Unknown":
            score += weight
    return round(min(score, 1.0), 2)

def _infer_micro_market(text: str | None) -> str | None:
    if not text:
        return None
    value = text.lower()
    mappings = [
        (r'\bdubai\s+marina\b|\bmarina\b', "Dubai Marina"),
        (r'\bjbr\b|\bjumeirah\s+beach\s+residence\b', "JBR"),
        (r'\bdowntown\b|\bburj\s+khalifa\b', "Downtown Dubai"),
        (r'\bbusiness\s+bay\b', "Business Bay"),
        (r'\bdifc\b', "DIFC"),
        (r'\bpalm\s+jumeirah\b', "Palm Jumeirah"),
        (r'\bjvc\b|\bjumeirah\s+village\s+circle\b', "JVC"),
        (r'\bjvt\b', "JVT"),
        (r'\bjlt\b', "JLT"),
        (r'\bdubai\s+hills\b', "Dubai Hills Estate"),
        (r'\barabian\s+ranches\b', "Arabian Ranches"),
        (r'\bsprings\b', "The Springs"),
        (r'\bmeadows\b', "The Meadows"),
        (r'\bgreens\b', "The Greens"),
        (r'\bal\s+barsha\b|\bbarsha\b', "Al Barsha"),
        (r'\bfurjan\b', "Al Furjan"),
        (r'\bdeira\b', "Deira"),
        (r'\bkarama\b', "Karama"),
        (r'\bmirdif\b', "Mirdif"),
        # Arabic-script aliases
        (r"دبي\s*مارينا", "Dubai Marina"),
        (r"الخليج\s*التجاري", "Business Bay"),
        (r"وسط\s*المدينة|برج\s*خليفة", "Downtown Dubai"),
        (r"نخلة\s*جميرا", "Palm Jumeirah"),
        ("البرشاء", "Al Barsha"),
        ("ديرة", "Deira"),
        ("الكرامة", "Karama"),
        ("مردف", "Mirdif"),
        ("الفوران", "Al Furjan"),
        (r"جميرا\s*بيتش\s*ريزيدنس", "JBR"),
        (r"المركز\s*المالي\s*الدولي", "DIFC"),
    ]
    for pattern, market in mappings:
        if _RE.search(pattern, value):
            return market
    return None

def _normalize_furnishing_canonical(text: str) -> str | None:
    for pattern, canonical in _FURNISHING_CANONICAL_PATTERNS:
        if pattern.search(text):
            return canonical
    return None

def _infer_transaction_type(text: str, intent: str | None = None) -> str | None:
    if intent == "RENT":
        return "rent"
    if intent == "LEASE":
        return "lease"
    if intent == "SELL":
        return "sale"
    if intent == "BUY":
        return "sale"
    if intent == "COMMERCIAL":
        lower = text.lower()
        if re.search(r'\b(for\s+rent|rent|rental|on\s+rent|for\s+lease|on\s+lease)\b|\b(?:\u0644\u0644\u0625\u064a\u062c\u0627\u0631|\u0644\u0644\u0627\u064a\u062c\u0627\u0631|\u0625\u064a\u062c\u0627\u0631|\u0627\u064a\u062c\u0627\u0631)\b', lower):
            return "rent"
        if re.search(r'\b(lease)\b', lower):
            return "lease"
        return "sale"
    lower = text.lower()
    if re.search(r'\b(for\s+rent|rent|rental|on\s+rent|for\s+lease|on\s+lease)\b|\b(?:\u0644\u0644\u0625\u064a\u062c\u0627\u0631|\u0644\u0644\u0627\u064a\u062c\u0627\u0631|\u0625\u064a\u062c\u0627\u0631|\u0627\u064a\u062c\u0627\u0631)\b', lower):
        return "rent"
    if re.search(r'\b(lease)\b', lower):
        return "lease"
    if re.search(r'\b(for\s+sale|sale|sell|selling|resale)\b|\b(?:\u0644\u0644\u0628\u064a\u0639|\u0628\u064a\u0639|\u0634\u0631\u0627\u0621)\b', lower):
        return "sale"
    return None

def _infer_asset_and_property_type(text: str, intent: str | None) -> tuple[str | None, str | None]:
    lower = text.lower()
    commercial_hint = bool(_RE.search(r'\b(commercial|office|shop|showroom|warehouse|godown|retail)\b', lower))
    if intent == "COMMERCIAL" or commercial_hint:
        if "office" in lower:
            return "commercial", "office"
        if "showroom" in lower:
            return "commercial", "showroom"
        if "shop" in lower or "retail" in lower:
            return "commercial", "shop"
        if "warehouse" in lower:
            return "commercial", "warehouse"
        if "godown" in lower:
            return "commercial", "godown"
        if "bare shell" in lower:
            return "commercial", "bare_shell"
        if "plug and play" in lower or "plug & play" in lower:
            return "commercial", "plug_and_play"
        return "commercial", "other"
    if "villa" in lower or "bungalow" in lower:
        return "residential", "villa"
    if "plot" in lower or "land" in lower:
        return "residential", "plot"
    if "independent house" in lower or "independent home" in lower or "independent" in lower:
        return "residential", "independent_house"
    if "studio" in lower:
        return "residential", "studio"
    if "duplex" in lower:
        return "residential", "duplex"
    if "penthouse" in lower:
        return "residential", "penthouse"
    if "jodi" in lower:
        return "residential", "jodi"
    if _RE.search(r'\bflat\b|\bapartment\b|\bhome\b', lower) or _RE.search(r'\d+(?:\.\d+)?\s*bhk', lower):
        return "residential", "apartment"
    return "residential", None

def _extract_timing_fields(text: str) -> dict:
    lower = text.lower()
    result = {
        "availability_status": None, "possession_status": None, "possession_date": None,
        "available_from": None, "ready_by": None, "construction_stage": None,
        "launch_timeline": None, "expected_possession": None,
    }
    available_from_m = re.search(r'(?im)\bavailable\s+from\b\s*[:\-]?\s*(.+)$', text)
    if available_from_m:
        raw = available_from_m.group(1).strip().strip("*_`~ ")
        if raw:
            result["available_from"] = raw
            result["availability_status"] = "coming_soon"
            result["possession_status"] = "Coming Soon"
    possession_m = re.search(r'(?im)\bpossession\b\s*[:\-]?\s*(.+)$', text)
    if possession_m:
        raw = possession_m.group(1).strip().strip("*_`~ ")
        if raw:
            result["possession_date"] = raw
            result["expected_possession"] = raw
            result["construction_stage"] = "under_construction"
            result["availability_status"] = result["availability_status"] or "under_construction"
            result["possession_status"] = result["possession_status"] or "Under Construction"
    if re.search(r'\bunder\s+construction\b', lower):
        result["availability_status"] = result["availability_status"] or "under_construction"
        result["construction_stage"] = result["construction_stage"] or "under_construction"
        result["possession_status"] = result["possession_status"] or "Under Construction"
    elif re.search(r'\bpre[-\s]?launch\b|\bnew\s+launch\b|\bupcoming\s+project\b', lower):
        result["availability_status"] = result["availability_status"] or "coming_soon"
        result["construction_stage"] = result["construction_stage"] or "pre_launch"
        result["possession_status"] = result["possession_status"] or "Coming Soon"
    elif re.search(r'\bready\s+to\s+move\b|\bimmediate\s+possession\b|\bready\b', lower):
        result["availability_status"] = result["availability_status"] or "available"
        result["construction_stage"] = result["construction_stage"] or "ready"
        result["possession_status"] = result["possession_status"] or "Ready"
    if re.search(r'\bavailable\b', lower) and not result["availability_status"]:
        result["availability_status"] = "available"
        result["possession_status"] = "Available"
    ready_by_m = re.search(r'(?im)\bready\s+by\b\s*[:\-]?\s*(.+)$', text)
    if ready_by_m:
        raw = ready_by_m.group(1).strip().strip("*_`~ ")
        if raw:
            result["ready_by"] = raw
            result["availability_status"] = result["availability_status"] or "coming_soon"
    return result

def _extract_price_per_sqft(text: str) -> float | None:
    patterns = [
        r'(?i)(?:rate|price|asking(?:\s+price)?|rent|cost)\s*[:\-]?\s*(?:aed\s*|dhs\s*)?\s*([\d,]+(?:\.\d+)?)\s*(?:\/-\s*)?(?:psf|psft|per\s+sq\.?\s*ft|per\s+sqft|per\s+square\s+foot)\b',
        r'(?i)(?:aed\s*|dhs\s*)?\s*([\d,]+(?:\.\d+)?)\s*(?:\/-\s*)?(?:psf|psft|per\s+sq\.?\s*ft|per\s+sqft|per\s+square\s+foot)\b',
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m and m.group(1).strip():
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None

def _infer_commercial_use_type(text: str) -> str | None:
    lower = text.lower()
    if "office" in lower:
        return "office"
    if "showroom" in lower or "flagship" in lower:
        return "showroom"
    if "shop" in lower or "retail" in lower:
        return "shop"
    if "warehouse" in lower:
        return "warehouse"
    if "godown" in lower:
        return "godown"
    return None

def _infer_fitout_status(text: str, furnishing: str | None = None) -> str | None:
    lower = text.lower()
    if furnishing:
        f = furnishing.lower()
        if "fully" in f:
            return "fully_furnished"
        if "semi" in f:
            return "semi_furnished"
        if "unfurnished" in f:
            return "bare_shell"
    if re.search(r'\bplug\s*&\s*play\b|\bplug\s+and\s+play\b', lower):
        return "plug_and_play"
    if "warm shell" in lower or "warmshell" in lower:
        return "warm_shell"
    if re.search(r'\bbare\s*shell\b', lower):
        return "bare_shell"
    if re.search(r'\bfully\s+furnished\b|\bfully\s+fur\b|\bff\b', lower):
        return "fully_furnished"
    if re.search(r'\bsemi\s+furnished\b|\bsemi\s+fur\b|\bsf\b', lower):
        return "semi_furnished"
    return None

def _infer_occupancy_type(text: str) -> str | None:
    lower = text.lower()
    if re.search(r'\bunder\s+construction\b', lower):
        return "under_construction"
    if re.search(r'\boccupied\b', lower):
        return "occupied"
    if re.search(r'\bvacant\b|\bempty\b', lower):
        return "vacant"
    if re.search(r'\bready\s+to\s+move\b|\bimmediate\s+possession\b', lower):
        return "vacant"
    return None

def _clean_person_name(name: str = "") -> str:
    clean = (name or "").strip()
    if re.fullmatch(r"\+?[\dXx\s().-]{7,}", clean):
        return ""
    clean = re.sub(r"\s*\([^)]*(?:\+?\d|X{2,})[^)]*\)\s*", " ", clean, flags=re.I)
    clean = re.sub(r"\s*\+?[\dXx][\dXx\s().-]{7,}\s*", " ", clean)
    clean = re.sub(r"\s{2,}", " ", clean).strip(" -")
    if re.fullmatch(r"\+?[\dXx\s().-]{7,}", clean):
        return ""
    return clean

def parse_message(raw_text: str, profile_name: str | None = None) -> dict:
    from normalize import preprocess_for_parsing, normalize_whatsapp_message
    text = preprocess_for_parsing(normalize_multilingual(raw_text))
    lower = text.lower()
    normalized_result = normalize_whatsapp_message(raw_text)
    result = {
        "intent": None, "principal": None, "bhk": None, "configuration": None,
        "price": None, "price_unit": None, "price_model": None, "price_per_sqft": None,
        "monthly_rent": None, "total_asking_price": None, "area_sqft": None,
        "furnishing": None, "furnishing_canonical": None, "location_raw": None,
        "building_name": None, "landmark_name": None, "street_name": None, "area": None,
        "micro_market": None, "developer": None, "asset_type": None, "property_type": None,
        "transaction_type": None, "commercial_use_type": None, "fitout_status": None,
        "occupancy_type": None, "floor_range": None, "rent_per_sqft": None,
        "availability_status": None, "possession_status": None, "possession_date": None,
        "available_from": None, "ready_by": None, "construction_stage": None,
        "launch_timeline": None, "expected_possession": None, "deposit": None,
        "lock_in_period": None, "notice_period": None, "lease_term": None,
        "recurring_charges": None, "freehold_status": None, "oc_status": None,
        "broker_name": None, "broker_phone": None, "forwarded": 0, "confidence": 0.0,
        "raw_payload": {}, "normalized_message": normalized_result["cleaned"],
        "team_members": [],
    }
    if _RE.search(r'\b(owner\s*(sale|direct|selling)?|direct\s*owner|owner\s*property)\b', lower):
        result["principal"] = "Owner"
    elif _RE.search(r'\b(client\s*(requirement|need|looking|want)|buyer\s*(requirement|need)|requirement)\b', lower):
        result["principal"] = "Buyer Client"
    else:
        result["principal"] = "Unknown"
    is_need = bool(_RE.search(r'\b(wanted|require|need|looking for|seeking|want to|in need of)\b', lower))
    is_pre_launch = bool(_RE.search(r'\b(pre.?launch|pre.?launching|upcoming project|new launch)\b', lower))
    is_rent = bool(_RE.search(r'\b(rent|rental|on rent|for rent|lease|on lease|for lease|tenant)\b', lower))
    commercial_text = _RE.sub(r'\bpost\s+office\b', ' ', lower)
    is_commercial = bool(_RE.search(r'\b(commercial|office|shop|showroom|warehouse|godown|retail)\b', commercial_text))
    is_sell = bool(_RE.search(r'\b(sale|sell|selling|available|ready to move|resale|for sale)\b', lower))
    is_buy = bool(_RE.search(r'\b(buy|buyer|purchase|wanted|require|need|looking for|seeking|requirement)\b', lower))
    if is_pre_launch:
        result["intent"] = "PRE-LAUNCH"
    elif is_buy:
        result["intent"] = "BUY"
    elif is_commercial and is_rent:
        result["intent"] = "COMMERCIAL"
    elif is_commercial:
        result["intent"] = "COMMERCIAL"
    elif is_rent and is_need:
        result["intent"] = "RENT"
    elif is_rent:
        result["intent"] = "RENT"
    elif is_sell:
        result["intent"] = "SELL"
    elif is_buy:
        result["intent"] = "BUY"
    else:
        result["intent"] = None
    clean_profile_name = _clean_person_name(profile_name or "")
    if clean_profile_name and clean_profile_name.lower() not in ("unknown", ""):
        result["broker_name"] = clean_profile_name
    sig_name, sig_phone = _extract_broker_from_signature(text)
    if sig_name:
        if not result.get("broker_name"):
            result["broker_name"] = sig_name
    result["broker_phone"] = sig_phone
    if not result["broker_phone"]:
        for source in (text, normalize_multilingual(raw_text)):
            phone_match = _RE.search(r'(?<!\d)(?:\+?971|00971|0)?[\s-]?5\d(?:[\s-]?\d){7}(?!\d)', source or "")
            if phone_match:
                result["broker_phone"] = re.sub(r"\D", "", phone_match.group(0))[-9:]
                break
    all_contacts = _extract_all_contacts(text)
    broker_phone_clean = (result.get("broker_phone") or "").replace(" ", "")
    result["team_members"] = [c for c in all_contacts if c.get("name") and c["phone"] != broker_phone_clean]
    if _RE.search(r'\b(forwarded|fw[d]?[:.]?|from:|shared by|sent by)\b', lower):
        result["forwarded"] = 1
    bhk_match = _RE.search(r'(\d+(?:\.\d+)?)\s*(bhk|rk|bedroom|b ed|b e d|\u063a\u0631\u0641(?:\u0629|\u0627\u062a)?)', lower)
    if bhk_match:
        result["bhk"] = bhk_match.group(1) + " BHK"
    elif _RE.search(r'\b(studio)\b', lower):
        result["bhk"] = "Studio"
    if result.get("intent") == "COMMERCIAL":
        result["bhk"] = None
    asset_type, property_type = _infer_asset_and_property_type(text, result.get("intent"))
    result["asset_type"] = asset_type
    result["property_type"] = property_type
    result["transaction_type"] = _infer_transaction_type(text)
    result["configuration"] = result.get("bhk")
    if result["asset_type"] == "commercial":
        result["configuration"] = None
        result["bhk"] = None
    text = re.sub(r"(\d)\s*:\s*(\d+)\s*(m|mn|million|millions|k|thousands?)\b", lambda m: f"{m.group(1)}.{m.group(2)} {m.group(3)}", text, flags=re.I)
    lower = text.lower()
    price_from_explicit_line = False
    if result["price"] is None:
        explicit_price_line = None
        for raw_line in text.splitlines():
            line = sanitize_whatsapp_text(raw_line).strip()
            if not line:
                continue
            if not re.search(r'\b(?:rent|rental|asking\s+price)\b', line, re.I):
                continue
            explicit_price_line = _RE.search(r'(?i)\b(?:rent|rental|asking\s+price)\b\s*[:\-]\s*(?:aed\s*|dhs\s*)?\s*([\d,]+(?:\.\d+)?)\s*(m|mn|millions?|k|thousands?)?\b', line)
            if explicit_price_line:
                break
        if explicit_price_line:
            amount = float(explicit_price_line.group(1).replace(",", ""))
            unit_raw = (explicit_price_line.group(2) or "").lower().rstrip("s")
            if unit_raw in ("m", "mn", "million"):
                result["price"] = amount; result["price_unit"] = "M"
            elif unit_raw in ("k", "thousand"):
                result["price"] = amount; result["price_unit"] = "K"
            else:
                if amount >= 1000000:
                    result["price"] = round(amount / 1000000, 2); result["price_unit"] = "M"
                else:
                    result["price"] = amount; result["price_unit"] = "abs"
            price_from_explicit_line = True
    if result.get("price") is None and not price_from_explicit_line:
        price_match = _RE.search(r'(?:aed\s*|dhs\s*)?\s*([\d,]+(?:\.\d+)?)\s*(m|mn|millions?|k|thousands?)\b', lower)
        if price_match and price_match.group(1).strip():
            try:
                amount = float(price_match.group(1).replace(",", ""))
            except ValueError:
                amount = None
            if amount and amount > 0:
                unit_raw = price_match.group(2).lower().rstrip("s")
                if unit_raw in ("m", "mn", "million"):
                    result["price"] = amount; result["price_unit"] = "M"
                elif unit_raw in ("k", "thousand"):
                    result["price"] = amount; result["price_unit"] = "K"
        else:
            abs_match = _RE.search(r'(?:aed|dhs)\s*([\d,]+(?:\.\d+)?)', lower)
            if abs_match and abs_match.group(1).strip():
                try:
                    amount = float(abs_match.group(1).replace(",", ""))
                except ValueError:
                    amount = None
                if amount and amount > 0:
                    result["price"] = amount; result["price_unit"] = "abs"
    price_per_sqft = _extract_price_per_sqft(text)
    if price_per_sqft is not None:
        result["price_per_sqft"] = price_per_sqft; result["price_model"] = "psf"
    elif result.get("price") is not None:
        result["price_model"] = "total"
    if result.get("transaction_type") == "rent" and result.get("price") is not None:
        unit = (result.get("price_unit") or "").lower()
        price_val = result["price"]
        if unit in ("m", "mn", "million"):
            price_aed = price_val * 1_000_000
        elif unit in ("k", "thousand"):
            price_aed = price_val * 1_000
        else:
            price_aed = price_val
        # An annual rent above AED 10M is implausible stock; treat it as a
        # mislabelled sale price.
        if price_aed >= 10_000_000:
            result["transaction_type"] = "sale"
    if result["asset_type"] == "commercial":
        result["commercial_use_type"] = _infer_commercial_use_type(text)
        result["fitout_status"] = _infer_fitout_status(text, result.get("furnishing"))
        result["occupancy_type"] = _infer_occupancy_type(text)
        result["floor_range"] = result.get("floor_range") or None
        if result.get("price_per_sqft") is not None:
            result["rent_per_sqft"] = result["price_per_sqft"]
    area_match = _RE.search(r'(\d+[\d,]*)\s*(sq\.?\s*ft|sqft|sft|sq\s*feet)', lower)
    if area_match and area_match.group(1).strip():
        result["area_sqft"] = float(area_match.group(1).replace(",", ""))
    if (_RE.search(r'\bfully\s+furnished\b|\bfully\s+fur\b', lower)
        or _RE.search(r'(?<![a-z0-9])f\s*/\s*f(?![a-z0-9])|(?<![a-z0-9])ff(?![a-z0-9])', lower)):
        result["furnishing"] = "Fully Furnished"
    elif (_RE.search(r'\bsemi\s+furnished\b|\bsemi\s+fur\b', lower)
          or _RE.search(r'(?<![a-z0-9])s\s*/\s*f(?![a-z0-9])|(?<![a-z0-9])sf(?![a-z0-9])', lower)):
        result["furnishing"] = "Semi Furnished"
    elif (_RE.search(r'\bun\s*-?\s*furnished\b|\bun\s+furn\b', lower)
          or _RE.search(r'(?<![a-z0-9])u\s*/\s*f(?![a-z0-9])|(?<![a-z0-9])uf(?![a-z0-9])', lower)):
        result["furnishing"] = "Unfurnished"
    result["furnishing_canonical"] = _normalize_furnishing_canonical(text) or _normalize_furnishing_canonical(result.get("furnishing") or "")
    timing_fields = _extract_timing_fields(text)
    result.update(timing_fields)
    if result.get("transaction_type") == "rent" and result.get("price") is not None:
        result["monthly_rent"] = result["price"]
    elif result.get("transaction_type") == "sale" and result.get("price") is not None:
        result["total_asking_price"] = result["price"]
    elif result.get("transaction_type") == "lease" and result.get("price") is not None:
        result["monthly_rent"] = result["price"]
    from lab.location import parse_location
    loc = parse_location(text)
    result["location_raw"] = loc.raw
    result["location"] = loc.to_dict() if hasattr(loc, "to_dict") else {}
    if loc.raw and len(loc.raw) >= 3:
        if loc.landmark:
            result["landmark_name"] = loc.landmark
        elif loc.micro_market:
            result["landmark_name"] = loc.micro_market
        elif loc.building:
            result["landmark_name"] = loc.building
        else:
            raw_words = loc.raw.split()
            has_proper_entity = any(w[0].isupper() and w.lower() not in {
                "large","small","big","new","old","converted","available","spacious",
                "luxury","premium","beautiful","good","great","best","top","super",
                "fine","upper","lower","ground","top","middle","front","rear","corner",
                "end","direct","urgent","immediate","ready","brand","fully","semi",
                "unfurnished","furnished","negotiable","affordable","exclusive","special",
                "rare","independent","private","separate","with","for","to","from","of",
                "by","at","in","on","and","or","the","a","an","is","has","have","this",
                "that","it","its","all","each","every","just","only","also","very","too",
                "more","less","some","any","no","not","up","down","out","off","over",
                "under","through","along","around","about","between","before","after",
            } for w in raw_words)
            if has_proper_entity:
                result["landmark_name"] = loc.raw
        if loc.building:
            result["building_name"] = loc.building
        if not result["building_name"]:
            bm = _RE.search(r'(?:building\s*name\s*[:.]?\s*|building\s*[:.]?\s+|project\s*[:.]?\s+|complex\s*[:.]?\s+)([A-Z][A-Za-z0-9][A-Za-z0-9 .&\'\-/]{2,})', text, _RE.I)
            if bm:
                candidate = bm.group(1).strip().rstrip("., ").split("\n")[0].strip()
                if len(candidate) >= 4 and not _RE.search(r'\b(rent|sale|bhk|sqft|price|call|contact|carpet|built.?up|super\s+area|plot\s+area|road\s+facing|modular|gas\s+pipeline| floor | storey | wing | tower)\b', candidate, _RE.I):
                    result["building_name"] = candidate
        if loc.micro_market:
            result["micro_market"] = loc.micro_market
        else:
            result["micro_market"] = _infer_micro_market(loc.raw) or _infer_micro_market(text)
        if loc.street:
            result["street_name"] = loc.street
    if not result.get("micro_market"):
        result["micro_market"] = _infer_micro_market(text)
    dev_keywords = ["by ", "developer ", "builder ", "promoted by "]
    for kw in dev_keywords:
        idx = lower.find(kw)
        if idx >= 0:
            after = text[idx + len(kw):].strip()
            dev_end = after.find("\n")
            if dev_end > 0:
                after = after[:dev_end]
            result["developer"] = after
            break
    result["confidence"] = _compute_parser_confidence(result)
    result["raw_payload"]["full_text"] = text
    from normalize import preprocess_for_parsing
    result["normalized_message"] = preprocess_for_parsing(raw_text)
    return result

def resolve_parsed(parsed: dict, raw_text: str) -> dict:
    result = {
        "building_id": None, "building_name": None, "landmark_id": None, "landmark_name": None,
        "street_id": None, "street_name": None, "project_id": None, "project_name": None,
        "developer_name": parsed.get("developer"), "micro_market": parsed.get("micro_market"),
        "parser_confidence": parsed.get("confidence", 0.0), "resolver_confidence": 0.0,
        "final_confidence": 0.0, "method": "unresolved", "method_detail": None,
        "candidates": [], "failure_category": None, "error": None,
    }
    name = (parsed.get("landmark_name") or parsed.get("street_name") or parsed.get("building_name") or parsed.get("location_raw") or raw_text)
    area = parsed.get("area") or parsed.get("micro_market") or ""
    developer = parsed.get("developer") or ""
    try:
        # The production source of truth is Supabase.  The historical CSV
        # registry below is retained only as a compatibility fallback for
        # installations which have not applied the database migration yet.
        db_name = parsed.get("building_name") or name
        db_available, db_match = _resolve_building_from_database(db_name)
        if db_available:
            if db_match:
                result["building_id"] = db_match["building_id"]
                result["building_name"] = db_match["building_name"]
                result["resolver_confidence"] = float(db_match["confidence"])
                result["final_confidence"] = round(
                    result["parser_confidence"] * 0.3
                    + result["resolver_confidence"] * 0.7,
                    2,
                )
                result["method"] = db_match["method"]
                result["method_detail"] = db_match.get("method_detail")
                result["candidates"] = db_match.get("candidates", [])
                result["micro_market"] = db_match.get("micro_market") or result["micro_market"]
            else:
                result["failure_category"] = "no_candidates"
                result["method_detail"] = "database_name_candidates_empty"
            return result

        candidates = []
        seen_bids = set()
        landmark_name = parsed.get("landmark_name")
        if landmark_name:
            from evidence.resolver import CACHE as R_CACHE
            from evidence.resolver import _load_landmarks
            _load_landmarks()
            lm_names = R_CACHE.get("landmarks_by_name", {})
            lm_aliases = R_CACHE.get("landmarks_by_alias", {})
            lm_to_bldgs = R_CACHE.get("lm_to_bldgs", {})
            landmarks_list = R_CACHE.get("landmarks_list", [])
            lm_lower = landmark_name.lower().strip()
            matched_lm = lm_names.get(lm_lower) or lm_aliases.get(lm_lower)
            if not matched_lm:
                from difflib import SequenceMatcher
                best_lm = None; best_ratio = 0.0
                for lm in landmarks_list:
                    ratio = SequenceMatcher(None, lm_lower, lm["name"].lower()).ratio()
                    if ratio > best_ratio and ratio >= 0.70:
                        best_ratio = ratio; best_lm = lm
                if best_lm:
                    matched_lm = best_lm
                    result["method_detail"] = f"lm_fuzzy:{best_lm['landmark_id']}"
                else:
                    result["failure_category"] = "unknown_landmark"
                    result["method_detail"] = f"unknown_landmark:{lm_lower}"
            if matched_lm:
                lid = matched_lm["landmark_id"]
                result["landmark_id"] = lid
                result["landmark_name"] = matched_lm.get("name", landmark_name)
                neighbors = lm_to_bldgs.get(lid, [])
                if neighbors:
                    from evidence.resolver import CACHE as R2_CACHE
                    from evidence.resolver import _load_registry
                    _load_registry()
                    buildings = R2_CACHE.get("buildings", {})
                    for link in sorted(neighbors, key=lambda x: x["distance_m"])[:5]:
                        bid = link["building_id"]
                        if bid in seen_bids:
                            continue
                        seen_bids.add(bid)
                        b_info = None
                        for cname, info in buildings.items():
                            if info["building_id"] == bid:
                                b_info = {"canonical_name": info["canonical_name"], "area": info.get("area", ""), "developer": info.get("developer", "")}
                                break
                        conf = max(0.50, 1.0 - (link["distance_m"] / 2000))
                        reasons = [f"{link['distance_m']}m from {matched_lm['name']}"]
                        if b_info and b_info.get("area"):
                            if area and b_info["area"].lower() == area.lower():
                                reasons.append(f"Same area: {area}")
                        candidates.append({
                            "building_id": bid, "building_name": b_info["canonical_name"] if b_info else f"Building #{bid}",
                            "confidence": round(conf, 2), "reasons": reasons, "method": f"lm:{lid}",
                            "landmark_id": lid, "landmark_name": matched_lm.get("name"),
                            "distance_m": link["distance_m"], "micro_market": b_info.get("area", "") if b_info else None,
                        })
                else:
                    result["failure_category"] = "no_nearby_buildings"
                    if not result["method_detail"]:
                        result["method_detail"] = f"lm_no_buildings:{lid}"
        from evidence.resolver import resolve as core_resolve
        bid, conf, method = core_resolve(name, area, developer)
        street_name = parsed.get("street_name")
        if street_name and not any(c["building_id"] == bid for c in candidates if bid):
            from evidence.resolver import resolve_by_street
            street_bids = resolve_by_street(street_name)
            if street_bids:
                from evidence.resolver import CACHE as R3_CACHE
                from evidence.resolver import _load_registry
                _load_registry()
                buildings = R3_CACHE.get("buildings", {})
                for sbid in street_bids[:5]:
                    if sbid in seen_bids:
                        continue
                    seen_bids.add(sbid)
                    b_info = None
                    for cname, info in buildings.items():
                        if info["building_id"] == sbid:
                            b_info = {"canonical_name": info["canonical_name"], "area": info.get("area", "")}
                            break
                    candidates.append({
                        "building_id": sbid, "building_name": b_info["canonical_name"] if b_info else f"Building #{sbid}",
                        "confidence": 0.75, "reasons": [f"On street: {street_name}"], "method": f"street:{street_name}",
                        "landmark_id": None, "landmark_name": None, "distance_m": None, "micro_market": b_info.get("area", "") if b_info else None,
                    })
        if bid and bid not in seen_bids:
            from evidence.resolver import CACHE as R4_CACHE
            from evidence.resolver import _load_registry
            _load_registry()
            buildings = R4_CACHE.get("buildings", {})
            b_name = None; b_area = None
            for cname, info in buildings.items():
                if info["building_id"] == bid:
                    b_name = info["canonical_name"]; b_area = info.get("area"); break
            candidates.append({
                "building_id": bid, "building_name": b_name or f"Building #{bid}",
                "confidence": round(conf, 2), "reasons": [f"Resolver match: {method}"], "method": method,
                "landmark_id": result.get("landmark_id"), "landmark_name": result.get("landmark_name"),
                "distance_m": None, "micro_market": b_area or area or None,
            })
            seen_bids.add(bid)
        candidates.sort(key=lambda x: -x["confidence"])
        if candidates:
            winner = None
            if bid and bid > 0:
                for c in candidates:
                    if c["building_id"] == bid:
                        winner = c; break
            if not winner:
                winner = candidates[0]; bid = winner["building_id"]; conf = winner["confidence"]; method = winner["method"]
            result["building_id"] = winner["building_id"]; result["building_name"] = winner["building_name"]
            result["micro_market"] = winner.get("micro_market") or result.get("micro_market")
            result["resolver_confidence"] = max(c["confidence"] for c in candidates if c["building_id"] == bid) if bid else 0.0
            result["final_confidence"] = round(result["parser_confidence"] * 0.3 + result["resolver_confidence"] * 0.7, 2)
            result["method"] = method; result["method_detail"] = method
        else:
            result["resolver_confidence"] = 0.0
            # Parser confidence says how well the message was parsed; it does
            # not mean that a building was resolved.
            result["final_confidence"] = 0.0
            if not result["failure_category"]:
                result["failure_category"] = "no_candidates"; result["method_detail"] = "no_candidates_found"
        result["candidates"] = candidates
    except Exception as e:
        result["method"] = "error"; result["error"] = str(e); result["failure_category"] = "resolver_error"
    return result


def _resolve_building_from_database(name: str) -> tuple[bool, dict | None]:
    """Call the tenant-aware SQL resolver.

    Returns ``(False, None)`` only when the migration/RPC is unavailable, so
    old deployments can continue using the compatibility registry.  A valid
    empty result is ``(True, None)`` and must not fall through to CSV IDs.
    """
    if not name or not str(name).strip():
        return True, None
    try:
        from routers.common import storage, get_tenant_id

        params = {"p_name": str(name).strip()}
        tenant_id = get_tenant_id()
        if tenant_id:
            params["p_tenant_id"] = tenant_id
        response = storage.client.rpc("resolve_building_name", params).execute()
        payload = response.data
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if not isinstance(payload, dict) or not payload.get("building_id"):
            return True, None
        return True, payload
    except Exception as exc:
        _logger.debug("Database building resolver unavailable: %s", exc)
        return False, None

def evaluate_parsed(raw_id: int, parsed: dict, expected: Optional[dict] = None):
    ev = Evaluation(raw_message_id=raw_id)
    extract_map = {"intent": "extracted_intent", "principal": "extracted_principal", "bhk": "extracted_bhk",
        "price": "extracted_price", "price_unit": "extracted_price_unit", "area_sqft": "extracted_area_sqft",
        "furnishing": "extracted_furnishing", "building_name": "extracted_building", "landmark_name": "extracted_landmark",
        "street_name": "extracted_street", "area": "extracted_area", "micro_market": "extracted_micro_market",
        "developer": "extracted_developer", "broker_name": "extracted_broker"}
    for ek, fn in extract_map.items():
        setattr(ev, fn, parsed.get(ek))
    expected_map = {"intent": "expected_intent", "principal": "expected_principal", "bhk": "expected_bhk",
        "price": "expected_price", "price_unit": "expected_price_unit", "area_sqft": "expected_area_sqft",
        "furnishing": "expected_furnishing", "building_name": "expected_building", "landmark_name": "expected_landmark",
        "street_name": "expected_street", "area": "expected_area", "micro_market": "expected_micro_market",
        "developer": "expected_developer", "broker_name": "expected_broker"}
    for ek, fn in expected_map.items():
        setattr(ev, fn, (expected or {}).get(ek))
    if expected:
        correct = 0; total = 0
        for ek in expected_map:
            exp = expected.get(ek); ext = parsed.get(ek)
            if exp is not None:
                total += 1
                if str(exp).strip().lower() == str(ext).strip().lower():
                    correct += 1
        ev.accuracy_overall = round(correct / max(total, 1), 4) if total > 0 else None
        ev.evaluated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return storage.save_evaluation(ev)

# ═══════════════════════════════════════════════════════════════════
# Webhook handler helpers
# ═══════════════════════════════════════════════════════════════════

_EVENT_CLASS = {
    "messages.upsert": "message", "MESSAGES_UPSERT": "message", "MESSAGES_SET": "message",
    "messages.update": "message", "messages.delete": "system", "connection.update": "connection",
    "qrupdated": "qr", "QR_UPDATED": "qr", "groups.upsert": "group", "groups.update": "group",
    "groups.participants.update": "group", "GROUPS_REFRESHED": "group", "CONVERSATIONS_UPSERT": "conversation",
    "presence.update": "presence", "call": "call",
}

def _classify_webhook_event(event: str, data: dict) -> str:
    base = _EVENT_CLASS.get(event, "system")
    if base == "message":
        msg_data = data.get("data", data)
        if not isinstance(msg_data, dict):
            return "system"
        msg = msg_data.get("message", {})
        has_text = bool(msg.get("conversation") or (msg.get("extendedTextMessage") or {}).get("text")
                       or msg.get("imageMessage") or msg.get("videoMessage") or msg.get("audioMessage") or msg.get("documentMessage"))
        if not has_text and not msg:
            return "system"
    return base

def _whatsapp_message_text(msg: dict) -> str:
    return (msg.get("conversation", "") or (msg.get("extendedTextMessage") or {}).get("text", "")
            or (msg.get("imageMessage") or {}).get("caption", "") or (msg.get("videoMessage") or {}).get("caption", "")
            or (msg.get("documentMessage") or {}).get("caption", "") or (msg.get("documentMessage") or {}).get("fileName", "")
            or ("[Voice message]" if msg.get("audioMessage") else "") or ("[Image]" if msg.get("imageMessage") else "")
            or ("[Video]" if msg.get("videoMessage") else "") or ("[Document]" if msg.get("documentMessage") else "")
            or ("[Sticker]" if msg.get("stickerMessage") else "") or "")

def _whatsapp_message_type(msg: dict) -> str:
    for mt in ("image", "video", "audio", "document", "sticker"):
        if msg.get(f"{mt}Message"):
            return mt
    return "text"

def _whatsapp_attachment_metadata(msg: dict, media: dict | None = None) -> dict:
    media = media or {}
    typed = next((msg.get(f"{kind}Message") or {} for kind in ("image", "video", "audio", "document", "sticker") if msg.get(f"{kind}Message")), {})
    return {"image": bool(msg.get("imageMessage")), "video": bool(msg.get("videoMessage")),
            "audio": bool(msg.get("audioMessage")), "document": bool(msg.get("documentMessage")),
            "sticker": bool(msg.get("stickerMessage")), "mime_type": typed.get("mimetype", ""),
            "file_name": (msg.get("documentMessage") or {}).get("fileName", ""),
            "storage_path": media.get("storage_path", ""), "file_length": media.get("file_length"),
            "capture_error": media.get("error", "")}

def _is_blocked_whatsapp_conversation(jid: str) -> bool:
    jid = (jid or "").strip().lower()
    return (not jid or jid == "status@broadcast" or jid == "broadcast"
            or jid.endswith("@broadcast") or jid.endswith("@newsletter"))

def _coerce_whatsapp_timestamp(value) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return ""
            if re.fullmatch(r"\d+(?:\.\d+)?", stripped):
                value = float(stripped)
            else:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
                return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(value, (int, float)):
            seconds = float(value)
            if seconds > 10_000_000_000:
                seconds = seconds / 1000
            return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""
    return ""

_REAL_ESTATE_SIGNAL_RE = re.compile(r"\b(bhk|rk|bed(?:room)?|flat|apartment|office|shop|showroom|warehouse|godown|retail|commercial|carpet|built\s*up|sq\.?\s*ft|sqft|sft|floor|parking|society|chsl|tower|building|project|villa|bungalow|duplex|penthouse|plot|land|tenant|landlord|possession|pre\s*launch)\b", re.IGNORECASE)
_REAL_ESTATE_ACTION_RE = re.compile(r"\b(sale|sell|selling|resale|available|rent|rental|lease|leased|buyer|buy|purchase|wanted|require|requirement|looking\s+for|need|seeking)\b", re.IGNORECASE)
_NON_REAL_ESTATE_TOPIC_RE = re.compile(r"\b(stock|stocks|share|shares|equity|trading|investing|investor|wealth\s+creation|mutual\s+fund|nifty|sensex|portfolio|crypto|bitcoin|ipo|jhunjhunwala)\b", re.IGNORECASE)

def _parsed_source_text(parsed: dict, fallback: str = "") -> str:
    raw_payload = parsed.get("raw_payload")
    if isinstance(raw_payload, dict) and isinstance(raw_payload.get("full_text"), str):
        return raw_payload["full_text"]
    return fallback or ""

def _parsed_has_market_anchor(parsed: dict, raw_text: str = "") -> bool:
    text = raw_text or _parsed_source_text(parsed)
    has_property_signal = bool(_REAL_ESTATE_SIGNAL_RE.search(text))
    has_market_action = bool(_REAL_ESTATE_ACTION_RE.search(text))
    has_non_real_estate_topic = bool(_NON_REAL_ESTATE_TOPIC_RE.search(text))
    if has_non_real_estate_topic and not has_property_signal:
        return False
    if parsed.get("bhk") or parsed.get("area_sqft") or parsed.get("building_name"):
        return True
    if parsed.get("micro_market"):
        return has_property_signal or has_market_action or bool(parsed.get("price"))
    return has_property_signal and (has_market_action or bool(parsed.get("price")))

def generate_summary_title(parsed: dict, raw_text: str = "") -> str | None:
    # A classifier route is not enough evidence for a property title.  Keep
    # the row auditable, but do not turn an unstructured message into the
    # misleading generic label "Property for sale".
    if not _parsed_has_market_anchor(parsed, raw_text):
        return None
    normalized_text = normalize_multilingual(raw_text)
    lower = normalized_text.lower()
    intent = (parsed.get("intent") or "").upper()
    message_type = (parsed.get("message_type") or "").upper()
    def clean_label(value):
        text = re.sub(r"\s+", " ", str(value or "").replace("_", " ")).strip(" ,|-\n\t")
        if text.upper() in {
            "", "UNKNOWN", "NOT KNOWN", "NOT SPECIFIED", "NOT AVAILABLE",
            "NOT IDENTIFIED", "NOT FOUND", "N/A", "NA", "LISTING",
            "REQUIREMENT", "PROPERTY", "TEXT", "NONE", "NULL", "NIL",
        }:
            return ""
        return text
    def format_price(value, unit: str = "") -> str:
        if isinstance(value, str):
            already_formatted = re.sub(r"\s+", " ", value).strip()
            if re.search(r"(?i)(?:aed|dhs|dirham).*(?:\bm\b|\bmn\b|\bmillion\b|\bk\b)", already_formatted):
                return already_formatted
            value = re.sub(r"[^\d.]", "", already_formatted)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        if number <= 0:
            return ""
        normalized_unit = clean_label(unit).lower()
        if normalized_unit in {"m", "mn", "million", "millions"}:
            return f"AED {number:g}M"
        if normalized_unit in {"k", "thousand"}:
            return f"AED {number:g}K"
        if number >= 1_000_000:
            return f"AED {number / 1_000_000:g}M"
        if number >= 1_000:
            return f"AED {round(number / 1_000):g}K"
        return ""
    prop_type = clean_label(parsed.get("property_type"))
    prop_pats = [(r'\bflat\b',"Flat"),(r'\bapartment\b',"Apartment"),(r'\bpenthouse\b',"Penthouse"),
                 (r'\bduplex\b',"Duplex"),(r'\bstudio\b',"Studio"),(r'\bbungalow\b',"Bungalow"),
                 (r'\bvilla\b',"Villa"),(r'\bhouse\b',"House"),(r'\bshop\b',"Shop"),
                 (r'\bshowroom\b',"Showroom"),(r'\brestaurant\b',"Restaurant"),(r'\bgym\b',"Gym"),
                 (r'\bgymkhana\b',"Gymkhana"),(r'\bsalon\b',"Salon"),(r'\bclinic\b',"Clinic"),
                 (r'\boutlet\b',"Outlet"),(r'\bretail\b',"Retail"),(r'\bcafe\b',"Cafe"),
                 (r'\bhotel\b',"Hotel"),(r'\b(?:marriage|banquet|party)\s*hall\b',"Banquet Hall"),
                 (r'\bhall\b',"Hall"),(r'\b(?:co[- ])?working|shared\s+office\b',"Co-Working"),
                 (r'\b(?:office|commercial)\b',"Commercial Office"),(r'\bgodown\b',"Godown"),
                 (r'\bwarehouse\b',"Warehouse"),(r'\bfactory\b',"Factory"),(r'\bworkshop\b',"Workshop"),
                 (r'\bplot\b',"Plot"),(r'\bland\b',"Land"),(r'\bsite\b',"Site")]
    if not prop_type:
        for pat, label in prop_pats:
            if re.search(pat, lower):
                prop_type = label; break
    trans_type = clean_label(parsed.get("transaction_type")).upper()
    if re.search(r'\bpre.?leased?\b', lower):
        trans_type = "PRE-LEASED"
    elif re.search(r'\bleased?\b', lower):
        trans_type = "LEASE"
    elif re.search(r'\bfor\s+sale\b', lower) or re.search(r'\bonsale\b', lower):
        trans_type = "SALE"
    elif re.search(r'\b(?:for\s+)?rent\b', lower):
        trans_type = "RENT"
    if not trans_type:
        if intent in {"RENT", "LEASE", "RENTAL_SEEKER"}:
            trans_type = "RENT"
        elif intent in {"SELL", "SALE", "BUY", "BUYER"}:
            trans_type = "SALE"
    bhk = clean_label(parsed.get("bhk") or parsed.get("configuration"))
    if re.fullmatch(r"\d+(?:\.\d+)?", bhk):
        bhk = f"{bhk} BHK"
    listing_count = parsed.get("listing_count")
    try:
        listing_count = int(listing_count) if listing_count is not None else None
    except (TypeError, ValueError):
        listing_count = None
    if listing_count and listing_count > 1 and bhk:
        bhk = f"{listing_count} × {bhk}"
    furnishing = clean_label(parsed.get("furnishing") or parsed.get("furnishing_canonical"))
    furnishing = re.sub(r"\bsemi furnished\b", "semi-furnished", furnishing, flags=re.IGNORECASE)
    subject = bhk.upper() if bhk and len(bhk) <= 8 else bhk
    if prop_type and (not subject or prop_type.lower() not in subject.lower()):
        subject = f"{subject} {prop_type}".strip()
    subject = subject or ("commercial property" if (parsed.get("asset_type") or "").lower() == "commercial" else "property")
    try:
        area_sqft = float(parsed.get("area_sqft") or 0)
    except (TypeError, ValueError):
        area_sqft = 0
    area_value = f"{int(area_sqft):,}" if area_sqft.is_integer() else f"{area_sqft:,.2f}".rstrip("0").rstrip(".")
    area_text = f"{area_value} sqft" if area_sqft > 0 else ""
    loc = parsed.get("micro_market") or parsed.get("location_raw") or parsed.get("area")
    if not loc:
        loc_pats = [r'Andheri\s*\(\s*[EW]\s*\)',r'Andheri\s+(?:East|West)',r'Bandra\s+(?:East|West)',
                    r'Bandra',r'Juhu',r'Khar\s+(?:East|West)',r'Khar',r'Dadar',r'Worli',
                    r'Malad\s+(?:East|West)',r'Powai',r'Goregaon\s+(?:East|West)',
                    r'Kandivali\s+(?:East|West)',r'Borivali\s+(?:East|West)',r'Dombivli',r'Thane',
                    r'Navi\s+Mumbai',r'Nerul',r'Vashi',r'Panvel',r'Chembur',r'Kurla',r'Ghatkopar',
                    r'Vile\s+Parle',r'Lower\s+Para?l',r'Prabhadevi',r'Marine\s+Lines?',
                    r'Colaba',r'Churchgate',r'Fort',r'Byculla',r'Mahim',r'Matunga',r'Sion',r'Wadala',
                    r'Dahisar',r'Mira\s+Road',r'Bhayandar',r'Vasai',r'Virar',r'Kalyan']
        for pat in loc_pats:
            lm = re.search(pat, raw_text, re.IGNORECASE)
            if lm:
                loc = re.sub(r'\(\s*([EW])\s*\)', lambda m: {"E":"East","W":"West"}.get(m.group(1).upper(), m.group(1)), lm.group(0)).replace("_", " ").strip()
                break
    loc = clean_label(loc)
    bldg = parsed.get("building_name")
    if not bldg:
        first_line = raw_text.split("\n")[0].strip()
        bm = re.search(r'["\u201C\u201D]([^"\u201C\u201D]{3,50})["\u201C\u201D]', first_line)
        if bm:
            cand = bm.group(1).strip().strip("_").strip()
            if cand and not re.search(r'(price|aed|dhs|sqft|floor|contact|call|property|available|building|tower)', cand, re.IGNORECASE):
                bldg = cand
    bldg = clean_label(bldg)
    places = []
    for place in (bldg, loc):
        if place and not any(place.casefold() in existing.casefold() or existing.casefold() in place.casefold() for existing in places):
            places.append(place)
    place_text = ", ".join(places)
    is_requirement = message_type == "REQUIREMENT" or intent in {"BUY","BUYER","REQUIREMENT","RENTAL_SEEKER","WANTED"}
    is_rent = trans_type in {"RENT","LEASE","RENTAL"}
    # For rent titles, the canonical monthly_rent is authoritative. The raw
    # AI price may still be a shorthand token such as 1.85 with unit=Lakh;
    # formatting that token again is how 1.85L became 18.5L in titles.
    if is_rent and parsed.get("monthly_rent"):
        price_text = format_price(parsed.get("monthly_rent"), "abs")
    else:
        price_text = format_price(
            parsed.get("price") or parsed.get("total_asking_price"),
            parsed.get("price_unit") or "",
        )
    furnishing_clean = (furnishing or "").strip().lower()
    furnishing_clean = "" if furnishing_clean in {"none", "null", "unknown", ""} else furnishing_clean
    descriptor = " ".join(part for part in (furnishing_clean, subject) if part).strip()
    if area_text:
        descriptor += f" with {area_text}"
    article = "an" if descriptor[:1].lower() in "aeiou" else "a"
    if is_requirement:
        title = f"Looking to {'rent' if is_rent else 'buy'} {article} {descriptor}"
        if place_text:
            title += f" in {place_text}"
        if price_text:
            title += f" with a {'monthly ' if is_rent else ''}budget of {price_text}"
    else:
        title = descriptor[:1].upper() + descriptor[1:]
        title += f" for {'rent' if is_rent else 'sale'}"
        if place_text:
            title += f" at {place_text}"
        if price_text:
            title += f" for {price_text}{' per month' if is_rent else ''}"
    return re.sub(r"\s+", " ", title).strip()

def _demote_weak_property_parse(parsed: dict, raw_text: str = "") -> dict:
    if _parsed_has_market_anchor(parsed, raw_text):
        return parsed
    cleaned = dict(parsed)
    for key in ("intent","price_unit","furnishing","location_raw","location","landmark_name","street_name","area","developer","broker_name","broker_phone"):
        cleaned[key] = None
    cleaned["price"] = None; cleaned["confidence"] = 0.0
    return cleaned

def _canonical_phone_from_jid(jid: str = "") -> str:
    digits = _digits_from_whatsapp_id(jid)
    if not digits:
        return ""
    if digits.startswith("91") and len(digits) >= 12:
        return digits[:12]
    if len(digits) >= 10:
        return digits[-10:]
    return digits

def _masked_phone_from_digits(digits: str = "") -> str:
    if not digits:
        return ""
    if digits.startswith("91") and len(digits) >= 12:
        return f"+91 {digits[2:4]}{'X'*6}{digits[10:12]}"
    if len(digits) >= 10:
        country = digits[:-10]; local = digits[-10:]
        return f"+{country} {local[:2]}{'X'*6}{local[-2:]}" if country else f"{local[:2]}{'X'*6}{local[-2:]}"
    return f"+{digits}"

def _phone_from_jid(jid: str = "") -> str:
    return _masked_phone_from_digits(_digits_from_whatsapp_id(jid))

def _format_whatsapp_sender(name: str = "", jid: str = "", phone: str = "") -> str:
    clean_name = (name or "").strip()
    display_phone = _masked_phone_from_digits(phone) or ("" if str(jid).endswith("@lid") else _phone_from_jid(jid))
    return clean_name or display_phone or "unknown"

def _resolve_group_name(jid: str) -> str:
    if not jid:
        return jid
    if jid.endswith("@g.us"):
        try:
            if _table_exists("sync_jobs"):
                row = storage.db.execute(
                    "SELECT group_name FROM sync_jobs WHERE group_id = ? AND group_name IS NOT NULL AND group_name != '' LIMIT 1",
                    (jid,)).fetchone()
                if row and row[0] and row[0] != jid:
                    return row[0]
        except Exception:
            pass
        return _group_jid_to_name(jid)
    if jid.endswith("@s.whatsapp.net") or jid.endswith("@lid"):
        return ""
    return jid

_PROFILE_PICTURE_FETCHED: set[str] = set()
_PROFILE_PICTURE_LOCK = asyncio.Lock()

async def _maybe_fetch_profile_picture(jid: str, broker_id: str, tenant_id: str):
    if not jid or "@s.whatsapp.net" not in jid:
        return
    async with _PROFILE_PICTURE_LOCK:
        if jid in _PROFILE_PICTURE_FETCHED:
            return
        _PROFILE_PICTURE_FETCHED.add(jid)
    try:
        phone = jid.split("@")[0].replace("+", "").strip()
        cached = storage.get_profile_photo(jid, tenant_id=tenant_id) if hasattr(storage, "get_profile_photo") else None
        if cached and cached.get("profile_photo_url") and cached.get("profile_photo_fetched_at"):
            try:
                fetched_at = datetime.fromisoformat(str(cached["profile_photo_fetched_at"]))
                if (datetime.now(timezone.utc) - fetched_at).total_seconds() < 6 * 3600:
                    return
            except Exception:
                pass
        _, resp = await _first_ingestor_response("GET", f"/profile-picture?jid={jid}" + (f"&broker_id={broker_id}" if broker_id else ""), timeout=8)
        if resp is None or resp.status_code >= 300:
            return
        data = resp.json()
        pic_url = data.get("url", "")
        if pic_url and data.get("ok"):
            storage.update_profile_photo(jid, pic_url, data.get("id", ""), tenant_id=tenant_id)
    except Exception:
        pass

def _process_single_raw(raw_id: int, ctx: dict):
    from extraction import process_raw_message
    process_raw_message(raw_id, ctx)

def _handle_system_event(event_class: str, event: str, data: dict, instance: str, tenant_id: str | None = None):
    if not tenant_id:
        print(f"[webhook] WARN: skipping system event {event} — no tenant resolved", flush=True)
        return
    msg_data = data.get("data", data)
    if event.startswith("WHATSAPP_") or event_class in {"presence", "call"}:
        try:
            storage.db.execute(
                """INSERT INTO whatsapp_events (tenant_id, broker_id, event_type, chat_jid, sender_jid, message_ids, payload, occurred_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (tenant_id, (msg_data.get("broker_id","") if isinstance(msg_data,dict) else ""), event,
                 (msg_data.get("chat_jid","") if isinstance(msg_data,dict) else ""),
                 (msg_data.get("sender_jid","") if isinstance(msg_data,dict) else ""),
                 json.dumps(msg_data.get("message_ids",[]) if isinstance(msg_data,dict) else []),
                 json.dumps(data),
                 _coerce_whatsapp_timestamp(msg_data.get("timestamp") if isinstance(msg_data,dict) else None) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        except Exception as exc:
            print(f"[webhook] whatsapp event persistence failed: {exc}", flush=True)
    if event_class == "connection":
        state = ""
        if isinstance(msg_data, dict):
            state = msg_data.get("state", "")
        get_bus().publish("connection.changed", {"instance": instance, "state": state})
    elif event_class == "conversation":
        conversations = data.get("conversations") or []
        broker_id = (msg_data.get("broker_id","") if isinstance(msg_data,dict) else "") or "legacy"
        try:
            persisted = storage.upsert_whatsapp_conversations(tenant_id, broker_id, instance, conversations)
            get_bus().publish("whatsapp.conversations.updated", {"instance": instance, "broker_id": broker_id, "count": persisted})
        except Exception as exc:
            print(f"[webhook] conversation directory persistence failed: {exc}", flush=True)
    elif event_class == "group":
        groups_list = data.get("groups") or (msg_data if isinstance(msg_data, list) else [msg_data])
        incoming_jids = set()
        directory_rows = []
        broker_id = (msg_data.get("broker_id","") if isinstance(msg_data,dict) else "") or "legacy"
        is_full_refresh = bool(data.get("groups"))
        for g in groups_list:
            if not isinstance(g, dict):
                continue
            jid = g.get("id") or g.get("remoteJid") or ""
            if not jid:
                continue
            incoming_jids.add(jid)
            name = g.get("name") or g.get("subject") or jid
            raw_participants = g.get("participants", []) if isinstance(g.get("participants"), list) else []
            participants = len(raw_participants) if raw_participants else g.get("size", 0)
            directory_rows.append({"jid": jid, "type": "group", "name": name, "message_count": 0, "source": "group_directory", "metadata": {"participants": participants}})
            participant_rows = []
            participant_jids = set()
            for participant in raw_participants:
                if not isinstance(participant, dict):
                    continue
                member_jid = str(participant.get("id") or participant.get("phone_jid") or participant.get("lid") or "").strip()
                if not member_jid:
                    continue
                participant_jids.add(member_jid)
                phone_source = str(participant.get("phone_jid") or participant.get("id") or participant.get("lid") or "").strip()
                participant_rows.append({"member_jid": member_jid, "member_phone": storage._normalize_phone(phone_source) or None, "display_name": str(participant.get("display_name") or "").strip() or None, "is_admin": bool(participant.get("is_admin") or participant.get("is_super_admin"))})
            try:
                storage.upsert_sync_job(source="whatsapp", instance=instance, group_id=jid, group_name=name, participants=participants, status="complete")
            except Exception as e:
                print(f"  upsert sync job failed for {jid}: {e}")
            if participant_rows:
                try:
                    storage.upsert_group_members(tenant_id, jid, participant_rows)
                except Exception as exc:
                    print(f"[webhook] group member persistence failed for {jid}: {exc}", flush=True)
            if is_full_refresh:
                try:
                    removed_members = storage.prune_group_members(tenant_id=tenant_id, group_id=jid, keep_member_jids=participant_jids)
                    if removed_members:
                        print(f"  Removed {removed_members} stale members for {jid}")
                except Exception as exc:
                    print(f"[webhook] prune group members failed for {jid}: {exc}", flush=True)
            get_bus().publish("group.updated", {"instance": instance, "jid": jid, "name": name, "participants": participants})
        if directory_rows:
            try:
                storage.upsert_whatsapp_conversations(tenant_id, broker_id, instance, directory_rows)
            except Exception as exc:
                print(f"[webhook] group directory conversation persistence failed: {exc}", flush=True)
        if is_full_refresh:
            try:
                removed_conversations = storage.prune_whatsapp_conversations(
                    tenant_id=tenant_id,
                    broker_id=broker_id,
                    keep_jids=incoming_jids,
                    conversation_types={"group"},
                )
                if removed_conversations:
                    print(f"  Removed {removed_conversations} stale WhatsApp group conversations", flush=True)
            except Exception as exc:
                print(f"[webhook] prune WhatsApp conversation directory failed: {exc}", flush=True)
        if data.get("groups") and incoming_jids:
            try:
                removed = storage.prune_sync_jobs(source="whatsapp", instance=instance, keep_jids=incoming_jids)
                if removed:
                    print(f"  Removed {removed} stale groups for {instance}")
            except Exception as e:
                print(f"  prune sync jobs failed: {e}")
    else:
        get_bus().publish("system.event", {"event": event, "instance": instance, "class": event_class})

# ── Schedule extraction ────────────────────────────────────────────
def _schedule_raw_extraction(raw_id: int, ctx: dict) -> bool:
    if not _EXTRACTION_SLOTS.acquire(blocking=False):
        return False
    try:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(_EXTRACTION_EXECUTOR, _process_single_raw, raw_id, ctx)
    except Exception as exc:
        _EXTRACTION_SLOTS.release()
        print(f"[webhook] schedule extraction error: {exc}", flush=True)
        return False
    def release_slot(completed):
        _EXTRACTION_SLOTS.release()
        try:
            error = completed.exception()
        except Exception as exc:
            error = exc
        if error:
            print(f"[webhook] extraction error raw_id={raw_id}: {error}", flush=True)
    future.add_done_callback(release_slot)
    return True

def _retry_schedule_raw_extraction(raw_id: int, ctx: dict, attempt: int = 0):
    if _schedule_raw_extraction(raw_id, ctx):
        return
    if attempt >= 5:
        print(f"[webhook] extraction deferred raw_id={raw_id}: queue saturated; message stays unprocessed for poll worker", flush=True)
        return
    try:
        loop = asyncio.get_running_loop()
        loop.call_later(0.5 * (attempt + 1), _retry_schedule_raw_extraction, raw_id, ctx, attempt + 1)
    except Exception:
        print(f"[webhook] extraction deferred raw_id={raw_id}: queue saturated; message stays unprocessed for poll worker", flush=True)

# ── Route definitions ──────────────────────────────────────────────

class IngestRequest(BaseModel):
    message: str
    group: str = "test"
    sender: str = "test-user"
    expected: Optional[dict] = None

class BatchIngestItem(BaseModel):
    message: str
    expected: Optional[dict] = None

class BatchIngestRequest(BaseModel):
    messages: list[BatchIngestItem]

@router.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    data = body if isinstance(body, dict) else {}
    event = data.get("event", "")
    msg_data_for_instance = data.get("data", {}) if isinstance(data.get("data", {}), dict) else {}
    instance = data.get("instance") or msg_data_for_instance.get("instance") or "unknown"
    webhook_broker_id = msg_data_for_instance.get("broker_id", "")
    resolved_tenant_id = None
    if webhook_broker_id:
        try:
            conn = await asyncio.to_thread(storage.get_org_whatsapp_connection_by_broker_id, webhook_broker_id)
            if conn and conn.get("organization_id"):
                resolved_tenant_id = conn["organization_id"]
        except Exception as exc:
            print(f"[webhook] tenant resolve error: {exc}", flush=True)
            raise HTTPException(503, "WhatsApp webhook tenant resolution is temporarily unavailable")
    if not resolved_tenant_id:
        print(f"[webhook] WARN: no tenant for broker_id={webhook_broker_id!r} — skipping message", flush=True)
        raise HTTPException(422, "WhatsApp webhook broker is not registered")
    try:
        event_class = _classify_webhook_event(event, data)
    except Exception as exc:
        print(f"[webhook] classify error: {exc}", flush=True)
        event_class = "system"
    if event_class != "message":
        if event_class in {"group", "conversation"}:
            asyncio.create_task(asyncio.to_thread(_handle_system_event, event_class, event, data, instance, resolved_tenant_id))
        else:
            await asyncio.to_thread(_handle_system_event, event_class, event, data, instance, resolved_tenant_id)
        return {"status": "event_handled", "event": event, "class": event_class}
    msg_data = data.get("data", data)
    key = msg_data.get("key", {})
    msg = msg_data.get("message", {})
    message_from_me = bool(key.get("fromMe") or key.get("from_me"))
    msg_text = _whatsapp_message_text(msg)
    if not msg_text.strip():
        return {"status": "ignored", "reason": "empty_message"}
    sender_data = msg_data.get("sender", {}) or {}
    push_name = msg_data.get("pushName", "") or sender_data.get("pushName", "") or ""
    sender_name = sender_data.get("name", "") or push_name
    sender_jid = key.get("participant", "") or sender_data.get("id", "")
    participant_phone_jid = key.get("participantAlt", "") or key.get("participant_pn", "") or sender_data.get("phone", "")
    resolved_phone = _canonical_phone_from_jid(str(participant_phone_jid))
    sender_phone = resolved_phone or (_canonical_phone_from_jid(sender_jid) if str(sender_jid).endswith("@s.whatsapp.net") else "")
    if not sender_phone and str(sender_jid).endswith("@lid"):
        group_jid = key.get("remoteJid", "") or msg_data.get("from", "")
        try:
            gm = await asyncio.to_thread(storage.resolve_lid_from_group_members, str(sender_jid), str(group_jid))
            if gm:
                sender_phone = _canonical_phone_from_jid(str(gm.get("member_phone") or ""))
                if not sender_name or sender_name == "unknown":
                    sender_name = str(gm.get("display_name") or "").strip() or sender_name
        except Exception as exc:
            print(f"[webhook] LID fallback error: {exc}", flush=True)
    sender = _format_whatsapp_sender(sender_name, sender_jid, sender_phone)
    group = key.get("remoteJid", "") or msg_data.get("from", "")
    if _is_blocked_whatsapp_conversation(group) or (sender_jid and _is_blocked_whatsapp_conversation(sender_jid)):
        return {"status": "ignored", "reason": "blocked_whatsapp_conversation", "jid": group}
    supplied_conversation_name = (msg_data.get("conversationName") or msg_data.get("chatName") or msg_data.get("conversation_name") or "").strip()
    group_name = supplied_conversation_name or await asyncio.to_thread(_resolve_group_name, group)
    is_dm = str(group).endswith("@s.whatsapp.net") or str(group).endswith("@lid")
    raw_group_name = "" if is_dm else group_name
    if supplied_conversation_name and str(group).endswith("@g.us"):
        try:
            await asyncio.to_thread(storage.upsert_sync_job, "whatsapp", instance or msg_data.get("instance",""), group, supplied_conversation_name, 0, "complete")
        except Exception as exc:
            print(f"[webhook] group name upsert error: {exc}", flush=True)
    message_id = msg_data.get("key", {}).get("id") or msg_data.get("id") or str(uuid.uuid4())
    message_uid = f"{webhook_broker_id or resolved_tenant_id}:{group}:{message_id}"
    if not is_dm:
        try:
            allowed = await asyncio.to_thread(
                extraction_allowed_for_group,
                resolved_tenant_id,
                str(group),
                group_name,
                webhook_broker_id,
                message_from_me=message_from_me,
                sender_phone=sender_phone,
            )
            if not allowed:
                # Do not insert unselected group traffic into raw_messages.
                # This is the ingestion boundary: suppressed traffic must not
                # consume database space or enter the extraction queue.
                return {
                    "status": "suppressed_unselected_group",
                    "stored": False,
                    "message_uid": message_uid,
                    "recoverable": False,
                }
        except Exception as exc:
            # Fail closed. A consent lookup outage must never turn into
            # all-group ingestion, and no raw payload should be persisted.
            print(f"[webhook] group selection check failed; dropping group message: {exc}", flush=True)
            raise HTTPException(503, "WhatsApp group selection is temporarily unavailable")
    try:
        existing = await asyncio.to_thread(storage.get_raw_by_uid, message_uid)
        if existing:
            return {"status": "duplicate", "raw_id": existing.id, "message": "already_saved"}
    except Exception:
        pass
    try:
        from lab.scheduler import PIPELINE_VERSION
    except ImportError:
        PIPELINE_VERSION = "0.0.0"
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message_timestamp = _coerce_whatsapp_timestamp(msg_data.get("messageTimestamp") or msg_data.get("timestamp")) or now
        from message_identity import author_content_fingerprint
        raw_fingerprint = author_content_fingerprint(
            sender_phone=sender_phone, sender_jid=sender_jid, message=msg_text
        )
        raw = RawMessage(tenant_id=resolved_tenant_id, group_name=raw_group_name, sender=sender,
            sender_jid=sender_jid, sender_phone=sender_phone, message=msg_text,
            author_content_fingerprint=raw_fingerprint or None,
            message_type=_whatsapp_message_type(msg), is_group=str(group).endswith("@g.us"),
            attachments=json.dumps(_whatsapp_attachment_metadata(msg, msg_data.get("media"))),
            reply_context=json.dumps(msg.get("extendedTextMessage",{}).get("contextInfo",{}) or msg.get("imageMessage",{}).get("contextInfo",{}) or msg.get("videoMessage",{}).get("contextInfo",{}) or {}),
            timestamp=message_timestamp, source="WHATSAPP", raw_payload=json.dumps(data),
            message_uid=message_uid, pipeline_version=PIPELINE_VERSION, synced_at=now, processed=False)
        def save_scoped_raw() -> int:
            previous = get_tenant_id()
            try:
                set_tenant_id(resolved_tenant_id)
                return storage.save_raw_message(raw)
            finally:
                set_tenant_id(previous)
        raw_id = await asyncio.to_thread(save_scoped_raw)
        conversation_type = "group" if str(group).endswith("@g.us") else "broadcast" if str(group).endswith("@broadcast") else "direct"
        try:
            await asyncio.to_thread(storage.touch_whatsapp_conversation, resolved_tenant_id, webhook_broker_id or "legacy", instance, group, conversation_type, message_timestamp)
        except Exception as exc:
            print(f"[webhook] conversation activity update failed: {exc}", flush=True)
    except Exception as exc:
        print(f"[webhook] save_raw_message error: {exc}", flush=True)
        raise HTTPException(503, "WhatsApp message persistence is temporarily unavailable")
    try:
        get_bus().publish("message.received", {"raw_id": raw_id, "group": group, "group_name": group_name,
            "sender": sender, "sender_jid": sender_jid, "sender_phone": sender_phone, "sender_name": sender_name,
            "message": msg_text[:200], "message_uid": message_uid, "instance": instance, "is_dm": is_dm})
    except Exception as exc:
        print(f"[webhook] bus publish error: {exc}", flush=True)
    extraction_ctx = {"sender_name": sender_name, "push_name": push_name, "sender_jid": sender_jid,
        "sender_phone": sender_phone, "group": group, "group_name": group_name, "msg_text": msg_text,
        "instance": instance, "is_dm": is_dm, "message_uid": message_uid, "message_id": message_id,
        "msg": msg, "tenant_id": resolved_tenant_id, "raw_payload": data,
        "attachments": _whatsapp_attachment_metadata(msg, msg_data.get("media")),
        "reply_context": msg.get("extendedTextMessage", {}).get("contextInfo", {}) or msg.get("imageMessage", {}).get("contextInfo", {}) or msg.get("videoMessage", {}).get("contextInfo", {}) or {},
        "source": "WHATSAPP", "is_group": not is_dm, "timestamp": message_timestamp,
        "synced_at": now, "event_id": message_id}
    if not _schedule_raw_extraction(raw_id, extraction_ctx):
        _retry_schedule_raw_extraction(raw_id, extraction_ctx)
    return {"status": "ok", "raw_id": raw_id, "message": "saved"}

@router.post("/ingest")
async def ingest(req: IngestRequest, user: dict = Depends(require_user)):
    from lab.scheduler import PIPELINE_VERSION
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw_id = storage.save_raw_message(RawMessage(
        group_name=req.group, sender=req.sender, message=req.message, message_type="text",
        timestamp=now, source="MANUAL", raw_payload=json.dumps({"manual": True}),
        pipeline_version=PIPELINE_VERSION, synced_at=now))
    parsed = _demote_weak_property_parse(parse_message(req.message), req.message)
    if not _parsed_has_market_anchor(parsed, req.message):
        return {"status": "ignored", "reason": "no_real_estate_anchor", "raw_id": raw_id, "parsed_id": None}
    # Typed persistence queues durable semantic indexing asynchronously.
    embedding_blob = None
    obs = ParsedObservation(raw_message_id=raw_id, intent=parsed.get("intent"),
        principal=parsed.get("principal"), bhk=parsed.get("bhk"), configuration=parsed.get("configuration"),
        price=parsed.get("price"), price_unit=parsed.get("price_unit"), price_model=parsed.get("price_model"),
        price_per_sqft=parsed.get("price_per_sqft"), monthly_rent=parsed.get("monthly_rent"),
        total_asking_price=parsed.get("total_asking_price"), area_sqft=parsed.get("area_sqft"),
        furnishing=parsed.get("furnishing"), furnishing_canonical=parsed.get("furnishing_canonical"),
        location_raw=parsed.get("location_raw"), location=json.dumps(parsed.get("location")) if parsed.get("location") else None,
        building_name=parsed.get("building_name"), landmark_name=parsed.get("landmark_name"),
        street_name=parsed.get("street_name"), area=parsed.get("area"), micro_market=parsed.get("micro_market"),
        developer=parsed.get("developer"), asset_type=parsed.get("asset_type"),
        property_type=parsed.get("property_type"), transaction_type=parsed.get("transaction_type"),
        commercial_use_type=parsed.get("commercial_use_type"), fitout_status=parsed.get("fitout_status"),
        occupancy_type=parsed.get("occupancy_type"), floor_range=parsed.get("floor_range"),
        rent_per_sqft=parsed.get("rent_per_sqft"), availability_status=parsed.get("availability_status"),
        possession_status=parsed.get("possession_status"), possession_date=parsed.get("possession_date"),
        available_from=parsed.get("available_from"), ready_by=parsed.get("ready_by"),
        construction_stage=parsed.get("construction_stage"), launch_timeline=parsed.get("launch_timeline"),
        expected_possession=parsed.get("expected_possession"), broker_name=parsed.get("broker_name"),
        broker_phone=parsed.get("broker_phone"), forwarded=parsed.get("forwarded", 0),
        confidence=parsed.get("confidence", 0.0), raw_payload=json.dumps(parsed.get("raw_payload", {})),
        embedding=embedding_blob, summary_title=generate_summary_title(parsed, req.message),
        deal_tags=list(parsed.get("deal_tags") or []), additional_charges=list(parsed.get("additional_charges") or []))
    parsed_id = storage.save_typed_observation(obs)
    resolver_result = resolve_parsed(parsed, req.message)
    dec = ResolverDecision(parsed_id=parsed_id, building_id=resolver_result.get("building_id"),
        building_name=resolver_result.get("building_name"), landmark_id=resolver_result.get("landmark_id"),
        landmark_name=resolver_result.get("landmark_name"), street_id=resolver_result.get("street_id"),
        street_name=resolver_result.get("street_name"), project_id=resolver_result.get("project_id"),
        project_name=resolver_result.get("project_name"), developer_name=resolver_result.get("developer_name"),
        parser_confidence=resolver_result.get("parser_confidence", 0.0),
        resolver_confidence=resolver_result.get("resolver_confidence", 0.0),
        final_confidence=resolver_result.get("final_confidence", 0.0),
        method=resolver_result.get("method", "unresolved"), method_detail=resolver_result.get("method_detail"),
        candidates=json.dumps(resolver_result.get("candidates", [])),
        failure_category=resolver_result.get("failure_category"), error=resolver_result.get("error"))
    storage.save_resolver_decision(dec)
    if req.expected:
        evaluate_parsed(raw_id, parsed, req.expected)
    return {"raw_id": raw_id, "parsed_id": parsed_id, "parsed": {k: v for k, v in parsed.items() if v is not None}, "resolver": resolver_result}

@router.post("/ingest/batch")
async def ingest_batch(req: BatchIngestRequest, user: dict = Depends(require_user)):
    results = []
    for item in req.messages:
        r = await ingest(IngestRequest(message=item.message, expected=item.expected), user=user)
        results.append(r)
    return {"count": len(results), "results": results}

# ── Lightweight extraction trigger (called by Go ingestor) ─────────
class TriggerExtractionRequest(BaseModel):
    raw_id: int
    tenant_id: Optional[str] = None

@router.post("/trigger-extraction")
async def trigger_extraction(req: TriggerExtractionRequest):
    """Trigger extraction for a raw_message already inserted by the Go ingestor.

    Raw messages are processed by the dedicated extraction worker. API web
    workers must not run extraction by default because a WhatsApp burst can
    otherwise consume every web worker and starve health, auth, and pairing.
    The old immediate path remains opt-in for local/single-process setups.
    """
    immediate_enabled = os.getenv(
        "PROPAI_API_IMMEDIATE_EXTRACTION",
        "",
    ).strip().lower() in {"1", "true", "yes"}
    if not immediate_enabled:
        return {
            "status": "queued_for_worker",
            "raw_id": req.raw_id,
        }

    try:
        row = await asyncio.to_thread(storage.get_raw_message, req.raw_id)
    except Exception as exc:
        raise HTTPException(404, f"raw_message {req.raw_id} not found: {exc}")
    if not row:
        raise HTTPException(404, f"raw_message {req.raw_id} not found")
    if row.processed:
        return {"status": "already_processed", "raw_id": req.raw_id}
    ctx = {
        "sender_name": row.sender or "",
        "push_name": row.sender or "",
        "sender_jid": row.sender_jid or "",
        "sender_phone": row.sender_phone or "",
        "group": row.group_name or "",
        "group_name": row.group_name or "",
        "msg_text": row.message or "",
        "instance": "",
        "is_dm": not row.is_group if row.is_group is not None else False,
        "message_uid": row.message_uid or "",
        "message_id": (row.message_uid or "").rsplit(":", 1)[-1] if row.message_uid else "",
        "msg": {},
        "tenant_id": req.tenant_id or row.tenant_id or "",
    }
    raw_payload = {}
    raw_payload_str = getattr(row, "raw_payload", None)
    if isinstance(raw_payload_str, str) and raw_payload_str.strip():
        try:
            raw_payload = json.loads(raw_payload_str)
        except (json.JSONDecodeError, TypeError):
            raw_payload = {}
    elif isinstance(raw_payload_str, dict):
        raw_payload = raw_payload_str
    if isinstance(raw_payload, dict):
        data = raw_payload.get("data", raw_payload) if isinstance(raw_payload, dict) else {}
        ctx["msg"] = data.get("message", {}) if isinstance(data, dict) else {}
        ctx["instance"] = data.get("instance", "") if isinstance(data, dict) else ""
        key = data.get("key", {}) if isinstance(data, dict) else {}
        if not ctx["message_id"]:
            ctx["message_id"] = key.get("id", "")
    if not _schedule_raw_extraction(req.raw_id, ctx):
        _retry_schedule_raw_extraction(req.raw_id, ctx)
    return {"status": "triggered", "raw_id": req.raw_id}


@router.get("/")
async def root():
    return RedirectResponse(FRONTEND_URL)

@router.get("/connect")
async def connect_page(user: dict = Depends(require_user)):
    return {"status": "ok", "frontend": f"{FRONTEND_URL}/settings", "message": f"Use the settings page at {FRONTEND_URL}/settings to connect WhatsApp"}
