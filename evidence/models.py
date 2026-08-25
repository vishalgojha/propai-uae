"""
Evidence Engine — Observation Model.

Every observation records:
  - What happened (listing, requirement, registration, etc.)
  - Where it happened (BuildingID)
  - When it was observed (ObservedAt)

This temporal model is the foundation for all market intelligence.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import json


# ── Supported observation types ────────────────────────────────
OBSERVATION_TYPES = {
    "SALE_LISTING": "Property listed for sale on a portal or broker feed",
    "RENT_LISTING": "Property listed for rent on a portal or broker feed",
    "BROKER_REQUIREMENT": "Broker/agent looking for a buyer or tenant",
    "BROKER_OFFER": "Broker/agent offering a specific property",
    "IGR_TRANSACTION": "Registered sale deed from IGR Maharashtra",
    "MAHARERA_PROJECT": "Project registration from MahaRERA",
    "PRICE_CHANGE": "Asking price changed on an active listing",
    "STATUS_CHANGE": "Listing status changed (active → sold/rented/withdrawn)",
    "BROKER_MENTION": "Building mentioned in broker WhatsApp group",
    "IMAGE_UPDATE": "New images added to a listing",
    "AMENITY_UPDATE": "Amenity list changed on a listing",
    "MANUAL_CORRECTION": "Human-edited correction to a record",
}

# ── Source identifiers ─────────────────────────────────────────
SOURCES = {
    "PORTAL": "Scraped property portal data",
    "HOUSING": "https://www.housing.com",
    "MAGICBRICKS": "https://www.magicbricks.com",
    "99ACRES": "https://www.99acres.com",
    "MAHARERA": "https://maharera.mahaonline.gov.in",
    "IGR": "https://igrmaharashtra.gov.in",
    "WHATSAPP": "Broker WhatsApp group messages",
    "MANUAL": "Manual entry by PropAI operator",
}

# ── Price types for structured payloads ────────────────────────
PRICE_TYPES = {
    "ASKING": "Listed asking price",
    "TRANSACTION": "Registered sale deed value",
    "BUDGET": "Buyer/tenant budget range",
    "OFFERED": "Price offered by a broker",
    "CURRENT": "Current market estimate",
}


@dataclass
class PricePayload:
    """Structured price information embedded in observation payload."""
    amount: float
    currency: str = "AED"
    price_type: str = "ASKING"  # one of PRICE_TYPES
    price_per_sqft: Optional[float] = None
    negotiable: Optional[bool] = None

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class PropertyDetails:
    """Structured property details embedded in observation payload."""
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    area_sqft: Optional[float] = None
    area_unit: str = "sqft"
    floor: Optional[int] = None
    total_floors: Optional[int] = None
    furnishing: Optional[str] = None  # "FULLY", "SEMI", "UNFURNISHED"
    facing: Optional[str] = None
    possession_date: Optional[str] = None  # ISO date
    amenities: list[str] = field(default_factory=list)

    def to_dict(self):
        return {k: v for k, v in asdict(self).items() if v is not None
                and not (k == "amenities" and not v)}


@dataclass
class Observation:
    """
    Core observation record.
    
    Immutable after creation. Never overwrite — append only.
    
    Fields:
      observation_id:   Unique identifier (UUID or auto-increment)
      building_id:      Resolved BuildingID (0 if unresolved)
      observation_type: One of OBSERVATION_TYPES
      source:           One of SOURCES
      observed_at:      When the event occurred (listing date, transaction date, etc.)
      payload:          Flexible JSON — varies by observation_type
      confidence:       0.0–1.0 how reliable this observation is
      source_reference: URL, message ID, or external key
      created_at:       When this observation was ingested into PropAI
    """
    observation_id: str = ""
    building_id: int = 0
    observation_type: str = ""
    source: str = ""
    observed_at: str = ""  # ISO date
    payload: dict = field(default_factory=dict)
    confidence: float = 1.0
    source_reference: str = ""
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat() + "Z"

    def to_dict(self) -> dict:
        return {
            "observation_id": self.observation_id,
            "building_id": self.building_id,
            "observation_type": self.observation_type,
            "source": self.source,
            "observed_at": self.observed_at,
            "payload": json.dumps(self.payload, default=str) if isinstance(self.payload, dict) else self.payload,
            "confidence": self.confidence,
            "source_reference": self.source_reference,
            "created_at": self.created_at,
        }

    def to_csv_row(self) -> dict:
        return {
            "observation_id": self.observation_id,
            "building_id": self.building_id,
            "observation_type": self.observation_type,
            "source": self.source,
            "observed_at": self.observed_at,
            "payload_json": json.dumps(self.payload, default=str),
            "confidence": self.confidence,
            "source_reference": self.source_reference,
            "created_at": self.created_at,
        }


OBSERVATION_CSV_FIELDS = [
    "observation_id", "building_id", "observation_type", "source",
    "observed_at", "payload_json", "confidence", "source_reference", "created_at",
]


@dataclass
class UnresolvedObservation:
    """
    An observation where BuildingID could not be resolved.
    
    These are queued for manual resolution or automatic re-attempt
    as the registry grows.
    """
    unresolved_id: str = ""
    raw_building_name: str = ""
    raw_area: str = ""
    observation_type: str = ""
    source: str = ""
    observed_at: str = ""
    payload: dict = field(default_factory=dict)
    raw_source_data: dict = field(default_factory=dict)
    confidence: float = 0.0
    source_reference: str = ""
    resolve_attempts: int = 0
    status: str = "pending"  # pending, resolved, discarded
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat() + "Z"
