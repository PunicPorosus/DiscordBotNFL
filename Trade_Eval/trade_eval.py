"""
NFL Trade Evaluator — Discord cog.
Evaluates draft pick trades using the Jimmy Johnson and Hill chart systems.

Usage: !trade 31 178 for 41 186 27R2
"""

import re
import asyncio
import importlib
import logging
from pathlib import Path

import discord
from discord.ext import commands, tasks

# Import modules - will be reloaded in setup()
from Trade_Eval import config, trade_charts, trade_logic, database, cache_manager, locks_order
from Trade_Eval import trade_image
from Trade_Eval.database import Database
from Trade_Eval.cache_manager import CacheManager
from Trade_Eval.pick_loader import DEFAULT_ROUND_GIDS
from BotUtils.time import now_eastern

logger = logging.getLogger("trade_eval.cog")

# Matches valid pick tokens anywhere in freeform text:
#   +1R2  — relative future pick (year-agnostic)
#   27R2  — absolute future pick (YYR# format)
#   5.176 — round.overall format
#   31    — raw overall pick number
# Uses (?<!\w) / (?!\w) instead of \b so the leading + in +NR tokens is captured.
_PICK_RE = re.compile(
    r'(?<!\w)(\+[1-3][rR][1-7]|\d{2}[rR]\d|\d\.\d{1,3}|\d{1,3})(?!\w)',
    re.IGNORECASE
)

# Matches "Round(Overall)" format e.g. "1(16)" or "3(77)" — converts to dot format e.g. "1.16", "3.77"
_ROUND_PAREN_RE = re.compile(r'(\d+)\((\d+)\)')

# Matches "YYYY Nth" future pick format e.g. "2027 3rd", "2026 1st" — converts to YYRn e.g. "27R3", "26R1"
_YEAR_ORD_RE = re.compile(r'20(\d{2})\s+(\d)(?:st|nd|rd|th)\b', re.IGNORECASE)


def _extract_pick_tokens(text: str) -> list:
    """Normalize pick formats then extract all pick tokens."""
    text = _YEAR_ORD_RE.sub(lambda m: f"{m.group(1)}R{m.group(2)}", text)
    text = _ROUND_PAREN_RE.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
    return _PICK_RE.findall(text)

# Threshold (Johnson points) for a "close" trade — within this range = orange embed
CLOSE_THRESHOLD_PCT = 0.05  # 5% difference on Johnson chart


def _parse_side(tokens: list) -> list | None:
    """
    Parse a list of pick token strings into a list of pick dicts.

    Returns list of dicts: [{"label": str, "overall": int, "years_out": int,
                              "johnson": float, "hill": float, "fitz_spiel": int}, ...]
    Returns None if any token fails to parse.
    """
    picks = []
    for token in tokens:
        result = trade_logic.parse_pick(token)
        if result is None:
            return None
        overall, years_out, label = result
        values = trade_logic.get_pick_values(overall, years_out)
        picks.append({
            "label": label,
            "overall": overall,
            "years_out": years_out,
            "johnson": values["johnson"],
            "hill": values["hill"],
            "fitz_spiel": values["fitz_spiel"],
            "stuart": values["stuart"],
        })
    return picks


def _format_side(picks: list, side_label: str) -> str:
    """
    Build a monospace-formatted string for one side of the trade.
    Columns: Pick label | Johnson | Hill | Fitz-Spiel | Stuart
    """
    LABEL_W = 13
    J_W = 7
    H_W = 6
    F_W = 10
    S_W = 6
    SEP_W = LABEL_W + J_W + H_W + F_W + S_W + 10

    lines = [f"{side_label}"]

    # Column header
    lines.append(f"  {'Pick':<{LABEL_W}} {'Johnson':>{J_W}} | {'Hill':>{H_W}} | {'Fitz-Spiel':>{F_W}} | {'Stuart':>{S_W}}")
    lines.append("  " + "-" * SEP_W)

    for p in picks:
        future_note = f" (-{p['years_out']}yr)" if p['years_out'] > 0 else ""
        label = f"{p['label']}{future_note}"
        lines.append(
            f"  {label:<{LABEL_W}} {p['johnson']:>{J_W}} | {p['hill']:>{H_W}.2f}"
            f" | {p['fitz_spiel']:>{F_W}} | {p['stuart']:>{S_W}.1f}"
        )

    # Separator + totals
    j_total = sum(p["johnson"] for p in picks)
    h_total = sum(p["hill"] for p in picks)
    f_total = sum(p["fitz_spiel"] for p in picks)
    s_total = sum(p["stuart"] for p in picks)
    lines.append("  " + "-" * SEP_W)
    lines.append(f"  {'TOTAL':<{LABEL_W}} {j_total:>{J_W}} | {h_total:>{H_W}.2f} | {f_total:>{F_W}} | {s_total:>{S_W}.1f}")

    return "\n".join(lines)


def _resolve_team(query: str, draft_cache: dict, mode: str) -> str | None:
    """
    Fuzzy-match a team name query against cached team names for a given mode.
    Returns the best matching team name, or None if no reasonable match found.
    """
    from difflib import get_close_matches
    teams = list(draft_cache.get(mode, {}).keys())
    if not teams:
        return None
    matches = get_close_matches(query, teams, n=1, cutoff=0.4)
    # Also try case-insensitive substring match as fallback
    if not matches:
        query_lower = query.lower()
        for team in teams:
            if query_lower in team.lower():
                return team
    return matches[0] if matches else None


