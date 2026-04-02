"""
data.sports.market_matcher – fuzzy-matches Kalshi sports market titles to ESPN games.

Matching algorithm:
  1. Lowercase and strip punctuation from the Kalshi market title
  2. Try exact alias lookup against team name dictionaries
  3. If no exact match, use difflib.SequenceMatcher (threshold 0.72) against
     all known canonical team names
  4. Extract both teams — market is only matchable if BOTH teams resolve
  5. Determine which team the market is asking about (home/away) and direction
  6. Cache the match result permanently for the life of that market (per-session)
  7. Markets that fail to match are added to a per-session skip set

Team alias coverage:
  NBA: all 30 teams with common abbreviations and city names
  NFL: all 32 teams with common abbreviations and city names
  NCAAB: top 68 tournament programs + common abbreviations

This module is stateless across bot restarts — the match cache lives in memory
only, so stale market assignments do not persist across runs.
"""

from __future__ import annotations

import difflib
import logging
import re
import string
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Fuzzy match threshold (0-1); below this → no_match
_FUZZY_THRESHOLD = 0.72

# Per-session caches
# market_id → (home_team, away_team, market_team, direction) or None for no_match
_match_cache: Dict[str, Optional[Tuple[str, str, str, str]]] = {}
_skip_set: set = set()

# ── Game-result market ID parsing ──────────────────────────────────────────────
# Kalshi game-result markets embed both team abbreviations in the market ID:
#   KXNBAGAME-26MAR13MEMDET-DET
#     date code : 26MAR13
#     team concat: MEMDET  (two 3-letter abbreviations concatenated)
#     yes suffix : DET     (the team whose win resolves YES)
# The regex captures (series, team_concat, yes_code).
_GAME_MARKET_RE = re.compile(
    r"^(KXNBAGAME|KXNCAAMBGAME|KXNFLGAME|KXNCAAWBGAME)-\d{2}[A-Z]{3}\d{2}([A-Z]{4,8})-([A-Z]{2,4})$"
)
_GAME_SERIES_SPORT: Dict[str, str] = {
    "KXNBAGAME": "nba",
    "KXNCAAMBGAME": "ncaab",
    "KXNFLGAME": "nfl",
    "KXNCAAWBGAME": "ncaaw",
}


# ── Team alias dictionaries ────────────────────────────────────────────────────

NBA_ALIASES: Dict[str, str] = {
    # Atlantic
    "celtics": "Boston Celtics",
    "boston celtics": "Boston Celtics",
    "bos": "Boston Celtics",
    "nets": "Brooklyn Nets",
    "brooklyn nets": "Brooklyn Nets",
    "bkn": "Brooklyn Nets",
    "knicks": "New York Knicks",
    "new york knicks": "New York Knicks",
    "nyk": "New York Knicks",
    "sixers": "Philadelphia 76ers",
    "76ers": "Philadelphia 76ers",
    "philadelphia 76ers": "Philadelphia 76ers",
    "phi": "Philadelphia 76ers",
    "raptors": "Toronto Raptors",
    "toronto raptors": "Toronto Raptors",
    "tor": "Toronto Raptors",
    # Central
    "bulls": "Chicago Bulls",
    "chicago bulls": "Chicago Bulls",
    "chi": "Chicago Bulls",
    "cavaliers": "Cleveland Cavaliers",
    "cavs": "Cleveland Cavaliers",
    "cleveland cavaliers": "Cleveland Cavaliers",
    "cle": "Cleveland Cavaliers",
    "pistons": "Detroit Pistons",
    "detroit pistons": "Detroit Pistons",
    "det": "Detroit Pistons",
    "pacers": "Indiana Pacers",
    "indiana pacers": "Indiana Pacers",
    "ind": "Indiana Pacers",
    "bucks": "Milwaukee Bucks",
    "milwaukee bucks": "Milwaukee Bucks",
    "mil": "Milwaukee Bucks",
    # Southeast
    "hawks": "Atlanta Hawks",
    "atlanta hawks": "Atlanta Hawks",
    "atl": "Atlanta Hawks",
    "hornets": "Charlotte Hornets",
    "charlotte hornets": "Charlotte Hornets",
    "cha": "Charlotte Hornets",
    "heat": "Miami Heat",
    "miami heat": "Miami Heat",
    "mia": "Miami Heat",
    "magic": "Orlando Magic",
    "orlando magic": "Orlando Magic",
    "orl": "Orlando Magic",
    "wizards": "Washington Wizards",
    "washington wizards": "Washington Wizards",
    "was": "Washington Wizards",
    "wsh": "Washington Wizards",
    # Northwest
    "nuggets": "Denver Nuggets",
    "denver nuggets": "Denver Nuggets",
    "den": "Denver Nuggets",
    "timberwolves": "Minnesota Timberwolves",
    "wolves": "Minnesota Timberwolves",
    "minnesota timberwolves": "Minnesota Timberwolves",
    "min": "Minnesota Timberwolves",
    "thunder": "Oklahoma City Thunder",
    "okc thunder": "Oklahoma City Thunder",
    "oklahoma city thunder": "Oklahoma City Thunder",
    "okc": "Oklahoma City Thunder",
    "trail blazers": "Portland Trail Blazers",
    "blazers": "Portland Trail Blazers",
    "portland trail blazers": "Portland Trail Blazers",
    "por": "Portland Trail Blazers",
    "jazz": "Utah Jazz",
    "utah jazz": "Utah Jazz",
    "uta": "Utah Jazz",
    # Pacific
    "warriors": "Golden State Warriors",
    "golden state warriors": "Golden State Warriors",
    "gsw": "Golden State Warriors",
    "gs warriors": "Golden State Warriors",
    "clippers": "Los Angeles Clippers",
    "la clippers": "Los Angeles Clippers",
    "los angeles clippers": "Los Angeles Clippers",
    "lac": "Los Angeles Clippers",
    "lakers": "Los Angeles Lakers",
    "la lakers": "Los Angeles Lakers",
    "los angeles lakers": "Los Angeles Lakers",
    "lal": "Los Angeles Lakers",
    "suns": "Phoenix Suns",
    "phoenix suns": "Phoenix Suns",
    "phx": "Phoenix Suns",
    "kings": "Sacramento Kings",
    "sacramento kings": "Sacramento Kings",
    "sac": "Sacramento Kings",
    # Southwest
    "mavericks": "Dallas Mavericks",
    "mavs": "Dallas Mavericks",
    "dallas mavericks": "Dallas Mavericks",
    "dal": "Dallas Mavericks",
    "rockets": "Houston Rockets",
    "houston rockets": "Houston Rockets",
    "hou": "Houston Rockets",
    "grizzlies": "Memphis Grizzlies",
    "memphis grizzlies": "Memphis Grizzlies",
    "mem": "Memphis Grizzlies",
    "pelicans": "New Orleans Pelicans",
    "new orleans pelicans": "New Orleans Pelicans",
    "nop": "New Orleans Pelicans",
    "no pelicans": "New Orleans Pelicans",
    "spurs": "San Antonio Spurs",
    "san antonio spurs": "San Antonio Spurs",
    "sas": "San Antonio Spurs",
    "sa spurs": "San Antonio Spurs",
}

