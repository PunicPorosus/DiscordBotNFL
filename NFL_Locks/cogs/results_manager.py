"""Results Manager - Handles calculating and posting weekly results."""

from discord.ext import commands
import logging
from datetime import datetime, timedelta
from NFL_Locks.utils.constants import EASTERN
from NFL_Locks.utils.data_utils import load_full_schedule
from NFL_Locks.utils.schedule_utils import get_week_range_text, get_max_week, get_current_season
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.command_names import CMD_POST_RESULTS_MANUAL, CMD_SHOW_RESULTS
from NFL_Locks.utils.command_utils import off_season_reply
from NFL_Locks.utils.rate_limiter import rate_limiter
from NFL_Locks.utils import scoring as scoring_mod

logger = logging.getLogger('cogs.results_manager')


class ResultsManager(commands.Cog):
    """Manages calculating and posting weekly results."""

    def __init__(self, bot):
        self.bot = bot

    # _compute_scores removed — all scoring logic lives in utils/scoring.py

    # -- Core posting logic ----------------------------------------------------

    # Weeks that ended longer ago than this window are skipped during catchup.
    # A missing results_posted row for an old week almost always means the DB
    # is fresh (new install or reset), not that a post was genuinely missed.
    # 14 days = two full NFL weeks, enough to cover any realistic outage.
    _CATCHUP_WINDOW_DAYS = 14

    async def catchup_results(self):
        """Post results for any recent weeks that have winners but haven't been posted.

        Weeks whose week_end is more than _CATCHUP_WINDOW_DAYS in the past are
        silently skipped — a stale un-posted row there indicates a fresh DB,
        not a missed post.
        """
        logger.info("Checking for results to post...")

        season = get_current_season()
        db = get_db()
        now = datetime.now(EASTERN)
        schedule = load_full_schedule()
        configured_guilds = await get_db().get_all_configured_guilds()

        for wk in range(1, get_max_week() + 1):
            if not await db.has_winners(season, wk):
                continue

            # Check if any configured guild still needs results for this week
            guilds_needing = [
                gid for gid in configured_guilds
                if not await db.is_results_posted(season, wk, gid)
            ]
            if not guilds_needing:
                logger.debug(f"Week {wk} results already posted to all guilds")
                continue

            # Determine when this week ended
            week_games = schedule.get(str(wk))
            if not week_games:
                continue

            first_game_utc = datetime.fromisoformat(
                week_games[0]["date"].replace('Z', '+00:00')
            )
            first_game = first_game_utc.astimezone(EASTERN)
            days_since_tuesday = (first_game.weekday() - 1) % 7
            week_start = first_game - timedelta(days=days_since_tuesday)
            week_end = week_start + timedelta(days=6, hours=23, minutes=59)

            if now <= week_end:
                # Week hasn't finished yet
                continue

            days_since_end = (now - week_end).days
            if days_since_end > self._CATCHUP_WINDOW_DAYS:
                logger.debug(
                    f"Week {wk} ended {days_since_end}d ago — outside catchup "
                    f"window ({self._CATCHUP_WINDOW_DAYS}d), skipping"
                )
                continue

            logger.info(
                f"Week {wk} winners exist but results not posted to "
                f"{len(guilds_needing)} guild(s) (ended {days_since_end}d ago). Posting now..."
            )
            await self.post_results(wk)

    async def post_results(self, week_number: int):
        """Send results to all configured channels that still need them."""
        season = get_current_season()
        db = get_db()

        winners = await db.get_winners(season, week_number)
        if not winners:
            logger.error(f"No winners set for week {week_number}")
            return

        winners_set = set(winners)
        winners_text = ", ".join(winners)
        configured_guilds = await get_db().get_all_configured_guilds()

        for guild_id, channel_id in configured_guilds.items():
            guild_id_str = str(guild_id)

            if await db.is_results_posted(season, week_number, guild_id_str):
                logger.debug(f"Results already posted for week {week_number}, guild {guild_id}")
                continue

            channel = self.bot.get_channel(channel_id)
            if not channel:
                logger.warning(f"Channel {channel_id} not found for guild {guild_id}")
                continue

            scheme = await db.get_scoring_scheme(guild_id_str)
            picks = await db.get_picks_for_week(season, week_number, guild_id_str)
            scores = scoring_mod.compute_week_scores(picks, winners_set, scheme)
            header = scoring_mod.results_header(scheme)

            await rate_limiter.send(channel,
                f"**Week {week_number} Results**\n**Winners:** {winners_text}"
            )

            if scores:
                sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
                results = "\n".join(
                    f"**{u}**: {pts} points" for u, pts in sorted_results
                )
                await rate_limiter.send(channel,
                    f"**{header} ({channel.guild.name}):**\n{results}"
                )

            await db.mark_results_posted(season, week_number, guild_id_str)
            logger.info(f"Posted results for week {week_number} to {channel.guild.name}")

    async def post_weekly_results_to_channel(self, channel, week_number: int):
        """Post results for one week to a single channel (used by auto_tasks)."""
        season = get_current_season()
        db = get_db()
        guild_id_str = str(channel.guild.id)

        if await db.is_results_posted(season, week_number, guild_id_str):
            logger.debug(
                f"Results already posted for week {week_number}, guild {channel.guild.id}"
            )
            return

        winners = await db.get_winners(season, week_number)
        if not winners:
            logger.error(f"No winners set for week {week_number}")
            return

        scheme = await db.get_scoring_scheme(guild_id_str)
        winners_set = set(winners)
        picks = await db.get_picks_for_week(season, week_number, guild_id_str)
        scores = scoring_mod.compute_week_scores(picks, winners_set, scheme)
        header = scoring_mod.results_header(scheme)

        await rate_limiter.send(
            channel,
            f"**Week {week_number} Results**\n**Winners:** {', '.join(winners)}"
        )

        if scores:
            sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            results = "\n".join(
                f"**{u}**: {pts} points" for u, pts in sorted_results
            )
            await rate_limiter.send(
                channel,
                f"**{header} ({channel.guild.name}):**\n{results}"
            )

        await db.mark_results_posted(season, week_number, guild_id_str)
        logger.info(f"Posted results for week {week_number} to {channel.guild.name}")

    # -- Admin commands --------------------------------------------------------

    @commands.command(name=CMD_POST_RESULTS_MANUAL)
    @commands.has_permissions(administrator=True)
    async def post_results_manual(self, ctx, week_num: int):
        """Manually post results for a specific week."""
        if not (1 <= week_num <= get_max_week()):
            await ctx.send(f"❌ Week number must be between {get_week_range_text()}")
            return

        season = get_current_season()
        db = get_db()

        if not await db.has_winners(season, week_num):
            await ctx.send(f"❌ No winners set for Week {week_num}. Use `!set_winners` first.")
            return

        await ctx.send(f"Posting results for Week {week_num}...")
        await self.post_results(week_num)
        await ctx.send(f"✅ Results posted for Week {week_num}.")

    @commands.command(name=CMD_SHOW_RESULTS)
    @commands.has_permissions(administrator=True)
    async def show_results(self, ctx, week_num: int):
        """Show results for a specific week without posting to any channel."""
        if await off_season_reply(ctx):
            return
        if not (1 <= week_num <= get_max_week()):
            await ctx.send(f"❌ Week number must be between {get_week_range_text()}")
            return

        season = get_current_season()
        db = get_db()
        guild_id = str(ctx.guild.id)

        winners = await db.get_winners(season, week_num)
        if not winners:
            await ctx.send(f"❌ No winners set for Week {week_num}.")
            return

        scheme = await db.get_scoring_scheme(guild_id)
        picks = await db.get_picks_for_week(season, week_num, guild_id)
        scores = scoring_mod.compute_week_scores(picks, set(winners), scheme)
        header = scoring_mod.results_header(scheme)

        msg = f"**Week {week_num} Results**\n**Winners:** {', '.join(winners)}\n\n"

        if scores:
            msg += f"**{header} ({ctx.guild.name}):**\n"
            sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            for user, pts in sorted_results:
                msg += f"**{user}**: {pts} points\n"
        else:
            msg += "No scorers this week."

        await ctx.send(msg)


async def setup(bot):
    await bot.add_cog(ResultsManager(bot))
