import json
import sys

from agents.building_enrichment.providers import (
    Crawl4AIBuildingDiscoveryProvider,
    EnrichmentResult,
    GooglePlacesProvider,
    _geocode_name_confidence,
    _locality_from_components,
    get_all_providers,
    _web_candidate_names,
)
from agents.building_enrichment.crawl_discovery import DiscoveryCandidate
from agents.building_enrichment.worker import BuildingEnrichmentWorker


class FakeProvider:
    name = "google_places"
    confidence = 0.95

    def enrich(self, **kwargs):
        return EnrichmentResult(
            provider=self.name,
            confidence=self.confidence,
            fields={"address": "Marina Gate, Dubai Marina"},
            source_url="https://example.test/place",
            source_record_id="place-1",
        )


class FakeStorage:
    def __init__(self):
        self.claimed = []
        self.completed = []
        self.history = []
        self.sources = []
        self.enriched = []
        self.updated = []
        self.suggestions = []
        self.retries = []
        self.recovered = 0
        self.backfilled = []

    def claim_building_job(self, job_id, provider=None):
        self.claimed.append((job_id, provider))
        return True

    def get_building(self, building_db_id=None, **kwargs):
        return {"id": building_db_id, "canonical_name": "Marina Gate", "micro_market": "Dubai Marina"}

    def update_building_from_enrichment(self, *args):
        self.updated.append(args)
        return True

    def record_enrichment_sources(self, *args):
        self.sources.append(args)

    def mark_building_enriched(self, *args):
        self.enriched.append(args)
        return True

    def backfill_linked_listings_from_building(self, *args):
        self.backfilled.append(args)
        return 1

    def add_enrichment_history(self, *args, **kwargs):
        self.history.append((args, kwargs))
        return True

    def complete_building_job(self, *args):
        self.completed.append(args)
        return True

    def retry_building_job(self, job_id, error, max_attempts=None):
        self.retries.append((job_id, error, max_attempts))
        return "pending"

    def recover_stale_building_jobs(self, max_attempts=None):
        self.recovered += 1
        return 0

    def create_enrichment_review_suggestion(self, *args):
        self.suggestions.append(args)
        return 101


def test_unassigned_job_is_claimed_with_configured_provider_without_sqlite_calls():
    storage = FakeStorage()
    worker = BuildingEnrichmentWorker(storage, {"provider": "google_places"})
    worker.providers = [FakeProvider()]

    assert worker._process_job({"id": 7, "building_id": 42, "provider": "unassigned"})
    assert storage.claimed == [(7, "google_places")]
    assert storage.enriched == [(42, "google_places", 0.95)]
    assert storage.sources
    assert storage.backfilled == [(42, {"address": "Marina Gate, Dubai Marina"}, 0.95)]
    assert storage.history[0][0][2] == "enriched"
    assert storage.completed == [(7, True)]


def test_low_confidence_result_is_reviewed_without_marking_building_enriched():
    storage = FakeStorage()
    provider = FakeProvider()
    provider.confidence = 0.4
    worker = BuildingEnrichmentWorker(storage, {"provider": "google_places"})
    worker.providers = [provider]

    assert worker._process_job({"id": 8, "building_id": 43, "provider": "unassigned"})
    assert storage.claimed == [(8, "google_places")]
    assert storage.suggestions
    assert storage.history[0][0][2] == "needs_review"
    assert storage.updated == []
    assert storage.sources == []
    assert storage.enriched == []
    assert storage.completed == [(8, True)]


def test_igr_provider_is_not_auto_registered():
    assert "igr" not in {provider.name for provider in get_all_providers({})}


def test_geocoder_rejects_generic_building_name_match():
    assert _geocode_name_confidence(
        "By Apartment",
        {"formatted_address": "Apartment Road, Downtown Dubai"},
    ) == 0.0


def test_geocoder_accepts_distinctive_building_name_match():
    assert _geocode_name_confidence(
        "Burj Vista",
        {"formatted_address": "Burj Vista, Downtown Dubai"},
    ) == 0.95


def test_places_locality_prefers_sublocality_over_city():
    assert _locality_from_components([
        {"longText": "Dubai", "types": ["locality"]},
        {"longText": "Business Bay", "types": ["sublocality_level_1"]},
    ]) == "Business Bay"


