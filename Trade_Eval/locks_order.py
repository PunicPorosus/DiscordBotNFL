"""
Derives NFL projected draft order from NFL Locks game result data.

Reads per-week JSON files from NFL_Locks (NFL_Locks/data/<year>/weekN.json),
computes records for all 32 teams in a single pass, and projects next year's
draft order using proper NFL playoff structure and tiebreakers.

Tiebreaker order (applied within any tied group):
  1. Overall win%
  2. Head-to-head record within the tied group
  3. Division record
  4. Strength of schedule (SOS) — approximation for remaining NFL tiebreakers
     that require data not tracked by NFL Locks (head-to-head conference record,
     common games, strength of victory, etc.)
  5. Alphabetical on full team name (determinism)

Playoff format (since 2020):
  7 teams per conference: 4 division winners + 3 wild cards = 14 total.
  Non-playoff (18 teams) → picks 1–18 (worst record = pick 1).
  Playoff teams (14)     → picks 19–32 (estimated by seeding; confirm post-season
                           with !trade.picks.set locks <team> <pick>).
"""

import logging
from collections import defaultdict

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

# NFL divisions — 4 per conference, 4 teams each
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

def _compute_all_stats() -> tuple[dict, dict, dict, dict]:
    """
    Read all scored NFL Locks week files in a single pass and compute:

    overall_records  — {abbr: {wins, losses, ties, games}}
    h2h              — {(min_abbr, max_abbr): {wins_a, wins_b, ties}}
                         where wins_a = wins by the alphabetically-first team
    div_records      — {abbr: {wins, losses, ties, games}} (same-division games only)
    opponents        — {abbr: [opp_abbr, ...]} for all completed games (SOS input)

    Returns (overall_records, h2h, div_records, opponents).
    All 32 teams are always present in every dict; zeros for teams with no games.
    """
    from NFL_Locks.utils.data_utils import load_full_schedule, load_week_file, get_all_week_files

    schedule = load_full_schedule() or {}
    overall   = {a: _blank_record() for a in ALL_ABBREVS}
    div_recs  = {a: _blank_record() for a in ALL_ABBREVS}
    h2h: dict[tuple, dict] = {}
    opponents: dict[str, list[str]] = defaultdict(list)

    weeks = get_all_week_files()
    for week_num in weeks:
        week_data = load_week_file(week_num)
        if not week_data:
            continue
        winners_list = week_data.get("_winners") or []
        if not winners_list:
            continue  # Week not yet scored

        winners  = set(winners_list)
        matchups = week_data.get("matchups") or schedule.get(str(week_num), [])

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
            elif away_won and home_won:
                winner, loser, is_tie = away, home, True   # treat as tie
            else:
                continue  # game not yet played

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

            # -- Opponents list (for SOS) ----------------------------------
            if away in ALL_ABBREVS:
                opponents[away].append(home)
            if home in ALL_ABBREVS:
                opponents[home].append(away)

    played = sum(1 for r in overall.values() if r["games"] > 0)
    logger.info(
        "locks_order: stats computed over %d weeks, %d teams have played games",
        len(weeks), played,
    )
    return overall, h2h, div_recs, dict(opponents)


def _compute_sos(overall: dict, opponents: dict) -> dict[str, float]:
    """
    Compute each team's opponents' average win% (strength of schedule proxy).
    Returns {abbr: avg_opponent_win_pct}.
    """
    sos: dict[str, float] = {}
    for abbr in ALL_ABBREVS:
        opp_list = opponents.get(abbr, [])
        if not opp_list:
            sos[abbr] = 0.0
        else:
            sos[abbr] = sum(
                _win_pct(overall.get(opp, _blank_record())) for opp in opp_list
            ) / len(opp_list)
    return sos


# -- Tiebreaker ranking --------------------------------------------------------

def _h2h_win_pct(team: str, group: list[str], h2h: dict) -> float:
    """
    H2H win% of team against all other members of group.
    Returns 0.5 if no games have been played within the group.
    """
    wins = losses = ties = 0
    for opp in group:
        if opp == team:
            continue
        key = (min(team, opp), max(team, opp))
        data = h2h.get(key, {})
        if team <= opp:   # team is the "a" key
            wins   += data.get("wins_a", 0)
            losses += data.get("wins_b", 0)
        else:             # team is the "b" key
            wins   += data.get("wins_b", 0)
            losses += data.get("wins_a", 0)
        ties += data.get("ties", 0)
    total = wins + losses + ties
    return (wins + 0.5 * ties) / total if total > 0 else 0.5


