"""Startup Coordinator - Orchestrates all startup tasks."""

from discord.ext import commands
import logging
import asyncio
from datetime import datetime, timezone
from NFL_Locks.utils.constants import EASTERN
from NFL_Locks.utils.config import DEFAULT_MAX_WEEK, STARTUP_WAIT_SECONDS
from NFL_Locks.utils.schedule_utils import get_max_week, find_current_week, get_current_season
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.command_names import CMD_STARTUP_STATUS, CMD_RERUN_STARTUP, CMD_FORCE_RECONNECT_CATCHUP

logger = logging.getLogger('cogs.startup_coordinator')

# Gap threshold: if the bot was offline longer than this, reaction catchup runs.
# 90 s covers normal process restarts; anything longer means real missed events.
_RECONNECT_CATCHUP_THRESHOLD_S = 90

class StartupCoordinator(commands.Cog):
    """Coordinates all startup tasks in the correct order."""

    # Class-level lock to prevent multiple instances
    _startup_lock = asyncio.Lock()
    _is_processing = False

    def __init__(self, bot):
        self.bot = bot
        self.startup_complete = False

    # -- Gateway event handlers ---------------------------------------------

    @commands.Cog.listener()
    async def on_resumed(self):
        """
        Discord successfully resumed the previous session — missed Gateway
        events were replayed automatically.  No catchup needed.
        """
        logger.info("Discord session resumed — Gateway replayed missed events, no catchup needed")

    @commands.Cog.listener()
    async def on_ready(self):
        """
        Fires on cold start AND on full reconnects where Discord could not
        resume the previous session (so missed events were NOT replayed).

        - First call  → run full initialization sequence.
        - Later calls → run reaction catchup only if the heartbeat gap
                        indicates the bot was offline long enough to have
                        missed meaningful reaction events.
        """
        if self._is_processing:
            return

        async with self._startup_lock:
            if self._is_processing:
                return
            self._is_processing = True
            try:
                if not self.startup_complete:
                    await self._run_cold_start()
                else:
                    await self._run_reconnect_catchup()
            finally:
                self._is_processing = False

    # -- Cold start --------------------------------------------------------

    async def _run_cold_start(self):
        """Full initialization sequence — runs once on first bot start."""
        self.startup_complete = True

        # Wait for bot to fully initialize
        await asyncio.sleep(STARTUP_WAIT_SECONDS)

        logger.info("=" * 50)
        logger.info("Starting bot initialization sequence...")
        logger.info("=" * 50)

        # -- Season length sanity check ----------------------------
        live_max_week = get_max_week()
        if live_max_week != DEFAULT_MAX_WEEK:
            logger.warning(
                f"❌ NFL SEASON LENGTH MISMATCH: schedule reports {live_max_week} weeks "
                f"but DEFAULT_MAX_WEEK in config.py is {DEFAULT_MAX_WEEK}. "
                f"If the NFL has expanded the regular season, update DEFAULT_MAX_WEEK "
                f"in NFL_Locks/utils/config.py to {live_max_week}."
            )
        else:
            logger.info(f"Season length check passed: {live_max_week} weeks (matches DEFAULT_MAX_WEEK)")
        # ---------------------------------------------------------

        try:
            # Get required cogs
            cache_cog = self.bot.get_cog('Cache')
            winners_cog = self.bot.get_cog('Winners')
            games_cog = self.bot.get_cog('GamesManager')
            results_cog = self.bot.get_cog('ResultsManager')
            reactions_cog = self.bot.get_cog('Reactions')

            missing_cogs = [
                name for name, cog in [
                    ('Cache', cache_cog), ('Winners', winners_cog),
                    ('GamesManager', games_cog), ('ResultsManager', results_cog),
                    ('Reactions', reactions_cog),
                ] if not cog
            ]
            if missing_cogs:
                logger.error(f"Missing required cogs: {', '.join(missing_cogs)}")
                self.startup_complete = False
                return

            # Step 1: Refresh guild names in DB from live Discord data
            logger.info("Step 1/7: Refreshing guild names...")
            guild_map = {g.id: g.name for g in self.bot.guilds}
            await get_db().refresh_guild_names(guild_map)
            logger.info(f"Guild names refreshed for {len(guild_map)} guild(s)")

            # Step 2: Build Message Cache
            logger.info("Step 2/7: Building message cache...")
            await cache_cog.build_message_cache()
            logger.info("Message cache built")

            # Step 2: Fetch Winners
            logger.info("Step 3/7: Checking for winners...")
            await winners_cog.catchup_winners()
            logger.info("Winners check complete")

            # Step 3 & 4: Handle Tuesday Posts (if needed)
            now = datetime.now(EASTERN)
            if now.weekday() == 1:  # Tuesday
                if now.hour < 8:
                    logger.info("Step 4-5/7: Skipping Tuesday tasks - before 8 AM (scheduled task will handle)")
                else:
                    logger.info("Step 4-5/7: Tuesday after 8 AM - running Tuesday routine catchup")
                    await self.run_tuesday_catchup(winners_cog, games_cog, results_cog)
            else:
                logger.info("Step 4/7: Checking for games to post...")
                await games_cog.catchup_games()
                logger.info("Games check complete")

                logger.info("Step 5/7: Checking for results to post...")
                await results_cog.catchup_results()
                logger.info("Results check complete")

            # Step 6: Reaction catchup for current week (pre-deadline only)
            logger.info("Step 6/7: Running startup reaction catchup...")
            catchup_cog = self.bot.get_cog('ReactionCatchup')
            if catchup_cog:
                from NFL_Locks.utils.data_utils import load_full_schedule
                from NFL_Locks.utils.time_utils import get_week_deadline
                _schedule = load_full_schedule()
                _now_et = datetime.now(EASTERN)
                _startup_week = find_current_week(_schedule, _now_et, get_max_week())
                if _startup_week is not None:
                    _deadline = get_week_deadline(_startup_week)
                    if _deadline and _now_et < _deadline:
                        await catchup_cog.process_week_reactions(_startup_week)
                        logger.info(f"Startup reaction catchup complete for Week {_startup_week}")
                    else:
                        logger.info(f"Week {_startup_week} deadline already passed — skipping catchup")
                else:
                    logger.info("Not in an active NFL week — skipping startup reaction catchup")
            else:
                logger.warning("ReactionCatchup cog not found — skipping startup reaction catchup")

            # Step 6: Post Lock Summaries if Missed
            logger.info("Step 7/7: Checking for missed lock summaries...")
            locks_cog = self.bot.get_cog('Locks')
            if locks_cog:
                await self.catchup_locks(locks_cog)
                logger.info("Lock summaries checked")
            else:
                logger.warning("Locks cog not found - skipping lock catchup")

            logger.info("=" * 50)
            logger.info("Bot initialization complete!")
            logger.info("=" * 50)
            print("✅ Startup tasks completed successfully")

        except Exception as e:
            logger.error(f"Error during cold start: {e}", exc_info=True)
            self.startup_complete = False

    # -- Reconnect catchup -------------------------------------------------

    async def _run_reconnect_catchup(self):
        """
        Called when on_ready fires after startup_complete is already True,
        meaning Discord could not resume the session and missed Gateway events
        were NOT replayed.

        Compares now against the last DB heartbeat.  If the bot was offline
        long enough to have missed meaningful reaction events, rebuilds picks
        for the current week from Discord's reaction state.
        """
        from NFL_Locks.utils.database import get_db

        db = get_db()
        last_hb = await db.get_last_heartbeat()
        now = datetime.now(timezone.utc)

        if last_hb is None:
            gap_seconds = float('inf')
        else:
            gap_seconds = (now - last_hb).total_seconds()

        logger.info(
            f"Reconnect detected (on_ready fired post-startup). "
            f"Heartbeat gap: {gap_seconds:.0f}s "
            f"(threshold: {_RECONNECT_CATCHUP_THRESHOLD_S}s)"
        )

        if gap_seconds < _RECONNECT_CATCHUP_THRESHOLD_S:
            logger.info("Gap below threshold — skipping reaction catchup")
            return

        logger.info(
            f"Gap {gap_seconds:.0f}s exceeds threshold — running reaction "
            f"catchup for current week"
        )
        catchup_cog = self.bot.get_cog('ReactionCatchup')
        if catchup_cog:
            from NFL_Locks.utils.data_utils import load_full_schedule
            _schedule = load_full_schedule()
            _now_et = datetime.now(EASTERN)
            _week = find_current_week(_schedule, _now_et, get_max_week())
            if _week is None:
                logger.info("Not in an active NFL week — skipping reconnect reaction catchup")
            else:
                try:
                    await catchup_cog.process_week_reactions(_week)
                    logger.info(f"Reconnect reaction catchup complete for Week {_week}")
                except Exception as e:
                    logger.error(f"Reconnect reaction catchup failed: {e}", exc_info=True)
        else:
            logger.warning("ReactionCatchup cog not found — cannot run reconnect catchup")
    
    async def catchup_locks(self, locks_cog):
        """Post lock summaries for any weeks that passed deadline but didn't post summary."""
        logger.info("Checking for missed lock summaries...")
        
        now = datetime.now(EASTERN)
        from NFL_Locks.utils.data_utils import load_full_schedule
        from NFL_Locks.utils.time_utils import get_week_deadline
        from datetime import timedelta
        
        from NFL_Locks.utils.schedule_utils import get_max_week
        schedule = load_full_schedule()

        # Check each week to see if it passed deadline but didn't post lock summary
        for wk in range(1, get_max_week() + 1):
            try:
                week_games = schedule.get(str(wk))
                if not week_games:
                    continue

                # Calculate deadline for this week
                deadline = get_week_deadline(wk)
                if not deadline:
                    continue

                # Check if deadline has passed but week hasn't ended yet
                first_game_utc = datetime.fromisoformat(week_games[0]["date"].replace('Z', '+00:00'))
                first_game = first_game_utc.astimezone(EASTERN)
                days_since_tuesday = (first_game.weekday() - 1) % 7
                week_start = first_game - timedelta(days=days_since_tuesday)
                week_end = week_start + timedelta(days=6, hours=23, minutes=59)
                
                # Only post if:
                # 1. Deadline has passed
                # 2. Week hasn't ended yet (still relevant)
                # 3. This week hasn't been locked yet
                if deadline < now <= week_end:
                    configured_guilds = await get_db().get_all_configured_guilds()

                    for guild_id in configured_guilds.keys():
                        if await get_db().needs_locks_posted(get_current_season(), wk, guild_id):
                            logger.info(f"Missed lock summary for week {wk}, guild {guild_id} - posting now")
                            
                            channel_id = configured_guilds[guild_id]
                            channel = self.bot.get_channel(channel_id)
                            
                            if channel:
                                await locks_cog.lock_reactions_for_week(wk, guild_id, channel)
                    
            except Exception as e:
                logger.error(f"Error checking lock for week {wk}: {e}")
                continue
        
        logger.info("Lock summary catchup complete")
    
    async def run_tuesday_catchup(self, winners_cog, games_cog, results_cog):
        """Run Tuesday tasks in correct order during catchup."""
        from NFL_Locks.utils.status_tracker import needs_results_posted

        logger.info("Running Tuesday catchup routine in correct order...")

        now = datetime.now(EASTERN)
        current_week, previous_week, in_nfl_season = winners_cog._get_current_week_info(now)

        if not in_nfl_season:
            logger.info("Off-season — skipping Tuesday catchup (results/leaderboard/games)")
            return

        if not previous_week:
            logger.warning("Could not determine previous week for Tuesday catchup")
            return

        configured_guilds = await get_db().get_all_configured_guilds()

        # Check if results already posted for all guilds for the previous week
        all_posted = True
        for gid in configured_guilds.keys():
            if await needs_results_posted(previous_week, int(gid)):
                all_posted = False
                break

        logger.info(f"Results already posted for Week {previous_week} (all guilds): {all_posted}")

        if not all_posted:
            logger.info(f"Posting results for Week {previous_week}")
            await results_cog.catchup_results()
            await asyncio.sleep(2)
        else:
            logger.info(f"Skipping results — already posted for Week {previous_week}")

        # Post games for current week if needed
        if current_week:
            logger.info(f"Checking if games need posting for Week {current_week}")
            await games_cog.catchup_games()

        logger.info("Tuesday catchup routine complete")

    # -- Admin commands ----------------------------------------------------

    @commands.command(name=CMD_STARTUP_STATUS)
    @commands.is_owner()
    async def startup_status(self, ctx):
        """Check startup completion status."""
        if self.startup_complete:
            await ctx.send("✅ Startup sequence completed successfully")
        elif self._is_processing:
            await ctx.send("⏳ Startup sequence in progress...")
        else:
            await ctx.send("❌ Startup sequence not yet run or failed")

    @commands.command(name=CMD_RERUN_STARTUP)
    @commands.is_owner()
    async def rerun_startup(self, ctx):
        """Manually re-run the startup sequence."""
        if self._is_processing:
            await ctx.send("❌ Startup sequence already in progress")
            return

        await ctx.send("Re-running startup sequence...")
        self.startup_complete = False
        await self.on_ready()

        if self.startup_complete:
            await ctx.send("✅ Startup sequence completed")
        else:
            await ctx.send("❌ Startup sequence failed — check logs")

    @commands.command(name=CMD_FORCE_RECONNECT_CATCHUP)
    @commands.is_owner()
    async def force_reconnect_catchup(self, ctx):
        """Force a reaction catchup as if the bot had just reconnected after a long gap."""
        await ctx.send("⏳ Running forced reconnect reaction catchup...")
        catchup_cog = self.bot.get_cog('ReactionCatchup')
        if not catchup_cog:
            await ctx.send("❌ ReactionCatchup cog not found")
            return
        from NFL_Locks.utils.data_utils import load_full_schedule
        from datetime import datetime
        _schedule = load_full_schedule()
        _now_et = datetime.now(EASTERN)
        _week = find_current_week(_schedule, _now_et, get_max_week())
        if _week is None:
            await ctx.send("❌ Not in an active NFL week — no catchup to run.")
            return
        try:
            await catchup_cog.process_week_reactions(_week)
            await ctx.send(f"✅ Reconnect reaction catchup complete for Week {_week}")
        except Exception as e:
            logger.error(f"Forced reconnect catchup failed: {e}", exc_info=True)
            await ctx.send(f"❌ Catchup failed: {e}")


async def setup(bot):
    await bot.add_cog(StartupCoordinator(bot))
