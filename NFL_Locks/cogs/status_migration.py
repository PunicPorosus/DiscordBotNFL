"""Status Migration - DB-backed status diagnostics and admin tools"""

from discord.ext import commands
import logging
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.schedule_utils import get_current_season
from NFL_Locks.utils.command_names import CMD_BUILD_STATUS, CMD_STATUS_INFO, CMD_CLEAR_PENDING_WORK

logger = logging.getLogger('cogs.status_migration')


class StatusMigration(commands.Cog):
    """Status inspection and reset commands (owner only)."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name=CMD_BUILD_STATUS)
    @commands.is_owner()
    async def build_status(self, ctx):
        """Deprecated: status now lives in SQLite and is built automatically.

        The old JSON rebuild command is no longer needed.  This command
        remains registered so existing invocations don't error.
        """
        await ctx.send(
            "ℹ️ **`!build_status` is deprecated.**\n\n"
            "Status is now stored in SQLite and migrated automatically on "
            "first boot from `bot_status.json` (if it existed).\n"
            "No manual rebuild is needed."
        )

    @commands.command(name=CMD_STATUS_INFO)
    @commands.is_owner()
    async def status_info(self, ctx, week_number: int = None):
        """Show DB-backed status info for the current (or given) week (owner only).

        Usage:
          !status_info        — current week
          !status_info 15     — Week 15
        """
        try:
            from NFL_Locks.utils.status_tracker import get_pending_work
            season = get_current_season()
            db = get_db()

            guilds = await db.get_all_configured_guilds()
            guild_count = len(guilds)

            pending = await get_pending_work(week_number)

            if not pending:
                await ctx.send("No week status rows found in DB yet.")
                return

            lines = [f"**Status Tracker Info** (Season {season})\n"]
            lines.append(f"Configured guilds: **{guild_count}**\n")

            for wk, entry in sorted(pending.items()):
                lines.append(
                    f"**Week {wk}**\n"
                    f"  winners fetched:    {'✅' if not entry['needs_winners'] else '❌'}\n"
                    f"  deadline passed:    {'✅' if entry['deadline_passed'] else '—'}\n"
                    f"  reactions final:    {'✅' if entry['reactions_finalized'] else '—'}\n"
                    f"  guilds need games:  {len(entry['guilds_needing_games'])}\n"
                    f"  guilds need results:{len(entry['guilds_needing_results'])}\n"
                    f"  guilds need locks:  {len(entry['guilds_needing_locks'])}\n"
                )

            await ctx.send("\n".join(lines)[:2000])

        except Exception as e:
            await ctx.send(f"❌ Error loading status: {e}")
            logger.error(f"Error in status_info: {e}", exc_info=True)

    @commands.command(name=CMD_CLEAR_PENDING_WORK)
    @commands.is_owner()
    async def clear_pending_work(self, ctx, week_number: int = None):
        """Reset status flags in the DB for a week (owner only).

        Clears games_posted and locks_posted for all guilds, and resets
        week-level flags (winners_fetched, deadline_passed, reactions_finalized).

        Usage:
          !clear_pending_work 15   — reset Week 15
          !clear_pending_work      — reset the most recent tracked week
        """
        try:
            db = get_db()
            season = get_current_season()

            if week_number is None:
                async with db._conn.execute(
                    "SELECT MAX(week) FROM week_status WHERE season=?", (season,)
                ) as cur:
                    row = await cur.fetchone()
                week_number = row[0] if row and row[0] else None

            if week_number is None:
                await ctx.send("❌ No weeks found in DB to clear.")
                return

            # Reset week-level flags
            await db._conn.execute(
                """UPDATE week_status
                   SET winners_fetched=0, deadline_passed=0, reactions_finalized=0
                   WHERE season=? AND week=?""",
                (season, week_number),
            )
            # Reset per-guild flags
            await db._conn.execute(
                """UPDATE week_guild_status
                   SET games_posted=0, locks_posted=0
                   WHERE season=? AND week=?""",
                (season, week_number),
            )
            # Remove results_posted rows for this week so they can be re-posted
            await db._conn.execute(
                "DELETE FROM results_posted WHERE season=? AND week=?",
                (season, week_number),
            )
            await db._conn.commit()

            await ctx.send(f"✅ Cleared all status flags for Season {season} Week {week_number}.")
            logger.info(f"Status flags cleared for season {season} week {week_number} by {ctx.author}")

        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
            logger.error(f"Error in clear_pending_work: {e}", exc_info=True)


async def setup(bot):
    await bot.add_cog(StatusMigration(bot))
