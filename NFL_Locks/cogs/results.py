from discord.ext import commands
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.schedule_utils import (
    get_max_week, get_current_season, find_current_week,
)
from NFL_Locks.utils.data_utils import load_full_schedule
from NFL_Locks.utils.constants import EASTERN
from NFL_Locks.utils.pick_format import format_picks_by_player, send_chunked
from datetime import datetime
from NFL_Locks.utils.command_names import CMD_SET_WINNERS, CMD_TALLY_SCORES, CMD_WEEKLY_RESULTS, CMD_SEASON_STANDINGS, CMD_CHECK_REACTIONS
from NFL_Locks.utils.command_utils import off_season_reply
from NFL_Locks.utils import scoring as scoring_mod

class Results(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # _compute_scores removed, all scoring logic lives in utils/scoring.py

    # -- Commands --------------------------------------------------------------

    @commands.command(name=CMD_SET_WINNERS)
    @commands.has_permissions(administrator=True)
    async def set_winners(self, ctx, week_number: int, *, winners_str: str):
        """Set the winning teams manually."""
        if not (1 <= week_number <= get_max_week()):
            await ctx.send(f"❌ Week must be between 1 and {get_max_week()}.")
            return

        winners = [w.upper() for w in winners_str.split()]
        season = get_current_season()
        db = get_db()
        await db.set_winners(season, week_number, winners)
        await ctx.send(f"✅ Set Week {week_number} winners: {', '.join(winners)}")

    @commands.command(name=CMD_TALLY_SCORES)
    @commands.has_permissions(administrator=True)
    async def tally_scores(self, ctx, week_number: int):
        """Calculate scores for THIS SERVER only (uses this server's scoring scheme)."""
        season = get_current_season()
        db = get_db()
        guild_id = str(ctx.guild.id)

        winners = await db.get_winners(season, week_number)
        if not winners:
            await ctx.send("❌ No winners set. Use `!set_winners` first.")
            return

        scheme = await db.get_scoring_scheme(guild_id)
        picks = await db.get_picks_for_week(season, week_number, guild_id)
        scores = scoring_mod.compute_week_scores(picks, set(winners), scheme)
        header = scoring_mod.results_header(scheme)

        if scores:
            sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            results = "\n".join(f"**{u}**: {pts} points" for u, pts in sorted_results)
            await ctx.send(f"**Week {week_number} {header} ({ctx.guild.name}):**\n{results}")
        else:
            await ctx.send("No scorers this week for this server.")

    @commands.command(name=CMD_WEEKLY_RESULTS)
    @commands.has_permissions(administrator=True)
    async def weekly_results(self, ctx, week_number: int):
        """Post weekly results for THIS SERVER."""
        if await off_season_reply(ctx):
            return
        season = get_current_season()
        db = get_db()
        guild_id = str(ctx.guild.id)

        winners = await db.get_winners(season, week_number)
        if not winners:
            await ctx.send(f"❌ No winners set for Week {week_number}.")
            return

        scheme = await db.get_scoring_scheme(guild_id)
        picks = await db.get_picks_for_week(season, week_number, guild_id)
        scores = scoring_mod.compute_week_scores(picks, set(winners), scheme)
        header = scoring_mod.results_header(scheme)

        await ctx.send(
            f"**Week {week_number} Results**\n**Winners:** {', '.join(winners)}"
        )

        if scores:
            sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            results = "\n".join(f"**{u}**: {pts} points" for u, pts in sorted_results)
            await ctx.send(f"**{header} ({ctx.guild.name}):**\n{results}")
        else:
            await ctx.send("No scorers this week!")

    async def post_season_standings_to_channel(self, channel, through_week: int = None):
        """Post season standings directly to a channel (used by auto_tasks and the command)."""
        season = get_current_season()
        db = get_db()
        guild_id = str(channel.guild.id)

        if through_week is None:
            through_week = await db.get_latest_week_with_winners(season)

        if not through_week:
            await channel.send("❌ No completed weeks found.")
            return

        scheme = await db.get_scoring_scheme(guild_id)
        season_scores: dict[str, int] = {}

        for wk in range(1, through_week + 1):
            winners = await db.get_winners(season, wk)
            if not winners:
                continue
            picks = await db.get_picks_for_week(season, wk, guild_id)
            week_scores = scoring_mod.compute_week_scores(picks, set(winners), scheme)
            for user, pts in week_scores.items():
                season_scores[user] = season_scores.get(user, 0) + pts

        if season_scores:
            sorted_standings = sorted(season_scores.items(), key=lambda x: x[1], reverse=True)
            standings = "\n".join(
                f"**{i+1}.** {u}: {pts} points"
                for i, (u, pts) in enumerate(sorted_standings)
            )
            await channel.send(
                f"**Season Standings ({channel.guild.name})** (Through Week {through_week}):\n{standings}"
            )
        else:
            await channel.send("No season data available yet.")

    @commands.command(name=CMD_SEASON_STANDINGS)
    @commands.has_permissions(administrator=True)
    async def season_standings(self, ctx, through_week: int = None):
        """Show cumulative season standings for THIS SERVER."""
        if await off_season_reply(ctx):
            return
        await self.post_season_standings_to_channel(ctx.channel, through_week)

    # !global_standings was removed here.
    # It merged picks across every configured guild by grouping on user_name
    # rather than user_id, so one person in two servers collapsed into a single
    # bucket (a wrong pick in one server wiped a perfect week in the other), and
    # two people sharing a display name became one entry. It also hardcoded
    # all-or-nothing regardless of either guild's configured scheme, so it
    # disagreed with every other standings surface by construction.
    # A rebuild should group on user_id and decide explicitly what a combined
    # table means when guilds run different schemes.

    @commands.command(name=CMD_CHECK_REACTIONS)
    @commands.has_permissions(administrator=True)
    async def check_reactions(self, ctx, week_number: int = None):
        """Show current picks for THIS SERVER, grouped by player.

        Usage: !check_reactions [week]

        Defaults to the current week. Formatted the same way as the automatic
        lock summary (shared via utils/pick_format) so the on-demand view and
        the posted one never disagree.
        """
        season = get_current_season()
        db = get_db()
        guild_id = str(ctx.guild.id)

        if week_number is None:
            week_number = find_current_week(
                load_full_schedule(), datetime.now(EASTERN), get_max_week()
            )
            if week_number is None:
                await ctx.send(
                    "Could not determine the current week. Pass a week number explicitly."
                )
                return

        if not (1 <= week_number <= get_max_week()):
            await ctx.send(f"Week number must be between 1 and {get_max_week()}.")
            return

        picks = await db.get_picks_for_week(season, week_number, guild_id)
        if not picks:
            await ctx.send(
                f"No picks recorded yet for Week {week_number} in this server."
            )
            return

        lines = format_picks_by_player(
            picks, f"**Week {week_number} Picks ({ctx.guild.name}):**\n"
        )
        await send_chunked(ctx.send, lines)


async def setup(bot):
    await bot.add_cog(Results(bot))
