"""
Derives NFL projected draft order from NFL Locks game result data.

Reads weekly results from the NFL Locks database (week_results) and the
schedule from full_schedule.json, computes records for all 32 teams in a single
pass, and projects next year's draft order using proper NFL playoff structure
and tiebreakers.

This used to read per-week JSON files (NFL_Locks/data/<year>/weekN.json) via
data_utils.get_all_week_files(). Results moved to SQLite and nothing writes
those files any more, so the glob matched nothing, every team computed as
0-0-0, and the draft order fell through every tiebreaker to the last one,
alphabetical by name. A sync looked like it worked and produced a reverse
alphabetical list.

Tiebreakers live in nfl_tiebreakers.py, which implements the rulebook's two
procedures (division ties and wild card ties) including the restart-after-each-
club rule, the two-club reversion, the wild card division-elimination rule and
the head-to-head sweep test. Draft order has its own sequence and uses
_draft_rank() below, not the playoff procedures.

The only steps not implemented are the point-based ones below strength of
schedule (combined point rankings, net points, net touchdowns), which need
per-game scores that NFL Locks does not store, and the coin toss, which is
alphabetical here so a projection is stable between runs.

Playoff format (since 2020):
  7 teams per conference: 4 division winners + 3 wild cards = 14 total.
  Non-playoff (18 teams) → picks 1–18 (worst record = pick 1).
  Playoff teams (14)     → picks 19–32 (estimated by seeding; confirm post-season
                           with !trade.picks.set locks <team> <pick>).
"""

import logging
from collections import defaultdict

from Trade_Eval import nfl_tiebreakers

logger = logging.getLogger("trade_eval.locks_order")


# -- Team metadata -------------------------------------------------------------

ABBREV_TO_FULL: dict[str, str] = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB":  "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC":  "Kansas City Chiefs",
    "LV":  "Las Vegas Raiders",
    "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE":  "New England Patriots",
    "NO":  "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF":  "San Francisco 49ers",
    "TB":  "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WSH": "Washington Commanders",
}

ALL_ABBREVS: frozenset[str] = frozenset(ABBREV_TO_FULL.keys())

# NFL divisions, 4 per conference, 4 teams each
DIVISIONS: dict[str, list[str]] = {
    "AFC East":  ["BUF", "MIA", "NE",  "NYJ"],
    "AFC North": ["BAL", "CIN", "CLE", "PIT"],
    "AFC South": ["HOU", "IND", "JAX", "TEN"],
    "AFC West":  ["DEN", "KC",  "LAC", "LV"],
    "NFC East":  ["DAL", "NYG", "PHI", "WSH"],
    "NFC North": ["CHI", "DET", "GB",  "MIN"],
    "NFC South": ["ATL", "CAR", "NO",  "TB"],
    "NFC West":  ["ARI", "LAR", "SEA", "SF"],
}

AFC_DIVISIONS = ["AFC East", "AFC North", "AFC South", "AFC West"]
NFC_DIVISIONS = ["NFC East", "NFC North", "NFC South", "NFC West"]
WILD_CARD_SPOTS = 3  # per conference

# Division membership: abbrev → division name
_DIV_MAP: dict[str, str] = {
    team: div for div, teams in DIVISIONS.items() for team in teams
}

# Conference membership: abbrev → "AFC"/"NFC". Derived from the division lists
# rather than written out again, so realignment only ever has to be edited in
# one place and the two cannot drift apart.
_CONF_MAP: dict[str, str] = {
    team: conf
    for conf, div_names in (("AFC", AFC_DIVISIONS), ("NFC", NFC_DIVISIONS))
    for div in div_names
    for team in DIVISIONS[div]
}


# -- Record helpers ------------------------------------------------------------

def _blank_record() -> dict:
    return {"wins": 0, "losses": 0, "ties": 0, "games": 0}


def _win_pct(rec: dict) -> float:
    g = rec["games"]
    return (rec["wins"] + 0.5 * rec["ties"]) / g if g > 0 else 0.0


def _add_win(records: dict, team: str) -> None:
    if team in records:
        records[team]["wins"]  += 1
        records[team]["games"] += 1


def _add_loss(records: dict, team: str) -> None:
    if team in records:
        records[team]["losses"] += 1
        records[team]["games"]  += 1


def _add_tie(records: dict, team: str) -> None:
    if team in records:
        records[team]["ties"]  += 1
        records[team]["games"] += 1


# -- Single-pass stats computation ---------------------------------------------

