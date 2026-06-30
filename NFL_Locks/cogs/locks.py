"""
Locks cog — posts a pick summary when a week's reaction deadline arrives,
then marks that week/guild as locked so the catchup won't re-post it.
"""

from discord.ext import commands, tasks
from datetime import datetime
from NFL_Locks.utils.time_utils import get_week_deadline, EASTERN
from NFL_Locks.utils.data_utils import load_full_schedule
from NFL_Locks.utils.schedule_utils import get_max_week, get_current_season
from NFL_Locks.utils.database import get_db
import logging
from NFL_Locks.utils.rate_limiter import rate_limiter

logger = logging.getLogger('cogs.locks')


class Locks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_lock_times.start()

    def cog_unload(self):
        self.check_lock_times.cancel()

    # -- Timer task ------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def check_lock_times(self):
        """Every 5 minutes, check if a week's reaction lock time has arrived."""
        now = datetime.now(EASTERN)
        schedule = load_full_schedule()

        for wk in range(1, get_max_week() + 1):
            if not schedule.get(str(wk)):
                continue

            lock_time = get_week_deadline(wk)
            if not lock_time:
                continue

            # Fire within the 5-minute window after the deadline
            if 0 <= (now - lock_time).total_seconds() < 300:
                logger.info(f"Deadline reached for Week {wk} — posting lock summaries")
                await self.lock_reactions_for_week(wk)

    # -- Core lock method ------------------------------------------------------

    async def lock_reactions_for_week(
        self,
        week_number: int,
        guild_id: int | str | None = None,
        channel=None,
    ):
        """
        Post a pick-lock summary for the given week.

        Called two ways:
          - From check_lock_times with just week_number → posts to all configured guilds.
          - From startup_coordinator with (week_number, guild_id, channel) → posts to
            one specific guild/channel (catch-up for a missed lock).
        """
        db = get_db()
        season = get_current_season()

        if channel is not None and guild_id is not None:
            # Single-guild path (startup catchup)
            await self._post_lock_summary(db, season, week_number, guild_id, channel)
            await db.mark_locks_posted(season, week_number, int(guild_id))
        else:
            # All-guilds path (live timer)
            configured_guilds = await get_db().get_all_configured_guilds()
            for gid, cid in configured_guilds.items():
                ch = self.bot.get_channel(cid)
                if ch:
                    await self._post_lock_summary(db, season, week_number, gid, ch)
                    await db.mark_locks_posted(season, week_number, int(gid))
                else:
                    logger.warning(f"Channel {cid} not found for guild {gid} during lock")

        logger.info(f"Week {week_number} lock summaries posted")

    # -- Formatting helper -----------------------------------------------------

    async def _post_lock_summary(self, db, season: int, week_number: int, guild_id, channel):
        """Fetch picks from DB and post the formatted lock summary."""
        picks = await db.get_picks_for_week(season, week_number, guild_id)

        if not picks:
            await rate_limiter.send(channel,
                f"**Week {week_number} — Picks Locked!**\n"
                f"No picks were recorded for this week."
            )
            return

        # Pivot {team: [user_names]} → {user_name: [teams]} for user-centric display
        user_picks: dict[str, list[str]] = {}
        for team, users in picks.items():
            for user in users:
                user_picks.setdefault(user, []).append(team)

        lines = [f"**Week {week_number} — Picks Locked!**\n"]
        for user in sorted(user_picks.keys()):
            teams = sorted(user_picks[user])
            lines.append(f"**{user}**: {', '.join(teams)}")

        # Discord has a 2000-char limit; chunk if needed
        message = "\n".join(lines)
        if len(message) <= 2000:
            await rate_limiter.send(channel, message)
        else:
            # Send header first, then user chunks
            await rate_limiter.send(channel, lines[0])
            chunk, chunk_len = [], 0
            for line in lines[1:]:
                if chunk_len + len(line) + 1 > 1900:
                    await rate_limiter.send(channel, "\n".join(chunk))
                    chunk, chunk_len = [], 0
                chunk.append(line)
                chunk_len += len(line) + 1
            if chunk:
                await rate_limiter.send(channel, "\n".join(chunk))


async def setup(bot):
    await bot.add_cog(Locks(bot))
