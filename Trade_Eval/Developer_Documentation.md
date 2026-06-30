# Trade Evaluator - Developer Documentation

## Table of Contents
1. [Architecture Overview](#architecture-overview)
2. [Module Reference](#module-reference)
3. [Data Structures](#data-structures)
4. [Algorithm Details](#algorithm-details)
5. [Discord Integration](#discord-integration)
6. [Testing & Validation](#testing--validation)
7. [Common Modifications](#common-modifications)
8. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

### Module Dependencies
```
config.py (no dependencies)
    ↓
trade_charts.py (no dependencies)
    ↓
trade_logic.py (imports: config, trade_charts)
    ↓
trade_eval.py (imports: config, trade_charts, trade_logic, discord, commands)
```

**Key principle:** Data → Logic → UI separation
- `trade_charts.py` = pure data (dictionaries only)
- `trade_logic.py` = pure functions (no Discord dependencies)
- `trade_eval.py` = Discord cog (UI layer only)

### Reload System
```python
# In trade_eval.py setup()
async def setup(bot):
    importlib.reload(config)
    importlib.reload(trade_charts)
    importlib.reload(trade_logic)
    await bot.add_cog(TradeEval(bot))
```

When `!reload` is called, the cog's `setup()` function runs, which reloads all three helper modules. This means changes to ANY of the four files are picked up without restarting the bot.

---

## Module Reference

### config.py

**Purpose:** Single source of truth for configuration

**Contents:**
```python
CURRENT_DRAFT_YEAR = 2026  # Update annually or via !trade.year.update
```

**Modification:** Edit directly or use `!trade.year.update <year>` command


### trade_charts.py

**Purpose:** Chart data only - no logic

**Contents:**
- `ROUND_STARTS: dict[int, int]` - Maps round number (1-7) to first overall pick
  - Example: `{1: 1, 2: 33, 3: 65, ...}`
- `JOHNSON: dict[int, float]` - Jimmy Johnson values, picks 1-256
- `HILL: dict[int, int]` - Rich Hill values, picks 1-257
- `FITZ_SPIEL: dict[int, int]` - Fitzgerald-Spielberger values, picks 1-256
- `STUART: dict[int, float]` - Stuart values, picks 1-224 (225-256 = 0.0)

**Data format:**
```python
JOHNSON = {
    1: 3000,
    2: 2600,
    # ... complete mapping ...
    256: 0.4
}
```

**Edge cases:**
- Pick 257 exists only in HILL chart (comp pick)
- Picks 225-256 are compensatory picks
- STUART chart uses `**{p: 0.0 for p in range(225, 257)}` for comp picks


### trade_logic.py

**Purpose:** Core algorithms and business logic

#### Constants
```python
_FUTURE_RE = re.compile(r'^(\d{2})R(\d)$', re.IGNORECASE)
# Matches: 27R2, 28R1 (year + R + round)
# Groups: (1) = year (27), (2) = round (2)

_ROUND_PICK_RE = re.compile(r'^(\d)\.(\d{1,3})$')
# Matches: 5.176, 3.77 (round.overall)
# Groups: (1) = round, (2) = overall pick number
```

#### Core Functions

**parse_pick(token: str) -> tuple | None**
```python
"""
Parse a pick token into (overall_pick, years_out, display_label).

Args:
    token: String like "33", "27R2", "5.176"

Returns:
    (overall: int, years_out: int, label: str) or None

Examples:
    "33" → (33, 0, "Pick 33")
    "27R2" → (48, 1, "2027 R2")  # mid-round 2 = pick 48, 1 year out
    "5.176" → (176, 0, "Pick 176")
    "invalid" → None
"""
```

**Implementation notes:**
- Mid-round calculation: `start + 15` (assumes 32-pick rounds)
- Future year validation: 1-5 years out only
- Returns None on any parse failure (don't raise exceptions)

**get_pick_values(overall: int, years_out: int) -> dict**
```python
"""
Get chart values for a pick with future discount applied.

Args:
    overall: Pick number 1-257
    years_out: 0 for current year, 1+ for future

Returns:
    {
        "johnson": float,
        "hill": int,
        "fitz_spiel": int,
        "stuart": float
    }

Future discount logic:
    - Determine original round from overall pick
    - Add years_out to get discounted_round
    - If discounted_round > 7: use half of pick 257 value
    - Else: use mid-pick value of discounted_round
"""
```

**Example:**
```python
# 2027 R1 (overall=16, years_out=1)
# Original round = 1
# Discounted round = 1 + 1 = 2
# Use mid-pick of round 2 (pick 48) values

get_pick_values(16, 1)
# Returns values from charts for pick 48, not pick 16
```

**find_trade_down(start_pick, gain_pick, chart, tolerance_pct, max_picks, years_out) -> dict | None**
```python
"""
Find picks to balance a trade-down scenario.

Args:
    start_pick: Pick you're giving away (e.g., 33)
    gain_pick: Pick you want to receive (e.g., 98)
    chart: "johnson" | "hill" | "fitz_spiel" | "stuart"
    tolerance_pct: Max % difference (0.05 = 5%)
    max_picks: Max total picks to receive (usually 3 or 5)
    years_out: Future discount for gain_pick (0 = current year)

Returns:
    {
        "picks": [98, 145],  # sorted list including gain_pick
        "delta": 5.0,        # positive = overpay, negative = underpay
        "pct_diff": 0.03     # 3% difference
    }
    or None if no solution found

Algorithm:
    1. Calculate start_value from chart
    2. Get gain_value (with future discount if years_out > 0)
    3. Try 2-pick solutions first (gain_pick + one other)
    4. If none work, try 3-pick solutions, etc.
    5. Return first solution within tolerance_pct
    6. Sort solutions by pct_diff (best balance first)
"""
```

**find_trade_up(target_pick, give_pick, chart, tolerance_pct, max_picks, years_out) -> dict | None**
```python
"""
Find picks to balance a trade-up scenario.

Args:
    target_pick: Pick you want to acquire (e.g., 10)
    give_pick: Pick you're giving (e.g., 33)
    chart: "johnson" | "hill" | "fitz_spiel" | "stuart"
    tolerance_pct: Max % difference (0.05 = 5%)
    max_picks: Max total picks to give (usually 3 or 5)
    years_out: Future discount for give_pick (0 = current year)

Returns: Same format as find_trade_down()

Algorithm: Mirror of find_trade_down, searching picks after target_pick
"""
```

**_get_position_pick(position: str, round_num: int, chart: str) -> int | None**
```python
"""
Get representative pick for early/middle/late position in a round.

Args:
    position: "early" | "middle" | "late"
    round_num: 1-7
    chart: "johnson" | "hill" | "fitz_spiel" | "stuart"

Returns:
    Overall pick number or None

Algorithm:
    1. Get round range (start, end) including comp picks
    2. Divide into thirds based on actual round size
    3. Calculate average VALUE of picks in that third
    4. Find pick whose value is closest to that average
    
Example:
    Round 3 = picks 65-96 (32 picks)
    early = 65-75 (first 11 picks)
    Calculate avg value: (65val + 66val + ... + 75val) / 11
    Find pick in 65-75 with value closest to avg
    → Might return 69 on Hill, 68 on Johnson (different curves)
"""
```

**Round label functions:**
```python
johnson_equivalent(value: int) -> str
hill_equivalent(value: float) -> str
fitz_spiel_equivalent(value: int) -> str
stuart_equivalent(value: float) -> str

"""
Convert a value to descriptive round label.

Args:
    value: Chart points

Returns:
    String like "early 3rd", "mid 2nd", "late 5th"

Algorithm:
    1. Find pick with value closest to input
    2. Determine round and position within round
    3. Format as "{position} {round_ordinal}"
"""
```


### trade_eval.py

**Purpose:** Discord cog - UI layer only

#### Helper Functions

**_extract_pick_tokens(text: str) -> list[str]**
```python
"""
Extract pick tokens from freeform text.

Regex patterns applied in order:
    1. _YEAR_ORD_RE: "2027 3rd" → "27R3"
    2. _ROUND_PAREN_RE: "1(16)" → "1.16"
    3. _PICK_RE: Extract all valid pick patterns

Returns: List of token strings
"""
```

**Regex patterns:**
```python
_PICK_RE = re.compile(r'\b(\d{2}[rR]\d|\d\.\d{1,3}|\d{1,3})\b', re.IGNORECASE)
# Matches: 27R2, 5.176, 33 (word boundary required)

_ROUND_PAREN_RE = re.compile(r'(\d+)\((\d+)\)')
# Matches: 1(16), 3(77)

_YEAR_ORD_RE = re.compile(r'20(\d{2})\s+(\d)(?:st|nd|rd|th)\b', re.IGNORECASE)
# Matches: 2027 3rd, 2026 1st
```

**_parse_side(tokens: list[str]) -> list[dict] | None**
```python
"""
Parse pick tokens into structured pick dictionaries.

Args:
    tokens: List of pick strings from _extract_pick_tokens

Returns:
    [
        {
            "label": "Pick 33",
            "overall": 33,
            "years_out": 0,
            "johnson": 580,
            "hill": 180,
            "fitz_spiel": 1228,
            "stuart": 12.3
        },
        # ... more picks ...
    ]
    or None if any token fails to parse

Uses:
    trade_logic.parse_pick() for parsing
    trade_logic.get_pick_values() for chart values
"""
```

**_format_side(picks: list[dict], side_label: str) -> str**
```python
"""
Format picks as monospace table for Discord embed.

Column widths:
    Label: 13 chars
    Johnson: 7 chars (right-aligned)
    Hill: 6 chars (right-aligned, 2 decimals)
    Fitz-Spiel: 10 chars (right-aligned)
    Stuart: 6 chars (right-aligned, 1 decimal)

Future pick notation:
    "2027 R2 (-1yr)" for 1 year out
    "2028 R1 (-2yr)" for 2 years out

Returns: Multi-line string with:
    - Side label
    - Column headers
    - Separator line
    - Pick rows
    - Separator line
    - TOTAL row
"""
```

#### Discord Commands

**!trade**
```python
@commands.command(name="trade")
async def trade_eval(ctx, *, args: str = None)

Flow:
    1. Split input by "\n" or " for "
    2. Extract tokens from each side
    3. Parse tokens to pick dicts
    4. Calculate totals per chart
    5. Determine winner per chart
    6. Color embed based on Johnson chart balance
        - Green: balanced (< 5% diff or tie)
        - Orange: lopsided
    7. Add fields: Side A, Side B, Chart Advantage
    8. Footer: Future pick discount explanation
```

**!find.trade.down / !find.trade.up**
```python
@commands.command(name="find.trade.down")
@commands.command(name="find.trade.down.johnson")
@commands.command(name="find.trade.down.hill")
# ... etc for all variants

Flow:
    1. Parse syntax: position or exact-pick
    2. If position: extract start_pick, position, round_num
    3. Call _find_trade_helper with direction, chart, best_mode
    4. Helper calls _get_position_pick for each chart
    5. Helper calls find_trade_down/up for each chart
    6. Build embed with results per chart
    7. Show "No solution" if None returned
```

**!trade.year.update**
```python
@commands.command(name="trade.year.update")
@commands.is_owner()
async def update_draft_year(ctx, year: int = None)

Flow:
    1. Validate year (2020-2040 range)
    2. Read config.py file
    3. Regex replace CURRENT_DRAFT_YEAR value
    4. Write back to file
    5. Tell user to run !reload
    6. Error handling for file I/O
```

---

## Data Structures

### Pick Dictionary
```python
{
    "label": str,          # Display name: "Pick 33" or "2027 R2"
    "overall": int,        # Overall pick number 1-257
    "years_out": int,      # 0 = current, 1+ = future
    "johnson": float,      # Johnson chart value
    "hill": int,           # Hill chart value
    "fitz_spiel": int,     # Fitz-Spiel chart value
    "stuart": float        # Stuart chart value
}
```

### Trade Solution Dictionary
```python
{
    "picks": list[int],    # Sorted list of overall pick numbers
    "delta": float,        # Value difference (positive = overpay)
    "pct_diff": float      # Percentage difference (0.03 = 3%)
}
```

### Embed Color Logic
```python
j_max = max(j_a, j_b)
close = j_max > 0 and (abs(j_diff) / j_max) < CLOSE_THRESHOLD_PCT  # 0.05

if close or j_winner == "tie":
    color = discord.Color.green()   # Balanced/fair
else:
    color = discord.Color.orange()  # Lopsided
```

---

## Algorithm Details

### Future Pick Discount Formula

**Old system (DEPRECATED):**
```python
value * 0.55  # Simple 55% reduction
```

**New system (CURRENT):**
```python
# One round penalty per year
# 2027 R1 → 2026 R2 mid-pick value
# 2028 R1 → 2026 R3 mid-pick value
# Future R8+ → half of pick 257

if years_out == 0:
    return chart[overall]
else:
    original_round = determine_round(overall)
    discounted_round = original_round + years_out
    
    if discounted_round > 7:
        return chart[257] / 2
    else:
        mid_pick = _round_mid_pick(discounted_round)
        return chart[mid_pick]
```

**Example walkthrough:**
```python
# Input: 2028 R1 (overall = 16, years_out = 2)
# Step 1: Determine original round
#   Pick 16 is in round 1 (ROUND_STARTS[1]=1, next=33)
# Step 2: Apply penalty
#   discounted_round = 1 + 2 = 3
# Step 3: Get mid-pick of round 3
#   Round 3 starts at 65
#   mid_pick = 65 + 15 = 80
# Step 4: Return chart value for pick 80
#   HILL[80] = 56
```

### Position Pick Calculation

**Algorithm:**
```python
def _get_position_pick(position, round_num, chart):
    # 1. Get round boundaries
    start = ROUND_STARTS[round_num]
    end = ROUND_STARTS[round_num + 1] - 1  # Or 257 for round 7
    
    # 2. Calculate third boundaries
    total = end - start + 1
    third_size = total / 3
    
    if position == "early":
        range_start = start
        range_end = start + int(third_size) - 1
    elif position == "middle":
        range_start = start + int(third_size)
        range_end = start + int(2 * third_size) - 1
    else:  # late
        range_start = start + int(2 * third_size)
        range_end = end
    
    # 3. Calculate average value in range
    values = [chart[p] for p in range(range_start, range_end + 1)]
    avg_value = sum(values) / len(values)
    
    # 4. Find pick closest to average
    best_pick = min(
        range(range_start, range_end + 1),
        key=lambda p: abs(chart[p] - avg_value)
    )
    
    return best_pick
```

**Example:**
```python
# Round 3, early, Hill chart
# Round 3 = picks 65-96 (32 picks)
# Third size = 32 / 3 = 10.67
# Early range = 65 to 65+10 = 65-75

# Values: HILL[65]=78, HILL[66]=76, ..., HILL[75]=63
# Average = (78+76+75+73+71+70+68+67+65+64+63) / 11 = 70.0
# Find pick with value closest to 70.0
# HILL[70] = 70 exactly → return 70

# Same for Johnson chart:
# JOHNSON[65]=265, JOHNSON[66]=260, ..., JOHNSON[75]=215
# Average = 240
# JOHNSON[68] = 250, JOHNSON[69] = 245
# Pick 69 is closest → return 69
```

### Trade Finder Search

**Combinatorial approach:**
```python
# Try increasing pick counts until solution found
for num_picks in range(2, max_picks + 1):
    if num_picks == 2:
        # Try single additional pick
        for p in range(start + 1, 258):
            test_value = chart[p] + required_pick_value
            if within_tolerance(test_value, target_value):
                return [p, required_pick]
    else:
        # Try all combinations of size num_picks-1
        # (required_pick is always included)
        from itertools import combinations
        for combo in combinations(valid_picks, num_picks - 1):
            test_value = sum(chart[p] for p in combo) + required_pick_value
            if within_tolerance(test_value, target_value):
                return sorted(list(combo) + [required_pick])
    
return None  # No solution found
```

**Performance:**
- 2 picks: O(n) where n = valid pick count (~220)
- 3 picks: O(n²) = ~48k combinations
- 4 picks: O(n³) = ~10M combinations
- 5 picks: O(n⁴) = ~2.3B combinations

**Why max_picks caps at 3/5:**
Performance degrades rapidly. 5-pick search can take several seconds.

---

## Discord Integration

### Embed Structure

**Trade Evaluation:**
```python
embed = discord.Embed(
    title="NFL Trade Evaluator",
    color=green_or_orange_based_on_balance
)
embed.add_field(name="\u200b", value=f"```\n{side_a_table}\n```", inline=False)
embed.add_field(name="\u200b", value=f"```\n{side_b_table}\n```", inline=False)
embed.add_field(
    name="Chart Advantage",
    value="Johnson: +50 to Side A (~early 4th)\nHill: +10.5 to Side B (~mid 5th)",
    inline=False
)
embed.set_footer(text="Future picks: one round penalty per year")
```

**Trade Finder:**
```python
embed = discord.Embed(
    title="Trade Down from Pick 33 + Early 3rd",
    description="(Standard: 5% tolerance, max 3 picks)",
    color=discord.Color.blue()
)
embed.add_field(
    name="📊 Johnson",
    value="**Get:** 56 + 69\nBalance: +5.00 (0.86%)\n(early 3rd = pick 69)",
    inline=False
)
# ... repeat for each chart
embed.set_footer(text="Give: Pick 33")
```

### Command Parsing Patterns

**Flexible separators:**
```python
# All of these work:
"!trade 14 for 28 59"
"!trade Side A: 14 for Side B: 28 59"
"!trade We get: 14\nThey get: 28 59"
```

**Token extraction ignores prose:**
```python
"Cowboys get pick 1(14) and Eagles get 1(28) and 2(59)"
# Extracted: ["1(14)", "1(28)", "2(59)"]
# Prose words ignored
```

---

## Testing & Validation

### Known Test Cases

**Basic trade evaluation:**
```python
Input: !trade 33 for 98
Expected:
    Johnson: 33=580, 98=108 → Need ~472 more
    Hill: 33=180, 98=37 → Need ~143 more
```

**Future pick discount:**
```python
Input: 27R1 (2027 Round 1)
Parse: overall=16 (mid-round 1), years_out=1
Discount: Round 1 + 1 year = Round 2
Value: HILL[48] = 121 (mid-round 2)

Input: 28R1 (2028 Round 1)
Parse: overall=16, years_out=2
Discount: Round 1 + 2 years = Round 3
Value: HILL[80] = 56 (mid-round 3)
```

**Trade finder:**
```python
Input: !find.trade.down.hill 33 + early 3rd
Expected flow:
    1. Get early 3rd pick on Hill: 69 (avg value 70)
    2. Hill[33] = 180, Hill[69] = 71
    3. Need additional value ≈ 109
    4. Search for pick with value ~109
    5. Hill[52] = 109 exactly
    6. Return [52, 69], delta=0, pct_diff=0.0%
```

### Validation Checklist

When modifying code:
- [ ] Parse "33", "27R2", "5.176" correctly
- [ ] Future picks discount by round penalty (not 55%)
- [ ] Position picks differ by chart (early 3rd ≠ same pick on all charts)
- [ ] Trade finder returns None when no solution
- [ ] Embed colors match balance thresholds
- [ ] Footer text reflects current discount system
- [ ] !reload picks up changes to all files

### Edge Case Tests

**Pick 257:**
```python
# Only exists in HILL chart
HILL[257] = 1
JOHNSON[257] → KeyError (use .get(257, 0))
```

**Compensatory picks:**
```python
# Picks 225-256 exist in all charts
# STUART sets them to 0.0
# JOHNSON has fractional values (0.95, 0.4, etc.)
```

**Future R7:**
```python
# 2027 R7 (years_out=1)
# Round 7 + 1 = Round 8 (doesn't exist)
# Use HILL[257] / 2 = 0.5
```

**Round boundaries with comp picks:**
```python
# Round 7 doesn't end at start+31
# Ends at pick 257 (variable size)
_get_round_range(7) → (193, 257)
```

---

## Common Modifications

### Adding a New Chart

**Step 1:** Add data to `trade_charts.py`
```python
MY_CHART = {
    1: 1000,
    2: 950,
    # ... complete mapping 1-256 or 1-257
}
```

**Step 2:** Update `trade_logic.py`
```python
# In get_pick_values()
return {
    "johnson": ...,
    "hill": ...,
    "fitz_spiel": ...,
    "stuart": ...,
    "my_chart": MY_CHART.get(mid_pick, 0),  # Add this line
}

# In _get_position_pick()
charts = {
    "johnson": trade_charts.JOHNSON,
    "hill": trade_charts.HILL,
    "fitz_spiel": trade_charts.FITZ_SPIEL,
    "stuart": trade_charts.STUART,
    "my_chart": trade_charts.MY_CHART,  # Add this line
}

# Add equivalent function
def my_chart_equivalent(value: float) -> str:
    best = min(range(1, 258), key=lambda p: abs(trade_charts.MY_CHART.get(p, 0) - value))
    return _pick_to_round_label(best)
```

**Step 3:** Update `trade_eval.py`
```python
# In _parse_side(), add to pick dict
picks.append({
    "label": label,
    "overall": overall,
    "years_out": years_out,
    "johnson": values["johnson"],
    "hill": values["hill"],
    "fitz_spiel": values["fitz_spiel"],
    "stuart": values["stuart"],
    "my_chart": values["my_chart"],  # Add this
})

# In _format_side(), add column
MY_W = 8
lines.append(
    f"  {label:<{LABEL_W}} {p['johnson']:>{J_W}} | {p['hill']:>{H_W}.2f}"
    f" | {p['fitz_spiel']:>{F_W}} | {p['stuart']:>{S_W}.1f} | {p['my_chart']:>{MY_W}.2f}"
)

# Add commands
@commands.command(name="find.trade.down.mychart")
async def find_trade_down_mychart(self, ctx, *, args: str = None):
    await self._find_trade_helper(ctx, args, "down", chart="my_chart", best_mode=False)
```

### Changing Future Discount Logic

**Example: Revert to 55% model**

`trade_logic.py`:
```python
def get_pick_values(overall: int, years_out: int) -> dict:
    if years_out == 0:
        return {
            "johnson": trade_charts.JOHNSON.get(overall, 0),
            # ... etc
        }
    
    # Apply 55% discount per year
    discount_factor = 0.55 ** years_out
    
    return {
        "johnson": trade_charts.JOHNSON.get(overall, 0) * discount_factor,
        "hill": trade_charts.HILL.get(overall, 0) * discount_factor,
        "fitz_spiel": trade_charts.FITZ_SPIEL.get(overall, 0) * discount_factor,
        "stuart": trade_charts.STUART.get(overall, 0.0) * discount_factor,
    }
```

`trade_eval.py` footer:
```python
embed.set_footer(text="Future picks discounted 55% per year")
```

### Adjusting Tolerance Thresholds

**Standard mode:** `tolerance_pct=0.05` (5%)
**Best mode:** `tolerance_pct=0.02` (2%)

To change:
```python
# In _find_trade_position_helper() and _find_trade_exact_helper()
tolerance = 0.03 if best_mode else 0.10  # New: 3% best, 10% standard
max_picks = 7 if best_mode else 4        # New: 7 best, 4 standard
```

### Adding Exact-Pick Syntax

**Current status:** Parser exists but returns "coming soon"

**To implement:**
```python
# In _find_trade_exact_helper()
# Remove placeholder message
# await ctx.send("Exact-pick mode coming soon!")

# Add implementation
if direction == "down":
    # User gave one pick, wants multiple picks back including specified ones
    # Need to modify find_trade_down to accept multiple required picks
    pass
else:
    # User giving multiple picks to get one back
    # Need to modify find_trade_up similarly
    pass
```

**Requires:** Updating `find_trade_down/up` to accept `required_picks: list[int]` instead of single `gain_pick`

---

## Troubleshooting

### Issue: !reload doesn't pick up changes

**Cause:** `importlib.reload()` not in `setup()` function

**Fix:**
```python
async def setup(bot):
    importlib.reload(config)      # Must be here
    importlib.reload(trade_charts)
    importlib.reload(trade_logic)
    await bot.add_cog(TradeEval(bot))
```

### Issue: Parse errors on valid picks

**Symptoms:** "Could not parse pick: `27R2`"

**Debug:**
```python
# Add logging to parse_pick()
result = trade_logic.parse_pick("27R2")
print(f"Parse result: {result}")  # Should be (48, 1, "2027 R2")

# Check CURRENT_DRAFT_YEAR
print(f"Current year: {config.CURRENT_DRAFT_YEAR}")  # Should be 2026
# 27R2 = 2027, years_out = 2027 - 2026 = 1 ✓
```

**Common causes:**
- CURRENT_DRAFT_YEAR not updated
- Regex pattern mismatch
- Year out of range (>5 years future)

### Issue: Wrong future pick values

**Symptoms:** 2027 R1 shows same value as 2026 R1

**Debug:**
```python
# Check discount application
values = get_pick_values(16, 1)  # 2027 R1
print(f"Hill value: {values['hill']}")  # Should be ~101 (round 2 mid value)
# NOT 305 (round 1 value)

# Verify round penalty logic
original_round = 1
discounted_round = 1 + 1  # = 2
mid_pick = _round_mid_pick(2)  # = 48
expected = HILL[48]  # = 121
```

### Issue: Trade finder returns no solution

**Symptoms:** All charts show "❌ No solution found"

**Possible causes:**
1. Tolerance too strict (try best mode)
2. Required pick creates impossible constraint
3. Not enough picks in valid range

**Debug:**
```python
# Check if ANY solution exists (ignore tolerance)
result = find_trade_down(33, 69, "hill", tolerance_pct=1.0, max_picks=5)
# If this returns None, no combination of ≤5 picks works at all

# Check individual pick values
print(f"Start: {HILL[33]}")  # 180
print(f"Gain: {HILL[69]}")   # 71
print(f"Needed: {180-71}")   # 109
print(f"Closest pick: {HILL[52]}")  # 109 (exact match exists)
```

### Issue: Embed formatting broken

**Symptoms:** Columns misaligned, text overflow

**Cause:** Column width constants don't match actual usage

**Fix:**
```python
# In _format_side()
# Ensure widths accommodate max values:
# Johnson max = 3000 (4 digits) → need width 7 for right-align + padding
# Hill max = 1000 (4 digits) → need width 6 for decimals
# Check actual output character count
```

### Issue: KeyError on chart lookup

**Symptoms:** `KeyError: 257` or similar

**Cause:** Pick number doesn't exist in chart

**Fix:**
```python
# Always use .get() with default
value = chart.get(pick, 0)  # NOT chart[pick]

# Exception: HILL chart goes to 257
if chart_name == "hill" and pick == 257:
    value = HILL[257]  # OK to use direct lookup
else:
    value = chart.get(pick, 0)
```

### Issue: Discord command not recognized

**Symptoms:** Bot doesn't respond to `!find.trade.down`

**Causes:**
1. Command name typo in decorator
2. Cog not loaded in bot.py
3. Command name conflicts with another cog

**Debug:**
```python
# Check loaded commands
print(bot.all_commands.keys())  # Should include "find.trade.down"

# Check cog loading
print(bot.extensions)  # Should include "Trade_Eval.trade_eval"

# Check for errors in bot.log
# Look for "Failed to load Trade_Eval.trade_eval"
```

---

## Performance Considerations

### Trade Finder Complexity

**2-pick search:** Fast (~220 iterations)
**3-pick search:** Medium (~24k iterations)
**4-pick search:** Slow (~2.6M iterations)
**5-pick search:** Very slow (~300M iterations)

**Optimization opportunity:**
Early exit once first solution found (already implemented).
Could add: Sort picks by value before search to find balanced solutions faster.

### Large Embed Limits

Discord embeds have limits:
- Title: 256 chars
- Description: 4096 chars
- Field value: 1024 chars
- Total: 6000 chars

**Current usage:**
- Trade eval: ~800 chars (safe)
- Trade finder (4 charts): ~1200 chars (safe)

**Risk:** Adding 5th+ chart could approach limits

### Reload Performance

Each `!reload` reloads 3 modules + cog = 4 imports.
`trade_charts.py` is large (all chart data) but imports are fast (<100ms).

---

## Code Style Conventions

### Naming
- Private functions: `_function_name` (underscore prefix)
- Public API: `function_name` (no prefix)
- Constants: `UPPER_CASE`
- Chart names: `"johnson"`, `"hill"`, `"fitz_spiel"`, `"stuart"` (lowercase, underscores)

### Return Types
- Parsing functions return `None` on failure (not exceptions)
- Value functions return `0` or `0.0` for missing picks (not None)
- Trade finders return `None` for no solution (not empty dict)

### Discord Patterns
- Embed color based on balance (green/orange)
- Field names use `"\u200b"` (zero-width space) for blank headers
- Code blocks use triple backticks: ` ```\n{content}\n``` `
- Footer text explains discount system

### Error Messages
- User-facing: "Could not parse pick: `27R2`"
- Internal: `return None` (silent failure)
- Owner commands: "❌ Error: {exception message}"

---

## Future Enhancement Ideas

1. **Exact-pick trade finder**
   - `!find.trade.down 33 for 98 + 145`
   - Requires multi-pick required set in algorithms

2. **Trade history**
   - Store evaluated trades in SQLite
   - `!trade.history` command
   - Analytics on chart usage

3. **Custom charts**
   - User-uploaded CSV → chart dict
   - Stored per-server or per-user

4. **Interactive mode**
   - Buttons to adjust picks
   - Live balance updates

5. **Chart comparison**
   - `!chart.compare 33` → show all chart values
   - Visualize value curves

6. **Multi-team trades**
   - 3+ sides (complex UI challenge)

7. **Historical accuracy**
   - Track real NFL trades
   - Compare chart predictions vs actual deals

---

## References

### Chart Sources
- **Johnson:** Original Jimmy Johnson chart (1991)
- **Hill:** Rich Hill, PatsPulpit / DraftTek (2020)
- **Fitz-Spiel:** Fitzgerald & Spielberger, Over The Cap (2019)
- **Stuart:** Chase Stuart, Football Perspective (2012)

### Discord.py Documentation
- Commands: https://discordpy.readthedocs.io/en/stable/ext/commands/
- Embeds: https://discordpy.readthedocs.io/en/stable/api.html#discord.Embed

### Python Patterns
- importlib.reload(): https://docs.python.org/3/library/importlib.html#importlib.reload
- itertools.combinations: https://docs.python.org/3/library/itertools.html#itertools.combinations
- Regex: https://docs.python.org/3/library/re.html

---

## Version History

**v2.0** (Current)
- Split into 4 files (config, charts, logic, eval)
- Changed future discount to "one round penalty per year"
- Added trade finder commands
- Added `!trade.year.update` command
- Added position-based syntax
- Added importlib.reload() system

**v1.0** (Original)
- Single file implementation
- 55% future discount
- Trade evaluation only
- No trade finder

---

**Last Updated:** 2026-04-24
**Maintainer:** See bot owner
**Questions:** Use `!help trade` for user docs, this file for dev docs
