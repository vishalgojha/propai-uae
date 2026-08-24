"""
WhatsApp Adapter.

Extracts observations from broker WhatsApp group messages.
Maps to observation types: BROKER_REQUIREMENT, BROKER_OFFER, BROKER_MENTION.

This is a high-signal source — brokers actively discuss requirements,
available inventory, and price expectations in real-time.
"""
import re
from typing import Optional

from evidence.adapters import ObservationAdapter


SOURCE = "WHATSAPP"
SOURCE_URL = ""  # No public URL — messages are scraped from groups


# ── Patterns for extracting structured data from unstructured messages ──

# "Need 2BHK in XYZ Building, budget 80L"
REQ_PATTERN = re.compile(
    r"(?:need|want|looking\s*for|requirement|client\s*looking|buyer\s*looking)"
    r".*?(\d+\s*(?:BHK|BR)|1\s*RK|studio).*?(?:in|at)\s+([A-Za-z\s]+?)(?:,|\.|$|budget|aed|dhs)",
    re.IGNORECASE,
)

# "Offering 3BHK in ABC Tower, 1.2Cr"
OFFER_PATTERN = re.compile(
    r"(?:offering|available|deal|listing|property)\s*(?:for\s*sale|for\s*rent)?"
    r".*?(\d+\s*(?:BHK|BR)|1\s*RK|studio).*?(?:in|at)\s+([A-Za-z\s]+?)(?:,|\.|$|budget|aed|dhs)",
    re.IGNORECASE,
)

# Price pattern: AED 85K, 95k, 2.5M, 1.5 million
PRICE_PATTERN = re.compile(
    r"(?:aed|dhs)?[\s.]*\s*(\d+\.?\d*)\s*(M|mn|million|K|k)",
    re.IGNORECASE,
)

# Building mention in general discussion
MENTION_PATTERN = re.compile(
    r"(?:in|at|near|opposite|behind|above)\s+([A-Z][A-Za-z\s]+?)(?:,|\.|$|\s+and\s)",
)


def extract(message: str, sender: str = "", timestamp: str = "") -> list[dict]:
    """
    Extract observations from a single WhatsApp message.
    
    Returns a list of observations (often 0 or 1, but a message
    can contain both a requirement and an offer).
    """
    observations = []
    
    # Try requirement pattern
    req_match = REQ_PATTERN.search(message)
    if req_match:
        unit_type = req_match.group(1).strip()
        building_name = req_match.group(2).strip()
        price_match = PRICE_PATTERN.search(message)
        budget = _parse_price(price_match.group(0)) if price_match else None
        
        payload = {
            "unit_type": unit_type,
            "budget": budget,
            "requirement_text": message.strip()[:500],
            "sender": sender,
            "building_name": building_name,
        }
        
        observations.append({
            "building_name": building_name,
            "area": "",
            "developer": "",
            "observation_type": "BROKER_REQUIREMENT",
            "source": SOURCE,
            "observed_at": timestamp,
            "payload": {k: v for k, v in payload.items() if v is not None},
            "source_reference": f"wa_{sender}_{timestamp}",
        })
    
    # Try offer pattern
    offer_match = OFFER_PATTERN.search(message)
    if offer_match:
        unit_type = offer_match.group(1).strip()
        building_name = offer_match.group(2).strip()
        price_match = PRICE_PATTERN.search(message)
        price = _parse_price(price_match.group(0)) if price_match else None
        
        payload = {
            "unit_type": unit_type,
            "price": price,
            "offer_text": message.strip()[:500],
            "sender": sender,
            "building_name": building_name,
        }
        
        observations.append({
            "building_name": building_name,
            "area": "",
            "developer": "",
            "observation_type": "BROKER_OFFER",
            "source": SOURCE,
            "observed_at": timestamp,
            "payload": {k: v for k, v in payload.items() if v is not None},
            "source_reference": f"wa_{sender}_{timestamp}",
        })
    
    # If no structured pattern matched but building mentioned
    if not observations:
        mention_match = MENTION_PATTERN.search(message)
        if mention_match:
            building_name = mention_match.group(1).strip()
            observations.append({
                "building_name": building_name,
                "area": "",
                "developer": "",
                "observation_type": "BROKER_MENTION",
                "source": SOURCE,
                "observed_at": timestamp,
                "payload": {
                    "snippet": message.strip()[:300],
                    "sender": sender,
                },
                "source_reference": f"wa_{sender}_{timestamp}",
            })
    
    return observations


def _parse_price(text: str) -> Optional[float]:
    """Parse UAE price format: 85K, 95k, 2.5M, 1.5 million (annual AED)."""
    match = PRICE_PATTERN.search(text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    if unit in ("m", "mn", "million"):
        return amount * 1000000
    if unit in ("k",):
        return amount * 1000
    return amount


def extract_batch(messages: list[dict]) -> list[dict]:
    """Extract observations from a batch of WhatsApp messages.
    
    Each message dict:
      - text: str
      - sender: str (optional)
      - timestamp: str (optional, ISO date)
    """
    all_obs = []
    for msg in messages:
        text = msg.get("text", "")
        sender = msg.get("sender", "")
        timestamp = msg.get("timestamp", "")
        all_obs.extend(extract(text, sender, timestamp))
    return all_obs


class WhatsAppAdapter:
    source = SOURCE
    
    def fetch(self, **kwargs) -> list[dict]:
        return []
