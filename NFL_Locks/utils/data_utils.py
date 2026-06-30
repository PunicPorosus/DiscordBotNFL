"""
Data utilities with support for new schedule format.
Schedule now has metadata for single source of truth.
Uses year-based folder structure: data/2025/weekX.json
"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"


def _get_season_dir():
    """Get the current season's data directory."""
    from NFL_Locks.utils.schedule_utils import get_current_season
    season = get_current_season()
    season_dir = DATA_DIR / str(season)
    season_dir.mkdir(parents=True, exist_ok=True)
    return season_dir


def load_json(filepath):
    """
    Generic JSON loader (backwards compatibility).
    
    Args:
        filepath: Path to JSON file
    
    Returns:
        dict or None
    """
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return None


def save_json(filepath, data):
    """
    Generic JSON saver (backwards compatibility).
    
    Args:
        filepath: Path to JSON file
        data: Data to save
    """
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving {filepath}: {e}")


def load_full_schedule():
    """
    Load the full NFL schedule.
    
    Returns:
        dict: Schedule in format {"1": [games], "2": [games], ...}
    """
    schedule_file = DATA_DIR / "full_schedule.json"
    
    if not schedule_file.exists():
        return None
    
    try:
        with open(schedule_file, "r") as f:
            data = json.load(f)
        
        # Handle new format with metadata at top level
        # Week data is at top level: {"_metadata": {...}, "1": [...], "2": [...]}
        if "_metadata" in data:
            # Filter out metadata keys (start with _) and return just week data
            week_data = {k: v for k, v in data.items() if not k.startswith("_")}
            return week_data
        
        # Old format - return as-is
        return data
    
    except Exception as e:
        print(f"Error loading schedule: {e}")
        return None


def get_schedule_metadata():
    """
    Get schedule metadata (season, total weeks, etc).
    
    Returns:
        dict: {"season": 2025, "total_weeks": 18, "last_updated": "...", ...}
        None if no metadata or old format
    """
    schedule_file = DATA_DIR / "full_schedule.json"
    
    if not schedule_file.exists():
        return None
    
    try:
        with open(schedule_file, "r") as f:
            data = json.load(f)
        
        return data.get("_metadata")
    
    except Exception as e:
        print(f"Error loading schedule metadata: {e}")
        return None


def load_week_file(week_number):
    """Load data for a specific week from current season folder."""
    season_dir = _get_season_dir()
    week_file = season_dir / f"week{week_number}.json"
    
    if not week_file.exists():
        return None
    
    try:
        with open(week_file, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading week {week_number}: {e}")
        return None


def save_week_file(week_number, data):
    """
    Save data for a specific week to current season folder.

    NOTE: Not called by any live cog. Retained for status_migration.py's
    one-time migration command (!build_status) and as a fallback utility.
    """
    season_dir = _get_season_dir()
    week_file = season_dir / f"week{week_number}.json"

    try:
        with open(week_file, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving week {week_number}: {e}")


def get_all_week_files():
    """
    Get list of all week files that exist in current season.

    NOTE: Not called by any live cog. Retained as a diagnostic/migration utility.

    Returns:
        list: [1, 2, 3, ...] for existing week files
    """
    season_dir = _get_season_dir()

    if not season_dir.exists():
        return []

    weeks = []
    for file in season_dir.glob("week*.json"):
        try:
            week_num = int(file.stem.replace("week", ""))
            weeks.append(week_num)
        except (ValueError, IndexError):
            continue

    return sorted(weeks)


def update_leaderboard(guild_id, user_scores, season_year):
    """
    Update leaderboard file for a guild/season.
    
    Note: Leaderboards are typically calculated on-the-fly from week files.
    This function saves a snapshot for archiving purposes.
    
    Args:
        guild_id: Discord guild ID
        user_scores: dict of {username: score}
        season_year: Season year (e.g., 2025)
    """
    season_dir = DATA_DIR / str(season_year)
    season_dir.mkdir(parents=True, exist_ok=True)
    
    leaderboard_file = season_dir / f"leaderboard_{guild_id}.json"
    
    try:
        with open(leaderboard_file, "w") as f:
            json.dump(user_scores, f, indent=2)
    except Exception as e:
        print(f"Error saving leaderboard: {e}")