def _compute_all_stats(
    winners_by_week: dict, ties_by_week: "dict | None" = None
) -> dict:
    """
    Walk every scored week once and compute:

    overall_records, {abbr: {wins, losses, ties, games}}
    h2h, {(min_abbr, max_abbr): {wins_a, wins_b, ties}}
                         where wins_a = wins by the alphabetically-first team
    div_records, {abbr: {wins, losses, ties, games}} (same-division games only)
    conf_records, {abbr: {wins, losses, ties, games}} (same-conference games only)
    opponents, {abbr: [opp_abbr, ...]} for all completed games (SOS input)

    winners_by_week, {week_number: [winning team abbrev, ...]} from the NFL
    Locks database. A week missing from the dict has not been scored yet.

    ties_by_week, {week_number: [[abbr, abbr], ...]}. A tied game records no
    winner under the house rule, so it has to be passed separately or the game
    is skipped entirely and both teams show one fewer game played.

    Returns a stats bundle: overall, h2h, division, conference, opponents,
    beaten, division_of, conference_of. All 32 teams are present in every
    record dict; zeros for teams with no games. The bundle is what
    nfl_tiebreakers.resolve() consumes.
    """
    from NFL_Locks.utils.data_utils import load_full_schedule

    schedule = load_full_schedule() or {}
    overall   = {a: _blank_record() for a in ALL_ABBREVS}
    div_recs  = {a: _blank_record() for a in ALL_ABBREVS}
    conf_recs = {a: _blank_record() for a in ALL_ABBREVS}
    beaten: dict[str, list[str]] = defaultdict(list)
    h2h: dict[tuple, dict] = {}
    opponents: dict[str, list[str]] = defaultdict(list)

    weeks = sorted(winners_by_week)
    for week_num in weeks:
        winners_list = winners_by_week.get(week_num) or []
        if not winners_list:
            continue  # Week not yet scored

        winners   = set(winners_list)
        tied_pairs = {
            tuple(sorted(pair)) for pair in (ties_by_week or {}).get(week_num, [])
        }
        matchups  = schedule.get(str(week_num), [])
        if not matchups:
            logger.warning("locks_order: no schedule entry for week %s", week_num)
            continue

        for game in matchups:
            away = game.get("away")
            home = game.get("home")
            if not away or not home:
                continue

            away_won = away in winners
            home_won = home in winners

            if away_won and not home_won:
                winner, loser, is_tie = away, home, False
            elif home_won and not away_won:
                winner, loser, is_tie = home, away, False
            elif tuple(sorted((away, home))) in tied_pairs:
                winner, loser, is_tie = away, home, True
            elif away_won and home_won:
                winner, loser, is_tie = away, home, True   # defensive, should not occur
            else:
                # Neither team won and the game is not a recorded tie. Could be
                # postponed, or part of a week stored incomplete past the
                # winners grace window. Deliberately NOT inferred as a tie:
                # guessing here is what silently corrupts standings.
                continue

            # -- Overall record --------------------------------------------
            if is_tie:
                _add_tie(overall, away)
                _add_tie(overall, home)
            else:
                _add_win(overall, winner)
                _add_loss(overall, loser)

            # -- Head-to-head record ---------------------------------------
            key = (min(away, home), max(away, home))
            if key not in h2h:
                h2h[key] = {"wins_a": 0, "wins_b": 0, "ties": 0}
            if is_tie:
                h2h[key]["ties"] += 1
            elif winner == key[0]:   # alphabetically-first team won
                h2h[key]["wins_a"] += 1
            else:
                h2h[key]["wins_b"] += 1

            # -- Division record -------------------------------------------
            if _DIV_MAP.get(away) == _DIV_MAP.get(home) and _DIV_MAP.get(away) is not None:
                if is_tie:
                    _add_tie(div_recs, away)
                    _add_tie(div_recs, home)
                else:
                    _add_win(div_recs, winner)
                    _add_loss(div_recs, loser)

            # -- Conference record -----------------------------------------
            # Every division game is also a conference game, so this is a
            # superset of the division record above, not an alternative to it.
            if _CONF_MAP.get(away) == _CONF_MAP.get(home) and _CONF_MAP.get(away) is not None:
                if is_tie:
                    _add_tie(conf_recs, away)
                    _add_tie(conf_recs, home)
                else:
                    _add_win(conf_recs, winner)
                    _add_loss(conf_recs, loser)

            # -- Opponents list (for SOS) ----------------------------------
            if away in ALL_ABBREVS:
                opponents[away].append(home)
            if home in ALL_ABBREVS:
                opponents[home].append(away)

            # -- Teams beaten (for strength of victory) --------------------
            # A tie counts as a victory for nobody, so neither club's list
            # grows, which matches how the rulebook computes SOV.
            if not is_tie and winner in ALL_ABBREVS:
                beaten[winner].append(loser)

    played = sum(1 for r in overall.values() if r["games"] > 0)
    logger.info(
        "locks_order: stats computed over %d weeks, %d teams have played games",
        len(weeks), played,
    )
    return {
        "overall": overall,
        "h2h": h2h,
        "division": div_recs,
        "conference": conf_recs,
        "opponents": dict(opponents),
        "beaten": dict(beaten),
        "division_of": _DIV_MAP,
        "conference_of": _CONF_MAP,
    }


