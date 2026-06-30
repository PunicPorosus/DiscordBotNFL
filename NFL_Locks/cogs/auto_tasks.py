# ==================== DYNAMIC AUTO TASKS ====================
"""
Dynamic scheduling system that posts results the morning after the last game.
- Week with MNF games → Posts Tuesday morning (traditional)
- Week without MNF → Posts Monday morning (like Week 18 2025)
"""

from discord.ext import commands, tasks
from datetime import datetime, timedelta
from NFL_Locks.utils.constants import EASTERN
from NFL_Locks.utils.data_utils import load_full_schedule
from NFL_Locks.utils.schedule_utils import get_max_week, get_current_season
from NFL_Locks.utils.database import get_db
import asyncio
import logging

logger = logging.getLogger('cogs.auto_tasks')


class AutoTasks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_posted_week = None  # In-session dedup guard
        logger.info("AutoTasks cog initialized with dynamic scheduling")
        self.dynamic_weekly_tasks.start()

    def cog_unload(self):
        self.dynamic_weekly_tasks.cancel()

    # -- Timing helpers --------------------------------------------------------

    def _get_week_end_time(self, week_num):
        """Return the estimated end time of a week (last kickoff + 4 hours)."""
        schedule = load_full_schedule()
        week_games = schedule.get(str(week_num))
        if not week_games:
            return None

        latest_game_time = None
        for game in week_games:
            game_time_utc = datetime.fromisoformat(game["date"].replace('Z', '+00:00'))
            game_time = game_time_utc.astimezone(EASTERN)
            if latest_game_time is None or game_time > latest_game_time:
                latest_game_time = game_time

        if not latest_game_time:
            return None

        return latest_game_time + timedelta(hours=4)

    def _get_results_posting_time(self, week_num):
        """Return 8 AM the morning after the last game of the week."""
        week_end = self._get_week_end_time(week_num)
        if not week_end:
            return None
        return (week_end + timedelta(days=1)).replace(
            hour=8, minute=0, second=0, microsecond=0
        )

    async def _find_week_to_post(self, now) -> tuple[int | None, bool]:
        """
        Find which week's results should be posted based on current time.

        Checks weeks within the last 30 days. A week is ready when:
          - The posting time has passed (8 AM after last game), AND
          - Results have not yet been posted to all configured guilds, AND
          - We haven't already posted it this session.

        Returns (week_num, is_ready).
        """
        schedule = load_full_schedule()
        max_week = get_max_week()
        season = get_current_season()
        db = get_db()
        configured_guilds = await get_db().get_all_configured_guilds()

        for week_num in range(1, max_week + 1):
            week_games = schedule.get(str(week_num))
            if not week_games:
                continue

            week_end = self._get_week_end_time(week_num)
            if not week_end:
                continue

            # Skip weeks that ended more than 30 days ago
            if (now - week_end).days > 30:
                continue

            post_time = self._get_results_posting_time(week_num)
            if not post_time or now < post_time:
                continue

            # In-session dedup
            if self.last_posted_week == week_num:
                logger.debug(f"Already posted Week {week_num} this session, skipping")
                continue

            # Check DB: skip if all configured guilds already have results posted.
            # Must use an explicit loop — await inside a generator expression
            # passed to all() is not valid and produces wrong results.
            if not configured_guilds:
                all_posted = False
            else:
                all_posted = True
                for gid in configured_guilds:
                    if not await db.is_results_posted(season, week_num, str(gid)):
                        all_posted = False
                        break

            if all_posted:
                logger.debug(f"Week {week_num} results already posted to all guilds")
                continue

            logger.info(f"Week {week_num} ready to post (post_time={post_time}, now={now})")
            return week_num, True

        return None, False

    # -- Main task -------------------------------------------------------------

    @tasks.loop(hours=1)
    async def dynamic_weekly_tasks(self):
        """
        Check every hour whether any week is ready for results posting.
        Only runs within the 7–10 AM posting window.
        """
        try:
            now = datetime.now(EASTERN)
            logger.debug(f"Dynamic check at {now.strftime('%A %I:%M %p ET')}")

            if not (7 <= now.hour < 10):
                return

            week_to_post, is_ready = await self._find_week_to_post(now)
            if not is_ready or week_to_post is None:
                return

            logger.info(f"{'═' * 39}")
            logger.info(f"POSTING RESULTS FOR WEEK {week_to_post}")
            logger.info(f"Time: {now.strftime('%A, %B %d at %I:%M %p ET')}")
            logger.info(f"{'═' * 39}")

            # Fetch winners before processing any server
            logger.info(f"Ensuring winners fetched for Week {week_to_post}...")
            await self._ensure_winners_fetched(week_to_post)
            await asyncio.sleep(2)

            schedule = load_full_schedule()
            max_week = get_max_week()
            next_week = week_to_post + 1 if week_to_post < max_week else None

            configured_guilds = await get_db().get_all_configured_guilds()
            if not configured_guilds:
                logger.error("No configured guilds found — cannot post results")
                return

            configured_channels = []
            for guild_id, channel_id in configured_guilds.items():
                channel = self.bot.get_channel(channel_id)
                if channel:
                    configured_channels.append(channel)
                else:
                    logger.warning(f"Channel {channel_id} not found for guild {guild_id}")

            # Process survivor results once (all guilds) before posting
            survivor_guild_results = await self._process_survivor_results(week_to_post)

            for channel in configured_channels:
                logger.info(f"Processing {channel.guild.name}...")

                logger.info(f"Posting Perfect Picks for Week {week_to_post}...")
                await self._post_results(channel, week_to_post)
                await asyncio.sleep(2)

                logger.info("Posting Season Leaderboard...")
                await self._post_leaderboard(channel, week_to_post)
                await asyncio.sleep(2)

                if next_week and next_week <= max_week:
                    logger.info(f"Posting Week {next_week} Matchups...")
                    week_games = schedule.get(str(next_week))
                    if week_games:
                        await self._post_games(channel, next_week, week_games)
                    await asyncio.sleep(2)
                else:
                    logger.info("End of season detected — posting season wrap-up...")
                    await self._post_season_wrapup(channel)
                    await asyncio.sleep(2)

                # Post survivor results to the survivor channel for this guild
                await self._post_survivor_results(
                    channel.guild, week_to_post, survivor_guild_results
                )
                await asyncio.sleep(2)

                logger.info(f"Completed posting for {channel.guild.name}")

            self.last_posted_week = week_to_post
            logger.info(f"All servers processed for Week {week_to_post}")

        except Exception as e:
            logger.error(f"Error in dynamic_weekly_tasks: {e}", exc_info=True)

    # -- Delegate helpers ------------------------------------------------------

    async def _ensure_winners_fetched(self, week_num):
        winners_cog = self.bot.get_cog('Winners')
        if winners_cog:
            await winners_cog.fetch_winners_for_week(week_num)

    async def _post_results(self, channel, week_num):
        results_cog = self.bot.get_cog('ResultsManager')
        if results_cog:
            await results_cog.post_weekly_results_to_channel(channel, week_num)

    async def _post_leaderboard(self, channel, current_week):
        results_cog = self.bot.get_cog('Results')
        if results_cog:
            await results_cog.post_season_standings_to_channel(channel, current_week)

    async def _post_games(self, channel, week_num, week_games):
        games_cog = self.bot.get_cog('GamesManager')
        if games_cog:
            await games_cog.post_games_to_channel(channel, week_num, week_games)

    async def _post_season_wrapup(self, channel):
        wrapup_cog = self.bot.get_cog('SeasonWrapup')
        if wrapup_cog:
            await wrapup_cog.post_season_wrapup(channel)
        else:
            logger.warning("SeasonWrapup cog not found — skipping wrap-up post")

    async def _process_survivor_results(self, week_num) -> dict:
        """Run survivor week processing and return per-guild results."""
        survivor_cog = self.bot.get_cog('SurvivorGame')
        if not survivor_cog:
            return {}
        try:
            return await survivor_cog.process_survivor_week(week_num)
        except Exception as e:
            logger.error(f"[SURVIVOR] Error processing Week {week_num}: {e}", exc_info=True)
            return {}

    async def _post_survivor_results(self, guild, week_num, guild_results: dict):
        """Post survivor results to the guild's survivor channel if configured."""
        from NFL_Locks.utils.database import get_db
        from NFL_Locks.utils.schedule_utils import get_current_season

        survivor_cog = self.bot.get_cog('SurvivorGame')
        if not survivor_cog:
            return

        db = get_db()
        season = get_current_season()
        guild_id = str(guild.id)

        config = await db.get_survivor_config(guild_id)
        if not config:
            return

        if await db.is_survivor_results_posted(season, week_num, guild_id):
            return

        results = guild_results.get(guild_id)
        if not results:
            return

        survivor_ch = self.bot.get_channel(config["channel_id"])
        if not survivor_ch:
            logger.warning(f"[SURVIVOR] Channel {config['channel_id']} not found for guild {guild_id}")
            return

        await survivor_cog.post_survivor_results(survivor_ch, week_num, results)
        await db.mark_survivor_results_posted(season, week_num, guild_id)
        logger.info(f"[SURVIVOR] Posted Week {week_num} results for {guild.name}")


async def setup(bot):
    await bot.add_cog(AutoTasks(bot))
