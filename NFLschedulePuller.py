"""
NFL Schedule Puller
===================
Fetches the full regular-season schedule from ESPN's core API and saves it to
data/full_schedule.json with full UTC kickoff datetimes and a _metadata block.

Usage:
    python NFLschedulePuller.py              # defaults to current calendar year
    python NFLschedulePuller.py 2026         # fetch a specific season year
    python NFLschedulePuller.py --season 2026

The saved format is:
    {
        "_metadata": {
            "season": 2026,
            "total_weeks": 18,
            "last_updated": "2026-08-15T09:02:31",
            "regular_season_weeks": [1, 2, ..., 18]
        },
        "1": [{"home": "KC", "away": "BAL", "date": "2026-09-03T20:20Z"}, ...],
        ...
    }

Dates are stored as full ISO 8601 UTC strings (e.g. "2026-09-03T20:20Z") so
the bot can derive accurate kickoff times, deadlines, and week-end detection.
"""

import sys
import json
import argparse
import requests
from pathlib import Path
from datetime import datetime


def fetch_full_schedule(season: int, max_weeks: int = 22):
    """
    Fetch the NFL regular season schedule from ESPN for a given season year.

    Probes up to max_weeks rounds and stops automatically after 3 consecutive
    empty weeks, so the script is forward-compatible with season length changes.

    Args:
        season:    NFL season year (e.g. 2026)
        max_weeks: Upper bound on weeks to probe (default 22 — well above 18)
    """
    schedule: dict = {}
    team_cache: dict = {}
    consecutive_empty_weeks = 0

    def get_team_abbr(competitor: dict) -> str:
        """Return team abbreviation from a competitor object, with caching."""
        team_info = competitor.get("team", {})

        if "abbreviation" in team_info:
            return team_info["abbreviation"]

        if "$ref" in team_info:
            ref = team_info["$ref"]
            if ref in team_cache:
                return team_cache[ref]
            try:
                team_data = requests.get(ref, timeout=10).json()
                abbr = team_data.get("abbreviation") or team_data.get("displayName", "UNKNOWN")
                team_cache[ref] = abbr
                return abbr
            except Exception:
                return "UNKNOWN"

        return "UNKNOWN"

    for week in range(1, max_weeks + 1):
        url = (
            f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
            f"/seasons/{season}/types/2/weeks/{week}/events"
        )
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"Error fetching week {week}: {e}")
            consecutive_empty_weeks += 1
            if consecutive_empty_weeks >= 3:
                print(f"Stopping at week {week} — 3 consecutive fetch errors.")
                break
            continue

        items = data.get("items", []) if data else []

        if not items:
            consecutive_empty_weeks += 1
            print(f"Week {week}: no games found")
            if consecutive_empty_weeks >= 3:
                print(f"Stopping at week {week} — appears to be end of regular season.")
                break
            continue

        consecutive_empty_weeks = 0
        games_this_week = 0

        for item in items:
            try:
                event = requests.get(item["$ref"], timeout=10).json()
                comp = event["competitions"][0]
                competitors = comp["competitors"]

                home = next(get_team_abbr(c) for c in competitors if c["homeAway"] == "home")
                away = next(get_team_abbr(c) for c in competitors if c["homeAway"] == "away")

                # Preserve the full ISO datetime string — DO NOT split on 'T'.
                # Downstream code relies on the time component for deadline detection,
                # reaction locking, and "has the week ended" checks.
                # Example: "2026-09-03T20:20Z"
                game_datetime = event["date"]

                schedule.setdefault(str(week), []).append({
                    "home": home,
                    "away": away,
                    "date": game_datetime,
                })

                print(f"Week {week}: {away} @ {home} on {game_datetime}")
                games_this_week += 1

            except Exception as e:
                print(f"Skipping game (error): {e}")
                continue

        if games_this_week == 0:
            consecutive_empty_weeks += 1

    # -- Save to data/full_schedule.json --------------------------------------
    data_dir = Path(__file__).parent / "NFL_Locks" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_file = data_dir / "full_schedule.json"

    week_numbers = sorted(int(k) for k in schedule)
    final_schedule = {
        "_metadata": {
            "season": season,
            "total_weeks": len(schedule),
            "last_updated": datetime.now().isoformat(),
            "regular_season_weeks": week_numbers,
        }
    }
    final_schedule.update(schedule)

    with open(output_file, "w") as f:
        json.dump(final_schedule, f, indent=2)

    print("\nWeekly Game Counts:")
    for wk in week_numbers:
        print(f"  Week {wk}: {len(schedule[str(wk)])} games")

    print(f"\nSaved to {output_file}")
    print(f"   Season: {season} | Total weeks: {len(schedule)}")
    print("   Datetimes are full UTC ISO strings (e.g. 2026-09-03T20:20Z)")


def _parse_args() -> int:
    """Parse CLI arguments and return the season year to fetch."""
    parser = argparse.ArgumentParser(description="Fetch NFL regular season schedule.")
    parser.add_argument(
        "season",
        nargs="?",
        type=int,
        default=None,
        help="Season year to fetch (e.g. 2026). Defaults to current calendar year.",
    )
    parser.add_argument(
        "--season",
        dest="season_flag",
        type=int,
        default=None,
        help="Season year (alternative --season flag form).",
    )
    args = parser.parse_args()

    # Positional arg takes precedence over --season flag; both default to current year
    year = args.season or args.season_flag or datetime.now().year
    return year


if __name__ == "__main__":
    season_year = _parse_args()
    print(f"Fetching NFL schedule for season {season_year}...")
    fetch_full_schedule(season_year)
