"""
NFL Trade Evaluator - Core Logic
Parsing, valuation, and trade-finding algorithms.
"""

import re
from itertools import combinations
from Trade_Eval import config, trade_charts

# Regex for future pick format: YYR# (e.g. "27R2", "28R1")
_FUTURE_RE = re.compile(r'^(\d{2})R(\d)$', re.IGNORECASE)

# Regex for relative future pick format: +NR# (e.g. "+1R2", "+3R1")
_REL_FUTURE_RE = re.compile(r'^\+([1-3])R([1-7])$', re.IGNORECASE)

# Regex for round.pick format (e.g. "5.176", "6.190")
_ROUND_PICK_RE = re.compile(r'^(\d)\.(\d{1,3})$')


def _round_mid_pick(round_num: int) -> int:
    """Return the mid-round overall pick for a given round number."""
    start = trade_charts.ROUND_STARTS.get(round_num)
    if start is None:
        return None
    return start + 15  # Mid of a 32-pick round


def parse_pick(token: str):
    """
    Parse a single pick token into (overall_pick, years_out, display_label).

    Supported formats:
      "31"     → overall pick 31, current year (years_out = 0)
      "5.176"  → round 5, overall pick 176, current year
      "6.190"  → round 6, overall pick 190, current year
      "27R2"   → 2027 Round 2 mid pick (pick 48), years_out calculated from CURRENT_DRAFT_YEAR
      "+1R2"   → 1 year from now, Round 2 mid pick — year-agnostic relative format
      "+3R1"   → 3 years from now, Round 1 mid pick (max years_out = 3)

    Returns (overall_pick, years_out, display_label) or None if cannot be parsed.
    """
    token = token.strip()

    # Relative future pick: +NR# format (e.g. "+1R2", "+3R1")
    m = _REL_FUTURE_RE.match(token)
    if m:
        years_out = int(m.group(1))   # already capped 1-3 by regex
        round_num = int(m.group(2))
        overall = _round_mid_pick(round_num)
        if overall is None:
            return None
        year_full = config.CURRENT_DRAFT_YEAR + years_out
        label = f"{year_full} R{round_num}"
        return (overall, years_out, label)

    # Future pick: YYR# format (e.g. "27R2")
    m = _FUTURE_RE.match(token)
    if m:
        year_short = int(m.group(1))
        round_num = int(m.group(2))
        year_full = 2000 + year_short
        years_out = year_full - config.CURRENT_DRAFT_YEAR

        # Sanity check - can't be in the past or too far future
        if years_out < 1 or years_out > 3:
            return None

        overall = _round_mid_pick(round_num)
        if overall is None:
            return None
        label = f"{year_full} R{round_num}"
        return (overall, years_out, label)

    # Round.pick format (e.g. "5.176", "6.190")
    m = _ROUND_PICK_RE.match(token)
    if m:
        overall = int(m.group(2))
        if 1 <= overall <= 257:
            return (overall, 0, f"Pick {overall}")
        return None

    # Raw overall pick number (e.g. "31")
    if token.isdigit():
        overall = int(token)
        if 1 <= overall <= 257:
            return (overall, 0, f"Pick {overall}")
        return None

    return None


def get_pick_values(overall: int, years_out: int) -> dict:
    """
    Return chart values for a pick, applying future discount if applicable.
    
    Future picks use "one round penalty per year" logic:
    - 2027 R1 (years_out=1) = 2026 R2 mid-pick value
    - 2027 R7 = half of pick 257 value
    - 2028 R1 (years_out=2) = 2026 R3 mid-pick value

    Returns:
        {"johnson": float, "hill": int, "fitz_spiel": int, "stuart": float}
    """
    if years_out == 0:
        # Current year pick - use normal values
        return {
            "johnson": trade_charts.JOHNSON.get(overall, 0),
            "hill": trade_charts.HILL.get(overall, 0),
            "fitz_spiel": trade_charts.FITZ_SPIEL.get(overall, 0),
            "stuart": trade_charts.STUART.get(overall, 0.0),
        }
    
    # Future pick - determine which round this pick is in
    original_round = None
    for r in sorted(trade_charts.ROUND_STARTS.keys()):
        next_start = trade_charts.ROUND_STARTS.get(r + 1, 258)
        if trade_charts.ROUND_STARTS[r] <= overall < next_start:
            original_round = r
            break
    
    if original_round is None:
        # Shouldn't happen, but fallback to zero values
        return {"johnson": 0, "hill": 0, "fitz_spiel": 0, "stuart": 0}
    
    # Apply round penalty
    discounted_round = original_round + years_out
    
    # If discounted beyond 7th round, use half of pick 257 value
    if discounted_round > 7:
        return {
            "johnson": trade_charts.JOHNSON.get(257, 0) / 2,
            "hill": trade_charts.HILL.get(257, 0) / 2,
            "fitz_spiel": trade_charts.FITZ_SPIEL.get(257, 0) / 2,
            "stuart": trade_charts.STUART.get(257, 0.0) / 2,
        }
    
    # Use mid-pick of the discounted round
    mid_pick = _round_mid_pick(discounted_round)
    if mid_pick is None:
        return {"johnson": 0, "hill": 0, "fitz_spiel": 0, "stuart": 0}
    
    return {
        "johnson": trade_charts.JOHNSON.get(mid_pick, 0),
        "hill": trade_charts.HILL.get(mid_pick, 0),
        "fitz_spiel": trade_charts.FITZ_SPIEL.get(mid_pick, 0),
        "stuart": trade_charts.STUART.get(mid_pick, 0.0),
    }


