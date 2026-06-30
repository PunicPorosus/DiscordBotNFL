from discord.ext import commands
import traceback
from NFL_Locks.utils.constants import NFL_TEAMS
from NFL_Locks.utils.data_utils import load_full_schedule
from NFL_Locks.utils.time_utils import EASTERN, get_week_deadline
from NFL_Locks.utils.schedule_utils import get_max_week, get_current_season
from NFL_Locks.utils.database import get_db
from datetime import datetime, timedelta
from NFL_Locks.utils.command_names import CMD_GAMES, CMD_FETCH_WINNERS_LEGACY, CMD_TEST_API


class Games(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name=CMD_GAMES)
    @commands.has_permissions(administrator=True)
    async def games(self, ctx):
        """Post current NFL week matchups with reactions."""
        print(f"[GAMES] !games command invoked by {ctx.author.name}")
        today = datetime.now(EASTERN)
        print(f"[GAMES] Current date/time: {today.strftime('%Y-%m-%d %I:%M %p ET')}")
        schedule = load_full_schedule()
        print(f"[GAMES] Loaded schedule with {len(schedule) if schedule else 0} weeks")

        print(f"[GAMES] Checking weeks 1-{get_max_week()} for current week...")
        for wk in range(1, get_max_week() + 1):
            week_games = schedule.get(str(wk))
            if not week_games:
                print(f"[GAMES] Week {wk}: No games found")
                continue

            first_game = datetime.fromisoformat(week_games[0]["date"]).replace(tzinfo=EASTERN)
            days_since_tuesday = (first_game.weekday() - 1) % 7
            week_start = first_game - timedelta(days=days_since_tuesday)
            week_end = week_start + timedelta(days=6, hours=23, minutes=59)

            print(f"[GAMES] Week {wk}: {week_start.strftime('%Y-%m-%d')} to {week_end.strftime('%Y-%m-%d')}")

            if week_start <= today <= week_end:
                print(f"[GAMES] Found current week: {wk} with {len(week_games)} games")
                await self.setup_week(ctx, wk, week_games)
                return

        print("[GAMES] No current week found")
        await ctx.send("❌ Could not determine current week.")

    async def setup_week(self, ctx, week_number, matchups):
        """Post games for the week and track message IDs in SQLite."""
        print(f"[GAMES] setup_week called for week {week_number} with {len(matchups)} matchups")
        try:
            season = get_current_season()
            db = get_db()
            guild_id = str(ctx.guild.id)
            channel_id = str(ctx.channel.id)
            cache_cog = self.bot.get_cog('Cache')

            # Clear any stale tracked messages for this week/guild before re-posting
            await db.clear_tracked_messages_for_week(season, week_number, guild_id)
            print(f"[GAMES] Cleared stale tracked messages for week {week_number}, guild {guild_id}")

            deadline = get_week_deadline(week_number)
            deadline_str = deadline.strftime("%A, %B %d at %I:%M %p ET") if deadline else ""

            print(f"[GAMES] Sending week {week_number} header message")
            await ctx.send(f"**Week {week_number} Matchups! React to pick winners:**")

            print(f"[GAMES] Starting to post {len(matchups)} matchups")
            for i, game in enumerate(matchups):
                away, home = game["away"], game["home"]
                print(f"[GAMES] Posting game {i+1}/{len(matchups)}: {away} @ {home}")
                msg = await ctx.send(f"{away} @ {home}")
                await msg.add_reaction(NFL_TEAMS[away])
                await msg.add_reaction(NFL_TEAMS[home])

                await db.add_tracked_message(
                    message_id=msg.id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    season=season,
                    week=week_number,
                )
                if cache_cog:
                    cache_cog.add_to_cache(msg.id, week_number)

                print(f"[GAMES] Tracked message {msg.id} for {away} @ {home}")

            print(f"[GAMES] All {len(matchups)} matchups posted and tracked")

            if deadline_str:
                await ctx.send(
                    f"Week {week_number} posted!\n"
                    f"**Submissions close at {deadline_str}**\n"
                    "React with team emojis to make your picks!\n"
                    "Use !mypoints (once per day) to see your point breakdown and total!"
                )
            else:
                await ctx.send(
                    f"Week {week_number} posted! React with team emojis to make your picks!\n"
                    "Use !mypoints (once per day) to see your point breakdown and total!"
                )

        except Exception as e:
            print(f"[GAMES] ERROR in setup_week: {e}")
            traceback.print_exc()
            await ctx.send(f"❌ Error posting games: {e}")

    @commands.command(name=CMD_FETCH_WINNERS_LEGACY)
    @commands.has_permissions(administrator=True)
    async def fetch_winners_legacy(self, ctx, week_number: int):
        """Manually fetch winners from ESPN API (Legacy — use !fetch_winners instead)."""
        from NFL_Locks.utils.espn_api import fetch_nfl_winners

        await ctx.send(
            f"This is the legacy command. Use `!fetch_winners {week_number}` instead.\n"
            f"Fetching winners for Week {week_number}..."
        )

        winners = await fetch_nfl_winners(week_number, season=season)
        if not winners:
            await ctx.send(
                f"❌ Could not fetch winners for Week {week_number}. "
                "Games may not be completed yet."
            )
            return

        season = get_current_season()
        db = get_db()
        await db.set_winners(season, week_number, winners)
        await ctx.send(f"**Week {week_number} Winners Saved:** {', '.join(winners)}")

    @commands.command(name=CMD_TEST_API)
    @commands.has_permissions(administrator=True)
    async def test_api(self, ctx, week_number: int):
        """Test ESPN API connection for a specific week."""
        from NFL_Locks.utils.espn_api import fetch_nfl_winners

        await ctx.send(f"Testing ESPN API for Week {week_number}...")
        winners = await fetch_nfl_winners(week_number, season=season)

        if winners:
            await ctx.send(f"✅ **ESPN API Response:** {', '.join(winners)}")
        else:
            await ctx.send(
                "❌ No data returned. Games may not be completed or API error occurred."
            )


async def setup(bot):
    await bot.add_cog(Games(bot))
