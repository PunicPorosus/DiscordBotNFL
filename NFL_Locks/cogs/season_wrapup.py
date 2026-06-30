"""Season Wrap-Up - Posts end-of-season statistics after Week 18"""

from discord.ext import commands
import logging
from NFL_Locks.utils.schedule_utils import get_max_week, get_current_season
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.command_names import CMD_POST_WRAPUP
from NFL_Locks.utils.rate_limiter import rate_limiter
from NFL_Locks.utils import scoring as scoring_mod

logger = logging.getLogger('cogs.season_wrapup')


class SeasonWrapup(commands.Cog):
    """Posts end-of-season statistics after the regular season ends."""

    def __init__(self, bot):
        self.bot = bot
        # NOTE: No background task here.
        # End-of-season detection and wrapup posting is owned entirely by
        # auto_tasks.py (dynamic_weekly_tasks), which calls
        # post_season_wrapup_to_channel() when it detects no next week exists.
        # Having a second independent task here was a source of duplicate posts.

    # -- Core logic ------------------------------------------------------------

    async def post_season_wrapup(self, channel):
        """Post comprehensive season statistics to a channel."""
        guild_id = str(channel.guild.id)
        season = get_current_season()
        db = get_db()
        max_week = get_max_week()

        # {user_name: {'total': int, 'weeks_with_points': int, 'weekly_scores': [(wk, pts)]}}
        user_stats: dict[str, dict] = {}

        scheme = await db.get_scoring_scheme(guild_id)

        for week_num in range(1, max_week + 1):
            winners = await db.get_winners(season, week_num)
            if not winners:
                continue

            picks = await db.get_picks_for_week(season, week_num, guild_id)
            week_scores = scoring_mod.compute_week_scores(picks, set(winners), scheme)

            for user, points in week_scores.items():
                if user not in user_stats:
                    user_stats[user] = {
                        'total': 0,
                        'weeks_with_points': 0,
                        'weekly_scores': [],
                    }
                user_stats[user]['total'] += points
                user_stats[user]['weeks_with_points'] += 1
                user_stats[user]['weekly_scores'].append((week_num, points))

        if not user_stats:
            await rate_limiter.send(channel, 
                f"**{season} Season Complete!**\n\n"
                f"No picks were recorded this season in {channel.guild.name}."
            )
            return

        # -- Awards ------------------------------------------------------------

        top_5 = sorted(
            user_stats.items(), key=lambda x: x[1]['total'], reverse=True
        )[:5]

        best_week_user, best_week, best_week_score = None, None, 0
        for user, stats in user_stats.items():
            for wk, score in stats['weekly_scores']:
                if score > best_week_score:
                    best_week_score = score
                    best_week = wk
                    best_week_user = user

        most_consistent = max(
            user_stats.items(),
            key=lambda x: x[1]['weeks_with_points'],
        ) if user_stats else None

        # -- Build message -----------------------------------------------------

        msg = (
            f"**{season} NFL Season Complete!**\n\n"
            f"The regular season has ended! Here are the final stats for "
            f"**{channel.guild.name}**:\n\n"
        )

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        msg += "**Top 5 Season Winners:**\n"
        for i, (user, stats) in enumerate(top_5):
            medal = medals[i] if i < len(medals) else f"{i+1}."
            msg += (
                f"{medal} **{user}** — {stats['total']} points "
                f"({stats['weeks_with_points']} weeks)\n"
            )

        if best_week_user:
            msg += (
                f"\n**Best Single Week Performance:**\n"
                f"**{best_week_user}** — {best_week_score} points (Week {best_week})\n"
            )

        if most_consistent:
            user, stats = most_consistent
            msg += (
                f"\n**Most Consistent Player:**\n"
                f"**{user}** — Scored in {stats['weeks_with_points']}/{max_week} weeks\n"
            )

        msg += "\nThank you for playing! See you next season!"

        await rate_limiter.send(channel, msg)
        logger.info(f"Posted season wrap-up for {channel.guild.name}")

    # _compute_scores removed — all scoring logic lives in utils/scoring.py

    # -- Admin command ---------------------------------------------------------

    @commands.command(name=CMD_POST_WRAPUP)
    @commands.has_permissions(administrator=True)
    async def post_wrapup(self, ctx):
        """Manually post season wrap-up statistics."""
        await ctx.send(f"Generating season wrap-up for {get_max_week()}-week season...")
        await self.post_season_wrapup(ctx.channel)
        await ctx.send("✅ Season wrap-up posted!")


async def setup(bot):
    await bot.add_cog(SeasonWrapup(bot))
