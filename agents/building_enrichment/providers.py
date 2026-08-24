"""Building Enrichment Providers - Base interface and implementations."""

import os
import time
import json
import hashlib
import logging
import re
import threading
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime, timezone

from extraction_quality import canonical_locality_alias

logger = logging.getLogger(__name__)


_GENERIC_BUILDING_WORDS = frozenset({
    "apartment", "apartments", "building", "buildings", "bldg", "tower",
    "towers", "residency", "residences", "residential", "society", "societies",
    "cooperative", "co", "operative", "complex", "heights", "view", "views",
    "park", "garden", "gardens", "enclave", "plaza", "house", "homes",
    "mansion", "mansions", "chsl", "chs", "phase", "wing", "block",
})


def _geocode_name_confidence(requested_name: str, result: dict) -> float:
    """Score whether a geocoder result actually names the requested building.

    Google can return a nearby address for vague queries. Coordinates are only
    safe to auto-apply when the result contains all distinctive requested name
    tokens; generic words such as "Apartment" or "Tower" are not evidence of
    an identity match.
    """
    requested_tokens = [
        token.casefold()
        for token in re.findall(r"[a-z0-9]+", str(requested_name or "").casefold())
    ]
    distinctive = [
        token for token in requested_tokens
        if len(token) > 2 and token not in _GENERIC_BUILDING_WORDS
    ]
    if not distinctive:
        return 0.0

    result_parts = [str(result.get("formatted_address") or "")]
    for component in result.get("address_components") or []:
        result_parts.extend(component.get("long_name") or "" for _ in [0])
        result_parts.extend(component.get("short_name") or "" for _ in [0])
    result_text = " ".join(result_parts).casefold()
    result_tokens = set(re.findall(r"[a-z0-9]+", result_text))

    matched = sum(token in result_tokens for token in distinctive)
    if matched == len(distinctive):
        return 0.95
    if matched and matched / len(distinctive) >= 0.5:
        return 0.55
    return 0.0


def _place_name_confidence(requested_name: str, place: dict) -> float:
    """Score a Places Text Search candidate without trusting its locality.

    Places returns the project name separately from the formatted address, so
    it is a better identity source than the Geocoding API.  Reuse the strict
    token guard by presenting both fields as candidate evidence.
    """
    display_name = place.get("displayName") or {}
    if isinstance(display_name, dict):
        display_name = display_name.get("text") or ""
    return _geocode_name_confidence(
        requested_name,
        {"formatted_address": f"{display_name} {place.get('formattedAddress') or ''}"},
    )


def _locality_from_components(components: list[dict] | None) -> str | None:
    """Return the most specific usable locality from a Google result."""
    priorities = (
        "sublocality_level_1",
        "sublocality_level_2",
        "neighborhood",
        "administrative_area_level_3",
        "locality",
    )
    for wanted in priorities:
        for component in components or []:
            if wanted not in (component.get("types") or []):
                continue
            value = (
                component.get("longText")
                or component.get("long_name")
                or component.get("shortText")
                or component.get("short_name")
            )
            if value and str(value).strip().casefold() not in {"dubai", "uae"}:
                return str(value).strip()
    return None


def _evidence_score(locality: str | None, evidence: dict | None) -> float:
    """Rank a provider candidate using internal evidence, never create facts."""
    key = str(locality or "").strip().casefold()
    if not key or not evidence:
        return 0.0
    score = 0.0
    for field, weight in (("source_localities", 0.30), ("broker_markets", 0.15)):
        votes = evidence.get(field) or {}
        total = sum(max(0.0, float(value or 0)) for value in votes.values())
        if total:
            matched = sum(
                max(0.0, float(value or 0))
                for name, value in votes.items()
                if str(name).strip().casefold() == key
            )
            score += weight * matched / total
    price = evidence.get("price")
    band = next((value for name, value in (evidence.get("price_bands") or {}).items()
                 if str(name).strip().casefold() == key), None)
    if price and band:
        low = float(band.get("p25") or band.get("p5") or 0)
        high = float(band.get("p75") or band.get("p95") or 0)
        if low and high and low <= float(price) <= high:
            score += 0.10
    return score


