from discord.ext import commands
import logging
import asyncio
from datetime import datetime, timedelta
from NFL_Locks.utils.constants import EASTERN
from NFL_Locks.utils.data_utils import load_full_schedule
from NFL_Locks.utils.espn_api import fetch_nfl_winners
from NFL_Locks.utils.schedule_utils import get_max_week, get_current_week_info, get_current_season
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.command_names import CMD_FETCH_WINNERS, CMD_SHOW_WINNERS

logger = logging.getLogger('cogs.winners')


class Winners(commands.Cog):
    """Manages fetching and storing game winners."""

    def __init__(self, bot):
        self.bot = bot

    def _get_current_week_info(self, now):
        """Determine current NFL week. Delegates to schedule_utils shared function."""
        return get_current_week_info(now)

    # -- Core logic ------------------------------------------------------------

    async def catchup_winners(self):
        """Check for and fetch missing winners based on the current date."""
        logger.info("Starting catchup_winners...")

        now = datetime.now(EASTERN)
        current_week, previous_week, in_nfl_season = self._get_current_week_info(now)
        season = get_current_season()
        db = get_db()

        # Determine which weeks are candidates
        if in_nfl_season:
            candidates = [w for w in (previous_week, current_week) if w]
        else:
            # Off-season: backfill winner data for any weeks missing it.
            # This is a read-only data operation — it does NOT trigger result
            # posting (catchup_results has its own staleness window for that).
            logger.info("Off-season: scanning all weeks for missing winner data")
            candidates = [
                wk for wk in range(1, get_max_week() + 1)
                if not await db.has_winners(season, wk)
            ]

        # Filter to only weeks actually missing winners
        weeks_to_check = [
            wk for wk in candidates
            if not await db.has_winners(season, wk)
        ]

        if not weeks_to_check:
            logger.info("No weeks need winners fetched")
            return

        logger.info(f"Checking winners for weeks: {weeks_to_check}")
        for wk in weeks_to_check:
            await self.fetch_winners_for_week(wk)

        logger.info("catchup_winners complete")

    async def fetch_winners_for_week(self, week_num: int) -> list[str] | None:
        """
        Fetch and store winners for a specific week if they aren't already stored.

        Returns the winners list on success, None if the week hasn't ended or
        the API returned nothing.
        """
        logger.info(f"fetch_winners_for_week called for week {week_num}")

        season = get_current_season()
        db = get_db()

        if await db.has_winners(season, week_num):
            logger.debug(f"Week {week_num} already has winners in DB")
            existing = await db.get_winners(season, week_num)
            return existing

        schedule = load_full_schedule()
        week_games = schedule.get(str(week_num))
        if not week_games:
            logger.warning(f"No games in schedule for week {week_num}")
            return None

        # Find the latest kickoff in the week to determine when the week ends
        now = datetime.now(EASTERN)
        latest_game_time = None

        for game in week_games:
            game_time_utc = datetime.fromisoformat(game["date"].replace('Z', '+00:00'))
            game_time = game_time_utc.astimezone(EASTERN)
            if latest_game_time is None or game_time > latest_game_time:
                latest_game_time = game_time

        if not latest_game_time:
            logger.warning(f"Could not determine latest game time for week {week_num}")
            return None

        # Allow 4 hours after last kickoff for games to finish
        week_end = latest_game_time + timedelta(hours=4)

        logger.info(
            f"Week {week_num} timing: now={now}, week_end={week_end}, "
            f"ended={now > week_end}"
        )

        if now <= week_end:
            logger.info(f"Week {week_num} hasn't ended yet (ends {week_end})")
            return None

        logger.info(f"Week {week_num} has ended, fetching winners from ESPN...")
        try:
            winners = await asyncio.wait_for(fetch_nfl_winners(week_num, season=season), timeout=8)
        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching winners for week {week_num}")
            return None
        except Exception as e:
            logger.error(f"Error fetching winners for week {week_num}: {e}", exc_info=True)
            return None

        if not winners:
            logger.warning(f"No winners returned by ESPN for week {week_num}")
            return None

        await db.set_winners(season, week_num, winners)
        logger.info(f"✅ Stored winners for week {week_num}: {winners}")
        return winners

    # -- Admin commands --------------------------------------------------------

    @commands.command(name=CMD_FETCH_WINNERS)
    @commands.has_permissions(administrator=True)
    async def fetch_winners(self, ctx, week_num: int = None):
        """Manually fetch winners for a specific week."""
        if week_num is None:
            now = datetime.now(EASTERN)
            current_week, previous_week, _ = self._get_current_week_info(now)
            week_num = previous_week if previous_week else current_week

            if week_num is None:
                await ctx.send("❌ Could not determine week number.")
                return

        if not (1 <= week_num <= get_max_week()):
            await ctx.send(f"❌ Week number must be between 1 and {get_max_week()}.")
            return

        await ctx.send(f"Fetching winners for Week {week_num}...")
        winners = await self.fetch_winners_for_week(week_num)

        if winners:
            await ctx.send(f"✅ Winners for Week {week_num}: {', '.join(winners)}")
        else:
            await ctx.send(
                f"❌ Could not fetch winners for Week {week_num}. "
                f"The week may not have ended yet."
            )

    @commands.command(name=CMD_SHOW_WINNERS)
    @commands.has_permissions(administrator=True)
    async def show_winners(self, ctx, week_num: int):
        """Show stored winners for a week."""
        if await get_db().get_bot_meta("off_season") == "true":
            season = get_current_season()
            await ctx.send(
                f"The {season} season data has been archived. "
                f"Check `data/archives/season_{season}/` for historical records."
            )
            return
        season = get_current_season()
        db = get_db()

        winners = await db.get_winners(season, week_num)

        if winners:
            await ctx.send(f"**Week {week_num} Winners:** {', '.join(winners)}")
        else:
            await ctx.send(f"No winners stored for Week {week_num}.")


async def setup(bot):
    await bot.add_cog(Winners(bot))
