"""User Points - Allow users to check their own points."""

from discord.ext import commands
import logging
from datetime import datetime, timedelta
from NFL_Locks.utils.constants import EASTERN
from NFL_Locks.utils.schedule_utils import get_current_season
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.command_names import CMD_MYPOINTS, CMD_RESET_COOLDOWNS

logger = logging.getLogger('cogs.user_points')


class UserPoints(commands.Cog):
    """Allows users to check their own points."""

    def __init__(self, bot):
        self.bot = bot
        self.last_check: dict[int, datetime] = {}  # {user_id: datetime}

    @commands.command(name=CMD_MYPOINTS)
    async def mypoints(self, ctx):
        """Check your own points with a weekly breakdown (once per 24 hours)."""
        user = ctx.author
        now = datetime.now(EASTERN)

        # Off-season check — doesn't count against the 24-hour cooldown
        off_season = await get_db().get_bot_meta("off_season")
        if off_season == "true":
            await ctx.send(
                f"{user.mention} The season hasn't started yet, you silly goose."
            )
            return

        # Enforce 24-hour cooldown
        last = self.last_check.get(user.id)
        if last is not None:
            elapsed = now - last
            if elapsed < timedelta(hours=24):
                hours_left = 24 - elapsed.total_seconds() / 3600
                await ctx.send(
                    f"{user.mention} You can check your points again in {hours_left:.1f} hours."
                )
                return

        self.last_check[user.id] = now

        season = get_current_season()
        guild_id = str(ctx.guild.id)
        db = get_db()

        scheme = await db.get_scoring_scheme(guild_id)

        # Check participation before computing points — a user can have zero or
        # negative total under additive scoring and still have played all season.
        pick_count = await db.get_user_season_pick_count(
            season=season, guild_id=guild_id, user_id=str(user.id)
        )
        if pick_count == 0:
            await ctx.send(
                f"{user.mention} No picks found for you this season."
            )
            return

        weekly_points, total = await db.calculate_user_points(
            season=season,
            guild_id=guild_id,
            user_id=str(user.id),
            scheme=scheme,
        )

        if not weekly_points:
            await ctx.send(
                f"{user.mention} You've made picks this season but no week results "
                f"have been posted yet."
            )
            return

        breakdown = "\n".join(
            f"Week {wk}: {'+' if pts > 0 else ''}{pts}"
            for wk, pts in sorted(weekly_points.items())
        )
        response = (
            f"**{user.display_name}'s {season} Season Points**\n\n"
            f"{breakdown}\n\n"
            f"**Total: {total} points**"
        )
        await ctx.send(response)

    @commands.command(name=CMD_RESET_COOLDOWNS)
    @commands.has_permissions(administrator=True)
    async def reset_cooldowns(self, ctx):
        """Reset all mypoints cooldowns (Admin only)."""
        self.last_check.clear()
        await ctx.send("All mypoints cooldowns have been reset.")


async def setup(bot):
    await bot.add_cog(UserPoints(bot))