def test_places_search_is_neutral_and_returns_provider_locality(monkeypatch):
    provider = GooglePlacesProvider({"api_key": "test-key"})
    monkeypatch.setattr(provider, "_check_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_save_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_rate_limit", lambda: None)
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'''{"places":[{"id":"place-1","displayName":{"text":"Burj Vista"},"formattedAddress":"Business Bay, Dubai","addressComponents":[{"longText":"Business Bay","types":["sublocality_level_1"]},{"longText":"Dubai","types":["locality"]}],"location":{"latitude":25.189,"longitude":55.276}}]}'''

    def fake_urlopen(request, timeout=0):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["url"] = request.full_url
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = provider.enrich("Burj Vista", micro_market="Deira")

    assert captured["url"].endswith("places:searchText")
    assert captured["body"]["textQuery"] == "Burj Vista, Mumbai, Maharashtra, India"
    assert "Deira" not in captured["body"]["textQuery"]
    assert result.confidence == 0.95
    assert result.fields["micro_market"] == "Business Bay"
    assert result.fields["geocode_source"] == "google_places_text_search"


def test_places_same_name_across_markets_requires_evidence(monkeypatch):
    provider = GooglePlacesProvider({"api_key": "test-key"})
    monkeypatch.setattr(provider, "_check_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_save_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_rate_limit", lambda: None)

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self):
            return b'''{"places":[{"id":"jvc","displayName":{"text":"Sunshine Heights"},"formattedAddress":"JVC, Dubai","addressComponents":[{"longText":"JVC","types":["sublocality_level_1"]}],"location":{"latitude":25.07,"longitude":55.28}},{"id":"deira","displayName":{"text":"Sunshine Heights"},"formattedAddress":"Deira, Dubai","addressComponents":[{"longText":"Deira","types":["sublocality_level_1"]}],"location":{"latitude":25.27,"longitude":55.33}}]}'''

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())

    ambiguous = provider.enrich("Sunshine Heights")
    assert ambiguous.fields == {}
    assert "Ambiguous same-name" in ambiguous.error

    ranked = provider.enrich(
        "Sunshine Heights",
        resolution_evidence={"source_localities": {"JVC": 4}},
    )
    assert ranked.fields["micro_market"] == "JVC"


def test_cached_geocoder_failure_preserves_error_and_cannot_look_successful(monkeypatch):
    provider = GooglePlacesProvider({"api_key": "test-key"})
    monkeypatch.setattr(provider, "_check_cache", lambda *_args: {
        "confidence": 0.0,
        "fields": {},
        "error": "ZERO_RESULTS",
        "source_record_id": "",
    })

    result = provider.enrich("Missing Building")

    assert result.cached is True
    assert result.fields == {}
    assert result.error == "ZERO_RESULTS"


def test_empty_enrichment_result_is_retried_not_completed():
    storage = FakeStorage()
    provider = FakeProvider()
    provider.enrich = lambda **_kwargs: EnrichmentResult(
        provider="google_places", confidence=0.0, fields={}
    )
    worker = BuildingEnrichmentWorker(storage, {"provider": "google_places", "max_retries": 3})
    worker.providers = [provider]

    assert worker._process_job({"id": 9, "building_id": 44, "provider": "google_places"}) is False
    assert storage.completed == []
    assert storage.retries == [(9, "Provider returned no enrichment fields", 3)]
    assert storage.history[0][0][2] == "retry_scheduled"


def test_missing_enrichment_provider_is_failed_without_retry_churn():
    storage = FakeStorage()
    worker = BuildingEnrichmentWorker(storage, {"provider": "crawl4ai"})
    worker.providers = []

    assert worker._process_job({"id": 10, "building_id": 45, "provider": "crawl4ai"}) is False
    assert storage.retries == []
    assert storage.completed == [(10, False, "No configured enrichment provider is available")]
    assert storage.history[0][0][2] == "configuration_unavailable"


def test_web_discovery_accepts_only_explicit_search_corrections():
    candidates = _web_candidate_names(
        "Deepak Silverline",
        [{
            "source_url": "https://www.google.com/search?q=deepak",
            "title": "These are results for Deepak Silverene JVC",
            "excerpt": "Search instead for Deepak Silverline JVC",
        }],
    )

    assert candidates
    assert candidates[0]["name"] == "Deepak Silverene"