NFL_ALIASES: Dict[str, str] = {
    # AFC East
    "bills": "Buffalo Bills",
    "buffalo bills": "Buffalo Bills",
    "buf": "Buffalo Bills",
    "dolphins": "Miami Dolphins",
    "miami dolphins": "Miami Dolphins",
    "mia": "Miami Dolphins",
    "patriots": "New England Patriots",
    "new england patriots": "New England Patriots",
    "ne patriots": "New England Patriots",
    "ne": "New England Patriots",
    "nep": "New England Patriots",
    "jets": "New York Jets",
    "new york jets": "New York Jets",
    "nyj": "New York Jets",
    # AFC North
    "ravens": "Baltimore Ravens",
    "baltimore ravens": "Baltimore Ravens",
    "bal": "Baltimore Ravens",
    "bengals": "Cincinnati Bengals",
    "cincinnati bengals": "Cincinnati Bengals",
    "cin": "Cincinnati Bengals",
    "browns": "Cleveland Browns",
    "cleveland browns": "Cleveland Browns",
    "cle": "Cleveland Browns",
    "steelers": "Pittsburgh Steelers",
    "pittsburgh steelers": "Pittsburgh Steelers",
    "pit": "Pittsburgh Steelers",
    # AFC South
    "texans": "Houston Texans",
    "houston texans": "Houston Texans",
    "hou": "Houston Texans",
    "colts": "Indianapolis Colts",
    "indianapolis colts": "Indianapolis Colts",
    "ind": "Indianapolis Colts",
    "jaguars": "Jacksonville Jaguars",
    "jacksonville jaguars": "Jacksonville Jaguars",
    "jax": "Jacksonville Jaguars",
    "jac": "Jacksonville Jaguars",
    "titans": "Tennessee Titans",
    "tennessee titans": "Tennessee Titans",
    "ten": "Tennessee Titans",
    # AFC West
    "broncos": "Denver Broncos",
    "denver broncos": "Denver Broncos",
    "den": "Denver Broncos",
    "chiefs": "Kansas City Chiefs",
    "kansas city chiefs": "Kansas City Chiefs",
    "kc": "Kansas City Chiefs",
    "kcc": "Kansas City Chiefs",
    "raiders": "Las Vegas Raiders",
    "las vegas raiders": "Las Vegas Raiders",
    "lv raiders": "Las Vegas Raiders",
    "lv": "Las Vegas Raiders",
    "lvr": "Las Vegas Raiders",
    "chargers": "Los Angeles Chargers",
    "la chargers": "Los Angeles Chargers",
    "los angeles chargers": "Los Angeles Chargers",
    "lac": "Los Angeles Chargers",
    # NFC East
    "cowboys": "Dallas Cowboys",
    "dallas cowboys": "Dallas Cowboys",
    "dal": "Dallas Cowboys",
    "giants": "New York Giants",
    "new york giants": "New York Giants",
    "nyg": "New York Giants",
    "ny giants": "New York Giants",
    "eagles": "Philadelphia Eagles",
    "philadelphia eagles": "Philadelphia Eagles",
    "phi": "Philadelphia Eagles",
    "commanders": "Washington Commanders",
    "washington commanders": "Washington Commanders",
    "was": "Washington Commanders",
    "wsh": "Washington Commanders",
    # NFC North
    "bears": "Chicago Bears",
    "chicago bears": "Chicago Bears",
    "chi": "Chicago Bears",
    "lions": "Detroit Lions",
    "detroit lions": "Detroit Lions",
    "det": "Detroit Lions",
    "packers": "Green Bay Packers",
    "green bay packers": "Green Bay Packers",
    "gb": "Green Bay Packers",
    "gbp": "Green Bay Packers",
    "vikings": "Minnesota Vikings",
    "minnesota vikings": "Minnesota Vikings",
    "min": "Minnesota Vikings",
    # NFC South
    "falcons": "Atlanta Falcons",
    "atlanta falcons": "Atlanta Falcons",
    "atl": "Atlanta Falcons",
    "panthers": "Carolina Panthers",
    "carolina panthers": "Carolina Panthers",
    "car": "Carolina Panthers",
    "saints": "New Orleans Saints",
    "new orleans saints": "New Orleans Saints",
    "no": "New Orleans Saints",
    "nos": "New Orleans Saints",
    "buccaneers": "Tampa Bay Buccaneers",
    "bucs": "Tampa Bay Buccaneers",
    "tampa bay buccaneers": "Tampa Bay Buccaneers",
    "tb": "Tampa Bay Buccaneers",
    "tbb": "Tampa Bay Buccaneers",
    # NFC West
    "cardinals": "Arizona Cardinals",
    "arizona cardinals": "Arizona Cardinals",
    "ari": "Arizona Cardinals",
    "az": "Arizona Cardinals",
    "rams": "Los Angeles Rams",
    "la rams": "Los Angeles Rams",
    "los angeles rams": "Los Angeles Rams",
    "lar": "Los Angeles Rams",
    "49ers": "San Francisco 49ers",
    "san francisco 49ers": "San Francisco 49ers",
    "sf": "San Francisco 49ers",
    "sf 49ers": "San Francisco 49ers",
    "niners": "San Francisco 49ers",
    "seahawks": "Seattle Seahawks",
    "seattle seahawks": "Seattle Seahawks",
    "sea": "Seattle Seahawks",
}

