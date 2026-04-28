"""
Stream the 1.56M Kalshi historical markets, compute upset rates by category
at extreme prices (>=0.85 and <=0.15). Output CSV ranked by sample size.
Excludes obvious-upset categories (team sports, individual sports, parlays).
"""
import json
import re
from pathlib import Path
from collections import defaultdict
from decimal import Decimal, InvalidOperation

INPUT = Path(r"C:\Users\caden\Desktop\prediction_market_bot\data\historical\kalshi_markets.jsonl")
OUTPUT = Path(r"C:\Users\caden\Desktop\prediction_market_bot\data\historical\upset_rates.csv")

# Category prefixes to skip entirely. Add to this list as you discover others.
EXCLUDE_PREFIXES = {
    # Team sports — high upset
    "KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME", "KXNFLGAME", "KXMLBGAME", "KXNHLGAME",
    "KXEPLGAME", "KXLALIGA", "KXBUNDESLIGA", "KXSERIEA", "KXLIGUE1", "KXMLS",
    "KXCHAMPIONSLEAGUE", "KXEPL", "KXEPLGOAL", "KXEPLFIRSTGOAL",
    # Individual sports / props — huge upset
    "KXATPMATCH", "KXWTAMATCH", "KXMMA", "KXBOXING", "KXTABLETENNIS", "KXDARTSMATCH",
    # Parlays / multi-game
    "KXMVE", "KXMVESPORTSMULTIGAMEEXTENDED", "KXMULTIGAME",
    # Esports
    "KXLOL", "KXCSGO", "KXDOTA",
    # Generic spread / props
    "KXNBASPREAD", "KXN