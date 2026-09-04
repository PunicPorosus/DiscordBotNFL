import aiohttp
from NFL_Locks.utils.constants import NFL_TEAMS

async def fetch_nfl_results(week_number: int, season: int | None = None):
    """Fetch completed-game results from ESPN's public API.

    Returns {"winners": [abbr, ...], "completed": int, "listed": int} or None
    on error. `completed` counts games ESPN marks finished; a tie is completed
    but contributes no winner, so completeness must be judged on `completed`.

    Parameters
    ----------
    week_number : int
        NFL regular-season week (1-18).
    season : int | None
        4-digit season year (e.g. 2025).  If None, ESPN defaults to whatever
        it considers the current season, unreliable for historical lookups.
        Always pass the season year for correctness.
    """
    try:
        url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
        params = {"seasontype": "2", "week": str(week_number)}
        if season is not None:
            params["year"] = str(season)

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    print(f"[ESPN] API error {response.status}")
                    return None

                data = await response.json()
                events = data.get("events", [])
                winners = []
                completed = 0
                for game in events:
                    status = game.get("status", {}).get("type", {})
                    if not status.get("completed", False):
                        continue
                    completed += 1
                    comp = game.get("competitions", [{}])[0]
                    for team in comp.get("competitors", []):
                        if team.get("winner", False):
                            abbr = team["team"].get("abbreviation", "")
                            if abbr in NFL_TEAMS:
                                winners.append(abbr)
                return {
                    "winners": winners,
                    "completed": completed,
                    "listed": len(events),
                }
    except Exception as e:
        print(f"[ESPN] Error fetching week {week_number}: {e}")
        return None


async def fetch_nfl_winners(week_number: int, season: int | None = None):
    """Backward-compatible wrapper returning just the winners list, or None.

    Prefer fetch_nfl_results(): callers cannot tell a short list from a complete
    one here, which is how an incomplete week used to get stored permanently.
    A tie is a completed game contributing no winner, so len(winners) is not a
    measure of how much of the week has finished.
    """
    results = await fetch_nfl_results(week_number, season=season)
    if not results or not results["winners"]:
        return None
    return results["winners"]