NCAAB_ALIASES: Dict[str, str] = {
    # Power conferences and common tournament programs
    "duke": "Duke Blue Devils",
    "duke blue devils": "Duke Blue Devils",
    "north carolina": "North Carolina Tar Heels",
    "unc": "North Carolina Tar Heels",
    "tar heels": "North Carolina Tar Heels",
    "kentucky": "Kentucky Wildcats",
    "uk": "Kentucky Wildcats",
    "wildcats": "Kentucky Wildcats",
    "kansas": "Kansas Jayhawks",
    "ku": "Kansas Jayhawks",
    "jayhawks": "Kansas Jayhawks",
    "gonzaga": "Gonzaga Bulldogs",
    "gonzaga bulldogs": "Gonzaga Bulldogs",
    "villanova": "Villanova Wildcats",
    "nova": "Villanova Wildcats",
    "michigan state": "Michigan State Spartans",
    "msu": "Michigan State Spartans",
    "spartans": "Michigan State Spartans",
    "michigan": "Michigan Wolverines",
    "wolverines": "Michigan Wolverines",
    "ucla": "UCLA Bruins",
    "bruins": "UCLA Bruins",
    "arizona": "Arizona Wildcats",
    "asu": "Arizona State Sun Devils",
    "texas": "Texas Longhorns",
    "longhorns": "Texas Longhorns",
    "florida": "Florida Gators",
    "gators": "Florida Gators",
    "indiana": "Indiana Hoosiers",
    "hoosiers": "Indiana Hoosiers",
    "ohio state": "Ohio State Buckeyes",
    "osu": "Ohio State Buckeyes",
    "buckeyes": "Ohio State Buckeyes",
    "wisconsin": "Wisconsin Badgers",
    "badgers": "Wisconsin Badgers",
    "purdue": "Purdue Boilermakers",
    "boilermakers": "Purdue Boilermakers",
    "iowa": "Iowa Hawkeyes",
    "hawkeyes": "Iowa Hawkeyes",
    "illinois": "Illinois Fighting Illini",
    "illini": "Illinois Fighting Illini",
    "illinois state": "Illinois State Redbirds",
    "illinois st": "Illinois State Redbirds",
    "ilst": "Illinois State Redbirds",      # Kalshi ticker code for Illinois State
    "penn state": "Penn State Nittany Lions",
    "psu": "Penn State Nittany Lions",
    "maryland": "Maryland Terrapins",
    "terps": "Maryland Terrapins",
    "nebraska": "Nebraska Cornhuskers",
    "rutgers": "Rutgers Scarlet Knights",
    "northwestern": "Northwestern Wildcats",
    "minnesota": "Minnesota Golden Gophers",
    "baylor": "Baylor Bears",
    "oklahoma": "Oklahoma Sooners",
    "sooners": "Oklahoma Sooners",
    "oklahoma state": "Oklahoma State Cowboys",
    "ok state": "Oklahoma State Cowboys",
    "texas tech": "Texas Tech Red Raiders",
    "red raiders": "Texas Tech Red Raiders",
    "tcu": "TCU Horned Frogs",
    "west virginia": "West Virginia Mountaineers",
    "wvu": "West Virginia Mountaineers",
    "mountaineers": "West Virginia Mountaineers",
    "iowa state": "Iowa State Cyclones",
    "cyclones": "Iowa State Cyclones",
    "kansas state": "Kansas State Wildcats",
    "k-state": "Kansas State Wildcats",
    "cincinnati": "Cincinnati Bearcats",
    "bearcats": "Cincinnati Bearcats",
    "houston": "Houston Cougars",
    "cougars": "Houston Cougars",
    "memphis": "Memphis Tigers",
    "tennessee": "Tennessee Volunteers",
    "tenn": "Tennessee Volunteers",
    "vols": "Tennessee Volunteers",
    "arkansas": "Arkansas Razorbacks",
    "razorbacks": "Arkansas Razorbacks",
    "lsu": "LSU Tigers",
    "auburn": "Auburn Tigers",
    "alabama": "Alabama Crimson Tide",
    "crimson tide": "Alabama Crimson Tide",
    "mississippi state": "Mississippi State Bulldogs",
    "ole miss": "Ole Miss Rebels",
    "vanderbilt": "Vanderbilt Commodores",
    "commodores": "Vanderbilt Commodores",
    "south carolina": "South Carolina Gamecocks",
    "gamecocks": "South Carolina Gamecocks",
    "georgia": "Georgia Bulldogs",
    "ga bulldogs": "Georgia Bulldogs",
    "virginia": "Virginia Cavaliers",
    "uva": "Virginia Cavaliers",
    "cavaliers": "Virginia Cavaliers",
    "virginia tech": "Virginia Tech Hokies",
    "hokies": "Virginia Tech Hokies",
    "nc state": "NC State Wolfpack",
    "wolfpack": "NC State Wolfpack",
    "wake forest": "Wake Forest Demon Deacons",
    "wake": "Wake Forest Demon Deacons",    # Kalshi ticker code for Wake Forest
    "syracuse": "Syracuse Orange",
    "notre dame": "Notre Dame Fighting Irish",
    "nd": "Notre Dame Fighting Irish",
    "fighting irish": "Notre Dame Fighting Irish",
    "louisville": "Louisville Cardinals",
    "pittsburgh": "Pittsburgh Panthers",
    "pitt": "Pittsburgh Panthers",
    "florida state": "Florida State Seminoles",
    "fsu": "Florida State Seminoles",
    "seminoles": "Florida State Seminoles",
    "miami fl": "Miami (FL) Hurricanes",
    "miami florida": "Miami (FL) Hurricanes",
    "hurricanes": "Miami (FL) Hurricanes",
    "connecticut": "Connecticut Huskies",
    "uconn": "Connecticut Huskies",
    "huskies": "Connecticut Huskies",
    "st johns": "St. John's Red Storm",
    "st john's": "St. John's Red Storm",
    "georgetown": "Georgetown Hoyas",
    "hoyas": "Georgetown Hoyas",
    "marquette": "Marquette Golden Eagles",
    "seton hall": "Seton Hall Pirates",
    "providence": "Providence Friars",
    "butler": "Butler Bulldogs",
    "xavier": "Xavier Musketeers",
    "creighton": "Creighton Bluejays",
    "depaul": "DePaul Blue Demons",
    "wichita state": "Wichita State Shockers",
    "shockers": "Wichita State Shockers",
    "dayton": "Dayton Flyers",
    "richmond": "Richmond Spiders",
    "davidson": "Davidson Wildcats",
    "loyola chicago": "Loyola Chicago Ramblers",
    "belmont": "Belmont Bruins",
    "furman": "Furman Paladins",
    "saint peter's": "Saint Peter's Peacocks",
    "saint peters": "Saint Peter's Peacocks",
    "oral roberts": "Oral Roberts Golden Eagles",
    "eastern washington": "Eastern Washington Eagles",
    "drake": "Drake Bulldogs",
    # Kalshi short codes used in KXNCAAMBGAME market IDs (fast-path aliases)
    "ncst": "NC State Wolfpack",
    "ttu": "Texas Tech Red Raiders",
    "ala": "Alabama Crimson Tide",
    "mich": "Michigan Wolverines",
    "minn": "Minnesota Golden Gophers",
    "miss": "Ole Miss Rebels",
    "syr": "Syracuse Orange",
    "ill": "Illinois Fighting Illini",
    "van": "Vanderbilt Commodores",
    "ore": "Oregon Ducks",
    "okla": "Oklahoma Sooners",
    "fla": "Florida Gators",
    "pur": "Purdue Boilermakers",
    "ku": "Kansas Jayhawks",
    "sju": "St. John's Red Storm",
    "mia": "Miami (FL) Hurricanes",
    "conn": "Connecticut Huskies",
    "usu": "Utah State Aggies",
    "ariz": "Arizona Wildcats",
}

