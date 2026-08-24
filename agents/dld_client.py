#!/usr/bin/env python3
"""Dubai Land Department (DLD) data client.

Replaces the legacy Maharashtra IGR scraper for the UAE launch. Two backends:

1. DubaiPulse open data (no API key) - DLD publishes real-estate transaction
   datasets as CKAN datastores on data.dubaipulse.ae. Enable by setting
   DLD_PULSE_RESOURCE_ID to the dataset resource id.
2. Official DLD "Dubai REST" API - set DLD_API_BASE + DLD_API_KEY for the
   subscription endpoints (transactions, rental index, permits).

All network failures degrade gracefully to empty results so enrichment
pipelines never hard-fail on connectivity.

CLI:
    python agents/dld_client.py search <building name>
    python agents/dld_client.py summary <building name>
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

PULSE_BASE = os.environ.get("DLD_PULSE_BASE", "https://data.dubaipulse.ae/api/3/action")
PULSE_RESOURCE_ID = os.environ.get("DLD_PULSE_RESOURCE_ID", "")
API_BASE = os.environ.get("DLD_API_BASE", "")
API_KEY = os.environ.get("DLD_API_KEY", "")

DLD_PORTAL_URL = "https://dubailand.gov.ae/en/"

_TIMEOUT = 8  # seconds


@dataclass
class DLDResult:
    """One transaction record normalised from whichever backend served it."""

    building_name: str = ""
    area: str = ""
    transaction_date: str = ""
    amount_aed: Optional[float] = None
    price_per_sqft: Optional[float] = None
    area_sqft: Optional[float] = None
    transaction_kind: str = ""  # sale / mortgage / gift ...
    source_url: str = DLD_PORTAL_URL
    raw: dict = field(default_factory=dict)


def _clean_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _to_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(re.sub(r"[^0-9.]", "", str(value)) or None)
    except (TypeError, ValueError):
        return None


# Field-name candidates seen across DLD dataset revisions.
_AMOUNT_KEYS = ("actual_worth", "trans_value", "amount", "transaction_amount", "price")
_PSF_KEYS = ("meter_trade_price_per_sqft", "price_per_sqft", "psf")
_SQFT_KEYS = ("procedure_area_in_sqft", "area_sqft", "unit_area_sqft")
_NAME_KEYS = ("project_name_en", "building_name_en", "building_name", "project_name")
_AREA_KEYS = ("area_name_en", "area_en", "area_name")
_DATE_KEYS = ("trans_date", "transaction_date", "date")
_KIND_KEYS = ("trans_group_en", "procedure_name_en", "transaction_type")


def _pick(record: dict, keys: tuple) -> Any:
    for key in keys:
        if record.get(key) not in (None, ""):
            return record[key]
    return None


def _normalise_record(record: dict, source_url: str) -> DLDResult:
    return DLDResult(
        building_name=_clean_name(_pick(record, _NAME_KEYS)),
        area=_clean_name(_pick(record, _AREA_KEYS)),
        transaction_date=str(_pick(record, _DATE_KEYS) or ""),
        amount_aed=_to_float(_pick(record, _AMOUNT_KEYS)),
        price_per_sqft=_to_float(_pick(record, _PSF_KEYS)),
        area_sqft=_to_float(_pick(record, _SQFT_KEYS)),
        transaction_kind=_clean_name(_pick(record, _KIND_KEYS)),
        source_url=source_url,
        raw=record,
    )


def search_transactions(query: str, limit: int = 20) -> tuple[list[DLDResult], str]:
    """Search DLD transaction records; returns (results, error)."""
    query = _clean_name(query)
    if not query:
        return [], "empty query"
    if API_KEY and API_BASE:
        return _search_official(query, limit)
    if PULSE_RESOURCE_ID:
        return _search_pulse(query, limit)
    return [], "DLD source not configured (set DLD_PULSE_RESOURCE_ID or DLD_API_KEY)"


def _search_pulse(query: str, limit: int) -> tuple[list[DLDResult], str]:
    url = f"{PULSE_BASE}/datastore_search"
    try:
        resp = requests.get(
            url,
            params={"resource_id": PULSE_RESOURCE_ID, "q": query, "limit": min(limit, 100)},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        records = (resp.json() or {}).get("result", {}).get("records") or []
    except Exception as exc:  # noqa: BLE001 - degrade gracefully
        logger.warning("DLD pulse search failed: %s", exc)
        return [], str(exc)
    source_url = f"https://data.dubaipulse.ae/dataset?resource_id={PULSE_RESOURCE_ID}"
    return [_normalise_record(r, source_url) for r in records], ""


def _search_official(query: str, limit: int) -> tuple[list[DLDResult], str]:
    try:
        resp = requests.get(
            f"{API_BASE.rstrip('/')}/transactions",
            params={"q": query, "limit": min(limit, 100)},
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("DLD official search failed: %s", exc)
        return [], str(exc)
    records = payload.get("data") or payload.get("records") or []
    return [_normalise_record(r, API_BASE) for r in records], ""


def building_summary(building_name: str) -> dict:
    """Aggregate DLD activity for one building into enrichment fields."""
    results, error = search_transactions(building_name, limit=100)
    wanted = building_name.casefold()
    mine = [r for r in results if wanted in r.building_name.casefold()] or results
    sales = [r for r in mine if r.amount_aed]
    summary: dict = {
        "transaction_count": len(mine),
        "error": error,
    }
    if sales:
        latest = sorted(sales, key=lambda r: r.transaction_date or "")[-1]
        psf_values = [r.price_per_sqft for r in sales if r.price_per_sqft]
        summary.update(
            {
                "last_transaction_date": latest.transaction_date,
                "last_transaction_price_aed": latest.amount_aed,
                "avg_price_per_sqft": round(sum(psf_values) / len(psf_values), 2) if psf_values else None,
                "source_url": latest.source_url,
                "confidence": 0.7 if not error else 0.4,
            }
        )
    elif not error:
        summary["error"] = "no matching DLD records"
    return summary


def main() -> int:
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3 or sys.argv[1] not in {"search", "summary"}:
        print(__doc__)
        return 1
    query = " ".join(sys.argv[2:])
    if sys.argv[1] == "summary":
        print(building_summary(query))
        return 0
    results, error = search_transactions(query)
    if error:
        print(f"error: {error}")
    for row in results:
        print(f"{row.transaction_date} | {row.building_name} | {row.area} | AED {row.amount_aed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