def _web_candidate_names(requested_name: str, pages: list[dict]) -> list[dict]:
    """Extract explicit search-engine spelling corrections from crawled pages.

    This deliberately only accepts names explicitly presented as a search
    correction (for example, Google's ``These are results for ...``). It does
    not infer a canonical building from arbitrary page prose.
    """
    requested = " ".join(str(requested_name or "").split()).strip()
    requested_tokens = set(re.findall(r"[a-z0-9]+", requested.casefold()))
    candidates: list[dict] = []
    seen: set[str] = set()
    correction_patterns = (
        r"results\s+for\s+[\"“”']?([^\"“”'\n]+)",
        r"search\s+instead\s+for\s+[\"“”']?([^\"“”'\n]+)",
    )
    for page in pages:
        text = " ".join(str(page.get(key) or "") for key in ("title", "excerpt", "text"))
        for pattern in correction_patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                name = re.split(r"\s+(?:marina|jvc|dubai|uae)\b", match.group(1), maxsplit=1, flags=re.IGNORECASE)[0]
                name = " ".join(name.strip(" .,;:!?\"“”'").split())
                if not name or name.casefold() == requested.casefold():
                    continue
                tokens = set(re.findall(r"[a-z0-9]+", name.casefold()))
                overlap = len(tokens & requested_tokens) / max(1, len(requested_tokens))
                if overlap < 0.25 or name.casefold() in seen:
                    continue
                seen.add(name.casefold())
                candidates.append({
                    "name": name,
                    "source_url": page.get("source_url") or page.get("url") or "",
                    "title": page.get("title") or "",
                    "excerpt": page.get("excerpt") or page.get("text") or "",
                    "name_overlap": round(overlap, 3),
                })
    return candidates


@dataclass
class EnrichmentResult:
    """Result from an enrichment provider."""
    provider: str
    confidence: float  # 0.0 to 1.0
    fields: dict = field(default_factory=dict)  # field_name -> value
    source_url: str = ""
    source_record_id: str = ""
    raw_data: dict = field(default_factory=dict)
    error: str = ""
    cached: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class BaseProvider(ABC):
    """Base class for building enrichment providers."""

    name: str = "base"
    priority: int = 0  # Higher = processed first
    rate_limit_delay: float = 1.0  # Seconds between requests

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._last_request_time = 0.0
        self._rate_limit_lock = threading.Lock()
        self._cache_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "enrichment_cache"
        )
        os.makedirs(self._cache_dir, exist_ok=True)

    def _get_cache_key(self, building_name: str, context: str = "") -> str:
        """Generate a cache key for a building name."""
        return hashlib.md5(f"{self.name}:{building_name}:{context}".encode()).hexdigest()

    def _get_cache_path(self, cache_key: str) -> str:
        """Get the file path for a cache entry."""
        return os.path.join(self._cache_dir, f"{self.name}_{cache_key}.json")

    def _check_cache(self, building_name: str, context: str = "") -> Optional[dict]:
        """Check if we have cached results for this building."""
        cache_key = self._get_cache_key(building_name, context)
        cache_path = self._get_cache_path(cache_key)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r") as f:
                    data = json.load(f)
                # Cache expires after 30 days
                if time.time() - data.get("timestamp", 0) < 30 * 24 * 3600:
                    return data.get("result")
            except Exception:
                pass
        return None

    def _save_cache(self, building_name: str, result: dict, context: str = ""):
        """Save results to cache."""
        cache_key = self._get_cache_key(building_name, context)
        cache_path = self._get_cache_path(cache_key)
        try:
            with open(cache_path, "w") as f:
                json.dump({"timestamp": time.time(), "result": result}, f)
        except Exception as e:
            logger.warning(f"Failed to save cache for {building_name}: {e}")

    def _rate_limit(self):
        """Enforce rate limiting between requests."""
        # Provider instances are shared by the worker's bounded thread pool.
        # Serialize the timestamp check so concurrency does not accidentally
        # turn the configured delay into an unbounded request burst.
        with self._rate_limit_lock:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.rate_limit_delay:
                time.sleep(self.rate_limit_delay - elapsed)
            self._last_request_time = time.time()

    @abstractmethod
    def enrich(self, building_name: str, canonical_name: str = None,
               micro_market: str = None, **kwargs) -> EnrichmentResult:
        """Enrich a building with data from this provider.

        Args:
            building_name: The canonical building name to enrich
            canonical_name: Alternative canonical name if different
            micro_market: Known micro market / locality
            **kwargs: Additional context

        Returns:
            EnrichmentResult with enriched fields
        """
        pass

    def is_available(self) -> bool:
        """Check if this provider is configured and available."""
        return True