def _group_by_value(items: list, key_fn) -> list[list]:
    """
    Sort items descending by key_fn and group those with equal values.
    Returns a list of groups, each group a list of items, highest value first.
    Uses epsilon comparison to avoid floating-point equality issues.
    """
    keyed = sorted(((key_fn(item), item) for item in items), key=lambda x: -x[0])
    groups: list[tuple[float, list]] = []
    for val, item in keyed:
        if groups and abs(groups[-1][0] - val) < 1e-9:
            groups[-1][1].append(item)
        else:
            groups.append((val, [item]))
    return [g[1] for g in groups]


# -- Playoff field construction ------------------------------------------------

def identify_playoff_teams(stats: dict) -> "tuple[set, set]":
    """
    Build the current playoff field: 4 division winners + 3 wild cards per
    conference = 14 teams.

    Division winners are decided by the DIVISION procedure and wild cards by
    the WILD CARD procedure. Those are different step orders, and using the
    division order for wild cards (which this did before) gets the last wild
    card wrong whenever two tied clubs have different conference records: that
    single call decides whether a team picks in the 1-18 range or the 19-32
    range, which is the largest error this projection can make.

    Returns (playoff_teams, non_playoff_teams) as sets of abbreviations.
    """
    playoff: set = set()

    for conf_divisions in (AFC_DIVISIONS, NFC_DIVISIONS):
        div_winners: set = set()
        for div_name in conf_divisions:
            ranked = nfl_tiebreakers.resolve(
                DIVISIONS[div_name], nfl_tiebreakers.DIVISION, stats
            )
            div_winners.add(ranked[0])

        playoff.update(div_winners)

        conf_teams = [t for div in conf_divisions for t in DIVISIONS[div]]
        wc_candidates = [t for t in conf_teams if t not in div_winners]
        ranked_wc = nfl_tiebreakers.resolve(
            wc_candidates, nfl_tiebreakers.WILD_CARD, stats
        )
        playoff.update(ranked_wc[:WILD_CARD_SPOTS])

    non_playoff = set(ALL_ABBREVS) - playoff
    return playoff, non_playoff


# -- Draft order ---------------------------------------------------------------

def _draft_rank(teams: "list[str]", stats: dict) -> list:
    """Order teams WORST first, by the draft selection rules.

    The draft has its own tiebreaker, which is not the playoff procedure:
    clubs with identical records are ordered by strength of schedule, the club
    with the LOWEST strength of schedule picking first. Clubs still tied are
    separated by the division procedure when they share a division, the wild
    card (conference) procedure when they share a conference, and a coin toss
    otherwise, which is alphabetical here so projections stay stable.
    """
    groups = _group_by_value(
        list(teams), lambda t: _win_pct(stats["overall"].get(t, _blank_record()))
    )

    ordered: list = []
    for group in reversed(groups):      # worst win% first
        if len(group) == 1:
            ordered.extend(group)
            continue

        for sos_group in reversed(_group_by_value(
            group, lambda t: nfl_tiebreakers._combined_pct(
                stats["opponents"].get(t, []), stats
            )
        )):                              # lowest SOS first
            if len(sos_group) == 1:
                ordered.extend(sos_group)
                continue

            divisions = {stats["division_of"].get(t) for t in sos_group}
            conferences = {stats["conference_of"].get(t) for t in sos_group}
            if len(divisions) == 1:
                scope = nfl_tiebreakers.DIVISION
            elif len(conferences) == 1:
                scope = nfl_tiebreakers.WILD_CARD
            else:
                ordered.extend(sorted(sos_group))
                continue

            # resolve() ranks best first; the worst club picks first.
            ordered.extend(reversed(nfl_tiebreakers.resolve(sos_group, scope, stats)))

    return ordered