def _pick_to_round_label(pick: int) -> str:
    """Convert an overall pick number to a descriptive label like 'early 3rd'."""
    ROUND_NAMES = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", 6: "6th", 7: "7th"}
    rnd = 7
    for r in sorted(trade_charts.ROUND_STARTS.keys()):
        next_start = trade_charts.ROUND_STARTS.get(r + 1, 258)
        if trade_charts.ROUND_STARTS[r] <= pick < next_start:
            rnd = r
            break
    start = trade_charts.ROUND_STARTS[rnd]
    offset = pick - start
    round_size = trade_charts.ROUND_STARTS.get(rnd + 1, 258) - start
    third = round_size / 3
    if offset < third:
        pos = "early"
    elif offset < 2 * third:
        pos = "mid"
    else:
        pos = "late"
    return f"{pos} {ROUND_NAMES[rnd]}"


def johnson_equivalent(value: int) -> str:
    """Return a round label for the pick whose Johnson value is closest to `value`."""
    best = min(range(1, 258), key=lambda p: abs(trade_charts.JOHNSON.get(p, 0) - value))
    return _pick_to_round_label(best)


def hill_equivalent(value: float) -> str:
    """Return a round label for the pick whose Hill value is closest to `value`."""
    best = min(range(1, 258), key=lambda p: abs(trade_charts.HILL.get(p, 0) - value))
    return _pick_to_round_label(best)


def fitz_spiel_equivalent(value: int) -> str:
    """Return a round label for the pick whose Fitz-Spiel value is closest to `value`."""
    best = min(range(1, 258), key=lambda p: abs(trade_charts.FITZ_SPIEL.get(p, 0) - value))
    return _pick_to_round_label(best)


def stuart_equivalent(value: float) -> str:
    """Return a round label for the pick whose Stuart value is closest to `value`."""
    best = min(range(1, 258), key=lambda p: abs(trade_charts.STUART.get(p, 0.0) - value))
    return _pick_to_round_label(best)


def _get_round_range(round_num: int) -> tuple:
    """Get the (start, end) pick numbers for a round including comp picks."""
    start = trade_charts.ROUND_STARTS.get(round_num)
    if start is None:
        return None
    # Round 7 goes to 257 (includes comp picks), others go to next round start - 1
    if round_num == 7:
        end = 257
    else:
        end = trade_charts.ROUND_STARTS.get(round_num + 1, 258) - 1
    return (start, end)


def _get_position_pick(position: str, round_num: int, chart: str) -> int:
    """
    Get the representative pick for a position (early/middle/late) in a round.
    
    Returns the pick whose chart value is closest to the average value of that third.
    """
    round_range = _get_round_range(round_num)
    if round_range is None:
        return None
    
    start, end = round_range
    total_picks = end - start + 1
    third_size = total_picks / 3
    
    # Determine which third
    if position == "early":
        range_start = start
        range_end = start + int(third_size) - 1
    elif position == "middle":
        range_start = start + int(third_size)
        range_end = start + int(2 * third_size) - 1
    elif position == "late":
        range_start = start + int(2 * third_size)
        range_end = end
    else:
        return None
    
    # Get chart dict
    charts = {
        "johnson": trade_charts.JOHNSON,
        "hill": trade_charts.HILL,
        "fitz_spiel": trade_charts.FITZ_SPIEL,
        "stuart": trade_charts.STUART,
    }
    chart_dict = charts.get(chart)
    if chart_dict is None:
        return None
    
    # Calculate average value in that range
    values = [chart_dict.get(p, 0) for p in range(range_start, range_end + 1)]
    avg_value = sum(values) / len(values) if values else 0
    
    # Find pick with value closest to average
    best_pick = min(
        range(range_start, range_end + 1),
        key=lambda p: abs(chart_dict.get(p, 0) - avg_value)
    )
    
    return best_pick