class TradeEval(commands.Cog):
    """Evaluates NFL draft pick trades using Johnson and Hill chart systems."""

    def __init__(self, bot):
        self.bot = bot
        self.db = Database(config.DB_PATH)
        self.cache_mgr = CacheManager(self.db, bot)
        # Populated in cog_load once DB is connected:
        # {mode: {team_name: [picks]}}  e.g. {"mock": {"Minnesota Vikings": [18, 50, ...]}}
        self.draft_cache: dict = {}

    async def cog_load(self):
        """Async setup: open DB connection, load cache, start sync task."""
        await self.db.connect()
        self.draft_cache["mock"] = await self.cache_mgr.load_to_memory("mock")
        self.draft_cache["nfl"] = await self.cache_mgr.load_to_memory("nfl")  # populated via !trade.picks.set
        self.draft_cache["locks"] = await self.cache_mgr.load_to_memory("locks")  # populated via auto-sync
        self.auto_sync_task.start()
        logger.info("TradeEval cog loaded — auto sync task started")

    def cog_unload(self):
        """Teardown: cancel sync task and close DB connection."""
        self.auto_sync_task.cancel()
        asyncio.create_task(self.db.close())

    # -- Auto sync task -----------------------------------------------------

    @tasks.loop(hours=1)
    async def auto_sync_task(self):
        """
        Runs every hour and fires syncs at 3 AM Eastern.

        Mock (Feb-Mar): daily sync from Google Sheets.
        Locks (Sep-Jan): nightly recompute from NFL Locks week file data.

        Checks is_stale() before syncing so a recently-manual-synced cache
        won't be unnecessarily re-fetched. After each sync the in-memory
        draft_cache is refreshed from the DB.
        """
        now = now_eastern()

        # Only act at 3 AM to keep sync times predictable
        if now.hour != 3:
            return

        # Mock offseason: daily during February and March
        if now.month in [2, 3]:
            last_mock = await self.db.get_last_sync("mock")
            if self.cache_mgr.is_stale(last_mock, threshold_hours=20):
                logger.info("Auto sync: starting mock draft sync")
                await self.cache_mgr.sync_mock_from_sheet(
                    config.PICK_SHEET_URL, DEFAULT_ROUND_GIDS
                )
                self.draft_cache["mock"] = await self.cache_mgr.load_to_memory("mock")

        # Locks-derived order: nightly during NFL season (Sep–Jan) and post-season wrap-up
        if now.month in (9, 10, 11, 12, 1):
            last_locks = await self.db.get_last_sync("locks")
            if self.cache_mgr.is_stale(last_locks, threshold_hours=20):
                logger.info("Auto sync: recomputing locks draft order from NFL Locks data")
                await self._sync_locks_order()
                self.draft_cache["locks"] = await self.cache_mgr.load_to_memory("locks")

    @auto_sync_task.before_loop
    async def before_auto_sync(self):
        """Wait until the bot is fully connected before the task loop begins."""
        await self.bot.wait_until_ready()

    async def _sync_locks_order(self) -> bool:
        """
        Recompute projected draft order from NFL Locks game result data and
        save to the 'locks' mode in the DB.

        Returns True on success, False if an exception occurred.
        Pure local computation — reads NFL Locks week files, no HTTP requests.
        """
        try:
            picks_by_team = locks_order.project_draft_order()
            if not picks_by_team:
                logger.warning("_sync_locks_order: project_draft_order returned empty dict")
                await self.db.update_sync_status("locks", "failed", error="No data computed")
                return False
            for team, picks in picks_by_team.items():
                await self.db.save_team_picks("locks", team, picks)
            await self.db.update_sync_status("locks", "success")
            logger.info("_sync_locks_order: saved projected picks for %d teams", len(picks_by_team))
            return True
        except Exception as e:
            logger.error("_sync_locks_order failed: %s", e, exc_info=True)
            await self.db.update_sync_status("locks", "failed", error=str(e))
            return False

    # -- Trade evaluation command -------------------------------------------

    @commands.command(name="trade")
    async def trade_eval(self, ctx, *, args: str = None):
        """
        Evaluate an NFL draft pick trade.

        Usage: !trade <side A picks> for <side B picks>

        Pick formats:
          31       -- overall pick number (current year)
          +1R2     -- 1 year from now, Round 2 (year-agnostic relative format)
          +3R1     -- 3 years from now, Round 1 (max +3)
          27R2     -- 2027 Round 2 (absolute year format, must be after current year)

        Examples:
          !trade 31 178 for 41 186 +1R2
          !trade 13 for +1R1 +2R2
        """
        if not args:
            await ctx.send(
                "**Usage:** `!trade <Side A picks> for <Side B picks>`\n"
                "**Examples:** `!trade 31 178 for 41 186 +1R2`\n"
                "**Future picks (relative):** `+1R2` = 1 yr from now R2, `+2R1`, `+3R4` (max +3)\n"
                "**Future picks (by year):** `27R2` = 2027 Round 2"
            )
            return

        # Determine separator: newline (multi-line input) or "for"
        normalized = args.lower()
        if "\n" in normalized:
            raw_parts = normalized.split("\n", 1)
        elif " for " in normalized:
            raw_parts = normalized.split(" for ", 1)
        else:
            await ctx.send(
                "Use `for` to separate sides, or put each side on its own line.\n"
                "**Examples:**\n"
                "`!trade 59 for 82 and 97`\n"
                "`!trade We get 59` (next line) `They get 82 and 97`"
            )
            return

        # Extract only pick-shaped tokens from each side — ignore all other words
        tokens_a = _extract_pick_tokens(raw_parts[0])
        tokens_b = _extract_pick_tokens(raw_parts[1])

        if not tokens_a or not tokens_b:
            await ctx.send("Both sides of the trade must have at least one pick.")
            return

        picks_a = _parse_side(tokens_a)
        picks_b = _parse_side(tokens_b)

        # Report parsing errors
        bad = []
        if picks_a is None:
            for t in tokens_a:
                if trade_logic.parse_pick(t) is None:
                    bad.append(t)
        if picks_b is None:
            for t in tokens_b:
                if trade_logic.parse_pick(t) is None:
                    bad.append(t)

        if picks_a is None or picks_b is None:
            bad_str = ", ".join(f"`{b}`" for b in bad)
            next_yy = str(config.CURRENT_DRAFT_YEAR + 1)[2:]
            await ctx.send(
                f"Could not parse pick(s): {bad_str}\n"
                f"**Pick formats:**\n"
                f"• Current year: overall pick number (e.g. `31`)\n"
                f"• Future (relative): `+1R2` = 1 year from now, Round 2  |  `+2R1`, `+3R4` etc.\n"
                f"• Future (by year): `{next_yy}R2` = {config.CURRENT_DRAFT_YEAR + 1} Round 2 "
                f"— only works for years **after** {config.CURRENT_DRAFT_YEAR}"
            )
            return

        # Calculate totals
        j_a = sum(p["johnson"] for p in picks_a)
        h_a = sum(p["hill"] for p in picks_a)
        f_a = sum(p["fitz_spiel"] for p in picks_a)
        s_a = sum(p["stuart"] for p in picks_a)
        j_b = sum(p["johnson"] for p in picks_b)
        h_b = sum(p["hill"] for p in picks_b)
        f_b = sum(p["fitz_spiel"] for p in picks_b)
        s_b = sum(p["stuart"] for p in picks_b)

        # Determine winners per chart.
        # Each column shows what that team SENDS. The team sending LESS wins
        # (they receive more than they give up), so the lower total = winner.
        j_diff = j_a - j_b
        j_winner = "b" if j_diff > 0 else ("a" if j_diff < 0 else "tie")

        h_diff = h_a - h_b
        h_winner = "b" if h_diff > 0 else ("a" if h_diff < 0 else "tie")

        f_diff = f_a - f_b
        f_winner = "b" if f_diff > 0 else ("a" if f_diff < 0 else "tie")

        s_diff = s_a - s_b
        s_winner = "b" if s_diff > 0 else ("a" if s_diff < 0 else "tie")

        # Embed color: green = balanced/fair, orange = lopsided
        j_max = max(j_a, j_b)
        close = j_max > 0 and (abs(j_diff) / j_max) < CLOSE_THRESHOLD_PCT
        if close or j_winner == "tie":
            embed_color = discord.Color.green()
        else:
            embed_color = discord.Color.orange()

        side_a_text = _format_side(picks_a, "SIDE A")
        side_b_text = _format_side(picks_b, "SIDE B")

        # Clean diffs for display — Johnson/Fitz-Spiel are integer charts but
        # Python float arithmetic can produce values like 54.2999999955; round them.
        j_d = round(j_diff)
        h_d = round(h_diff, 2)
        f_d = round(f_diff)
        s_d = round(s_diff, 2)

        # Advantage lines.
        # j_d is signed (j_a - j_b); use abs() for display in all cases.
        if j_winner == "tie":
            j_adv = "Johnson: Even"
        elif j_winner == "a":
            j_adv = f"Johnson: {trade_logic.johnson_equivalent(abs(j_d))} to Team A (+{abs(j_d)})"
        else:
            j_adv = f"Johnson: {trade_logic.johnson_equivalent(abs(j_d))} to Team B (+{abs(j_d)})"

        if h_winner == "tie":
            h_adv = "Hill: Even"
        elif h_winner == "a":
            h_adv = f"Hill: {trade_logic.hill_equivalent(abs(h_d))} to Team A (+{int(abs(h_d))})"
        else:
            h_adv = f"Hill: {trade_logic.hill_equivalent(abs(h_d))} to Team B (+{int(abs(h_d))})"

        if f_winner == "tie":
            f_adv = "Fitz-Spiel: Even"
        elif f_winner == "a":
            f_adv = f"Fitz-Spiel: {trade_logic.fitz_spiel_equivalent(abs(f_d))} to Team A (+{abs(f_d)})"
        else:
            f_adv = f"Fitz-Spiel: {trade_logic.fitz_spiel_equivalent(abs(f_d))} to Team B (+{abs(f_d)})"

        if s_winner == "tie":
            s_adv = "Stuart: Even"
        elif s_winner == "a":
            s_adv = f"Stuart: {trade_logic.stuart_equivalent(abs(s_d))} to Team A (+{abs(s_d):.1f})"
        else:
            s_adv = f"Stuart: {trade_logic.stuart_equivalent(abs(s_d))} to Team B (+{abs(s_d):.1f})"

        # -- Try image first, fall back to embed if Pillow missing/fails ------
        # Offloaded to a thread executor so Pillow's CPU work doesn't block
        # the asyncio event loop while other users' commands are waiting.
        image_buf = await asyncio.get_event_loop().run_in_executor(
            None,
            trade_image.render,
            picks_a, picks_b,
            j_a, h_a, f_a, s_a,
            j_b, h_b, f_b, s_b,
            j_winner, h_winner, f_winner, s_winner,
            j_adv, h_adv, f_adv, s_adv,
            close,
        )

        if image_buf:
            try:
                await ctx.send(file=discord.File(image_buf, filename="trade.png"))
                return
            except discord.Forbidden:
                logger.warning(
                    "Missing 'Attach Files' permission in #%s — falling back to embed. "
                    "Grant the bot Attach Files permission in that channel to enable image output.",
                    ctx.channel.name,
                )
            except Exception:
                logger.warning("Failed to send trade image", exc_info=True)

        # Fallback: text embed (Pillow not installed, render error, or missing permissions)
        embed = discord.Embed(title="NFL Trade Evaluator", color=embed_color)
        embed.add_field(name="​", value=f"```\n{side_a_text}\n```", inline=False)
        embed.add_field(name="​", value=f"```\n{side_b_text}\n```", inline=False)
        embed.add_field(
            name="Chart Advantage",
            value=f"{j_adv}\n{h_adv}\n{f_adv}\n{s_adv}",
            inline=False
        )
        embed.set_footer(text="Future picks: one round penalty per year (2027 R1 = 2026 R2 value, etc.)")
        await ctx.send(embed=embed)

    # -- Trade finder helpers -----------------------------------------------

    async def _find_trade_helper(self, ctx, args: str, direction: str, chart: str = None, best_mode: bool = False):
        """
        Helper for trade finder commands.

        Supports position syntax: "33 + early 3rd"
        """
        if not args:
            if direction == "down":
                example1 = f"!find.trade.down{'.'+chart if chart else ''} 33 for 98"
                example2 = f"!find.trade.down{'.'+chart if chart else ''} 33 + early 3rd"
            else:
                example1 = f"!find.trade.up{'.'+chart if chart else ''} 10 with 33"
                example2 = f"!find.trade.up{'.'+chart if chart else ''} 10 with late 2nd"

            await ctx.send(
                f"**Exact pick syntax:** `{example1}`\n"
                f"**Position syntax:** `{example2}`\n"
                "**Positions:** early, middle, late\n"
                "**Rounds:** 1-7"
            )
            return

        position_match = re.match(r'(\d+)\s*\+\s*(early|middle|late)\s+(\d)(?:st|nd|rd|th)?', args.lower())
        if position_match:
            await self._find_trade_position_helper(ctx, args, direction, chart, best_mode, position_match)
            return

        args_lower = args.lower()
        if " for " in args_lower:
            separator = " for "
        elif " with " in args_lower:
            separator = " with "
        else:
            await ctx.send("Use `for` or `with` to separate sides, or use position syntax like `33 + early 3rd`")
            return

        await self._find_trade_exact_helper(ctx, args, direction, chart, best_mode, separator)

    async def _find_trade_position_helper(self, ctx, args: str, direction: str, chart: str, best_mode: bool, match):
        """Handle position-based trade finding."""
        start_pick = int(match.group(1))
        position = match.group(2)
        round_num = int(match.group(3))

        if not (1 <= start_pick <= 257):
            await ctx.send("Pick must be between 1 and 257")
            return

        if not (1 <= round_num <= 7):
            await ctx.send("Round must be between 1 and 7")
            return

        tolerance = 0.02 if best_mode else 0.05
        max_picks = 5 if best_mode else 3

        charts_to_run = [chart] if chart else ["johnson", "hill", "fitz_spiel", "stuart"]

        results = []
        for ch in charts_to_run:
            gain_pick = trade_logic._get_position_pick(position, round_num, ch)
            if gain_pick is None:
                continue

            if direction == "down":
                if gain_pick <= start_pick:
                    await ctx.send(f"Cannot trade down from {start_pick} to get pick {gain_pick} (that's a trade up!)")
                    return
                result = trade_logic.find_trade_down(start_pick, gain_pick, ch, tolerance, max_picks, years_out=0)
            else:
                if gain_pick >= start_pick:
                    await ctx.send(f"Cannot trade up from {start_pick} to get pick {gain_pick} (that's a trade down!)")
                    return
                result = trade_logic.find_trade_up(gain_pick, start_pick, ch, tolerance, max_picks, years_out=0)

            results.append((ch, gain_pick, result))

        if not results:
            await ctx.send("No charts available")
            return

        def ord_suffix(n):
            return "st" if n == 1 else "nd" if n == 2 else "rd" if n == 3 else "th"

        if direction == "down":
            title = f"Trade Down from Pick {start_pick} + {position.title()} {round_num}{ord_suffix(round_num)}"
            give_label = f"Give: Pick {start_pick}"
        else:
            title = f"Trade Up to Pick {start_pick} + {position.title()} {round_num}{ord_suffix(round_num)}"
            give_label = f"Get: Pick {start_pick}"

        mode_label = f"({'Best' if best_mode else 'Standard'}: {int(tolerance*100)}% tolerance, max {max_picks} picks)"

        embed = discord.Embed(title=title, description=mode_label, color=discord.Color.blue())

        for ch, gain_pick, result in results:
            chart_name = ch.replace("_", "-").title()

            if result is None:
                value = f"No solution found\n({position} {round_num}{ord_suffix(round_num)} = pick {gain_pick} on {chart_name})"
            else:
                if direction == "down":
                    picks_str = " + ".join(str(p) for p in result['picks'])
                    value = f"**Get:** {picks_str}\n"
                else:
                    picks_str = " + ".join(str(p) for p in result['picks'])
                    value = f"**Give:** {picks_str}\n"

                value += f"Balance: {result['delta']:+.2f} ({result['pct_diff']*100:.2f}%)\n"
                value += f"({position} {round_num}{ord_suffix(round_num)} = pick {gain_pick} on {chart_name})"

            embed.add_field(name=f"Chart: {chart_name}", value=value, inline=False)

        embed.set_footer(text=give_label)
        await ctx.send(embed=embed)

    async def _find_trade_exact_helper(self, ctx, args: str, direction: str, chart: str, best_mode: bool, separator: str):
        """
        Handle exact-pick trade finding.

        Computes the value gap between both sides using all specified picks,
        then searches for additional picks to close that gap.

        Trade down — exactly one give pick; one or more anchor get picks:
          !find.trade.down 33 for 98       → give 33, already getting 98, find more
          !find.trade.down 33 for 98 114   → give 33, already getting 98+114, balanced?

        Trade up — one or more anchor give picks; exactly one get pick:
          !find.trade.up 10 with 33        → want 10, giving 33, find more to offer
          !find.trade.up 10 with 33 50     → want 10, giving 33+50, balanced?
        """
        parts = args.lower().split(separator, 1)
        if len(parts) != 2:
            await ctx.send(f"Format: `<picks> {separator} <picks>`")
            return

        give_tokens = _extract_pick_tokens(parts[0])
        get_tokens  = _extract_pick_tokens(parts[1])

        if not give_tokens or not get_tokens:
            await ctx.send("Both sides must have at least one pick.")
            return

        give_picks = []
        get_picks  = []

        for token in give_tokens:
            parsed = trade_logic.parse_pick(token)
            if parsed is None:
                await ctx.send(
                    f"Could not parse pick: `{token}`\n"
                    f"Current draft year is **{config.CURRENT_DRAFT_YEAR}** — future format (e.g. `27R2`) only works for picks *after* this year. "
                    f"For current-year picks use the overall pick number (e.g. `31`), "
                    f"or for future rounds use next year and beyond (e.g. `{str(config.CURRENT_DRAFT_YEAR + 1)[2:]}R2`)."
                )
                return
            give_picks.append(parsed)   # (overall, years_out, label)

        for token in get_tokens:
            parsed = trade_logic.parse_pick(token)
            if parsed is None:
                await ctx.send(
                    f"Could not parse pick: `{token}`\n"
                    f"Current draft year is **{config.CURRENT_DRAFT_YEAR}** — future format (e.g. `27R2`) only works for picks *after* this year. "
                    f"For current-year picks use the overall pick number (e.g. `31`), "
                    f"or for future rounds use next year and beyond (e.g. `{str(config.CURRENT_DRAFT_YEAR + 1)[2:]}R2`)."
                )
                return
            get_picks.append(parsed)

        if direction == "down" and len(give_picks) != 1:
            await ctx.send("Trade down: specify exactly one pick to give — e.g., `!find.trade.down 33 for 98`")
            return
        if direction == "up" and len(get_picks) != 1:
            await ctx.send("Trade up: specify exactly one pick to receive — e.g., `!find.trade.up 10 with 33`")
            return

        tolerance  = 0.02 if best_mode else 0.05
        max_picks  = 5    if best_mode else 3
        charts_to_run = [chart] if chart else ["johnson", "hill", "fitz_spiel", "stuart"]

        # The "anchor" pick determines what counts as worse (higher-numbered) for search
        if direction == "down":
            anchor_pick = give_picks[0][0]      # overall pick number you're trading away
            already_locked = {p[0] for p in get_picks}
        else:
            anchor_pick = get_picks[0][0]       # overall pick number you want to acquire
            already_locked = {p[0] for p in give_picks}

        # Search pool: picks worse than anchor, not already specified on either side
        all_locked = {p[0] for p in give_picks} | {p[0] for p in get_picks}
        search_pool = [p for p in range(anchor_pick + 1, 258) if p not in all_locked]

        # Max additional picks = budget minus what's already on the "add" side
        existing_on_add_side = len(get_picks) if direction == "down" else len(give_picks)
        max_additional = max(1, max_picks - existing_on_add_side)

        # Per-chart results
        chart_results = []
        for ch in charts_to_run:
            give_total = sum(trade_logic.get_pick_values(p[0], p[1])[ch] for p in give_picks)
            get_total  = sum(trade_logic.get_pick_values(p[0], p[1])[ch] for p in get_picks)

            status, result = trade_logic.find_trade_balance(
                locked_give_value=give_total,
                locked_get_value=get_total,
                chart=ch,
                direction=direction,
                tolerance_pct=tolerance,
                max_additional=max_additional,
                search_pool=search_pool,
            )
            chart_results.append((ch, give_total, get_total, status, result))

        # -- Build embed --------------------------------------------------------
        give_label = " + ".join(p[2] for p in give_picks)
        get_label  = " + ".join(p[2] for p in get_picks)

        # Check whether any chart found something to add (affects title wording)
        any_needs_more = any(s in ("found", "not_found") for _, _, _, s, _ in chart_results)
        if direction == "down":
            title = f"Trade Down: Give {give_label} → Receive {get_label}" + (" + ?" if any_needs_more else "")
        else:
            title = f"Trade Up: Receive {get_label} → Give {give_label}" + (" + ?" if any_needs_more else "")

        mode_label = f"({'Best' if best_mode else 'Standard'}: {int(tolerance*100)}% tolerance, max {max_picks} total picks)"
        embed = discord.Embed(title=title, description=mode_label, color=discord.Color.blue())

        for ch, give_total, get_total, status, result in chart_results:
            chart_name = ch.replace("_", "-").title()

            if status == "balanced":
                delta = result["delta"]
                pct   = result["pct_diff"] * 100
                sign  = "+" if delta >= 0 else ""
                if direction == "down":
                    value = f"✅ Already balanced\nReceive side: {sign}{delta:+.1f} ({pct:.1f}%)"
                else:
                    value = f"✅ Already balanced\nGive side: {sign}{delta:+.1f} ({pct:.1f}%)"

            elif status == "overpaid":
                delta = result["delta"]
                pct   = result["pct_diff"] * 100
                if direction == "down":
                    value = f"⚠️ Receiving {abs(pct):.1f}% too much value\n(by {abs(delta):.1f} pts on {chart_name})"
                else:
                    value = f"⚠️ Giving {abs(pct):.1f}% too much value\n(by {abs(delta):.1f} pts on {chart_name})"

            elif status == "found":
                additional = " + ".join(str(p) for p in result["picks"])
                delta = result["delta"]
                pct   = result["pct_diff"] * 100
                if direction == "down":
                    value = (
                        f"**Also get:** {additional}\n"
                        f"Balance: {delta:+.1f} ({pct:.1f}%)"
                    )
                else:
                    value = (
                        f"**Also give:** {additional}\n"
                        f"Balance: {delta:+.1f} ({pct:.1f}%)"
                    )

            else:  # not_found
                gap = give_total - get_total if direction == "down" else get_total - give_total
                value = (
                    f"No single solution within {int(tolerance*100)}%\n"
                    f"Remaining gap: {gap:.1f} pts — try position syntax or adjust picks"
                )

            embed.add_field(name=f"Chart: {chart_name}", value=value, inline=False)

        embed.set_footer(text=f"Give: {give_label}  |  Receive: {get_label}")
        await ctx.send(embed=embed)

    # -- Standard trade down commands ---------------------------------------

    @commands.command(name="find.trade.down")
    async def find_trade_down_all(self, ctx, *, args: str = None):
        """Find trade down scenarios on all charts."""
        await self._find_trade_helper(ctx, args, "down", chart=None, best_mode=False)

    @commands.command(name="find.trade.down.johnson")
    async def find_trade_down_johnson(self, ctx, *, args: str = None):
        """Find trade down scenarios on Johnson chart."""
        await self._find_trade_helper(ctx, args, "down", chart="johnson", best_mode=False)

    @commands.command(name="find.trade.down.hill")
    async def find_trade_down_hill(self, ctx, *, args: str = None):
        """Find trade down scenarios on Hill chart."""
        await self._find_trade_helper(ctx, args, "down", chart="hill", best_mode=False)

    @commands.command(name="find.trade.down.fitz")
    async def find_trade_down_fitz(self, ctx, *, args: str = None):
        """Find trade down scenarios on Fitz-Spiel chart."""
        await self._find_trade_helper(ctx, args, "down", chart="fitz_spiel", best_mode=False)

    @commands.command(name="find.trade.down.stuart")
    async def find_trade_down_stuart(self, ctx, *, args: str = None):
        """Find trade down scenarios on Stuart chart."""
        await self._find_trade_helper(ctx, args, "down", chart="stuart", best_mode=False)

    # -- Best mode trade down commands --------------------------------------

    @commands.command(name="find.trade.down.best.johnson")
    async def find_trade_down_best_johnson(self, ctx, *, args: str = None):
        """Find best trade down scenarios on Johnson chart (2% tolerance, 5 picks)."""
        await self._find_trade_helper(ctx, args, "down", chart="johnson", best_mode=True)

    @commands.command(name="find.trade.down.best.hill")
    async def find_trade_down_best_hill(self, ctx, *, args: str = None):
        """Find best trade down scenarios on Hill chart (2% tolerance, 5 picks)."""
        await self._find_trade_helper(ctx, args, "down", chart="hill", best_mode=True)

    @commands.command(name="find.trade.down.best.fitz")
    async def find_trade_down_best_fitz(self, ctx, *, args: str = None):
        """Find best trade down scenarios on Fitz-Spiel chart (2% tolerance, 5 picks)."""
        await self._find_trade_helper(ctx, args, "down", chart="fitz_spiel", best_mode=True)

    @commands.command(name="find.trade.down.best.stuart")
    async def find_trade_down_best_stuart(self, ctx, *, args: str = None):
        """Find best trade down scenarios on Stuart chart (2% tolerance, 5 picks)."""
        await self._find_trade_helper(ctx, args, "down", chart="stuart", best_mode=True)

    # -- Standard trade up commands -----------------------------------------

    @commands.command(name="find.trade.up")
    async def find_trade_up_all(self, ctx, *, args: str = None):
        """Find trade up scenarios on all charts."""
        await self._find_trade_helper(ctx, args, "up", chart=None, best_mode=False)

    @commands.command(name="find.trade.up.johnson")
    async def find_trade_up_johnson(self, ctx, *, args: str = None):
        """Find trade up scenarios on Johnson chart."""
        await self._find_trade_helper(ctx, args, "up", chart="johnson", best_mode=False)

    @commands.command(name="find.trade.up.hill")
    async def find_trade_up_hill(self, ctx, *, args: str = None):
        """Find trade up scenarios on Hill chart."""
        await self._find_trade_helper(ctx, args, "up", chart="hill", best_mode=False)

    @commands.command(name="find.trade.up.fitz")
    async def find_trade_up_fitz(self, ctx, *, args: str = None):
        """Find trade up scenarios on Fitz-Spiel chart."""
        await self._find_trade_helper(ctx, args, "up", chart="fitz_spiel", best_mode=False)

    @commands.command(name="find.trade.up.stuart")
    async def find_trade_up_stuart(self, ctx, *, args: str = None):
        """Find trade up scenarios on Stuart chart."""
        await self._find_trade_helper(ctx, args, "up", chart="stuart", best_mode=False)

    # -- Best mode trade up commands ----------------------------------------

    @commands.command(name="find.trade.up.best.johnson")
    async def find_trade_up_best_johnson(self, ctx, *, args: str = None):
        """Find best trade up scenarios on Johnson chart (2% tolerance, 5 picks)."""
        await self._find_trade_helper(ctx, args, "up", chart="johnson", best_mode=True)

    @commands.command(name="find.trade.up.best.hill")
    async def find_trade_up_best_hill(self, ctx, *, args: str = None):
        """Find best trade up scenarios on Hill chart (2% tolerance, 5 picks)."""
        await self._find_trade_helper(ctx, args, "up", chart="hill", best_mode=True)

    @commands.command(name="find.trade.up.best.fitz")
    async def find_trade_up_best_fitz(self, ctx, *, args: str = None):
        """Find best trade up scenarios on Fitz-Spiel chart (2% tolerance, 5 picks)."""
        await self._find_trade_helper(ctx, args, "up", chart="fitz_spiel", best_mode=True)

    @commands.command(name="find.trade.up.best.stuart")
    async def find_trade_up_best_stuart(self, ctx, *, args: str = None):
        """Find best trade up scenarios on Stuart chart (2% tolerance, 5 picks)."""
        await self._find_trade_helper(ctx, args, "up", chart="stuart", best_mode=True)

    # -- Admin commands -----------------------------------------------------

    @commands.command(name="trade.year.update")
    @commands.is_owner()
    async def update_draft_year(self, ctx, year: int = None):
        """
        Update the current draft year in config.

        Usage: !trade.year.update 2027

        After updating, use !reload to apply changes.
        """
        if year is None:
            await ctx.send(f"Current draft year: **{config.CURRENT_DRAFT_YEAR}**\nUsage: `!trade.year.update <year>`")
            return

        if year < 2020 or year > 2040:
            await ctx.send("Year must be between 2020 and 2040")
            return

        config_path = Path(__file__).parent / "config.py"

        try:
            with open(config_path, 'r') as f:
                content = f.read()

            new_content = re.sub(
                r'CURRENT_DRAFT_YEAR\s*=\s*\d+',
                f'CURRENT_DRAFT_YEAR = {year}',
                content
            )

            with open(config_path, 'w') as f:
                f.write(new_content)

            await ctx.send(
                f"Updated draft year to **{year}**\n"
                f"Use `!reload` to apply changes."
            )

        except Exception as e:
            await ctx.send(f"Error updating config: {e}")

    # -- User setup commands ------------------------------------------------

    @commands.command(name="trade.mode")
    async def trade_mode(self, ctx, mode: str = None):
        """
        Set or show your draft mode.

        Usage:
          !trade.mode mock   -- use mock offseason picks (Google Sheets)
          !trade.mode nfl    -- use manually-entered NFL draft order
          !trade.mode show   -- show your current mode and team
        """
        user_ctx = await self.db.get_user_context(ctx.author.id)

        if mode is None or mode.lower() == "show":
            if not user_ctx:
                await ctx.send("No profile set up yet. Use `!trade.mode mock` or `!trade.mode nfl` to get started.")
                return
            team = user_ctx["my_team"] or "not set"
            await ctx.send(f"Mode: **{user_ctx['mode']}** | Team: **{team}**")
            return

        mode = mode.lower()
        if mode not in ("mock", "nfl"):
            await ctx.send("Valid modes: `mock` or `nfl`")
            return

        current_team = user_ctx["my_team"] if user_ctx else None
        await self.db.save_user_context(ctx.author.id, mode, current_team)
        await ctx.send(f"Mode set to **{mode}**.")

    @commands.command(name="trade.picks.load")
    async def trade_picks_load(self, ctx, target: str = None, *, team_name: str = None):
        """
        Set your team.

        Usage: !trade.picks.load mine <team name>
        Example: !trade.picks.load mine Vikings
        """
        if target is None or target.lower() != "mine" or not team_name:
            await ctx.send("Usage: `!trade.picks.load mine <team name>`\nExample: `!trade.picks.load mine Vikings`")
            return

        user_ctx = await self.db.get_user_context(ctx.author.id)
        mode = user_ctx["mode"] if user_ctx else "mock"

        matched = _resolve_team(team_name, self.draft_cache, mode)
        if not matched:
            teams = list(self.draft_cache.get(mode, {}).keys())
            sample = ", ".join(teams[:6]) + ("..." if len(teams) > 6 else "")
            await ctx.send(
                f"Team `{team_name}` not found in **{mode}** cache.\n"
                f"Examples: {sample}\n"
                f"Run `!trade.sync {mode}` if the cache is empty."
            )
            return

        await self.db.save_user_context(ctx.author.id, mode, matched)
        picks = self.draft_cache[mode].get(matched, [])
        await ctx.send(f"Team set to **{matched}** ({len(picks)} picks in **{mode}** mode).")

    @commands.command(name="trade.picks.show")
    async def trade_picks_show(self, ctx):
        """Show your current team, mode, and their draft picks."""
        user_ctx = await self.db.get_user_context(ctx.author.id)
        if not user_ctx or not user_ctx.get("my_team"):
            await ctx.send("No team set. Use `!trade.picks.load mine <team>` first.")
            return

        mode = user_ctx["mode"]
        team = user_ctx["my_team"]
        picks = self.draft_cache.get(mode, {}).get(team)

        if not picks:
            await ctx.send(f"No picks cached for **{team}** in **{mode}** mode. Try `!trade.sync {mode}`.")
            return

        picks_str = "  ".join(str(p) for p in sorted(picks))
        embed = discord.Embed(
            title=f"{team}",
            description=f"Mode: **{mode}** | {len(picks)} picks",
            color=discord.Color.blue()
        )
        embed.add_field(name="Draft Picks", value=f"```{picks_str}```", inline=False)
        await ctx.send(embed=embed)

    # -- Cache management commands (admin only) -----------------------------

    @commands.command(name="trade.sync")
    @commands.is_owner()
    async def trade_sync(self, ctx, target: str = "all"):
        """
        Force a cache refresh from the source.

        Usage:
          !trade.sync mock   -- refresh Google Sheets picks
          !trade.sync locks  -- recompute projected order from NFL Locks data
          !trade.sync all    -- refresh mock + locks
        """
        target = target.lower()
        if target not in ("mock", "locks", "all"):
            await ctx.send("Usage: `!trade.sync [mock|locks|all]`")
            return

        await ctx.send(f"Syncing **{target}**...")

        if target in ("mock", "all"):
            ok = await self.cache_mgr.sync_mock_from_sheet(config.PICK_SHEET_URL, DEFAULT_ROUND_GIDS)
            if ok:
                self.draft_cache["mock"] = await self.cache_mgr.load_to_memory("mock")
                await ctx.send(f"Mock sync complete — {len(self.draft_cache['mock'])} teams loaded.")
            else:
                await ctx.send("Mock sync failed. Check logs.")

        if target in ("locks", "all"):
            ok = await self._sync_locks_order()
            if ok:
                self.draft_cache["locks"] = await self.cache_mgr.load_to_memory("locks")
                lines = locks_order.record_summary()
                team_count = len(self.draft_cache["locks"])
                # Show first 20 lines (non-playoff teams); playoff teams summarized
                non_playoff_lines = [ln for ln in lines if "[NP]" in ln]
                playoff_lines     = [ln for ln in lines if "[PL]" in ln]
                preview = non_playoff_lines[:20]
                if playoff_lines:
                    preview.append(f"--- {len(playoff_lines)} playoff teams (picks {len(non_playoff_lines)+1}–{team_count}) ---")
                    preview.extend(playoff_lines[:4])
                    if len(playoff_lines) > 4:
                        preview.append(f"... +{len(playoff_lines)-4} more playoff teams")
                summary = "\n".join(preview)
                await ctx.send(
                    f"Locks sync complete — {team_count} teams projected.\n"
                    f"`[NP]` = non-playoff  `[PL]` = playoff (estimated by seeding)\n"
                    f"```\n{summary}\n```"
                )
            else:
                await ctx.send("Locks sync failed. Check logs — NFL Locks week files may not be populated yet.")

    @commands.command(name="trade.cache.status")
    @commands.is_owner()
    async def trade_cache_status(self, ctx):
        """Show cache health: last sync times, staleness, and consecutive failure counts."""
        from BotUtils.time import EASTERN

        embed = discord.Embed(title="Trade Cache Status", color=discord.Color.blue())

        for mode in ("mock", "nfl", "locks"):
            last_sync = await self.db.get_last_sync(mode)
            consecutive = await self.db.get_consecutive_failures(mode)
            team_count = len(self.draft_cache.get(mode, {}))
            threshold = 20  # mock and locks both use 20h; nfl has no auto-sync

            if last_sync is None:
                sync_str = "Never synced"
                stale_str = "No data"
            else:
                sync_local = last_sync.astimezone(EASTERN)
                sync_str = sync_local.strftime("%Y-%m-%d %I:%M %p ET")
                stale_str = "Stale" if self.cache_mgr.is_stale(last_sync, threshold) else "Fresh"

            failure_str = f"{consecutive} consecutive failure(s)" if consecutive > 0 else "No failures"

            embed.add_field(
                name=f"{mode.upper()} mode",
                value=(
                    f"Last sync: {sync_str}\n"
                    f"Status: {stale_str}\n"
                    f"Teams cached: {team_count}\n"
                    f"Failures: {failure_str}"
                ),
                inline=True
            )

        await ctx.send(embed=embed)

    @commands.command(name="trade.execute")
    @commands.is_owner()
    async def trade_execute(self, ctx, mode: str = None, *, trade_desc: str = None):
        """
        Manually update picks for a real-world trade, or reset a sync failure counter.

        Usage:
          !trade.execute mock reset   -- re-arm failure alerts after fixing a sheet issue
          !trade.execute nfl reset    -- re-arm failure alerts after manual NFL pick entry
          Full pick-swap logic (trading picks between teams in the cache) is a future update.
        """
        if mode is None:
            await ctx.send(
                "Usage: `!trade.execute <mode> reset` to clear failure alerts.\n"
                "Full trade execution syntax (swapping picks) coming in a later update."
            )
            return

        mode = mode.lower()
        if mode not in ("mock", "nfl"):
            await ctx.send("Mode must be `mock` or `nfl`.")
            return

        if trade_desc and trade_desc.strip().lower() == "reset":
            await self.db.reset_consecutive_failures(mode)
            await ctx.send(f"Consecutive failure counter reset for **{mode}** mode.")
            return

        await ctx.send(
            "Full trade execution (swapping specific picks) is coming in a later update.\n"
            "For now: `!trade.execute <mode> reset` to clear the failure alert."
        )

    @commands.command(name="trade.picks.set")
    @commands.is_owner()
    async def trade_picks_set(self, ctx, mode: str = None, *, args: str = None):
        """
        Manually set a team's picks. Replaces any existing picks for that team.
        Resets the consecutive sync failure counter for the mode.

        Usage: !trade.picks.set <mode> <team> <pick1> <pick2> ...
        Examples:
          !trade.picks.set nfl "Kansas City Chiefs" 29 61 93 125 157 189 221
          !trade.picks.set mock Vikings 18 50 82 114 146 178 210
        """
        if mode is None or args is None:
            await ctx.send(
                "Usage: `!trade.picks.set <mode> <team> <picks...>`\n"
                "Example: `!trade.picks.set nfl \"Kansas City Chiefs\" 29 61 93 125`"
            )
            return

        mode = mode.lower()
        if mode not in ("mock", "nfl"):
            await ctx.send("Mode must be `mock` or `nfl`.")
            return

        # Split args into team name and pick numbers.
        # Team name may be quoted ("Kansas City Chiefs") or unquoted (Vikings).
        import shlex
        try:
            parts = shlex.split(args)
        except ValueError:
            parts = args.split()

        if len(parts) < 2:
            await ctx.send("Need at least a team name and one pick number.")
            return

        # Find the split point: first part that looks like a pick number
        team_parts = []
        pick_parts = []
        for i, part in enumerate(parts):
            if part.isdigit() and 1 <= int(part) <= 257:
                pick_parts = parts[i:]
                break
            team_parts.append(part)

        if not team_parts or not pick_parts:
            await ctx.send(
                "Could not separate team name from pick numbers.\n"
                "Tip: quote multi-word team names — `\"Kansas City Chiefs\" 29 61 ...`"
            )
            return

        team_query = " ".join(team_parts)

        # Parse pick numbers
        picks = []
        bad = []
        for p in pick_parts:
            if p.isdigit() and 1 <= int(p) <= 257:
                picks.append(int(p))
            else:
                bad.append(p)

        if bad:
            await ctx.send(f"Invalid pick number(s): {', '.join(bad)} — must be 1–257.")
            return

        if not picks:
            await ctx.send("No valid pick numbers provided.")
            return

        # Fuzzy-match the team name against existing cache, or accept as-is if no cache yet
        matched = _resolve_team(team_query, self.draft_cache, mode)
        team_name = matched if matched else team_query

        # Save to DB and refresh in-memory cache
        await self.db.save_team_picks(mode, team_name, sorted(picks))
        await self.db.reset_consecutive_failures(mode)
        self.draft_cache[mode] = await self.cache_mgr.load_to_memory(mode)

        confirmed = f"**{team_name}**" + (f" (matched from `{team_query}`)" if matched and matched.lower() != team_query.lower() else "")
        await ctx.send(
            f"Set {len(picks)} picks for {confirmed} in **{mode}** mode: "
            f"`{' '.join(str(p) for p in sorted(picks))}`"
        )

    # -- Team-based trade finding -------------------------------------------

    @commands.command(name="find.trade.down.team")
    async def find_trade_down_team(self, ctx, start_pick: int = None, *, args: str = None):
        """
        Find trade-down options using a real team's picks.

        Usage: !find.trade.down.team <pick> for <team>
        Example: !find.trade.down.team 18 for Ravens

        Requires your team to be set with !trade.picks.load mine <team>.
        """
        if start_pick is None or not args:
            await ctx.send("Usage: `!find.trade.down.team <pick> for <team>`\nExample: `!find.trade.down.team 18 for Ravens`")
            return
        args_lower = args.lower()
        if args_lower.startswith("for "):
            team_query = args[4:].strip()
        elif args_lower.startswith("with "):
            team_query = args[5:].strip()
        else:
            await ctx.send("Usage: `!find.trade.down.team <pick> for <team>` or `... with <team>`")
            return
        await self._team_trade_helper(ctx, start_pick, team_query, "down")

    @commands.command(name="find.trade.up.team")
    async def find_trade_up_team(self, ctx, target_pick: int = None, *, args: str = None):
        """
        Find trade-up options offering your picks to a specific team.

        Usage: !find.trade.up.team <pick> with <team>
        Example: !find.trade.up.team 10 with Chargers

        Requires your team to be set with !trade.picks.load mine <team>.
        """
        if target_pick is None or not args:
            await ctx.send("Usage: `!find.trade.up.team <pick> with <team>`\nExample: `!find.trade.up.team 10 with Chargers`")
            return
        args_lower = args.lower()
        if args_lower.startswith("with "):
            team_query = args[5:].strip()
        elif args_lower.startswith("for "):
            team_query = args[4:].strip()
        else:
            await ctx.send("Usage: `!find.trade.up.team <pick> with <team>` or `... for <team>`")
            return
        await self._team_trade_helper(ctx, target_pick, team_query, "up")

    async def _team_trade_helper(self, ctx, pick: int, team_query: str, direction: str):
        """Shared logic for team-based trade finding commands."""
        user_ctx = await self.db.get_user_context(ctx.author.id)
        if not user_ctx or not user_ctx.get("my_team"):
            await ctx.send("Set your team first: `!trade.picks.load mine <team>`")
            return

        mode = user_ctx["mode"]
        my_team = user_ctx["my_team"]
        my_picks = self.draft_cache.get(mode, {}).get(my_team)
        if not my_picks:
            await ctx.send(f"No picks cached for **{my_team}** in **{mode}** mode. Try `!trade.sync {mode}`.")
            return

        partner_team = _resolve_team(team_query, self.draft_cache, mode)
        if not partner_team:
            await ctx.send(f"Team `{team_query}` not found in **{mode}** cache.")
            return

        partner_picks = self.draft_cache[mode].get(partner_team, [])
        if not partner_picks:
            await ctx.send(f"No picks cached for **{partner_team}**.")
            return

        charts = ["johnson", "hill", "fitz_spiel", "stuart"]
        best: dict = {}

        if direction == "down":
            for gain_pick in partner_picks:
                remaining = [p for p in partner_picks if p != gain_pick]
                for ch in charts:
                    result = trade_logic.find_trade_down(
                        pick, gain_pick, ch,
                        tolerance_pct=0.05, max_picks=3,
                        available_picks=remaining
                    )
                    if result and (ch not in best or result["pct_diff"] < best[ch][1]["pct_diff"]):
                        best[ch] = (gain_pick, result)
            title = f"Trade Down from Pick {pick} to {partner_team}"
            give_label = f"You give: Pick {pick}"
        else:
            for give_pick in my_picks:
                remaining = [p for p in my_picks if p != give_pick]
                for ch in charts:
                    result = trade_logic.find_trade_up(
                        pick, give_pick, ch,
                        tolerance_pct=0.05, max_picks=3,
                        available_picks=remaining
                    )
                    if result and (ch not in best or result["pct_diff"] < best[ch][1]["pct_diff"]):
                        best[ch] = (give_pick, result)
            title = f"Trade Up to Pick {pick} from {partner_team}"
            give_label = f"You receive: Pick {pick}"

        if not best:
            await ctx.send(f"No balanced trades found between **{my_team}** and **{partner_team}** for pick {pick}.")
            return

        embed = discord.Embed(title=title, color=discord.Color.blue())
        embed.description = f"{my_team} vs {partner_team} | Mode: **{mode}**"

        for ch in charts:
            if ch not in best:
                continue
            anchor_pick, result = best[ch]
            chart_name = ch.replace("_", "-").title()
            picks_str = " + ".join(str(p) for p in result["picks"])
            if direction == "down":
                value = f"**Receive:** {picks_str}\nBalance: {result['delta']:+.2f} ({result['pct_diff']*100:.1f}%)"
            else:
                value = f"**Give:** {picks_str}\nBalance: {result['delta']:+.2f} ({result['pct_diff']*100:.1f}%)"
            embed.add_field(name=f"Chart: {chart_name}", value=value, inline=False)

        embed.set_footer(text=give_label)
        await ctx.send(embed=embed)


async def setup(bot):
    # Force reload of helper modules when cog reloads
    # Note: database/cache_manager are reloaded for code changes but connections
    # are managed by cog_load/cog_unload, not the module reload
    importlib.reload(config)
    importlib.reload(trade_charts)
    importlib.reload(trade_logic)
    importlib.reload(database)
    importlib.reload(cache_manager)
    importlib.reload(trade_image)

    await bot.add_cog(TradeEval(bot))