async def load_results_by_week() -> tuple[dict, dict]:
    """Read every scored week of the current season from the NFL Locks DB.

    Returns (winners_by_week, ties_by_week). The NFL Locks DB singleton is
    connected once in bot.py before extensions load, so this only ever reads an
    already-open connection.
    """
    from NFL_Locks.utils.database import get_db
    from NFL_Locks.utils.schedule_utils import get_current_season, get_max_week

    db = get_db()
    season = get_current_season()
    winners_by_week: dict[int, list[str]] = {}
    ties_by_week: dict[int, list] = {}

    for week in range(1, get_max_week() + 1):
        winners = await db.get_winners(season, week)
        if winners:
            winners_by_week[week] = winners
            ties = await db.get_ties(season, week)
            if ties:
                ties_by_week[week] = ties

    logger.info(
        "locks_order: loaded %d scored week(s), %d with ties, for season %s",
        len(winners_by_week), len(ties_by_week), season,
    )
    return winners_by_week, ties_by_week


def project_draft_order(
    winners_by_week: dict, ties_by_week: "dict | None" = None
) -> dict[str, list[int]]:
    """
    Compute all stats from NFL Locks results and project next year's draft order.

    Non-playoff teams (18): picks 1-18, worst team picks first.
    Playoff teams (14):     picks 19-32, estimated by seeding.

    Non-playoff order follows the DRAFT selection rules (record, then lowest
    strength of schedule, then the division or conference procedure), not the
    playoff tiebreakers: the draft has its own sequence and using the playoff
    one ordered tied teams by the wrong key.

    Playoff order remains an estimate, since the real picks 19-32 depend on how
    far each club goes in January. Confirm with !trade.picks.set locks.

    Returns {full_team_name: [pick_number]}.
    """
    stats = _compute_all_stats(winners_by_week, ties_by_week)
    playoff_teams, non_playoff_teams = identify_playoff_teams(stats)

    # Worst first: pick 1 is the worst non-playoff club.
    draft_non_playoff = _draft_rank(list(non_playoff_teams), stats)
    draft_playoff     = _draft_rank(list(playoff_teams), stats)

    result: dict[str, list[int]] = {}
    for i, abbr in enumerate(draft_non_playoff):
        result[ABBREV_TO_FULL[abbr]] = [i + 1]
    offset = len(draft_non_playoff)
    for i, abbr in enumerate(draft_playoff):
        result[ABBREV_TO_FULL[abbr]] = [offset + i + 1]

    logger.info(
        "locks_order: projected %d non-playoff (picks 1-%d) + %d playoff (picks %d-%d)",
        len(draft_non_playoff), len(draft_non_playoff),
        len(draft_playoff),
        offset + 1,
        offset + len(draft_playoff),
    )
    return result


# -- Publicly exported helpers -------------------------------------------------

def compute_team_records(
    winners_by_week: dict, ties_by_week: "dict | None" = None
) -> dict[str, dict]:
    """Return overall W-L-T records for all 32 teams. Convenience wrapper."""
    return _compute_all_stats(winners_by_week, ties_by_week)["overall"]


def record_summary(
    winners_by_week: dict, ties_by_week: "dict | None" = None
) -> list[str]:
    """
    Compute full stats, project draft order, and return one line per team.

    Format:
      " 1. [NP] Cleveland Browns        2-9-0  (0.182)"
      "19. [PL] Kansas City Chiefs      11-0-0 (1.000)"

    [NP] = non-playoff projected  [PL] = playoff projected
    """
    stats = _compute_all_stats(winners_by_week, ties_by_week)
    overall = stats["overall"]
    playoff_teams, _ = identify_playoff_teams(stats)

    order = project_draft_order(winners_by_week, ties_by_week)
    pick_by_full = {full: picks[0] for full, picks in order.items()}

    lines: list[str] = []
    for abbr in sorted(ALL_ABBREVS, key=lambda a: pick_by_full.get(ABBREV_TO_FULL[a], 99)):
        full   = ABBREV_TO_FULL[abbr]
        pick   = pick_by_full.get(full, "?")
        status = "PL" if abbr in playoff_teams else "NP"
        rec    = overall.get(abbr, _blank_record())
        w, l, t = rec["wins"], rec["losses"], rec["ties"]
        pct    = _win_pct(rec)
        lines.append(f"{pick:2}. [{status}] {full:<26}  {w}-{l}-{t}  ({pct:.3f})")
    return lines


def has_results(winners_by_week: dict) -> bool:
    """True when at least one week has been scored.

    Guards the sync against publishing a draft order built from nothing: with
    no results every team is 0-0-0 and the order is just the final alphabetical
    tiebreaker, which reads like a real projection but is not one.
    """
    return any(winners_by_week.get(w) for w in winners_by_week)
