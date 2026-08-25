"""
Intelligence Computation Plan.

All market intelligence is DERIVED from observations, never stored.
Queries are executed against the observation store at request time.

Design principles:
  - No computed metrics are persisted (no pre-aggregation)
  - Every query is a temporal slice (time range is always required)
  - Results are cached per-query, never pre-computed
  - All prices are in AED, all areas in sqft

Intelligence metrics (ordered by implementation priority):

  1. CURRENT SUPPLY/DEMAND
     - Supply: count of active SALE_LISTING + RENT_LISTING per building/micro_market
     - Demand: count of BROKER_REQUIREMENT per building/micro_market
     - Supply-to-Demand ratio (SDR): supply_count / demand_count
       - SDR < 1: seller's market (more buyers than inventory)
       - SDR > 3: buyer's market (excess inventory)
       - SDR 1-3: balanced

  2. AVERAGE / MEDIAN ASKING PRICE
     - By building, micro_market, bedroom count
     - Time-windowed (30d, 90d, 180d, 1y)
     - Filter by observation_type = SALE_LISTING, source != IGR
     - Price per sqft for normalized comparison

  3. PRICE TREND
     - Month-over-month change in avg/median asking price
     - Requires at least 2 data points in consecutive months
     - Signal: "appreciating", "stable", "declining"
     - Thresholds: >2% MoM = appreciating, < -2% = declining

  4. INVENTORY VELOCITY
     - Days on market: observed_at of listing → status change to sold/rented
     - Average days on market by building and micro_market
     - Requires STATUS_CHANGE or IGR_TRANSACTION as termination signal
     - Low velocity (< 30 days) = hot building

  5. BROKER ACTIVITY
     - Total BROKER_OFFER + BROKER_REQUIREMENT per building per week
     - Unique broker count per building (by sender phone)
     - Broker attention index: current week count / 4-week average
     - High index = building is "trending" in broker circles

  6. RENTAL YIELD
     - (Annual rent / property price) * 100
     - Annual rent = RENT_LISTING.price * 12
     - Property price = SALE_LISTING.price (same or nearby unit)
     - By building, micro_market
     - Compare to bank FD rate (~7%) to identify investment-grade buildings

  7. MARKET TEMPERATURE
     - Composite score (0-100) for a micro_market
     - Components:
       - Price momentum (30%): recent vs 6-month avg price
       - Inventory velocity (25%): days on market
       - Broker intensity (20%): broker activity index
       - Supply ratio (15%): SDR score
       - Transaction volume (10%): IGR_TRANSACTION count
     - Interpretation: 0-30 = cold, 30-60 = neutral, 60-80 = warm, 80-100 = hot

  8. LIQUIDITY SCORE
     - How quickly a building can be bought/sold
     - Components:
       - Number of active listings (40%)
       - Average days on market (30%)
       - Number of unique brokers active (20%)
       - Recent transaction count (10%)
     - Score 0-100, higher = more liquid


Usage:
  from evidence.intelligence import IntelligenceEngine
  
  engine = IntelligenceEngine()
  
  # Get current supply/demand for a micro market
  sdr = engine.supply_demand_ratio(micro_market="Worli", days=90)
  
  # Price trend for a building
  trend = engine.price_trend(building_id=123, months=6)
  
  # Full market temperature report
  temp = engine.market_temperature(micro_market="Thane West")
"""

from datetime import datetime, timedelta
from typing import Optional


class IntelligenceEngine:
    """Derives market intelligence from observations.
    
    All queries are temporal. Always specify a time range.
    """

    def __init__(self, data_path: str = ""):
        self.data_path = data_path

    def supply_demand_ratio(
        self,
        building_id: int = 0,
        micro_market: str = "",
        days: int = 90,
    ) -> dict:
        """Calculate supply-to-demand ratio.
        
        Returns:
            {
                "supply": int,
                "demand": int,
                "ratio": float,
                "interpretation": str,
                "period_days": int,
            }
        """
        return {"supply": 0, "demand": 0, "ratio": 0, "interpretation": "insufficient_data", "period_days": days}

    def price_trend(
        self,
        building_id: int = 0,
        micro_market: str = "",
        months: int = 6,
    ) -> dict:
        """Calculate price trend over months.
        
        Returns:
            {
                "months": [{"month": str, "avg_price": float, "median_price": float}],
                "overall_trend": str,  # appreciating / stable / declining
                "mom_change_pct": float,
                "data_points": int,
            }
        """
        return {"months": [], "overall_trend": "insufficient_data", "mom_change_pct": 0, "data_points": 0}

    def inventory_velocity(
        self,
        building_id: int = 0,
        micro_market: str = "",
        days: int = 180,
    ) -> dict:
        """Calculate average days on market.
        
        Returns:
            {
                "avg_days": float,
                "median_days": float,
                "samples": int,
                "interpretation": str,
            }
        """
        return {"avg_days": 0, "median_days": 0, "samples": 0, "interpretation": "insufficient_data"}

    def broker_activity(
        self,
        building_id: int = 0,
        micro_market: str = "",
        days: int = 30,
    ) -> dict:
        """Calculate broker attention index.
        
        Returns:
            {
                "current_week_count": int,
                "four_week_avg": float,
                "attention_index": float,
                "unique_brokers": int,
                "interpretation": str,
            }
        """
        return {"current_week_count": 0, "four_week_avg": 0, "attention_index": 0, "unique_brokers": 0, "interpretation": "insufficient_data"}

    def rental_yield(
        self,
        building_id: int = 0,
        micro_market: str = "",
        days: int = 90,
    ) -> dict:
        """Calculate estimated rental yield.
        
        Returns:
            {
                "yield_pct": float,
                "avg_annual_rent": float,
                "avg_property_price": float,
                "samples": int,
                "interpretation": str,
            }
        """
        return {"yield_pct": 0, "avg_annual_rent": 0, "avg_property_price": 0, "samples": 0, "interpretation": "insufficient_data"}

    def market_temperature(
        self,
        micro_market: str,
        days: int = 90,
    ) -> dict:
        """Composite market temperature score.
        
        Returns:
            {
                "micro_market": str,
                "score": int,  # 0-100
                "label": str,  # cold / neutral / warm / hot
                "components": {
                    "price_momentum": {"score": float, "weight": 0.3},
                    "inventory_velocity": {"score": float, "weight": 0.25},
                    "broker_intensity": {"score": float, "weight": 0.2},
                    "supply_ratio": {"score": float, "weight": 0.15},
                    "transaction_volume": {"score": float, "weight": 0.1},
                },
                "data_quality": str,  # high / medium / low — based on sample sizes
            }
        """
        return {
            "micro_market": micro_market,
            "score": 0,
            "label": "insufficient_data",
            "components": {},
            "data_quality": "low",
        }

    def liquidity_score(
        self,
        building_id: int = 0,
        micro_market: str = "",
        days: int = 90,
    ) -> dict:
        """Calculate how quickly a building can be bought/sold.
        
        Returns:
            {
                "score": int,  # 0-100
                "label": str,
                "components": {...},
                "data_quality": str,
            }
        """
        return {"score": 0, "label": "insufficient_data", "components": {}, "data_quality": "low"}