def find_trade_down(start_pick: int, gain_pick: int, chart: str, tolerance_pct: float = 0.05, max_picks: int = 3, years_out: int = 0, available_picks: list = None):
    """
    Find a trade-down scenario where you give up start_pick and receive multiple picks including gain_pick.
    
    Args:
        start_pick: The pick you're trading away
        gain_pick: A pick you want to acquire (in addition to trading down)
        chart: Which chart to use ("johnson", "hill", "fitz_spiel", "stuart")
        tolerance_pct: Maximum % difference allowed (0.05 = 5%)
        max_picks: Maximum total picks you're willing to receive
        years_out: Years in future for gain_pick (0 = current year)
    
    Returns:
        dict with keys: picks (list of ints), delta (float), pct_diff (float)
        or None if no solution found
    """
    charts = {
        "johnson": trade_charts.JOHNSON,
        "hill": trade_charts.HILL,
        "fitz_spiel": trade_charts.FITZ_SPIEL,
        "stuart": trade_charts.STUART,
    }
    
    chart_dict = charts.get(chart)
    if chart_dict is None:
        return None
    
    start_value = chart_dict.get(start_pick, 0)
    
    # Get discounted value for gain_pick
    gain_values = get_pick_values(gain_pick, years_out)
    gain_value = gain_values.get(chart, 0)
    
    # Value we need to make up
    needed_value = start_value - gain_value
    
    # Try with 2 picks first (trade-down pick + gain_pick)
    for num_additional in range(1, max_picks):
        solutions = []
        
        search_pool = available_picks if available_picks is not None else range(start_pick + 1, 258)

        if num_additional == 1:
            # Just need one additional pick
            for p in search_pool:
                if p == gain_pick:
                    continue
                combo_value = chart_dict.get(p, 0) + gain_value
                delta = combo_value - start_value
                pct_diff = abs(delta) / start_value if start_value > 0 else 0

                if pct_diff <= tolerance_pct:
                    solutions.append({
                        "picks": sorted([p, gain_pick]),
                        "delta": delta,
                        "pct_diff": pct_diff
                    })
        else:
            # Need multiple additional picks - brute force all combinations
            valid_picks = [p for p in search_pool if p != gain_pick]
            
            for combo in combinations(valid_picks, num_additional):
                combo_value = sum(chart_dict.get(p, 0) for p in combo) + gain_value
                delta = combo_value - start_value
                pct_diff = abs(delta) / start_value if start_value > 0 else 0
                
                if pct_diff <= tolerance_pct:
                    solutions.append({
                        "picks": sorted(list(combo) + [gain_pick]),
                        "delta": delta,
                        "pct_diff": pct_diff
                    })
        
        # If we found solutions with this pick count, return the best one
        if solutions:
            # Sort by pct_diff (closest to 0 first)
            solutions.sort(key=lambda x: x["pct_diff"])
            return solutions[0]
    
    return None


