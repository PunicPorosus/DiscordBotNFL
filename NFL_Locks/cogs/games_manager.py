# Games Manager - Handles posting weekly matchups to channels

from discord.ext import commands
import discord
import logging
import asyncio
from datetime import datetime
from NFL_Locks.utils.constants import EASTERN, NFL_TEAMS
from NFL_Locks.utils.data_utils import load_full_schedule
from NFL_Locks.utils.time_utils import get_week_deadline, is_deadline_passed
from NFL_Locks.utils.schedule_utils import (
    get_week_range_text, get_max_week, get_current_week_info, get_current_season
)
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.command_names import CMD_POST_GAMES, CMD_REPOST_GAMES
from NFL_Locks.utils.rate_limiter import rate_limiter

logger = logging.getLogger('cogs.games_manager')


class GamesManager(commands.Cog):
    """Manages posting game matchups to channels."""

    def __init__(self, bot):
        self.bot = bot

    def _get_current_week_info(self, now):
        """Determine current NFL week. Delegates to schedule_utils shared function."""
        current_week, _, _ = get_current_week_info(now)
        return current_week

    # -- Core logic ------------------------------------------------------------

    async def catchup_games(self):
        """Post games for the current week to any guilds that haven't received them."""
        logger.info("Checking if games need to be posted...")

        now = datetime.now(EASTERN)
        current_week = self._get_current_week_info(now)

        if not current_week:
            logger.debug("Not currently in an NFL week")
            return

        if is_deadline_passed(current_week):
            logger.info(f"Week {current_week} deadline has passed — skipping game post")
            return

        season = get_current_season()
        db = get_db()
        configured_guilds = await get_db().get_all_configured_guilds()

        # A guild needs games if it has no tracked messages for this week yet
        guilds_needing_games = [
            gid for gid, _cid in configured_guilds.items()
            if not await db.get_messages_for_week(season, current_week, str(gid))
        ]

        if not guilds_needing_games:
            logger.debug(f"Week {current_week} games already posted to all guilds")
            return

        logger.info(
            f"Week {current_week} games need posting to {len(guilds_needing_games)} guild(s)"
        )

        schedule = load_full_schedule()
        week_games = schedule.get(str(current_week))
        if not week_games:
            logger.warning(f"No games found in schedule for week {current_week}")
            return

        for guild_id in guilds_needing_games:
            channel_id = configured_guilds[guild_id]
            channel = self.bot.get_channel(channel_id)
            if not channel:
                logger.warning(f"Channel {channel_id} not found for guild {guild_id}")
                continue
            await self.post_games_to_channel(channel, current_week, week_games)

    async def post_games_to_channel(self, channel, week_number, matchups):
        """
        Post game matchups to a channel with team emoji reactions.

        Existing tracked messages for this week/guild are deleted from Discord
        and cleared from the DB before new ones are posted.
        """
        try:
            logger.info(f"Posting games to #{channel.name} for week {week_number}")

            season = get_current_season()
            db = get_db()
            guild_id = str(channel.guild.id)
            cache_cog = self.bot.get_cog('Cache')

            # -- Clean up existing messages -------------------------------------
            existing = await db.get_messages_for_week(season, week_number, guild_id)
            if existing:
                all_ids = [mid for mids in existing.values() for mid in mids]
                logger.info(f"Cleaning up {len(all_ids)} old messages")
                for message_id in all_ids:
                    try:
                        msg = await channel.fetch_message(message_id)
                        await msg.delete()
                        if cache_cog:
                            cache_cog.remove_from_cache(message_id)
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.error(f"Error deleting message {message_id}: {e}")
                    await asyncio.sleep(0.5)

            await db.clear_tracked_messages_for_week(season, week_number, guild_id)

            # -- Post new matchups ----------------------------------------------
            deadline = get_week_deadline(week_number)
            deadline_str = (
                deadline.strftime("%A, %B %d at %I:%M %p ET") if deadline else ""
            )

            await rate_limiter.send(channel, f"**Week {week_number} Matchups! React to pick winners:**")

            channel_id = str(channel.id)
            new_count = 0

            for game in matchups:
                try:
                    away, home = game["away"], game["home"]
                    message = await rate_limiter.send(channel, f"{away} @ {home}")

                    if away in NFL_TEAMS:
                        await message.add_reaction(NFL_TEAMS[away])
                    if home in NFL_TEAMS:
                        await message.add_reaction(NFL_TEAMS[home])

                    await db.add_tracked_message(
                        message_id=message.id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        season=season,
                        week=week_number,
                        team_a=away,
                        team_b=home,
                    )
                    if cache_cog:
                        cache_cog.add_to_cache(message.id, week_number)

                    new_count += 1
                except Exception as e:
                    logger.error(f"Error posting matchup {game}: {e}")

            logger.info(f"Tracked {new_count} new messages for week {week_number}")

            # -- Footer --------------------------------------------------------
            if deadline_str:
                await rate_limiter.send(channel, 
                    f"Week {week_number} posted!\n"
                    f"**Submissions close at {deadline_str}**\n"
                    "React with team emojis to make your picks!\n"
                    "Any wrong picks lead to zero points for the week!"
                )
            else:
                await rate_limiter.send(channel, 
                    f"Week {week_number} posted! React with team emojis to make your picks!\n"
                    "Any wrong picks lead to zero points for the week!\n"
                    "Use !mypoints (once per day) to see your point breakdown and total!"
                )

            return True

        except Exception as e:
            logger.error(f"Error in post_games_to_channel: {e}", exc_info=True)
            return False

    # -- Admin commands --------------------------------------------------------

    @commands.command(name=CMD_POST_GAMES)
    @commands.has_permissions(administrator=True)
    async def post_games(self, ctx, week_num: int = None):
        """Manually post games for a specific week."""
        if week_num is None:
            week_num = self._get_current_week_info(datetime.now(EASTERN))
            if week_num is None:
                await ctx.send("❌ Could not determine current week.")
                return

        if not (1 <= week_num <= get_max_week()):
            await ctx.send(f"❌ Week number must be between {get_week_range_text()}.")
            return

        schedule = load_full_schedule()
        week_games = schedule.get(str(week_num))
        if not week_games:
            await ctx.send(f"❌ No games found for Week {week_num}.")
            return

        await ctx.send(f"Posting games for Week {week_num}...")
        success = await self.post_games_to_channel(ctx.channel, week_num, week_games)

        if success:
            await ctx.send(f"✅ Posted {len(week_games)} games for Week {week_num}.")
        else:
            await ctx.send(f"❌ Failed to post games for Week {week_num}.")

    @commands.command(name=CMD_REPOST_GAMES)
    @commands.has_permissions(administrator=True)
    async def repost_games(self, ctx, week_num: int = None):
        """Delete old game messages and repost them."""
        if week_num is None:
            week_num = self._get_current_week_info(datetime.now(EASTERN))
            if week_num is None:
                await ctx.send("❌ Could not determine current week.")
                return

        schedule = load_full_schedule()
        week_games = schedule.get(str(week_num))
        if not week_games:
            await ctx.send(f"❌ No games found for Week {week_num}.")
            return

        await ctx.send(f"Reposting games for Week {week_num}...")
        success = await self.post_games_to_channel(ctx.channel, week_num, week_games)

        if success:
            await ctx.send(f"✅ Reposted {len(week_games)} games for Week {week_num}.")
        else:
            await ctx.send(f"❌ Failed to repost games for Week {week_num}.")


async def setup(bot):
    await bot.add_cog(GamesManager(bot))
