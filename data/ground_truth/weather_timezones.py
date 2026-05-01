"""City timezone mapping for weather markets.

Maps Kalshi city abbreviations to IANA timezone names. Used by the
weather sniping strategy to compute close-time windows and identify
the start of the local day for ASOS observation aggregation.

Phoenix uses America/Phoenix because Arizona does not observe DST.
"""
from typing import Dict

CITY_TZ_MAP: Dict[str, str] = {
    "PHX": "America/Phoenix",
    "LV": "America/Los_Angeles",
    "HOU": "America/Chicago",
    "SATX": "America/Chicago",
    "NOLA": "America/Chicago",
    "ATL": "America/New_York",
    "DAL": "America/Chicago",
    "DC": "America/New_York",
    "SFO": "America/Los_Angeles",
    "SEA": "America/Los_Angeles",
    "OKC": "America/Chicago",
    "BOS": "America/New_York",
    "MIN": "America/Chicago",
    "MIA": "America/New_York",
    "AUS": "America/Chicago",
    "CHI": "America/Chicago",
    "DEN": "America/Denver",
    "NYC": "America/New_York",
    "PHIL": "America/New_York",
    "LAX": "America/Los_Angeles",
}
