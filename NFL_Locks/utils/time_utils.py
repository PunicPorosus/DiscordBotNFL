"""
Time utilities for NFL picks bot with dynamic deadline calculation.
Uses actual kickoff times from schedule instead of hardcoded rules.

Note: EASTERN is imported from BotUtils.time (the bot-wide source of truth).
"""

from datetime import datetime
from BotUtils.time import EASTERN  # noqa: F401 — re-exported for existing cog imports

def get_week_deadline(week_number):
    """
    Get the deadline for a week (kickoff of first game).
    
    Now dynamically calculated from schedule instead of hardcoded rules.
    No more special cases for Thanksgiving or Christmas!
    
    Args:
        week_number: NFL week (1-18)
    
    Returns:
        datetime object in Eastern timezone, or None if not found
    """
    from NFL_Locks.utils.data_utils import load_full_schedule
    
    schedule = load_full_schedule()
    if not schedule:
        return None
    
    week_games = schedule.get(str(week_number))
    if not week_games:
        return None
    
    # Find the earliest kickoff in the week
    earliest_time = None

    for game in week_games:
        game_datetime_str = game.get("date")
        if not game_datetime_str:
            continue

        try:
            # Parse ISO format datetime
            # Format from ESPN: "2025-12-14T20:15Z" (UTC)
            game_datetime = datetime.fromisoformat(game_datetime_str.replace('Z', '+00:00'))

            # Convert to Eastern time
            game_datetime_et = game_datetime.astimezone(EASTERN)

            if earliest_time is None or game_datetime_et < earliest_time:
                earliest_time = game_datetime_et

        except Exception as e:
            print(f"Error parsing datetime for {game.get('away')} @ {game.get('home')}: {e}")
            continue

    return earliest_time


def get_week_deadline_string(week_number):
    """
    Get a formatted string of the deadline.
    
    Args:
        week_number: NFL week (1-18)
    
    Returns:
        String like "Thursday, December 12 at 8:15 PM ET" or None
    """
    deadline = get_week_deadline(week_number)
    
    if not deadline:
        return None
    
    return deadline.strftime("%A, %B %d at %I:%M %p ET")


def get_all_week_games_with_times(week_number):
    """
    Get all games for a week with their kickoff times.
    
    Args:
        week_number: NFL week (1-18)
    
    Returns:
        List of dicts: [{"away": "KC", "home": "BUF", "kickoff": datetime}, ...]
    """
    from NFL_Locks.utils.data_utils import load_full_schedule
    
    schedule = load_full_schedule()
    if not schedule:
        return []
    
    week_games = schedule.get(str(week_number))
    if not week_games:
        return []
    
    games_with_times = []
    
    for game in week_games:
        game_datetime_str = game.get("date")
        if not game_datetime_str:
            continue
        
        try:
            # Parse and convert to Eastern
            game_datetime = datetime.fromisoformat(game_datetime_str.replace('Z', '+00:00'))
            game_datetime_et = game_datetime.astimezone(EASTERN)
            
            games_with_times.append({
                "away": game.get("away"),
                "home": game.get("home"),
                "kickoff": game_datetime_et
            })
        
        except Exception as e:
            print(f"Error parsing game time: {e}")
            continue
    
    # Sort by kickoff time
    games_with_times.sort(key=lambda x: x["kickoff"])
    
    return games_with_times


def is_deadline_passed(week_number):
    """
    Check if the deadline for a week has passed.
    
    Args:
        week_number: NFL week (1-18)
    
    Returns:
        bool: True if deadline passed, False otherwise
    """
    deadline = get_week_deadline(week_number)
    
    if not deadline:
        return False
    
    now = datetime.now(EASTERN)
    return now >= deadline


def time_until_deadline(week_number):
    """
    Get time remaining until deadline.
    
    Args:
        week_number: NFL week (1-18)
    
    Returns:
        timedelta object, or None if deadline not found
    """
    deadline = get_week_deadline(week_number)
    
    if not deadline:
        return None
    
    now = datetime.now(EASTERN)
    return deadline - now


def get_deadline_warning_times(week_number):
    """
    Get times for pre-deadline warnings (15 min, 5 min before).
    
    Args:
        week_number: NFL week (1-18)
    
    Returns:
        dict: {"15min": datetime, "5min": datetime, "deadline": datetime}
    """
    from datetime import timedelta
    
    deadline = get_week_deadline(week_number)
    
    if not deadline:
        return None
    
    return {
        "15min": deadline - timedelta(minutes=15),
        "5min": deadline - timedelta(minutes=5),
        "deadline": deadline
    }


# ============ DEPRECATED - For test.py only ============
# Production cogs should use the functions above directly

def is_submissions_open(week_number):
    """DEPRECATED: Use 'not is_deadline_passed(week)' instead"""
    return not is_deadline_passed(week_number)


def get_week_lock_time(week_number):
    """DEPRECATED: Use 'get_week_deadline(week)' instead"""
    return get_week_deadline(week_number)


def thanksgiving_date(year):
    """DEPRECATED: Deadlines now come from schedule, not hardcoded dates"""
    from datetime import datetime
    from calendar import monthcalendar
    
    november = monthcalendar(year, 11)
    thursdays = [week[3] for week in november if week[3] != 0]
    thanksgiving_day = thursdays[3]
    
    return datetime(year, 11, thanksgiving_day, tzinfo=EASTERN)