# NCAA Women's Basketball aliases — Kalshi ticker codes and common names
# ESPN displayName for women's teams matches the school name (e.g. "North Carolina Tar Heels")
# Exceptions: Tennessee women = "Tennessee Lady Vols", Georgia women = "Georgia Lady Bulldogs"
NCAAW_ALIASES: Dict[str, str] = {
    # ACC
    "unc": "North Carolina Tar Heels",
    "north carolina": "North Carolina Tar Heels",
    "tar heels": "North Carolina Tar Heels",
    "md": "Maryland Terrapins",
    "maryland": "Maryland Terrapins",
    "terps": "Maryland Terrapins",
    "duke": "Duke Blue Devils",
    "blue devils": "Duke Blue Devils",
    "nc state": "NC State Wolfpack",
    "wolfpack": "NC State Wolfpack",
    "virginia tech": "Virginia Tech Hokies",
    "vt": "Virginia Tech Hokies",
    "hokies": "Virginia Tech Hokies",
    "virginia": "Virginia Cavaliers",
    "uva": "Virginia Cavaliers",
    "cavaliers": "Virginia Cavaliers",
    "florida state": "Florida State Seminoles",
    "fsu": "Florida State Seminoles",
    "seminoles": "Florida State Seminoles",
    "louisville": "Louisville Cardinals",
    "pittsburgh": "Pittsburgh Panthers",
    "pitt": "Pittsburgh Panthers",
    "miami fl": "Miami (FL) Hurricanes",
    "hurricanes": "Miami (FL) Hurricanes",
    "boston college": "Boston College Eagles",
    "bc": "Boston College Eagles",
    "wake forest": "Wake Forest Demon Deacons",
    "wake": "Wake Forest Demon Deacons",
    "georgia tech": "Georgia Tech Yellow Jackets",
    "notre dame": "Notre Dame Fighting Irish",
    "nd": "Notre Dame Fighting Irish",
    "fighting irish": "Notre Dame Fighting Irish",
    "syracuse": "Syracuse Orange",
    # SEC
    "south carolina": "South Carolina Gamecocks",
    "sc": "South Carolina Gamecocks",
    "scrc": "South Carolina Gamecocks",
    "gamecocks": "South Carolina Gamecocks",
    "tennessee": "Tennessee Lady Vols",
    "tenn": "Tennessee Lady Vols",
    "lady vols": "Tennessee Lady Vols",
    "lsu": "LSU Tigers",
    "texas am": "Texas A&M Aggies",
    "tamu": "Texas A&M Aggies",
    "aggies": "Texas A&M Aggies",
    "georgia": "Georgia Lady Bulldogs",
    "ga": "Georgia Lady Bulldogs",
    "lady bulldogs": "Georgia Lady Bulldogs",
    "florida": "Florida Gators",
    "gators": "Florida Gators",
    "alabama": "Alabama Crimson Tide",
    "crimson tide": "Alabama Crimson Tide",
    "kentucky": "Kentucky Wildcats",
    "uk": "Kentucky Wildcats",
    "arkansas": "Arkansas Razorbacks",
    "razorbacks": "Arkansas Razorbacks",
    "mississippi state": "Mississippi State Bulldogs",
    "miss st": "Mississippi State Bulldogs",
    "ole miss": "Ole Miss Rebels",
    "vanderbilt": "Vanderbilt Commodores",
    "auburn": "Auburn Tigers",
    "missouri": "Missouri Tigers",
    "miz": "Missouri Tigers",
    # Big 12
    "texas": "Texas Longhorns",
    "tex": "Texas Longhorns",
    "longhorns": "Texas Longhorns",
    "baylor": "Baylor Bears",
    "bears": "Baylor Bears",
    "kansas": "Kansas Jayhawks",
    "ku": "Kansas Jayhawks",
    "jayhawks": "Kansas Jayhawks",
    "kansas state": "Kansas State Wildcats",
    "ksu": "Kansas State Wildcats",
    "k-state": "Kansas State Wildcats",
    "oklahoma": "Oklahoma Sooners",
    "ou": "Oklahoma Sooners",
    "sooners": "Oklahoma Sooners",
    "oklahoma state": "Oklahoma State Cowgirls",
    "okst": "Oklahoma State Cowgirls",
    "iowa state": "Iowa State Cyclones",
    "isu": "Iowa State Cyclones",
    "cyclones": "Iowa State Cyclones",
    "west virginia": "West Virginia Mountaineers",
    "wvu": "West Virginia Mountaineers",
    "tcu": "TCU Horned Frogs",
    "texas tech": "Texas Tech Lady Raiders",
    "ttu": "Texas Tech Lady Raiders",
    "colorado": "Colorado Buffaloes",
    "colo": "Colorado Buffaloes",
    "buffaloes": "Colorado Buffaloes",
    "arizona": "Arizona Wildcats",
    "arizona state": "Arizona State Sun Devils",
    "asu": "Arizona State Sun Devils",
    "utah": "Utah Utes",
    # Big Ten
    "iowa": "Iowa Hawkeyes",
    "hawkeyes": "Iowa Hawkeyes",
    "ohio state": "Ohio State Buckeyes",
    "osu": "Ohio State Buckeyes",
    "buckeyes": "Ohio State Buckeyes",
    "michigan": "Michigan Wolverines",
    "wolverines": "Michigan Wolverines",
    "michigan state": "Michigan State Spartans",
    "msu": "Michigan State Spartans",
    "spartans": "Michigan State Spartans",
    "indiana": "Indiana Hoosiers",
    "hoosiers": "Indiana Hoosiers",
    "purdue": "Purdue Boilermakers",
    "boilermakers": "Purdue Boilermakers",
    "minnesota": "Minnesota Golden Gophers",
    "gophers": "Minnesota Golden Gophers",
    "penn state": "Penn State Nittany Lions",
    "psu": "Penn State Nittany Lions",
    "illinois": "Illinois Fighting Illini",
    "illini": "Illinois Fighting Illini",
    "wisconsin": "Wisconsin Badgers",
    "badgers": "Wisconsin Badgers",
    "nebraska": "Nebraska Cornhuskers",
    "rutgers": "Rutgers Scarlet Knights",
    "northwestern": "Northwestern Wildcats",
    # Pac-12 / independents
    "stanford": "Stanford Cardinal",
    "stan": "Stanford Cardinal",
    "cardinal": "Stanford Cardinal",
    "ucla": "UCLA Bruins",
    "bruins": "UCLA Bruins",
    "usc": "USC Trojans",
    "trojans": "USC Trojans",
    "oregon": "Oregon Ducks",
    "ducks": "Oregon Ducks",
    "washington": "Washington Huskies",
    "wash": "Washington Huskies",
    # Big East / American
    "connecticut": "Connecticut Huskies",
    "uconn": "Connecticut Huskies",
    "conn": "Connecticut Huskies",
    "huskies": "Connecticut Huskies",
    "villanova": "Villanova Wildcats",
    "nova": "Villanova Wildcats",
    "marquette": "Marquette Golden Eagles",
    "creighton": "Creighton Bluejays",
    "seton hall": "Seton Hall Pirates",
    "georgetown": "Georgetown Hoyas",
    "depaul": "DePaul Blue Demons",
    "providence": "Providence Friars",
    "st johns": "St. John's Red Storm",
    "st john's": "St. John's Red Storm",
    "butler": "Butler Bulldogs",
    "xavier": "Xavier Musketeers",
    "houston": "Houston Cougars",
    "cougars": "Houston Cougars",
    "memphis": "Memphis Tigers",
    "cincinnati": "Cincinnati Bearcats",
    "ucf": "UCF Knights",
    "south florida": "South Florida Bulls",
    # Common Kalshi code overrides (2-letter codes that differ from NCAAB)
    "sc": "South Carolina Gamecocks",   # override generic "sc" for women's
    # Kalshi short codes used in KXNCAAWBGAME market IDs that differ from
    # the full-name aliases above.  Without these the fast path in match_market
    # falls through to title extraction, which can fail for mid-major schools.
    "ncst": "NC State Wolfpack",
    "mich": "Michigan Wolverines",
    "minn": "Minnesota Golden Gophers",
    "miss": "Ole Miss Rebels",          # MISSMINN = Ole Miss vs Minnesota
    "okla": "Oklahoma Sooners",
    "ore": "Oregon Ducks",
    "ala": "Alabama Crimson Tide",
    "ill": "Illinois Fighting Illini",
    "van": "Vanderbilt Commodores",
    "syr": "Syracuse Orange",
    "scar": "South Carolina Gamecocks",
    # Mid-majors / newer programs appearing in women's tournament
    "pfw": "Purdue Fort Wayne Mastodons",
    "south alabama": "South Alabama Jaguars",
    "usa": "South Alabama Jaguars",
    "fgcu": "Florida Gulf Coast Eagles",
    "florida gulf coast": "Florida Gulf Coast Eagles",
    "purdue fort wayne": "Purdue Fort Wayne Mastodons",
    "southern indiana": "Southern Indiana Screaming Eagles",
    "usi": "Southern Indiana Screaming Eagles",
    "george washington": "George Washington Colonials",
    "gw": "George Washington Colonials",
}