def find_trade_balance(
    locked_give_value: float,
    locked_get_value: float,
    chart: str,
    direction: str,
    tolerance_pct: float = 0.05,
    max_additional: int = 2,
    search_pool=None,
) -> tuple:
    """
    Find additional picks that close the value gap in a trade where both sides
    already have picks locked in.

    direction="down": you gave locked_give_value, already receiving locked_get_value.
                      Searches for MORE picks to add to the receive side.
    direction="up":   you want locked_get_value, already giving locked_give_value.
                      Searches for MORE picks to add to the give side.

    search_pool: iterable of overall pick numbers to search. Typically picks
                 worse (higher-numbered) than the best pick in the trade.

    Returns (status, result):
        status  = "balanced" | "overpaid" | "found" | "not_found"
        result  = dict {picks, delta, pct_diff} when status is "found"
                  dict {delta, pct_diff}         when "balanced" or "overpaid"
                  None                            when "not_found"
    """
    charts = {
        "johnson":   trade_charts.JOHNSON,
        "hill":      trade_charts.HILL,
        "fitz_spiel": trade_charts.FITZ_SPIEL,
        "stuart":    trade_charts.STUART,
    }
    chart_dict = charts.get(chart, {})

    base_value = locked_give_value if direction == "down" else locked_get_value
    if base_value <= 0:
        return ("not_found", None)

    # deficit > 0 means the "add" side needs more value
    if direction == "down":
        deficit = locked_give_value - locked_get_value
    else:
        deficit = locked_get_value - locked_give_value

    pct_diff = abs(deficit) / base_value

    if pct_diff <= tolerance_pct:
        return ("balanced", {"delta": deficit, "pct_diff": pct_diff})

    if deficit < 0:
        # Already getting/giving too much
        return ("overpaid", {"delta": deficit, "pct_diff": pct_diff})

    # Search for 1..max_additional picks that sum to approximately deficit
    pool = list(search_pool) if search_pool is not None else list(range(1, 258))

    for count in range(1, max_additional + 1):
        solutions = []
        if count == 1:
            for p in pool:
                val = chart_dict.get(p, 0)
                d = val - deficit
                pd = abs(d) / base_value
                if pd <= tolerance_pct:
                    solutions.append({"picks": [p], "delta": d, "pct_diff": pd})
        else:
            for combo in combinations(pool, count):
                val = sum(chart_dict.get(p, 0) for p in combo)
                d = val - deficit
                pd = abs(d) / base_value
                if pd <= tolerance_pct:
                    solutions.append({"picks": sorted(combo), "delta": d, "pct_diff": pd})
        if solutions:
            solutions.sort(key=lambda x: x["pct_diff"])
            return ("found", solutions[0])

    return ("not_found", None)


def find_trade_up(target_pick: int, give_pick: int, chart: str, tolerance_pct: float = 0.05, max_picks: int = 3, years_out: int = 0, available_picks: list = None):
    """
    Find a trade-up scenario where you give multiple picks including give_pick to receive target_pick.
    
    Args:
        target_pick: The pick you want to acquire
        give_pick: A pick you're willing to give up (in addition to trading up)
        chart: Which chart to use ("johnson", "hill", "fitz_spiel", "stuart")
        tolerance_pct: Maximum % difference allowed (0.05 = 5%)
        max_picks: Maximum total picks you're willing to give
        years_out: Years in future for give_pick (0 = current year)
    
    Returns:
        dict with keys: picks (list of ints), delta (float), pct_diff (float)
        or None if no solution found
    """
    charts = {
        "johnson": trade_charts.JOHNSON,
        "hill": trade_charts.HILL,
        "fitz_spiel": trade_charts.FITZ_SPIEL,
        "stuart": trade_charts.STUART,
    }
    
    chart_dict = charts.get(chart)
    if chart_dict is None:
        return None
    
    target_value = chart_dict.get(target_pick, 0)
    
    # Get discounted value for give_pick
    give_values = get_pick_values(give_pick, years_out)
    give_value = give_values.get(chart, 0)
    
    # Value we need to make up
    needed_value = target_value - give_value
    
    # Try with 2 picks first (give_pick + one additional)
    for num_additional in range(1, max_picks):
        solutions = []
        
        search_pool = available_picks if available_picks is not None else range(target_pick + 1, 258)

        if num_additional == 1:
            # Just need one additional pick to give
            for p in search_pool:
                if p == give_pick:
                    continue
                combo_value = chart_dict.get(p, 0) + give_value
                delta = combo_value - target_value
                pct_diff = abs(delta) / target_value if target_value > 0 else 0

                if pct_diff <= tolerance_pct:
                    solutions.append({
                        "picks": sorted([p, give_pick]),
                        "delta": delta,
                        "pct_diff": pct_diff
                    })
        else:
            # Need multiple additional picks
            valid_picks = [p for p in search_pool if p != give_pick]
            
            for combo in combinations(valid_picks, num_additional):
                combo_value = sum(chart_dict.get(p, 0) for p in combo) + give_value
                delta = combo_value - target_value
                pct_diff = abs(delta) / target_value if target_value > 0 else 0
                
                if pct_diff <= tolerance_pct:
                    solutions.append({
                        "picks": sorted(list(combo) + [give_pick]),
                        "delta": delta,
                        "pct_diff": pct_diff
                    })
        
        # If we found solutions with this pick count, return the best one
        if solutions:
            solutions.sort(key=lambda x: x["pct_diff"])
            return solutions[0]
    
    return None
