"""
NFL tiebreaking procedures, implemented as written in the rulebook.

Two separate procedures exist and they are not interchangeable:

  To break a tie WITHIN A DIVISION
      two clubs:      head-to-head, division, common, conference, SOV, SOS, ...
      three or more:  head-to-head (among the clubs), division, common,
                      conference, SOV, SOS, ...

  To break a tie for a WILD CARD berth
      two clubs:      head-to-head, conference, common (min 4), SOV, SOS, ...
      three or more:  head-to-head sweep, conference, common (min 4), SOV,
                      SOS, ...

Three structural rules that a single sorted pass cannot express, and which are
the reason this is a separate module rather than another comparison key:

  1. RESTART. The procedures rank ONE club at a time. As soon as a step
     separates a club, that club takes the spot and the whole procedure begins
     again at step 1 with the clubs that remain. A step that merely narrows the
     field restarts with the narrowed field.

  2. TWO-CLUB REVERSION. When eliminations leave exactly two clubs, the
     two-club format is applied from its first step, not the remainder of the
     three-or-more list.

  3. WILD CARD DIVISION ELIMINATION. When two clubs from the same division are
     tied for a wild card, only the higher ranked of them (by the DIVISION
     procedure) is eligible. The others are eliminated from that comparison and
     return to contention for the next spot.

Head-to-head is applied differently by group size, which is the subtlety most
implementations miss. For three or more clubs in a wild card tie it is a SWEEP
test: it applies only when one club has beaten every other club in the group,
or lost to every other club. A club with a merely better record against the
group does not advance on head-to-head.

Not implemented, because NFL Locks stores no scores: the steps below strength
of schedule (combined point rankings, net points, net touchdowns). Those steps
report "not applicable" and the procedure continues past them, ending at a
deterministic alphabetical fallback that stands in for the coin toss. Storing
per-game scores is what it would take to finish the chain.
"""

import logging

logger = logging.getLogger("trade_eval.nfl_tiebreakers")

DIVISION = "division"
WILD_CARD = "wild_card"

# Wild card common-games step carries an explicit minimum; the division
# procedure's common-games step does not.
WILD_CARD_COMMON_GAMES_MIN = 4


# -- Record helpers ------------------------------------------------------------

def _blank() -> dict:
    return {"wins": 0, "losses": 0, "ties": 0, "games": 0}


def _pct(rec: dict) -> float:
    g = rec["games"]
    return (rec["wins"] + 0.5 * rec["ties"]) / g if g > 0 else 0.0


def _pair_key(a: str, b: str) -> tuple:
    return (min(a, b), max(a, b))


def _record_against(team: str, opponents: "set | list", stats: dict) -> dict:
    """team's aggregate record against a set of opponents, from the h2h table."""
    h2h = stats["h2h"]
    rec = _blank()
    for opp in opponents:
        if opp == team:
            continue
        data = h2h.get(_pair_key(team, opp))
        if not data:
            continue
        if team <= opp:
            wins, losses = data["wins_a"], data["wins_b"]
        else:
            wins, losses = data["wins_b"], data["wins_a"]
        ties = data["ties"]
        rec["wins"] += wins
        rec["losses"] += losses
        rec["ties"] += ties
        rec["games"] += wins + losses + ties
    return rec


def _combined_pct(teams: "list[str]", stats: dict) -> float:
    """Combined win% of a list of teams, counting repeats (NFL SOV/SOS style).

    The rulebook computes strength of victory and strength of schedule from the
    COMBINED records of those clubs, so a team played twice counts twice.
    """
    overall = stats["overall"]
    wins = losses = ties = 0
    for t in teams:
        rec = overall.get(t, _blank())
        wins += rec["wins"]
        losses += rec["losses"]
        ties += rec["ties"]
    total = wins + losses + ties
    return (wins + 0.5 * ties) / total if total > 0 else 0.0


# -- Individual steps ----------------------------------------------------------
# Each returns {club: value} with higher = better, or None when the step does
# not apply and the procedure should move on.

