"""
Schedule utilities for dynamic week handling.
Handles future NFL expansion (18 → 19+ weeks) automatically.
Uses schedule metadata as single source of truth.
"""

from datetime import datetime, timedelta
from NFL_Locks.utils.data_utils import load_full_schedule, get_schedule_metadata
from NFL_Locks.utils.config import DEFAULT_MAX_WEEK
from NFL_Locks.utils.constants import EASTERN

# Cache metadata for performance (loaded once per bot session)
_cached_metadata = None

def _get_metadata():
    """Get cached metadata or load it."""
    global _cached_metadata
    
    if _cached_metadata is None:
        _cached_metadata = get_schedule_metadata()
    
    return _cached_metadata


def get_max_week():
    """
    Get the maximum week number from the schedule.
    Uses metadata for instant lookup instead of iterating.
    
    Returns:
        int: Highest week number with games, or 18 as fallback
    """
    metadata = _get_metadata()
    
    # Use metadata if available (fast - O(1))
    if metadata and "total_weeks" in metadata:
        return metadata["total_weeks"]
    
    # Fallback: iterate through schedule (slower - O(n))
    schedule = load_full_schedule()
    
    if not schedule:
        return DEFAULT_MAX_WEEK  # Ultimate fallback
    
    max_week = 0
    for week_str in schedule.keys():
        try:
            week_num = int(week_str)
            if week_num > max_week:
                max_week = week_num
        except ValueError:
            continue
    
    return max_week if max_week > 0 else DEFAULT_MAX_WEEK


def get_all_weeks():
    """
    Get list of all week numbers in the schedule.
    Uses metadata for instant lookup.
    
    Returns:
        list: Sorted list of week numbers [1, 2, 3, ..., 18]
    """
    metadata = _get_metadata()
    
    # Use metadata if available (fast - O(1))
    if metadata and "regular_season_weeks" in metadata:
        return metadata["regular_season_weeks"]
    
    # Fallback: iterate through schedule
    schedule = load_full_schedule()
    
    if not schedule:
        return list(range(1, get_max_week() + 1))  # Fallback: derive from max week
    
    weeks = []
    for week_str in schedule.keys():
        try:
            week_num = int(week_str)
            weeks.append(week_num)
        except ValueError:
            continue
    
    return sorted(weeks)


def get_season_year():
    """
    Get the current season year.
    
    Returns:
        int: Season year (e.g., 2025), or None
    """
    metadata = _get_metadata()
    
    if metadata and "season" in metadata:
        return metadata["season"]
    
    return None


def get_schedule_last_updated():
    """
    Get when the schedule was last updated.
    
    Returns:
        str: ISO timestamp, or None
    """
    metadata = _get_metadata()
    
    if metadata and "last_updated" in metadata:
        return metadata["last_updated"]
    
    return None


def week_exists(week_number):
    """
    Check if a week exists in the schedule.
    
    Args:
        week_number: Week to check
    
    Returns:
        bool: True if week has games, False otherwise
    """
    schedule = load_full_schedule()
    
    if not schedule:
        return 1 <= week_number <= 18  # Fallback
    
    return str(week_number) in schedule


def get_week_range_text():
    """
    Get text describing valid week range.
    
    Returns:
        str: "1-18" or "1-19" etc based on actual schedule
    """
    max_week = get_max_week()
    return f"1-{max_week}"


def refresh_metadata_cache():
    """Force reload of metadata cache (call after schedule update)."""
    global _cached_metadata
    _cached_metadata = None
    return _get_metadata()


# -- Week detection -------------------------------------------------------------