# Canonical name set for fuzzy matching (populated at module load)
_ALL_CANONICAL: Dict[str, str] = {}  # canonical → sport key


def _build_canonical_index() -> None:
    """Build the reverse index from canonical team name to sport."""
    for alias_dict, sport in (
        (NBA_ALIASES, "nba"),
        (NFL_ALIASES, "nfl"),
        (NCAAB_ALIASES, "ncaab"),
        # Note: NCAAW not added here — many teams share names with NCAAB.
        # Sport resolution for ncaaw relies on sport_hint from the market ID prefix.
    ):
        for canonical in alias_dict.values():
            _ALL_CANONICAL[canonical.lower()] = sport


_build_canonical_index()

# Punctuation stripper
_PUNCT_RE = re.compile(r"[^\w\s]")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", _PUNCT_RE.sub(" ", text.lower())).strip()


def _alias_lookup(token: str, sport: Optional[str] = None) -> Optional[str]:
    """Try exact alias lookup, optionally scoped to a sport."""
    alias_dicts = []
    if sport in (None, "nba"):
        alias_dicts.append(NBA_ALIASES)
    if sport in (None, "nfl"):
        alias_dicts.append(NFL_ALIASES)
    if sport in (None, "ncaab"):
        alias_dicts.append(NCAAB_ALIASES)
    if sport in (None, "ncaaw"):
        alias_dicts.append(NCAAW_ALIASES)

    for d in alias_dicts:
        if token in d:
            return d[token]
    return None