def test_crawl4ai_provider_is_disabled_by_default():
    assert not Crawl4AIBuildingDiscoveryProvider({}).is_available()


def test_crawl4ai_provider_requires_installed_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "crawl4ai", None)
    assert not Crawl4AIBuildingDiscoveryProvider({"web_search_enabled": True}).is_available()


def test_crawl4ai_provider_returns_candidate_for_worker_verification(monkeypatch):
    provider = Crawl4AIBuildingDiscoveryProvider({"web_search_enabled": True})
    monkeypatch.setattr(provider, "_check_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_save_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_rate_limit", lambda: None)
    monkeypatch.setattr(
        "agents.building_enrichment.crawl_discovery.crawl_discovery_pages_sync",
        lambda *_args, **_kwargs: [DiscoveryCandidate(
            building_name="Deepak Silverline",
            source_url="https://www.google.com/search?q=deepak",
            title="These are results for Deepak Silverene JVC",
            excerpt="These are results for Deepak Silverene JVC",
        )],
    )

    result = provider.enrich("Deepak Silverline", micro_market="JVC")

    assert result.raw_data["resolved_name"] == "Deepak Silverene"
    assert result.source_url.startswith("https://www.google.com")
    assert result.fields == {}


def test_crawl4ai_provider_returns_structured_claims_without_promoting_them(monkeypatch):
    provider = Crawl4AIBuildingDiscoveryProvider({"web_search_enabled": True})
    monkeypatch.setattr(provider, "_check_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_save_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_rate_limit", lambda: None)
    monkeypatch.setattr(
        "agents.building_enrichment.crawl_discovery.crawl_discovery_pages_sync",
        lambda *_args, **_kwargs: [DiscoveryCandidate(
            building_name="Monalisa",
            source_url="https://example.test/monalisa",
            title="Monalisa Apartments",
            excerpt="Address: 12 Example Road, JVC, Dubai\nDeveloper: Example Homes\nDLD Permit No: 7123456789",
            structured_fields={
                "address": {"value": "12 Example Road, JVC, Dubai", "confidence": 0.82, "evidence": "Address: 12 Example Road"},
                "developer": {"value": "Example Homes", "confidence": 0.82, "evidence": "Developer: Example Homes"},
            },
        )],
    )

    result = provider.enrich("Monalisa", micro_market="JVC")

    assert result.fields == {}
    assert result.raw_data["structured_fields"]["developer"]["value"] == "Example Homes"


def test_crawl4ai_uses_source_locality_when_building_has_no_locality(monkeypatch):
    provider = Crawl4AIBuildingDiscoveryProvider({"web_search_enabled": True})
    monkeypatch.setattr(provider, "_check_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_save_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_rate_limit", lambda: None)
    captured = {}

    def fake_discovery(names, templates, localities):
        captured["locality"] = localities[names[0]]
        return []

    monkeypatch.setattr(
        "agents.building_enrichment.crawl_discovery.crawl_discovery_pages_sync",
        fake_discovery,
    )

    provider.enrich(
        "West Avenue",
        micro_market="No locality",
        resolution_evidence={"source_localities": {"Dubai Marina": 4}},
    )

    assert captured["locality"] == "Dubai Marina"


def test_crawl4ai_normalizes_known_locality_typo(monkeypatch):
    provider = Crawl4AIBuildingDiscoveryProvider({"web_search_enabled": True})
    monkeypatch.setattr(provider, "_check_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_save_cache", lambda *_args: None)
    monkeypatch.setattr(provider, "_rate_limit", lambda: None)
    captured = {}

    def fake_discovery(names, templates, localities):
        captured["locality"] = localities[names[0]]
        return []

    monkeypatch.setattr(
        "agents.building_enrichment.crawl_discovery.crawl_discovery_pages_sync",
        fake_discovery,
    )
    provider.enrich(
        "Some Building",
        micro_market="No locality",
        resolution_evidence={"source_localities": {"Buisness Bay": 3}},
    )
    assert captured["locality"] == "Business Bay"