def get_current_week_info(now=None):
    """
    Determine the current NFL week and whether the bot is in-season.

    Single source of truth — previously duplicated across Winners and
    GamesManager cogs (with the GamesManager copy missing the grace period).

    Args:
        now: datetime to evaluate against (Eastern). Defaults to datetime.now(EASTERN).

    Returns:
        (current_week, previous_week, in_nfl_season)
            current_week  — int or None  (the week whose window contains now)
            previous_week — int or None  (the most recently completed week)
            in_nfl_season — bool         (True during season + grace period)
    """
    from NFL_Locks.utils.data_utils import load_full_schedule
    from NFL_Locks.utils.config import SEASON_END_GRACE_DAYS
    from datetime import datetime, timedelta

    if now is None:
        now = datetime.now(EASTERN)

    schedule = load_full_schedule()
    if not schedule:
        return None, None, False

    current_week = None
    previous_week = None
    in_nfl_season = False
    last_week_end = None

    max_week = get_max_week()

    for wk in range(1, max_week + 1):
        week_games = schedule.get(str(wk))
        if not week_games:
            continue

        first_game_utc = datetime.fromisoformat(week_games[0]["date"].replace('Z', '+00:00'))
        first_game = first_game_utc.astimezone(EASTERN)

        days_since_tuesday = (first_game.weekday() - 1) % 7
        week_start = first_game - timedelta(days=days_since_tuesday)
        week_end = week_start + timedelta(days=6, hours=23, minutes=59)

        if wk == max_week:
            last_week_end = week_end

        if week_start <= now <= week_end:
            current_week = wk
            in_nfl_season = True
            if wk > 1:
                previous_week = wk - 1
            break
        elif now > week_end:
            previous_week = wk

    # Grace period: stay in-season for SEASON_END_GRACE_DAYS after the final
    # week ends so the Tuesday results/standings post for Week 18 still fires.
    if (
        not in_nfl_season
        and previous_week == max_week
        and last_week_end is not None
        and now <= last_week_end + timedelta(days=SEASON_END_GRACE_DAYS)
    ):
        in_nfl_season = True

    return current_week, previous_week, in_nfl_season


# ============ SEASON MANAGEMENT FUNCTIONS ============

def get_current_season():
    """
    Get the current season year from schedule metadata.
    This is the PRIMARY function - uses schedule as single source of truth.
    
    Returns:
        int: Season year from schedule (e.g., 2025)
    """
    season = get_season_year()
    
    if season:
        return season
    
    # If schedule metadata not available, schedule needs updating
    # Return a safe default but log a warning
    import warnings
    warnings.warn(
        "Schedule metadata not found. Run !update_schedule_now to update the schedule.",
        UserWarning
    )
    
    # Default to 2025 as safe fallback
    # (Admin should run !update_schedule_now to fix this)
    return 2025


def ensure_season_directory(season_year):
    """
    Ensure archive directory exists for a season.

    Args:
        season_year: Season year (e.g., 2025)

    Returns:
        Path: Path to season archive directory
    """
    from pathlib import Path

    archive_dir = Path(__file__).parent.parent / "data" / "archives" / f"season_{season_year}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    return archive_dir


def find_current_week(
    schedule: dict, now: datetime, max_week: int
) -> int | None:
    """Return the current NFL week number, or None if not in an active week.

    A week spans from the Tuesday before its first game through the following
    Monday at 23:59 ET.  Weeks that have no games in the schedule are skipped.

    Args:
        schedule: Full schedule dict keyed by str week number (from load_full_schedule()).
        now:      Current datetime, timezone-aware (Eastern).
        max_week: Upper bound for week search (from get_max_week()).

    Returns:
        Week number (int) if now falls inside an active week, else None.
    """
    for wk in range(1, max_week + 1):
        week_games = schedule.get(str(wk))
        if not week_games:
            continue

        first_game_utc = datetime.fromisoformat(
            week_games[0]["date"].replace('Z', '+00:00')
        )
        first_game = first_game_utc.astimezone(EASTERN)
        days_since_tuesday = (first_game.weekday() - 1) % 7
        week_start = first_game - timedelta(days=days_since_tuesday)
        week_end = week_start + timedelta(days=6, hours=23, minutes=59)

        if week_start <= now <= week_end:
            return wk

    return None
