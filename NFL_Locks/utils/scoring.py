"""
Scoring logic for NFL Locks.

All scoring schemes live here. results_manager and database both delegate
to this module so the logic is never duplicated.

Supported schemes
-----------------
SCHEME_ALL_OR_NOTHING  (default)
    A user earns 1 point per correct pick only if they had zero wrong picks
    that week. Any single loss wipes the entire week's points.

SCHEME_ADDITIVE
    +1 for every correct pick, -1 for every wrong pick. Score can go
    negative. No all-or-nothing wipe.
"""

SCHEME_ALL_OR_NOTHING = "all_or_nothing"
SCHEME_ADDITIVE       = "additive"

VALID_SCHEMES: frozenset[str] = frozenset({SCHEME_ALL_OR_NOTHING, SCHEME_ADDITIVE})

# Human-readable labels used in Discord output
SCHEME_LABELS: dict[str, str] = {
    SCHEME_ALL_OR_NOTHING: "All-or-Nothing",
    SCHEME_ADDITIVE:       "Additive (+1/-1)",
}


def compute_week_scores(
    picks: dict[str, list[str]],
    winners: set[str],
    scheme: str,
) -> dict[str, int]:
    """
    Score a full week's picks (all users) against the winning teams.

    Parameters
    ----------
    picks   : {team_abbr: [user_name, ...]}  (from db.get_picks_for_week)
    winners : set of winning team abbreviations
    scheme  : one of VALID_SCHEMES

    Returns
    -------
    {user_name: points}

    Under all_or_nothing, only users with at least 1 point are included.
    Under additive, all users who made at least one pick are included,
    even if their score is zero or negative.

    Unknown schemes fall back to all_or_nothing silently.
    """
    if scheme == SCHEME_ADDITIVE:
        return _compute_additive(picks, winners)
    return _compute_all_or_nothing(picks, winners)


def compute_user_week_score(
    user_teams: set[str],
    winners: set[str],
    scheme: str,
) -> int:
    """
    Score a single user's picks for a single week.

    Parameters
    ----------
    user_teams : set of team abbreviations the user picked
    winners    : set of winning team abbreviations
    scheme     : one of VALID_SCHEMES

    Returns the user's point total for the week.
    Under additive this can be negative. Returns 0 if user_teams is empty.
    """
    if not user_teams:
        return 0
    if scheme == SCHEME_ADDITIVE:
        correct = len(user_teams & winners)
        wrong   = len(user_teams - winners)
        return correct - wrong
    # all_or_nothing: any wrong pick wipes the week
    if user_teams - winners:
        return 0
    return len(user_teams & winners)


def results_header(scheme: str) -> str:
    """
    Return the Discord header label for the per-user scores block.

    all_or_nothing → "Perfect Picks"
    additive       → "Week Scores"
    """
    if scheme == SCHEME_ADDITIVE:
        return "Week Scores"
    return "Perfect Picks"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _compute_all_or_nothing(
    picks: dict[str, list[str]],
    winners: set[str],
) -> dict[str, int]:
    """Any wrong pick wipes the whole week. Returns only users with > 0 points."""
    losers: set[str] = set()
    for team, users in picks.items():
        if team not in winners:
            losers.update(users)

    scores: dict[str, int] = {}
    for team, users in picks.items():
        if team in winners:
            for user in users:
                if user not in losers:
                    scores[user] = scores.get(user, 0) + 1
    return scores


def _compute_additive(
    picks: dict[str, list[str]],
    winners: set[str],
) -> dict[str, int]:
    """
    +1 per correct pick, -1 per wrong pick.
    Returns all users who made at least one pick, even if score is 0 or negative.
    """
    scores: dict[str, int] = {}
    for team, users in picks.items():
        delta = 1 if team in winners else -1
        for user in users:
            scores[user] = scores.get(user, 0) + delta
    return scores
