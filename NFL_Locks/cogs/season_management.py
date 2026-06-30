"""Season Management - Migrate and manage season data."""

from discord.ext import commands
import logging
import os
from NFL_Locks.utils.schedule_utils import get_max_week, get_current_season
from NFL_Locks.utils.data_utils import update_leaderboard, DATA_DIR
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.command_names import CMD_REBUILD_LEADERBOARD, CMD_CURRENT_SEASON, CMD_ARCHIVE_SEASON, CMD_LIST_SEASONS

logger = logging.getLogger('cogs.season_management')


class SeasonManagement(commands.Cog):
    """Manages season transitions and data migration."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name=CMD_REBUILD_LEADERBOARD)
    @commands.has_permissions(administrator=True)
    async def rebuild_leaderboard(self, ctx, season_year: int = None):
        """Rebuild the leaderboard from the database for this server."""
        season = get_current_season()
        if season_year is None:
            season_year = season

        if season_year != season:
            await ctx.send(
                f"⚠️ Requested season {season_year} but DB only holds {season} data. "
                f"Showing current season."
            )
            season_year = season

        await ctx.send(f"Rebuilding leaderboard for {season_year} season...")

        db = get_db()
        guild_id = str(ctx.guild.id)
        user_scores: dict[str, int] = {}

        for week_num in range(1, get_max_week() + 1):
            winners = await db.get_winners(season_year, week_num)
            if not winners:
                continue

            picks = await db.get_picks_for_week(season_year, week_num, guild_id)
            winners_set = set(winners)

            losers: set[str] = set()
            for team, users in picks.items():
                if team not in winners_set:
                    losers.update(users)

            for team, users in picks.items():
                if team in winners_set:
                    for user in users:
                        if user not in losers:
                            user_scores[user] = user_scores.get(user, 0) + 1

        update_leaderboard(ctx.guild.id, user_scores, season_year)
        await ctx.send(
            f"✅ Leaderboard rebuilt with {len(user_scores)} users for {season_year} season!"
        )

    @commands.command(name=CMD_CURRENT_SEASON)
    @commands.has_permissions(administrator=True)
    async def current_season(self, ctx):
        """Show the current season year."""
        await ctx.send(f"Current NFL season: **{get_current_season()}**")

    @commands.command(name=CMD_ARCHIVE_SEASON)
    @commands.has_permissions(administrator=True)
    async def archive_season(self, ctx, season_year: int):
        """Manually archive a season's DB rows to JSON (normally runs automatically on Aug 1)."""
        db = get_db()
        idempotency_key = f"season_{season_year}_db_archived"

        already_archived = await db.get_bot_meta(idempotency_key)
        if already_archived:
            archive_dir = DATA_DIR / "archives" / str(season_year)
            await ctx.send(
                f"Season {season_year} was already archived on {already_archived}. "
                f"Files are at `{archive_dir}`."
            )
            return

        archive_dir = DATA_DIR / "archives" / str(season_year)
        await ctx.send(f"Archiving season {season_year} to `{archive_dir}`...")

        try:
            counts = await db.archive_and_purge_season(season_year, archive_dir)
        except Exception as e:
            logger.error(f"Manual archive failed for season {season_year}: {e}", exc_info=True)
            await ctx.send(f"Archive failed: {e}")
            return

        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).isoformat()
        await db.set_bot_meta(idempotency_key, timestamp)
        await db.set_bot_meta("off_season", "true")

        summary = ", ".join(f"{k}: {v}" for k, v in counts.items())
        await ctx.send(
            f"Season {season_year} archived.\n"
            f"Rows exported: {summary}\n"
            f"Files saved to: `{archive_dir}`"
        )

    @commands.command(name=CMD_LIST_SEASONS)
    @commands.has_permissions(administrator=True)
    async def list_seasons(self, ctx):
        """List the current active season and any archived seasons."""
        current_season = get_current_season()
        archives_dir = DATA_DIR / "archives"

        archived = []
        if archives_dir.exists():
            for item in os.listdir(archives_dir):
                if (archives_dir / item).is_dir() and item.isdigit():
                    archived.append(int(item))

        archived.sort(reverse=True)

        lines = [f"**Current active season:** {current_season}"]
        if archived:
            archived_list = "\n".join(f"- {s}" for s in archived)
            lines.append(f"\n**Archived seasons:**\n{archived_list}")
        else:
            lines.append("\nNo archived seasons found.")

        await ctx.send("\n".join(lines))


async def setup(bot):
    await bot.add_cog(SeasonManagement(bot))
