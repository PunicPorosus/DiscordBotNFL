"""Periodic Reaction Catchup - Runs in background to keep reactions up to date."""

from discord.ext import commands, tasks
import logging
import asyncio
from datetime import datetime, timedelta
from NFL_Locks.utils.constants import EASTERN
from NFL_Locks.utils.data_utils import load_full_schedule
from NFL_Locks.utils.time_utils import get_week_deadline
from NFL_Locks.utils.schedule_utils import get_max_week, get_current_season, find_current_week
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.command_names import CMD_FORCE_REACTION_CATCHUP

logger = logging.getLogger('cogs.reaction_catchup')


class ReactionCatchup(commands.Cog):
    """Periodically processes reactions to keep data up to date."""

    def __init__(self, bot):
        self.bot = bot
        self.last_catchup_time = {}   # {week: datetime}
        self.synced_weeks_final = set()  # Weeks that have had final pre-deadline sync
        self.is_processing = False

        self.periodic_catchup.add_exception_type(Exception)
        self.periodic_catchup.start()

        logger.info("Reaction catchup task initialized")

    def cog_unload(self):
        self.periodic_catchup.cancel()

    # -- Background task -------------------------------------------------------

    @tasks.loop(minutes=5)
    async def periodic_catchup(self):
        """
        Single pre-deadline sync that runs ~15 minutes before kickoff.

        After sync completes, reactions are locked and no further tracking
        occurs until Tuesday.
        """
        if self.is_processing:
            logger.debug("Catchup already in progress, skipping")
            return

        self.is_processing = True
        try:
            now = datetime.now(EASTERN)
            schedule = load_full_schedule()
            max_week = get_max_week()

            # Find current NFL week
            current_week = find_current_week(schedule, now, max_week)
            if not current_week:
                logger.debug("Not in an active NFL week")
                return

            deadline = get_week_deadline(current_week)
            if not deadline:
                return

            time_until = (deadline - now).total_seconds() / 60  # minutes

            # Run sync in the 10-20 minute window before deadline
            if not (10 <= time_until <= 20):
                return
            if current_week in self.synced_weeks_final:
                return

            logger.info(
                f"[PRE-DEADLINE SYNC] Running final sync for Week {current_week} — "
                f"deadline in {int(time_until)} min at {deadline.strftime('%I:%M %p ET')}"
            )

            configured_guilds = await get_db().get_all_configured_guilds()

            # Announce before sync — locks channels
            for guild_id, channel_id in configured_guilds.items():
                channel = self.bot.get_channel(channel_id)
                if channel:
                    try:
                        await channel.send(
                            f"**Final Sync Notice** \n\n"
                            f"Picks are being synced for Week {current_week}.\n"
                            f"**Deadline:** {deadline.strftime('%A at %I:%M %p ET')}\n\n"
                            f"After this sync completes (~30 seconds), "
                            f"your picks will be **LOCKED** and no further changes will be tracked.\n\n"
                            f"Make your final adjustments NOW!"
                        )
                    except Exception as e:
                        logger.error(f"Error announcing in guild {guild_id}: {e}")

            # Announce before sync — survivor channels (separate from locks)
            survivor_cog = self.bot.get_cog('SurvivorGame')
            if survivor_cog:
                survivor_configs = await get_db().get_all_survivor_configs()
                for scfg in survivor_configs:
                    s_channel = self.bot.get_channel(scfg["channel_id"])
                    if s_channel:
                        try:
                            await s_channel.send(
                                f"**Survivor Final Sync Notice**\n\n"
                                f"Survivor picks are being synced for Week {current_week}.\n"
                                f"**Deadline:** {deadline.strftime('%A at %I:%M %p ET')}\n\n"
                                f"Make your final pick NOW — changes will be **LOCKED** in ~30 seconds."
                            )
                        except Exception as e:
                            logger.error(
                                f"Error announcing survivor sync in channel "
                                f"{scfg['channel_id']}: {e}"
                            )

            await self.process_week_reactions(current_week)

            self.synced_weeks_final.add(current_week)
            self.last_catchup_time[current_week] = datetime.now(EASTERN)

            # Announce completion — locks channels
            for guild_id, channel_id in configured_guilds.items():
                channel = self.bot.get_channel(channel_id)
                if channel:
                    try:
                        await channel.send(
                            f"✅ **Sync Complete!**\n\n"
                            f"Week {current_week} picks are now **LOCKED**.\n"
                            f"No further changes will be tracked until results are posted on Tuesday."
                        )
                    except Exception as e:
                        logger.error(f"Error announcing completion in guild {guild_id}: {e}")

            # Announce completion — survivor channels
            if survivor_cog:
                for scfg in survivor_configs:
                    s_channel = self.bot.get_channel(scfg["channel_id"])
                    if s_channel:
                        try:
                            await s_channel.send(
                                f"✅ **Survivor Sync Complete!**\n\n"
                                f"Week {current_week} Survivor picks are now **LOCKED**.\n"
                                f"Results will be posted after games conclude."
                            )
                        except Exception as e:
                            logger.error(
                                f"Error announcing survivor completion in channel "
                                f"{scfg['channel_id']}: {e}"
                            )

            logger.info(f"[PRE-DEADLINE SYNC] Complete for Week {current_week} — reactions now locked")

        except Exception as e:
            logger.error(f"Error in periodic_catchup: {e}", exc_info=True)
        finally:
            self.is_processing = False

    # -- Core rebuild ----------------------------------------------------------

    async def process_week_reactions(self, week_number: int):
        """
        Rebuild all picks from scratch for a specific week.

        Sets reconciliation_active on the Reactions cog so live events are
        buffered rather than written during the rebuild. Per-guild fetch is
        attempted up to 3 times with exponential backoff (30/60/120 s).

        On success: flushes buffered reactions for that guild.
        On failure: discards the buffer, marks the guild failed in DB, and
                    alerts the configured channel.
        """
        from NFL_Locks.utils.message_fetcher import get_message_fetcher

        reactions_cog = self.bot.get_cog('Reactions')
        if not reactions_cog:
            logger.error("Reactions cog not found")
            return

        db = get_db()
        season = get_current_season()
        message_fetcher = get_message_fetcher(self.bot)
        configured_guilds = await db.get_all_configured_guilds()

        logger.info(f"[REBUILD] Syncing reactions for Week {week_number} (season {season})")

        reactions_cog.reconciliation_active = True
        try:
            for guild_id, channel_id in configured_guilds.items():
                guild_id_str = str(guild_id)
                picks_written, success = await self._reconcile_guild(
                    guild_id_str, week_number, season, db, reactions_cog, message_fetcher
                )

                if success:
                    logger.info(
                        f"[REBUILD] Guild {guild_id_str} OK — "
                        f"{picks_written} reactions written; flushing buffer"
                    )
                    await reactions_cog.flush_pending_reactions(guild_id_str)
                else:
                    logger.error(
                        f"[REBUILD] Guild {guild_id_str} FAILED — "
                        f"discarding buffer and marking failed in DB"
                    )
                    reactions_cog.clear_pending_for_guild(guild_id_str)
                    await db.mark_reconciliation_failed(season, week_number, guild_id_str)
                    await self._notify_admin(
                        channel_id,
                        f"Reconciliation failed for Week {week_number} after 3 attempts. "
                        f"Picks may be incomplete. Run `!force_reaction_catchup {week_number}` "
                        f"once the issue is resolved."
                    )
        finally:
            reactions_cog.reconciliation_active = False
            # Discard any remaining buffer entries for guilds not yet processed
            reactions_cog.pending_reactions.clear()
            logger.info(f"[REBUILD] Reconciliation gate lifted for Week {week_number}")

        # Survivor uses its own gate + rebuild — delegate after locks finishes
        # so the two rebuilds don't race on Discord rate limits.
        survivor_cog = self.bot.get_cog('SurvivorGame')
        if survivor_cog:
            await survivor_cog.process_week_survivor_reactions(week_number)

    async def _reconcile_guild(
        self,
        guild_id_str: str,
        week_number: int,
        season: str,
        db,
        reactions_cog,
        message_fetcher,
    ) -> tuple[int, bool]:
        """
        Fetch and rebuild picks for one guild, with up to 3 attempts.

        Backoff schedule: 30 s -> 60 s -> 120 s.
        On HTTP 429: sleeps max(scheduled_backoff, retry_after) seconds.

        Returns:
            (picks_written, success)
        """
        import discord

        BACKOFF_SECS = [30, 60, 120]

        messages_by_channel = await db.get_messages_for_week(season, week_number, guild_id_str)
        if not messages_by_channel:
            logger.debug(
                f"[REBUILD] No tracked messages for guild {guild_id_str}, Week {week_number}"
            )
            return 0, True  # Nothing to do — not a failure

        guild = self.bot.get_guild(int(guild_id_str))
        if not guild:
            logger.warning(f"[REBUILD] Guild {guild_id_str} not in cache — skipping")
            return 0, False

        int_keyed = {int(cid): mids for cid, mids in messages_by_channel.items()}

        # Build matchup map once before retry loop — it's a pure DB read and
        # doesn't need to be repeated on each attempt.
        matchup_map = await db.get_matchup_map_for_week(season, week_number, guild_id_str)
        logger.debug(
            f"[REBUILD] Guild {guild_id_str} — {len(matchup_map)} matchup messages mapped"
        )

        for attempt, backoff_secs in enumerate(BACKOFF_SECS, start=1):
            try:
                fetched = await message_fetcher.fetch_messages_bulk(
                    int_keyed,
                    batch_size=3,
                    delay_between_batches=1.0,
                )

                # Build live Discord state: {user_id: {'name': str, 'teams': set}}
                # matchup_map enforces one-pick-per-matchup during state building.
                discord_state = await message_fetcher.build_reaction_state(
                    fetched, matchup_map=matchup_map
                )

                # Fetch current DB state: {user_id: set[team]}
                db_state = await db.get_picks_by_user_id(season, week_number, guild_id_str)

                # Apply delta — only write what changed
                added = 0
                removed = 0

                for user_id, info in discord_state.items():
                    db_teams = db_state.get(user_id, set())
                    for team in info['teams'] - db_teams:
                        await db.add_pick(
                            season, week_number, guild_id_str,
                            user_id, info['name'], team
                        )
                        added += 1

                for user_id, db_teams in db_state.items():
                    discord_teams = discord_state.get(user_id, {}).get('teams', set())
                    for team in db_teams - discord_teams:
                        await db.remove_pick(
                            season, week_number, guild_id_str, user_id, team
                        )
                        removed += 1

                logger.info(
                    f"[REBUILD] Guild {guild_id_str} attempt {attempt} success — "
                    f"+{added} added, -{removed} removed"
                )
                return added + removed, True

            except discord.HTTPException as e:
                if e.status == 429:
                    sleep_secs = max(backoff_secs, getattr(e, 'retry_after', backoff_secs))
                    logger.warning(
                        f"[REBUILD] Guild {guild_id_str} attempt {attempt} — "
                        f"rate limited (429); sleeping {sleep_secs:.1f}s"
                    )
                else:
                    sleep_secs = backoff_secs
                    logger.warning(
                        f"[REBUILD] Guild {guild_id_str} attempt {attempt} — "
                        f"HTTP {e.status}; sleeping {sleep_secs}s"
                    )
                if attempt < len(BACKOFF_SECS):
                    await asyncio.sleep(sleep_secs)

            except Exception as e:
                logger.warning(
                    f"[REBUILD] Guild {guild_id_str} attempt {attempt} failed: {e}; "
                    f"sleeping {backoff_secs}s"
                )
                if attempt < len(BACKOFF_SECS):
                    await asyncio.sleep(backoff_secs)

        logger.error(
            f"[REBUILD] Guild {guild_id_str} exhausted all {len(BACKOFF_SECS)} attempts"
        )
        return 0, False

    async def _notify_admin(self, channel_id: int, message: str):
        """Send an alert to the guild's configured channel."""
        channel = self.bot.get_channel(channel_id)
        if channel:
            try:
                await channel.send(f"**[Bot Alert]** {message}")
            except Exception as e:
                logger.error(
                    f"Failed to send admin notification to channel {channel_id}: {e}"
                )
        else:
            logger.warning(
                f"Admin notification channel {channel_id} not found — message: {message}"
            )

    # -- Startup catchup -------------------------------------------------------

    @tasks.loop(count=1)
    async def initial_catchup(self):
        """Run once after bot startup to establish a baseline for the current week."""
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)  # Allow other cogs to finish initializing

        logger.info("Running initial reaction catchup after startup...")

        try:
            now = datetime.now(EASTERN)
            schedule = load_full_schedule()

            current_week = find_current_week(schedule, now, get_max_week())
            if not current_week:
                logger.info("Not in an active NFL week, skipping initial catchup")
                return

            deadline = get_week_deadline(current_week)
            if not deadline or now >= deadline:
                logger.info(f"Week {current_week} deadline passed, skipping initial catchup")
                return

            await self.process_week_reactions(current_week)
            self.last_catchup_time[current_week] = now
            logger.info(f"Initial reaction catchup complete for Week {current_week}")

        except Exception as e:
            logger.error(f"Error in initial_catchup: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_ready(self):
        """Start initial catchup when bot is ready."""
        if not self.initial_catchup.is_running():
            self.initial_catchup.start()

    # -- Admin command ---------------------------------------------------------

    @commands.command(name=CMD_FORCE_REACTION_CATCHUP)
    @commands.has_permissions(administrator=True)
    async def force_reaction_catchup(self, ctx, week_num: int = None):
        """
        Manually trigger a full reaction rebuild for a specific week.

        Clears all stored picks and rebuilds from current Discord reactions.
        Defaults to the current week if week_num is omitted.

        Examples:
            !force_reaction_catchup
            !force_reaction_catchup 18
        """
        max_week = get_max_week()

        if week_num is None:
            now = datetime.now(EASTERN)
            schedule = load_full_schedule()
            week_num = find_current_week(schedule, now, max_week)
            if week_num is None:
                await ctx.send("❌ Could not determine current week.")
                return

        if not (1 <= week_num <= max_week):
            await ctx.send(f"❌ Week number must be between 1 and {max_week}.")
            return

        await ctx.send(f"Rebuilding picks for Week {week_num}...")
        await self.process_week_reactions(week_num)
        self.last_catchup_time[week_num] = datetime.now(EASTERN)
        await ctx.send(f"✅ Completed rebuild for Week {week_num}.")



async def setup(bot):
    await bot.add_cog(ReactionCatchup(bot))
