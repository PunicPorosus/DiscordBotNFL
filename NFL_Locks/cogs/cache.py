"""Message cache for fast week lookups."""

from discord.ext import commands, tasks
import logging
from NFL_Locks.utils.schedule_utils import get_current_season
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.command_names import CMD_REBUILD_CACHE, CMD_CACHE_STATS

logger = logging.getLogger('cogs.cache')


class Cache(commands.Cog):
    """Caches message IDs for fast week lookups."""

    def __init__(self, bot):
        self.bot = bot
        self.message_to_week_cache: dict[int, int] = {}  # {message_id: week_num}
        self._heartbeat_task.start()

    def cog_unload(self):
        self._heartbeat_task.cancel()

    # -- Heartbeat -------------------------------------------------------------

    @tasks.loop(minutes=1)
    async def _heartbeat_task(self):
        """Write a timestamp to the DB every minute so reconnect logic can
        measure how long the bot was actually offline."""
        try:
            await get_db().set_heartbeat()
        except Exception as e:
            logger.warning(f"Heartbeat write failed: {e}")

    @_heartbeat_task.before_loop
    async def _before_heartbeat(self):
        await self.bot.wait_until_ready()

    async def build_message_cache(self):
        """
        Populate the in-memory cache from the tracked_messages DB table.

        Replaces the old JSON file scan across all week files.
        """
        logger.info("Building message cache from DB...")
        db = get_db()
        season = get_current_season()
        self.message_to_week_cache = await db.get_all_tracked_messages(season)
        logger.info(f"Cached {len(self.message_to_week_cache)} message IDs")

    def get_week_from_cache(self, message_id: int) -> int | None:
        """Return the week number for a message ID, or None if not cached."""
        return self.message_to_week_cache.get(message_id)

    def add_to_cache(self, message_id: int, week_num: int):
        """Add a message to the in-memory cache."""
        self.message_to_week_cache[message_id] = week_num
        logger.debug(f"Added message {message_id} to cache for week {week_num}")

    def remove_from_cache(self, message_id: int) -> int | None:
        """Remove a message from the cache. Returns the week it was in, or None."""
        week_num = self.message_to_week_cache.pop(message_id, None)
        if week_num is not None:
            logger.debug(f"Removed message {message_id} (week {week_num}) from cache")
        return week_num

    def clear_cache(self):
        """Clear the entire cache."""
        count = len(self.message_to_week_cache)
        self.message_to_week_cache.clear()
        logger.info(f"Cleared {count} entries from cache")

    @commands.command(name=CMD_REBUILD_CACHE)
    @commands.has_permissions(administrator=True)
    async def rebuild_cache(self, ctx):
        """Manually rebuild the message cache from the database."""
        await ctx.send("Rebuilding message cache...")
        await self.build_message_cache()
        await ctx.send(f"✅ Cache rebuilt with {len(self.message_to_week_cache)} messages")

    @commands.command(name=CMD_CACHE_STATS)
    @commands.has_permissions(administrator=True)
    async def cache_stats(self, ctx):
        """Show cache statistics."""
        total = len(self.message_to_week_cache)

        # Count messages per week
        week_counts: dict[int, int] = {}
        for week_num in self.message_to_week_cache.values():
            week_counts[week_num] = week_counts.get(week_num, 0) + 1

        msg = "**Cache Statistics**\n"
        msg += f"Total cached messages: {total}\n\n"

        if week_counts:
            msg += "**Messages per week:**\n"
            for week in sorted(week_counts.keys()):
                msg += f"Week {week}: {week_counts[week]} messages\n"

        await ctx.send(msg)


async def setup(bot):
    await bot.add_cog(Cache(bot))