class DLDProvider(BaseProvider):
    """Dubai Land Department data provider.

    DLD provides:
    - Registered transaction history (sales, mortgages, gifts)
    - Title deed / permit references
    - Price-per-sqft benchmarks per building and area

    Data access goes through agents/dld_client.py: the DubaiPulse open-data
    CKAN endpoint by default, or the official Dubai REST API when
    DLD_API_BASE/DLD_API_KEY are configured.
    """

    name = "dld"
    priority = 10
    rate_limit_delay = 2.0  # Respect public data endpoints

    def enrich(self, building_name: str, canonical_name: str = None,
               micro_market: str = None, **kwargs) -> EnrichmentResult:
        """Enrich building with DLD transaction aggregates."""
        cached = self._check_cache(building_name)
        if cached:
            return EnrichmentResult(
                provider=self.name,
                confidence=cached.get("confidence", 0.0),
                fields=cached.get("fields", {}),
                source_url=cached.get("source_url", ""),
                source_record_id=cached.get("source_record_id", ""),
                raw_data=cached,
                error=cached.get("error", ""),
                cached=True,
            )

        try:
            from agents.dld_client import building_summary
            summary = building_summary(canonical_name or building_name)
        except Exception as exc:  # noqa: BLE001
            summary = {"error": str(exc), "confidence": 0.0}

        fields = {
            key: summary[key]
            for key in (
                "transaction_count",
                "last_transaction_date",
                "last_transaction_price_aed",
                "avg_price_per_sqft",
            )
            if summary.get(key) is not None
        }
        result = EnrichmentResult(
            provider=self.name,
            confidence=summary.get("confidence", 0.0),
            fields=fields,
            source_url=summary.get("source_url", "https://dubailand.gov.ae/en/"),
            raw_data=summary,
            error=summary.get("error", ""),
        )
        self._save_cache(building_name, result.to_dict())
        return result

    def is_available(self) -> bool:
        return True


class RERAProvider(BaseProvider):
    """RERA (Real Estate Regulatory Authority) data provider.

    Dubai RERA operates under DLD and regulates:
    - Project registration and escrow account details
    - Developer information and Trakheesi permits
    - Project status and completion dates
    - Ejari rental contract registration

    Portal: https://dubailand.gov.ae/en/
    """

    name = "rera"
    priority = 20
    rate_limit_delay = 2.0

    def enrich(self, building_name: str, canonical_name: str = None,
               micro_market: str = None, **kwargs) -> EnrichmentResult:
        """Enrich building with RERA data."""
        address = kwargs.get("address")
        pincode = kwargs.get("pincode")
        context = ", ".join(str(part).strip() for part in (address, pincode) if part and str(part).strip())
        cached = self._check_cache(building_name, context)
        if cached:
            return EnrichmentResult(
                provider=self.name,
                confidence=cached.get("confidence", 0.0),
                fields=cached.get("fields", {}),
                source_url=cached.get("source_url", ""),
                source_record_id=cached.get("source_record_id", ""),
                raw_data=cached,
                error=cached.get("error", ""),
                cached=True,
            )

        # RERA enrichment logic would go here
        result = EnrichmentResult(
            provider=self.name,
            confidence=0.0,
            fields={},
            error="RERA provider not yet implemented",
        )

        self._save_cache(building_name, result.to_dict())
        return result

    def is_available(self) -> bool:
        return True


