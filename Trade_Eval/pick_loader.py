"""
Pick Loader - Google Sheets Integration
Loads team pick ownership from public Google Sheets for mock drafts and real NFL drafts.
"""

import requests
import csv
from io import StringIO
from difflib import get_close_matches


# GID mapping for sheet tabs - update these for your specific sheet
# To find GIDs: open each tab in browser and copy the #gid=XXXXXXX from URL
DEFAULT_ROUND_GIDS = {
    1: "651951279",   # Round 1
    2: "496755821",          # Round 2
    3: "1648445952",          # Round 3
    4: "1242493964",          # Round 4
    5: "907030450",          # Round 5
    6: "1554294941",          # Round 6
    7: "462986722",          # Round 7
}


def _parse_pick_number(pick_str: str) -> int | None:
    """
    Parse pick number from format like '1.18' or '2.33'.
    
    Args:
        pick_str: String like "1.18" (round.overall)
    
    Returns:
        Overall pick number (18, 33, etc.) or None if invalid
    """
    if not pick_str or '.' not in pick_str:
        return None
    
    try:
        parts = pick_str.split('.')
        if len(parts) != 2:
            return None
        overall = int(parts[1])
        if 1 <= overall <= 257:
            return overall
        return None
    except (ValueError, IndexError):
        return None


def _fetch_sheet_as_csv(sheet_id: str, gid: str) -> str:
    """
    Fetch a Google Sheet tab as CSV text.
    
    Args:
        sheet_id: The Google Sheets document ID
        gid: The sheet/tab ID (found in URL after #gid=)
    
    Returns:
        CSV text content
    
    Raises:
        requests.exceptions.RequestException on fetch failure
    """
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.text


def _parse_round_sheet(csv_text: str) -> dict:
    """
    Parse a round sheet CSV into team → pick mapping.
    
    Args:
        csv_text: CSV content from sheet
    
    Returns:
        {"Minnesota Vikings": 18, "Las Vegas Raiders": 1, ...}
        Maps team name to single pick number for this round
    """
    reader = csv.reader(StringIO(csv_text))
    team_picks = {}
    
    for row in reader:
        if len(row) < 3:  # Need at least columns A, B, C (index 0-2)
            continue
        
        pick_str = row[0].strip()  # Column A
        # Column B is empty in the format
        team_name = row[2].strip() if len(row) > 2 else ""  # Column C
        
        if not pick_str or not team_name:
            continue
        
        # Skip header rows
        if pick_str.lower() in ['pick', 'round'] or team_name.lower() in ['team', 'teams']:
            continue
        
        pick_num = _parse_pick_number(pick_str)
        if pick_num is not None:
            team_picks[team_name] = pick_num
    
    return team_picks


def load_team_picks(sheet_url: str, team_name: str, round_gids: dict = None) -> dict:
    """
    Load all picks for a team from Google Sheets.
    
    Args:
        sheet_url: Full Google Sheets URL
        team_name: Team name (fuzzy match supported)
        round_gids: Optional dict mapping round number to GID
                   If None, uses DEFAULT_ROUND_GIDS from module
    
    Returns:
        {
            "team": "Minnesota Vikings",  # Matched team name
            "picks": [18, 50, 82, 114, 146, 178, 210],  # Sorted list
            "rounds_loaded": 7,  # Number of rounds successfully loaded
            "error": None or "error message"
        }
    """
    if round_gids is None:
        round_gids = DEFAULT_ROUND_GIDS
    
    try:
        # Extract sheet ID from URL
        # Format: https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit...
        if '/d/' not in sheet_url:
            return {"team": None, "picks": [], "rounds_loaded": 0, "error": "Invalid Google Sheets URL"}
        
        sheet_id = sheet_url.split('/d/')[1].split('/')[0]
        
        # Fetch Round 1 to build team list for fuzzy matching
        if round_gids.get(1) is None:
            return {"team": None, "picks": [], "rounds_loaded": 0, "error": "Round 1 GID not configured"}
        
        try:
            round1_csv = _fetch_sheet_as_csv(sheet_id, round_gids[1])
            round1_teams = _parse_round_sheet(round1_csv)
            all_team_names = list(round1_teams.keys())
        except Exception as e:
            return {"team": None, "picks": [], "rounds_loaded": 0, "error": f"Failed to fetch Round 1: {e}"}
        
        # Fuzzy match team name
        matches = get_close_matches(team_name, all_team_names, n=1, cutoff=0.6)
        if not matches:
            # Show first 10 teams as examples
            examples = ', '.join(all_team_names[:10])
            return {"team": None, "picks": [], "rounds_loaded": 0, 
                   "error": f"Team '{team_name}' not found. Examples: {examples}..."}
        
        matched_team = matches[0]
        picks = []
        rounds_loaded = 0
        
        # Fetch each round and extract this team's pick
        for round_num in range(1, 8):
            gid = round_gids.get(round_num)
            if gid is None:
                # GID not configured, skip this round
                continue
            
            try:
                csv_text = _fetch_sheet_as_csv(sheet_id, gid)
                round_picks = _parse_round_sheet(csv_text)
                
                if matched_team in round_picks:
                    picks.append(round_picks[matched_team])
                    rounds_loaded += 1
            except Exception as e:
                # Round fetch failed, skip it
                continue
        
        picks.sort()
        
        return {
            "team": matched_team,
            "picks": picks,
            "rounds_loaded": rounds_loaded,
            "error": None
        }
    
    except Exception as e:
        return {"team": None, "picks": [], "rounds_loaded": 0, "error": f"Error loading picks: {e}"}


def update_gids(gids: dict) -> dict:
    """
    Update the default GID mapping.
    
    Args:
        gids: Dict like {1: "651951279", 2: "123456", ...}
    
    Returns:
        Updated GID mapping
    """
    DEFAULT_ROUND_GIDS.update(gids)
    return DEFAULT_ROUND_GIDS