def _fuzzy_lookup(token: str) -> Optional[str]:
    """Fuzzy match against all canonical team names. Returns canonical or None."""
    canonicals = list(_ALL_CANONICAL.keys())
    matches = difflib.get_close_matches(token, canonicals, n=1, cutoff=_FUZZY_THRESHOLD)
    if matches:
        matched_lower = matches[0]
        # Return the properly-cased canonical name
        for alias_dict in (NBA_ALIASES, NFL_ALIASES, NCAAB_ALIASES):
            for canonical in alias_dict.values():
                if canonical.lower() == matched_lower:
                    return canonical
    return None


def _extract_teams_from_title(title: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract two team names from a Kalshi market title.

    Supports common Kalshi formats:
      "Will the Lakers beat the Celtics?"
      "Lakers vs Celtics"
      "NBA: Golden State Warriors at Boston Celtics"
      "Will Kansas City Chiefs win vs Philadelphia Eagles?"
    """
    norm = _normalize(title)

    # Try "vs" or "at" separators first — most reliable
    for sep in (" vs ", " vs. ", " v ", " at "):
        idx = norm.find(sep)
        if idx != -1:
            left = norm[:idx].strip()
            right = norm[idx + len(sep):].strip().split("?")[0].split(" win")[0].strip()
            # Strip leading articles
            for art in ("the ", "will the ", "will "):
                if left.startswith(art):
                    left = left[len(art):]
                if right.startswith(art):
                    right = right[len(art):]
            return left, right

    # "Will X beat Y" or "Will X win against Y"
    m = re.search(
        r"will (?:the )?(.+?) (?:beat|defeat|win against|cover against) (?:the )?(.+?)(?:\?|$)",
        norm,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # "Will X win" (single team)
    m = re.search(r"will (?:the )?(.+?) win\b", norm)
    if m:
        return m.group(1).strip(), None

    return None, None


def _resolve_team(raw: Optional[str]) -> Optional[str]:
    """Resolve a raw team string to a canonical team name."""
    if not raw:
        return None
    # Try exact alias first
    result = _alias_lookup(raw)
    if result:
        return result
    # Try each word/phrase subset (e.g. "lakers" extracted from "la lakers game 7")
    tokens = raw.split()
    for length in (3, 2, 1):
        for i in range(len(tokens) - length + 1):
            phrase = " ".join(tokens[i:i + length])
            result = _alias_lookup(phrase)
            if result:
                return result
    # Fall back to fuzzy matching
    return _fuzzy_lookup(raw)


def match_market(market_id: str, title: str, sport_hint: Optional[str] = None) -> Optional[dict]:
    """
    Match a Kalshi market to an ESPN game via team name resolution.

    Parameters
    ----------
    market_id  : Kalshi market ID (used as the permanent cache key)
    title      : human-readable market title / question
    sport_hint : optional "nba" | "nfl" | "ncaab" to narrow alias lookups

    Returns
    -------
    dict with keys:
      home_team   : str
      away_team   : str
      market_team : str  — which team the market is asking about
      direction   : "win" | "lose"
      sport       : "nba" | "nfl" | "ncaab"
    or None if the market cannot be matched.

    The result is cached permanently for the life of the session; call this
    function freely — it performs real work only on the first call per market_id.
    """
    if market_id in _skip_set:
        return None

    if market_id in _match_cache:
        cached = _match_cache[market_id]
        if cached is None:
            return None
        home_team, away_team, market_team, direction = cached
        # Prefer sport_hint (derived from market ID prefix, always accurate).
        # Fall back to _sport_of_team for NBA/NFL/NCAAB; ncaaw teams share
        # names with ncaab so sport_hint is the only reliable source for them.
        sport = sport_hint or _sport_of_team(market_team) or "nba"
        return {
            "home_team": home_team,
            "away_team": away_team,
            "market_team": market_team,
            "direction": direction,
            "sport": sport,
        }

    # ── Fast path: parse team abbreviations directly from the market ID ──────
    # For KXNBAGAME / KXNCAAMBGAME markets the ID encodes both teams and the
    # YES outcome, which is more reliable than regex-ing the question text.
    # Falls back to title extraction if abbreviations don't resolve.
    id_m = _GAME_MARKET_RE.match(market_id)
    if id_m:
        series, team_concat, yes_code = id_m.group(1), id_m.group(2), id_m.group(3)
        sport_from_id = _GAME_SERIES_SPORT[series]
        # Split the concatenated team codes; try splits until one half matches
        # the yes_code (handles both 3+3 and edge-case lengths).
        parsed_codes: Optional[Tuple[str, str]] = None
        for split in range(2, len(team_concat) - 1):
            a, b = team_concat[:split], team_concat[split:]
            if yes_code.lower() in (a.lower(), b.lower()):
                parsed_codes = (a, b)
                break
        if parsed_codes is None and len(team_concat) >= 6:
            parsed_codes = (team_concat[:3], team_concat[3:])  # best-effort 3+3
        if parsed_codes:
            code_a, code_b = parsed_codes
            res_a = _alias_lookup(code_a.lower(), sport_from_id)
            res_b = _alias_lookup(code_b.lower(), sport_from_id)
            if res_a and res_b:
                yes_team = res_a if yes_code.lower() == code_a.lower() else res_b
                other_team = res_b if yes_team == res_a else res_a
                result_tuple = (yes_team, other_team, yes_team, "win")
                _match_cache[market_id] = result_tuple
                logger.debug(
                    "MarketMatcher: game-id match %s → yes=%s other=%s sport=%s",
                    market_id, yes_team, other_team, sport_from_id,
                )
                return {
                    "home_team": yes_team,
                    "away_team": other_team,
                    "market_team": yes_team,
                    "direction": "win",
                    "sport": sport_from_id,
                }
            logger.debug(
                "MarketMatcher: game-id parse %s — abbreviations unresolved "
                "(%s=%r, %s=%r), falling back to title matching",
                market_id, code_a, res_a, code_b, res_b,
            )
        sport_hint = sport_hint or sport_from_id  # carry sport into title fallback

    raw_left, raw_right = _extract_teams_from_title(title)

    team1 = _resolve_team(raw_left)
    team2 = _resolve_team(raw_right)

    if team1 is None:
        logger.debug("MarketMatcher: no_match for %s — team1 unresolved (raw=%r)", market_id, raw_left)
        _match_cache[market_id] = None
        _skip_set.add(market_id)
        return None

    # If we only have one team (single-team market), team2 is None
    if team2 is None and raw_right is not None:
        logger.debug("MarketMatcher: no_match for %s — team2 unresolved (raw=%r)", market_id, raw_right)
        _match_cache[market_id] = None
        _skip_set.add(market_id)
        return None

    # Determine direction from title
    norm_title = _normalize(title)
    direction = "win"
    if any(word in norm_title for word in ("beat", "defeat", "win against", "cover")):
        direction = "win"
    elif "lose" in norm_title or "not win" in norm_title:
        direction = "lose"

    # The market team is the one the question is primarily asking about
    market_team = team1

    # For "X vs Y" formats we treat X as home and Y as away by convention
    # (ESPN may differ; the shock detector resolves home/away from game state)
    home_team = team1
    away_team = team2 or team1  # single-team market: home==away for matching

    sport = sport_hint or _sport_of_team(market_team) or "nba"

    result_tuple = (home_team, away_team, market_team, direction)
    _match_cache[market_id] = result_tuple

    logger.debug(
        "MarketMatcher: matched %s → home=%s away=%s market_team=%s dir=%s sport=%s",
        market_id, home_team, away_team, market_team, direction, sport,
    )
    return {
        "home_team": home_team,
        "away_team": away_team,
        "market_team": market_team,
        "direction": direction,
        "sport": sport,
    }


def _sport_of_team(team_name: str) -> Optional[str]:
    """Return the sport for a canonical team name."""
    tl = team_name.lower()
    for alias_dict, sport in (
        (NBA_ALIASES, "nba"),
        (NFL_ALIASES, "nfl"),
        (NCAAB_ALIASES, "ncaab"),
    ):
        if any(canonical.lower() == tl for canonical in alias_dict.values()):
            return sport
    return None


def clear_match_cache() -> None:
    """Clear the match cache and skip set (call between sessions)."""
    _match_cache.clear()
    _skip_set.clear()


def match_coverage_stats() -> dict:
    """Return cache statistics for the ghost-mode validation checklist."""
    total = len(_match_cache)
    matched = sum(1 for v in _match_cache.values() if v is not None)
    skipped = len(_skip_set)
    return {
        "total_seen": total,
        "matched": matched,
        "skipped": skipped,
        "match_rate_pct": round(100 * matched / total, 1) if total else 0.0,
    }
