"""
Season utilities for archiving and season management.
Separated from schedule_utils which handles schedule metadata.
"""

from pathlib import Path
from NFL_Locks.utils.data_utils import DATA_DIR


def get_current_season():
    """
    Get the current season year from schedule metadata.

    Returns:
        int: Season year from schedule (e.g., 2025)
    """
    # Get from schedule metadata (single source of truth)
    try:
        from NFL_Locks.utils.schedule_utils import get_season_year
        season = get_season_year()
        if season:
            return season
    except ImportError:
        pass
    
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
    archive_dir = DATA_DIR / "archives" / f"season_{season_year}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    
    return archive_dir