def _step_head_to_head_pair(a: str, b: str, stats: dict):
    rec = _record_against(a, [b], stats)
    if rec["games"] == 0:
        return None  # They have not played; step does not apply.
    return {a: _pct(rec), b: 1.0 - _pct(rec)}


def _step_head_to_head_group(group: "list[str]", stats: dict):
    """Division three-or-more: win% in games among the tied clubs.

    Applies only once every club in the group has played every other club,
    which is what makes the comparison meaningful.
    """
    for i, a in enumerate(group):
        for b in group[i + 1:]:
            if _record_against(a, [b], stats)["games"] == 0:
                return None
    return {c: _pct(_record_against(c, group, stats)) for c in group}


def _sweep(group: "list[str]", stats: dict) -> "tuple[str | None, str | None]":
    """Wild card three-or-more head-to-head: (swept_all, swept_by_all).

    Returns the club that beat every other club in the group, and the club that
    lost to every other club. Either may be None. A club that merely has the
    best record against the group is neither.
    """
    beat_all = lost_all = None
    for club in group:
        others = [c for c in group if c != club]
        results = [_record_against(club, [o], stats) for o in others]
        if any(r["games"] == 0 for r in results):
            continue  # Has not played every other club.
        if all(r["wins"] > 0 and r["losses"] == 0 and r["ties"] == 0 for r in results):
            beat_all = club
        if all(r["losses"] > 0 and r["wins"] == 0 and r["ties"] == 0 for r in results):
            lost_all = club
    return beat_all, lost_all


def _step_division_record(group: "list[str]", stats: dict):
    return {c: _pct(stats["division"].get(c, _blank())) for c in group}


def _step_conference_record(group: "list[str]", stats: dict):
    return {c: _pct(stats["conference"].get(c, _blank())) for c in group}


def _step_common_games(group: "list[str]", stats: dict, minimum: int = 0):
    """Win% against opponents common to every club in the group.

    `minimum` is the rulebook's minimum game count, 4 for wild card ties and
    none for division ties. Every club must meet it or the step is skipped.
    """
    opponents = stats["opponents"]
    common = None
    for club in group:
        opps = {o for o in opponents.get(club, []) if o not in group}
        common = opps if common is None else (common & opps)
    if not common:
        return None

    values = {}
    for club in group:
        rec = _record_against(club, common, stats)
        if rec["games"] < max(minimum, 1):
            return None
        values[club] = _pct(rec)
    return values


def _step_strength_of_victory(group: "list[str]", stats: dict):
    return {c: _combined_pct(stats["beaten"].get(c, []), stats) for c in group}


def _step_strength_of_schedule(group: "list[str]", stats: dict):
    return {c: _combined_pct(stats["opponents"].get(c, []), stats) for c in group}


def _step_points_unavailable(group: "list[str]", stats: dict):
    """Placeholder for the point-based steps.

    Combined point rankings, net points and net touchdowns all need per-game
    scores, which NFL Locks does not store. Returning None keeps the procedure
    honest: the step is skipped rather than approximated with something that
    happens to be available.
    """
    return None


def _steps_for(scope: str, group_size: int) -> list:
    """The ordered step list for a scope, as the rulebook prints it."""
    if scope == DIVISION:
        steps = [
            ("head-to-head",
             lambda g, s: (_step_head_to_head_pair(g[0], g[1], s) if len(g) == 2
                           else _step_head_to_head_group(g, s))),
            ("division record", _step_division_record),
            ("common games", lambda g, s: _step_common_games(g, s, 0)),
            ("conference record", _step_conference_record),
        ]
    else:
        steps = [
            ("head-to-head",
             lambda g, s: (_step_head_to_head_pair(g[0], g[1], s) if len(g) == 2
                           else None)),  # 3+ handled by the sweep rule
            ("conference record", _step_conference_record),
            ("common games",
             lambda g, s: _step_common_games(g, s, WILD_CARD_COMMON_GAMES_MIN)),
        ]
    steps += [
        ("strength of victory", _step_strength_of_victory),
        ("strength of schedule", _step_strength_of_schedule),
        ("point rankings (no score data)", _step_points_unavailable),
    ]
    return steps


# -- Procedures ----------------------------------------------------------------