class GooglePlacesProvider(BaseProvider):
    """Google Places API provider.

    Provides:
    - Building address
    - Coordinates (lat/lng)
    - Place ID
    - Ratings and reviews
    - Opening hours (for commercial)
    - Photos

    Requires API key in GOOGLE_PLACES_API_KEY env var.
    """

    name = "google_places"
    priority = 30
    rate_limit_delay = 0.1  # Google allows faster requests
    _cache_version = "places-text-search-v1"

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.api_key = (
            self.config.get("api_key")
            or os.environ.get("GOOGLE_PLACES_API_KEY", "")
            or os.environ.get("GOOGLE_MAPS_API_KEY", "")
            or os.environ.get("GOOGLE_places_API_KEY", "")
        )

    def is_available(self) -> bool:
        return bool(self.api_key)

    def _get_cache_key(self, building_name: str, context: str = "") -> str:
        # Invalidate results cached before candidate-name validation was added.
        return hashlib.md5(
            f"{self.name}:{self._cache_version}:{building_name}:{context}".encode()
        ).hexdigest()

    def enrich(self, building_name: str, canonical_name: str = None,
               micro_market: str = None, **kwargs) -> EnrichmentResult:
        """Enrich building with Google Places data."""
        if not self.is_available():
            return EnrichmentResult(
                provider=self.name,
                confidence=0.0,
                fields={},
                error="Google Places API key not configured",
            )

        # Never use an unverified stored micro-market as query input.  That
        # creates a circular feedback loop (bad DB locality -> biased Google
        # query -> apparent confirmation).  The neutral city-scoped query is
        # the identity lookup; source and network evidence may rank returned
        # candidates later, but cannot manufacture the provider result.
        requested_name = canonical_name or building_name
        context = "Mumbai, Maharashtra, India"
        cached = self._check_cache(building_name, context)
        if cached:
            return EnrichmentResult(
                provider=self.name,
                confidence=cached.get("confidence", 0.0),
                fields=cached.get("fields", {}),
                source_url=cached.get("source_url", ""),
                source_record_id=cached.get("source_record_id", ""),
                raw_data=cached,
                error=cached.get("error", ""),
                cached=True,
            )

        self._rate_limit()
        query = f"{requested_name}, Mumbai, Maharashtra, India"
        places_url = "https://places.googleapis.com/v1/places:searchText"
        try:
            request = urllib.request.Request(
                places_url,
                data=json.dumps({"textQuery": query, "maxResultCount": 10}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": (
                        "places.id,places.displayName,places.formattedAddress,"
                        "places.addressComponents,places.location,places.plusCode"
                    ),
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            places = payload.get("places") or []
            if not places:
                error = ((payload.get("error") or {}).get("message") or "No Places Text Search result")
                result = EnrichmentResult(provider=self.name, confidence=0.0, error=error, raw_data=payload)
            else:
                evidence = kwargs.get("resolution_evidence") or {}
                scored_results = []
                for candidate in places:
                    name_confidence = _place_name_confidence(requested_name, candidate)
                    locality = _locality_from_components(candidate.get("addressComponents"))
                    scored_results.append((name_confidence + _evidence_score(locality, evidence), name_confidence, locality, candidate))
                _score, match_confidence, resolved_market, match = max(
                    scored_results,
                    key=lambda item: item[0],
                )
                if match_confidence < 0.7:
                    result = EnrichmentResult(
                        provider=self.name,
                        confidence=match_confidence,
                        fields={},
                        error=(
                            "Geocoder returned no sufficiently matching building name; "
                            "coordinates require review"
                        ),
                        source_url=places_url,
                        raw_data={"places": places},
                    )
                    self._save_cache(building_name, result.to_dict(), context)
                    return result
                credible = [item for item in scored_results if item[1] >= 0.7]
                distinct_markets = {str(item[2] or "").casefold() for item in credible if item[2]}
                if len(distinct_markets) > 1 and len(credible) > 1:
                    ranked = sorted(credible, key=lambda item: item[0], reverse=True)
                    if ranked[0][0] - ranked[1][0] < 0.10:
                        result = EnrichmentResult(
                            provider=self.name,
                            confidence=match_confidence,
                            fields={},
                            error="Ambiguous same-name Places results require stronger source, broker, or price evidence",
                            source_url=places_url,
                            raw_data={"places": places},
                        )
                        self._save_cache(building_name, result.to_dict(), context)
                        return result
                location = match.get("location") or {}
                plus = match.get("plusCode") or {}
                fields = {
                    "address": match.get("formattedAddress"),
                    "micro_market": resolved_market,
                    "latitude": location.get("latitude"),
                    "longitude": location.get("longitude"),
                    "google_place_id": match.get("id"),
                    "plus_code": plus.get("compoundCode") or plus.get("globalCode"),
                    "geocode_query": query,
                    "geocode_source": "google_places_text_search",
                    "geocode_confidence": match_confidence,
                    "geocoded_at": datetime.now(timezone.utc).isoformat(),
                }
                result = EnrichmentResult(
                    provider=self.name,
                    confidence=match_confidence,
                    fields={key: value for key, value in fields.items() if value is not None},
                    source_url=places_url,
                    source_record_id=match.get("id", ""),
                    raw_data={"result": match},
                )
        except Exception as exc:
            result = EnrichmentResult(provider=self.name, confidence=0.0, error=str(exc))

        self._save_cache(building_name, result.to_dict(), context)
        return result


class Crawl4AIBuildingDiscoveryProvider(BaseProvider):
    """Web-first spelling discovery for unresolved building names.

    Crawl4AI is used only to discover an explicitly surfaced search correction.
    A result is not considered enrichment until Google Places verifies the
    discovered candidate in the worker.
    """

    name = "crawl4ai"
    priority = 50
    rate_limit_delay = 2.0

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.enabled = bool(self.config.get("web_search_enabled", False))
        self.search_url_template = self.config.get(
            "web_search_url_template"
        ) or os.environ.get(
            "BUILDING_ENRICHMENT_SEARCH_URL_TEMPLATE",
            "https://www.google.com/search?q=%22{query}%22+{locality}+Mumbai",
        )

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        try:
            import crawl4ai  # noqa: F401
        except ImportError:
            logger.error(
                "Crawl4AI web search is enabled but the crawl4ai package is not installed"
            )
            return False
        return True

    def enrich(self, building_name: str, canonical_name: str = None,
               micro_market: str = None, **kwargs) -> EnrichmentResult:
        requested = canonical_name or building_name
        evidence = kwargs.get("resolution_evidence") or {}
        address = str(kwargs.get("address") or "").strip()
        pincode = str(kwargs.get("pincode") or "").strip()

        # The building row often has no locality yet. Use bounded, structured
        # evidence derived from the source listings in that case. This keeps
        # discovery anchored to the broker's actual market instead of asking
        # the web to resolve a bare, potentially ambiguous building name.
        context = str(micro_market or "").strip()
        if not context or context.casefold() in {"no locality", "unknown", "dubai"}:
            locality_votes = {}
            for field in ("source_localities", "broker_markets"):
                for locality, votes in (evidence.get(field) or {}).items():
                    value = str(locality or "").strip()
                    if value:
                        value = canonical_locality_alias(value)
                        locality_votes[value] = locality_votes.get(value, 0) + float(votes or 0)
            if locality_votes:
                context = max(locality_votes, key=locality_votes.get)
        context_parts = [part for part in (context, address, pincode) if part]
        context = ", ".join(dict.fromkeys(context_parts)) or "Mumbai"
        source_contexts = kwargs.get("resolution_evidence", {}).get("source_contexts") or []
        # The source slice is deliberately not appended wholesale to the URL.
        # It is retained in the provider result for auditability while the
        # deterministic locality remains the actual search constraint.
        cached = self._check_cache(requested, context)
        if cached:
            return EnrichmentResult(
                provider=self.name,
                confidence=cached.get("confidence", 0.0),
                fields=cached.get("fields", {}),
                source_url=cached.get("source_url", ""),
                source_record_id=cached.get("source_record_id", ""),
                raw_data=cached.get("raw_data") or cached,
                error=cached.get("error", ""),
                cached=True,
            )

        self._rate_limit()
        try:
            from .crawl_discovery import crawl_discovery_pages_sync

            pages = crawl_discovery_pages_sync(
                [requested], [self.search_url_template], {requested: context}
            )
            page_dicts = [
                {
                    "source_url": page.source_url,
                    "title": page.title,
                    "excerpt": page.excerpt,
                    "name_match": page.name_match,
                    "locality_match": page.locality_match,
                    "structured_fields": page.structured_fields or {},
                }
                for page in pages
            ]
            structured_fields = {}
            for page in page_dicts:
                for field_name, claim in (page.get("structured_fields") or {}).items():
                    current = structured_fields.get(field_name)
                    if current is None or float(claim.get("confidence") or 0) > float(current.get("confidence") or 0):
                        structured_fields[field_name] = {
                            **claim,
                            "source_url": page.get("source_url") or "",
                        }
            candidates = _web_candidate_names(requested, page_dicts)
            if not candidates:
                result = EnrichmentResult(
                    provider=self.name, confidence=0.0, fields={},
                    error="No explicit web spelling correction found",
                    raw_data={"pages": page_dicts, "candidates": [], "structured_fields": structured_fields},
                )
            else:
                candidate = candidates[0]
                confidence = min(
                    0.9,
                    0.55 + 0.15 * min(1.0, float(candidate.get("name_overlap") or 0.0))
                    + (0.15 if len(candidates) >= 2 else 0.0),
                )
                result = EnrichmentResult(
                    provider=self.name,
                    confidence=confidence,
                    fields={},
                    source_url=candidate.get("source_url", ""),
                    raw_data={
                        "pages": page_dicts,
                        "candidates": candidates,
                        "resolved_name": candidate["name"],
                        "source_contexts": source_contexts[:5],
                        "structured_fields": structured_fields,
                    },
                )
        except Exception as exc:
            result = EnrichmentResult(provider=self.name, confidence=0.0, fields={}, error=str(exc))

        self._save_cache(requested, result.to_dict(), context)
        return result


class OpenStreetMapProvider(BaseProvider):
    """OpenStreetMap (OSM) data provider.

    Provides:
    - Building footprints
    - Address details
    - Coordinates
    - Nearby amenities
    - Building type

    Uses Overpass API for queries.
    """

    name = "openstreetmap"
    priority = 40
    rate_limit_delay = 1.0

    def enrich(self, building_name: str, canonical_name: str = None,
               micro_market: str = None, **kwargs) -> EnrichmentResult:
        """Enrich building with OSM data."""
        cached = self._check_cache(building_name)
        if cached:
            return EnrichmentResult(
                provider=self.name,
                confidence=cached.get("confidence", 0.0),
                fields=cached.get("fields", {}),
                source_url=cached.get("source_url", ""),
                source_record_id=cached.get("source_record_id", ""),
                raw_data=cached,
                error=cached.get("error", ""),
                cached=True,
            )

        # OSM enrichment logic would go here
        result = EnrichmentResult(
            provider=self.name,
            confidence=0.0,
            fields={},
            error="OpenStreetMap provider not yet implemented",
        )

        self._save_cache(building_name, result.to_dict())
        return result

    def is_available(self) -> bool:
        """OSM is always available (free, public)."""
        return True


# Provider registry
PROVIDERS = {
    "dld": DLDProvider,
    "rera": RERAProvider,
    "google_places": GooglePlacesProvider,
    "openstreetmap": OpenStreetMapProvider,
    "crawl4ai": Crawl4AIBuildingDiscoveryProvider,
}


def get_provider(name: str, config: dict = None) -> Optional[BaseProvider]:
    """Get a provider instance by name."""
    provider_class = PROVIDERS.get(name)
    if provider_class:
        return provider_class(config)
    return None


def get_all_providers(config: dict = None) -> list[BaseProvider]:
    """Get all available providers sorted by priority."""
    providers = []
    for name, cls in PROVIDERS.items():
        p = cls(config)
        if p.is_available():
            providers.append(p)
    providers.sort(key=lambda p: p.priority, reverse=True)
    return providers