def _group_by_value(items: list, key_fn) -> list[list]:
    """
    Sort items descending by key_fn and group those with equal values.
    Returns a list of groups, each group a list of items — highest value first.
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


def _rank_teams(
    teams: list[str],
    overall: dict,
    h2h: dict,
    div_recs: dict,
    sos: dict,
) -> list[str]:
    """
    Rank teams best-to-worst using the NFL tiebreaker sequence:
      1. Overall win%
      2. Head-to-head within tied group
      3. Division record
      4. SOS (proxy for remaining NFL tiebreakers)
      5. Alphabetical (determinism)

    The h2h tiebreaker is applied strictly within each tied subset — teams at
    different win% levels never influence each other's h2h calculation.

    Returns a list ordered best → worst (highest win% first).
    """
    if len(teams) <= 1:
        return list(teams)

    result: list[str] = []

    # Stage 1: group by overall win%
    for win_group in _group_by_value(teams, lambda a: _win_pct(overall.get(a, _blank_record()))):
        if len(win_group) == 1:
            result.extend(win_group)
            continue

        # Stage 2: h2h within THIS win% group
        for h2h_group in _group_by_value(win_group, lambda a: _h2h_win_pct(a, win_group, h2h)):
            if len(h2h_group) == 1:
                result.extend(h2h_group)
                continue

            # Stage 3: division record
            for div_group in _group_by_value(h2h_group, lambda a: _win_pct(div_recs.get(a, _blank_record()))):
                if len(div_group) == 1:
                    result.extend(div_group)
                    continue

                # Stage 4: SOS (higher = harder schedule = better for seeding)
                for sos_group in _group_by_value(div_group, lambda a: sos.get(a, 0.0)):
                    if len(sos_group) == 1:
                        result.extend(sos_group)
                        continue

                    # Stage 5: alphabetical (always deterministic)
                    result.extend(sorted(sos_group))

    return result


# -- Playoff field construction ------------------------------------------------

def _identify_division_winner(
    div_teams: list[str], overall: dict, h2h: dict, div_recs: dict, sos: dict
) -> str:
    """Return the current division leader using full tiebreaker order."""
    ranked = _rank_teams(div_teams, overall, h2h, div_recs, sos)
    return ranked[0]


def identify_playoff_teams(
    overall: dict, h2h: dict, div_recs: dict, sos: dict
) -> tuple[set[str], set[str]]:
    """
    Build the current playoff field using full tiebreaker ranking.

    Format: 4 division winners + 3 wild cards per conference = 14 total.
    Wild cards are the 3 best non-division-winner records in the conference,
    broken by the same tiebreaker sequence (h2h within tied WC group, then
    division record, then SOS).

    Returns (playoff_teams, non_playoff_teams) as sets of abbreviations.
    """
    playoff: set[str] = set()

    for conf_divisions in (AFC_DIVISIONS, NFC_DIVISIONS):
        # Division winners
        div_winners: set[str] = set()
        for div_name in conf_divisions:
            winner = _identify_division_winner(DIVISIONS[div_name], overall, h2h, div_recs, sos)
            div_winners.add(winner)

        playoff.update(div_winners)

        # Wild cards — best remaining teams in conference
        conf_teams = [t for div in conf_divisions for t in DIVISIONS[div]]
        wc_candidates = [t for t in conf_teams if t not in div_winners]
        ranked_wc = _rank_teams(wc_candidates, overall, h2h, div_recs, sos)
        playoff.update(ranked_wc[:WILD_CARD_SPOTS])

    non_playoff = set(ALL_ABBREVS) - playoff
    return playoff, non_playoff


# -- Draft order projection ----------------------------------------------------

def project_draft_order() -> dict[str, list[int]]:
    """
    Compute all stats from NFL Locks data and project next year's draft order.

    Non-playoff teams (18): picks 1–18, worst team gets pick 1.
    Playoff teams (14):     picks 19–32, worst playoff team (by seeding) gets 19.

    Both groups use the same tiebreaker sequence. Playoff team order is an
    estimate (assumes worse seeding = earlier exit = earlier pick). Use
    !trade.picks.set locks to confirm once the NFL finalizes post-season picks.

    Returns {full_team_name: [pick_number]}.
    """
    overall, h2h, div_recs, opponents = _compute_all_stats()
    sos = _compute_sos(overall, opponents)
    playoff_teams, non_playoff_teams = identify_playoff_teams(overall, h2h, div_recs, sos)

    # Rank each group best-to-worst; reverse for draft order (worst = earliest pick)
    ranked_non_playoff = _rank_teams(list(non_playoff_teams), overall, h2h, div_recs, sos)
    ranked_playoff     = _rank_teams(list(playoff_teams),     overall, h2h, div_recs, sos)

    # Worst team → pick 1; best non-playoff team → pick 18
    result: dict[str, list[int]] = {}
    for i, abbr in enumerate(reversed(ranked_non_playoff)):
        result[ABBREV_TO_FULL[abbr]] = [i + 1]
    offset = len(ranked_non_playoff)
    for i, abbr in enumerate(reversed(ranked_playoff)):
        result[ABBREV_TO_FULL[abbr]] = [offset + i + 1]

    logger.info(
        "locks_order: projected %d non-playoff (picks 1-%d) + %d playoff (picks %d-%d)",
        len(ranked_non_playoff), len(ranked_non_playoff),
        len(ranked_playoff),
        offset + 1,
        offset + len(ranked_playoff),
    )
    return result


# -- Publicly exported helpers -------------------------------------------------

def compute_team_records() -> dict[str, dict]:
    """Return overall W-L-T records for all 32 teams. Convenience wrapper."""
    overall, _, _, _ = _compute_all_stats()
    return overall


def record_summary() -> list[str]:
    """
    Compute full stats, project draft order, and return one line per team.

    Format:
      " 1. [NP] Cleveland Browns        2-9-0  (0.182)"
      "19. [PL] Kansas City Chiefs      11-0-0 (1.000)"

    [NP] = non-playoff projected  [PL] = playoff projected
    """
    overall, h2h, div_recs, opponents = _compute_all_stats()
    sos = _compute_sos(overall, opponents)
    playoff_teams, _ = identify_playoff_teams(overall, h2h, div_recs, sos)

    order = project_draft_order()
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