def _best_by(values: dict) -> list:
    best = max(values.values())
    return sorted(c for c, v in values.items() if abs(v - best) < 1e-9)


def _resolve_two(clubs: "list[str]", scope: str, stats: dict) -> list:
    """Two-club procedure. Returns [better, worse]."""
    a, b = sorted(clubs)

    # A wild card tie between two clubs of the same division is settled by the
    # division procedure, per the note at the head of the wild card rules.
    if scope == WILD_CARD and stats["division_of"].get(a) == stats["division_of"].get(b):
        scope = DIVISION

    for name, step in _steps_for(scope, 2):
        values = step([a, b], stats)
        if values is None:
            continue
        if abs(values[a] - values[b]) < 1e-9:
            continue
        winner = a if values[a] > values[b] else b
        logger.debug("tiebreak %s: %s over %s on %s", scope, winner,
                     b if winner == a else a, name)
        return [a, b] if winner == a else [b, a]

    # Coin toss in the rulebook. Alphabetical here so a projection is stable
    # run to run rather than ordering by dict iteration.
    return [a, b]


def _wild_card_eligible(group: "list[str]", stats: dict) -> list:
    """Highest ranked club from each division; the rest are eliminated."""
    by_division: dict = {}
    for club in group:
        by_division.setdefault(stats["division_of"].get(club), []).append(club)

    eligible = []
    for div, clubs in by_division.items():
        if len(clubs) == 1:
            eligible.append(clubs[0])
        else:
            eligible.append(resolve(clubs, DIVISION, stats)[0])
    return sorted(eligible)


def resolve(clubs: "list[str]", scope: str, stats: dict) -> list:
    """Rank clubs best to worst: record first, then the given tie procedure.

    The rulebook procedures only ever apply to clubs that are ALREADY tied on
    won-lost-tied percentage, so record is settled here first and a procedure
    runs only inside a group that record could not separate. Callers can
    therefore hand in a whole division or conference.

    Within a tied group, clubs are ranked one at a time and the procedure
    restarts from step 1 with the remainder, which is what the rulebook
    specifies and what a single sort cannot reproduce.
    """
    clubs = sorted(set(clubs))
    if len(clubs) <= 1:
        return clubs

    overall = stats["overall"]
    by_record: dict = {}
    for club in clubs:
        by_record.setdefault(round(_pct(overall.get(club, _blank())), 9), []).append(club)

    if len(by_record) > 1:
        ordered = []
        for pct in sorted(by_record, reverse=True):
            ordered.extend(resolve(by_record[pct], scope, stats))
        return ordered

    if len(clubs) == 2:
        return _resolve_two(clubs, scope, stats)

    group = clubs
    if scope == WILD_CARD:
        eligible = _wild_card_eligible(clubs, stats)
        if len(eligible) == 1:
            first = eligible[0]
            return [first] + resolve([c for c in clubs if c != first], scope, stats)
        group = eligible

    # Wild card three-or-more head-to-head is a sweep test, not a record.
    if scope == WILD_CARD and len(group) > 2:
        beat_all, lost_all = _sweep(group, stats)
        if beat_all:
            return [beat_all] + resolve([c for c in clubs if c != beat_all], scope, stats)
        if lost_all:
            # Eliminated from this comparison, still eligible for the next spot.
            remaining = [c for c in group if c != lost_all]
            ordered = resolve(remaining, scope, stats)
            first = ordered[0]
            return [first] + resolve([c for c in clubs if c != first], scope, stats)

    for name, step in _steps_for(scope, len(group)):
        values = step(group, stats)
        if values is None:
            continue
        top = _best_by(values)
        if len(top) == len(group):
            continue  # Step separated nobody.

        if len(top) == 1:
            first = top[0]
            logger.debug("tiebreak %s: %s ranks first on %s", scope, first, name)
        else:
            # Narrowed but still tied: restart the procedure with the survivors,
            # which reverts to the two-club format when two remain.
            first = resolve(top, scope, stats)[0]

        return [first] + resolve([c for c in clubs if c != first], scope, stats)

    first = sorted(group)[0]
    return [first] + resolve([c for c in clubs if c != first], scope, stats)
