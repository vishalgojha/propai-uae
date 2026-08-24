"""
Example Observations — one per source + per observation type.

These illustrate the expected input format for each source adapter.
Run with `python -m evidence.example_observations` to see the output.
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evidence.models import Observation, OBSERVATION_TYPES, SOURCES
from evidence.pipeline import create_pipeline

EXAMPLES = {}


# ── HOUSING.COM — SALE_LISTING ──────────────────────────────────
EXAMPLES["housing_sale"] = {
    "building_name": "Lodha Belvedere",
    "area": "Worli",
    "developer": "Lodha Group",
    "observation_type": "SALE_LISTING",
    "source": "HOUSING",
    "observed_at": "2026-06-15",
    "source_reference": "https://www.bayut.com/property/marina-gate-dubai-marina-dubai",
    "payload": {
        "bedrooms": 3,
        "bathrooms": 3,
        "area_sqft": 1850,
        "price": 85000000,
        "price_per_sqft": 45946,
        "floor": 18,
        "total_floors": 52,
        "furnishing": "FULLY",
        "facing": "Sea",
        "possession_date": "2026-12",
        "amenities": ["Pool", "Gym", "Parking", "Clubhouse"],
    },
}

# ── HOUSING.COM — RENT_LISTING ──────────────────────────────────
EXAMPLES["housing_rent"] = {
    "building_name": "Runwal Greens",
    "area": "Mulund",
    "developer": "Runwal Group",
    "observation_type": "RENT_LISTING",
    "source": "HOUSING",
    "observed_at": "2026-06-20",
    "source_reference": "https://www.bayut.com/property/burj-vista-downtown-dubai-dubai",
    "payload": {
        "bedrooms": 2,
        "bathrooms": 2,
        "area_sqft": 950,
        "price": 45000,
        "price_per_sqft": 47,
        "floor": 12,
        "total_floors": 35,
        "furnishing": "SEMI",
        "possession_date": "Immediate",
        "amenities": ["Gym", "Garden", "Parking"],
    },
}

# ── MAGICBRICKS — BROKER_OFFER ──────────────────────────────────
EXAMPLES["magicbricks_offer"] = {
    "building_name": "Oberoi Sky City",
    "area": "Goregaon East",
    "developer": "Oberoi Realty",
    "observation_type": "BROKER_OFFER",
    "source": "MAGICBRICKS",
    "observed_at": "2026-06-18",
    "source_reference": "https://www.magicbricks.com/property/oberoi-sky-city-goregaon-east",
    "payload": {
        "bedrooms": 2,
        "bathrooms": 2,
        "area_sqft": 1200,
        "price": 19500000,
        "price_per_sqft": 16250,
        "floor": 8,
        "furnishing": "SEMI",
        "possession_date": "Ready to Move",
        "posted_by": "Agent",
        "building_name": "Oberoi Sky City",
    },
}

# ── MAGICBRICKS — SALE_LISTING (Builder) ────────────────────────
EXAMPLES["magicbricks_sale"] = {
    "building_name": "Godrej Woodsville",
    "area": "Thane West",
    "developer": "Godrej Properties",
    "observation_type": "SALE_LISTING",
    "source": "MAGICBRICKS",
    "observed_at": "2026-06-10",
    "source_reference": "https://www.magicbricks.com/property/godrej-woodsville-thane-west",
    "payload": {
        "bedrooms": 3,
        "bathrooms": 2,
        "area_sqft": 1450,
        "price": 32000000,
        "price_per_sqft": 22069,
        "floor": 15,
        "total_floors": 28,
        "furnishing": "FULLY",
        "possession_date": "Ready to Move",
        "posted_by": "Builder",
        "amenities": ["Pool", "Clubhouse", "Jogging Track"],
    },
}

# ── MAHARERA — NEW PROJECT REGISTRATION ─────────────────────────
EXAMPLES["maharera_registration"] = {
    "building_name": "Piramal Mahalaxmi",
    "area": "Mahalaxmi",
    "developer": "Piramal Realty",
    "observation_type": "MAHARERA_PROJECT",
    "source": "MAHARERA",
    "observed_at": "2026-04-01",
    "source_reference": "P51800012345",
    "payload": {
        "rera_number": "P51800012345",
        "project_status": "Registered",
        "total_floors": 45,
        "total_units": 320,
        "project_area_sqmt": 28000,
        "project_address": "Marina Gate, Dubai Marina, Dubai",
    },
}

# ── MAHARERA — PROJECT STATUS CHANGE ────────────────────────────
EXAMPLES["maharera_completion"] = {
    "building_name": "Lodha The Park",
    "area": "Worli",
    "developer": "Lodha Group",
    "observation_type": "MAHARERA_PROJECT",
    "source": "MAHARERA",
    "observed_at": "2026-05-30",
    "source_reference": "P51800045678",
    "payload": {
        "rera_number": "P51800045678",
        "project_status": "Completed",
    },
}

# ── IGR — SALE DEED TRANSACTION ─────────────────────────────────
EXAMPLES["igr_transaction"] = {
    "building_name": "Kalpataru Aura",
    "area": "Bhandup West",
    "developer": "",
    "observation_type": "IGR_TRANSACTION",
    "source": "IGR",
    "observed_at": "2026-05-12",
    "source_reference": "DEED-MH-2026-1234567",
    "payload": {
        "deed_number": "DEED-MH-2026-1234567",
        "consideration_amount": 12500000,
        "stamp_duty_paid": 750000,
        "area_sqft": 850,
        "buyer_name": "Amit Sharma",
        "seller_name": "Rohan Desai",
        "sub_register_office": "Bhandup",
        "deed_type": "Sale Deed",
    },
}

# ── IGR — MULTI-UNIT SALE ───────────────────────────────────────
EXAMPLES["igr_multi_transaction"] = {
    "building_name": "Rustomjee Urbania",
    "area": "Thane West",
    "developer": "",
    "observation_type": "IGR_TRANSACTION",
    "source": "IGR",
    "observed_at": "2026-06-01",
    "source_reference": "DEED-MH-2026-2345678",
    "payload": {
        "deed_number": "DEED-MH-2026-2345678",
        "consideration_amount": 45000000,
        "stamp_duty_paid": 2700000,
        "area_sqft": 2100,
        "buyer_name": "Priya Patel",
        "seller_name": "Keystone Realtors",
        "sub_register_office": "Thane",
        "deed_type": "Sale Deed",
    },
}

# ── WHATSAPP — BROKER REQUIREMENT ───────────────────────────────
EXAMPLES["whatsapp_requirement"] = {
    "building_name": "Hiranandani Gardens",
    "area": "Powai",
    "developer": "",
    "observation_type": "BROKER_REQUIREMENT",
    "source": "WHATSAPP",
    "observed_at": "2026-06-22",
    "source_reference": "wa_+919876543210_2026-06-22",
    "payload": {
        "unit_type": "2 BHK",
        "budget": 15000000,
        "requirement_text": "Need 2BHK in Hiranandani Gardens, budget 1.5Cr. Client ready to close in 15 days.",
        "sender": "+919876543210",
    },
}

# ── WHATSAPP — BROKER OFFER ─────────────────────────────────────
EXAMPLES["whatsapp_offer"] = {
    "building_name": "Sobha City",
    "area": "Thane West",
    "developer": "",
    "observation_type": "BROKER_OFFER",
    "source": "WHATSAPP",
    "observed_at": "2026-06-21",
    "source_reference": "wa_+919876543211_2026-06-21",
    "payload": {
        "unit_type": "3 BHK",
        "price": 27500000,
        "offer_text": "Available 3BHK in Sobha City, 1300 sqft, floor 16, 2.75Cr negotiable. Owner ready.",
        "sender": "+919876543211",
    },
}

# ── WHATSAPP — BROKER MENTION ───────────────────────────────────
EXAMPLES["whatsapp_mention"] = {
    "building_name": "The 42",
    "area": "",
    "developer": "",
    "observation_type": "BROKER_MENTION",
    "source": "WHATSAPP",
    "observed_at": "2026-06-19",
    "source_reference": "wa_+919876543212_2026-06-19",
    "payload": {
        "snippet": "Anyone have a deal in The 42? My client is very specific about that building only.",
        "sender": "+919876543212",
    },
}

# ── 99ACRES — SALE_LISTING ──────────────────────────────────────
EXAMPLES["99acres_sale"] = {
    "building_name": "Sarvodaya Heights",
    "area": "Vile Parle West",
    "developer": "Sarvodaya Developers",
    "observation_type": "SALE_LISTING",
    "source": "99ACRES",
    "observed_at": "2026-06-14",
    "source_reference": "https://www.99acres.com/property/sarvodaya-heights-vile-parle-west",
    "payload": {
        "bedrooms": 2,
        "bathrooms": 2,
        "area_sqft": 1100,
        "price": 23000000,
        "price_per_sqft": 20909,
        "floor": 5,
        "total_floors": 15,
        "furnishing": "FULLY",
        "facing": "East",
        "possession_date": "Immediate",
    },
}

# ── PORTAL — SALE_LISTING (legacy format from v1 scrape) ─────────
EXAMPLES["portal_sale"] = {
    "building_name": "Omkar Alta Monte",
    "area": "Malabar Hill",
    "developer": "Omkar Realtors",
    "observation_type": "SALE_LISTING",
    "source": "PORTAL",
    "observed_at": "2026-03-10",
    "source_reference": "",
    "payload": {
        "bedrooms": 4,
        "bathrooms": 5,
        "area_sqft": 3200,
        "price": 240000000,
        "price_per_sqft": 75000,
        "furnishing": "FULLY",
        "total_floors": 40,
        "possession_date": "Ready to Move",
    },
}


def demonstrate():
    """Run each example through the pipeline and print results."""
    from evidence.pipeline import create_pipeline
    from evidence.resolver import resolve

    print("=" * 72)
    print("PropAI Evidence Engine — Example Observations")
    print("=" * 72)

    for key, obs in EXAMPLES.items():
        print(f"\n── {key.upper()} ──────────────────────────────────────")
        print(f"  Type: {obs['observation_type']}")
        print(f"  Source: {obs['source']}")
        print(f"  Building: {obs['building_name']}")
        print(f"  Area: {obs.get('area', '')}")
        print(f"  Date: {obs['observed_at']}")

        # Show resolution result
        bid, confidence, method = resolve(
            obs["building_name"],
            obs.get("area", ""),
            obs.get("developer", ""),
        )
        print(f"  Resolution: building_id={bid}, confidence={confidence}, method={method}")

        # Show key payload fields
        payload = obs.get("payload", {})
        if "price" in payload:
            print(f"  Price: AED {payload['price']:,}")
        if "bedrooms" in payload:
            print(f"  Unit: {payload['bedrooms']} BHK")
        if "area_sqft" in payload:
            print(f"  Area: {payload['area_sqft']} sqft")


if __name__ == "__main__":
    demonstrate()
