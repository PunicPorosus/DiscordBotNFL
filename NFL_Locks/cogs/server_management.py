"""
Server Management - Commands for admins to control bot functionality

Provides !start_locks and !end_locks commands for server administrators to:
- Enable/disable bot functions for their server
- Set up initial configuration
- Control participation in weekly picks
"""

from discord.ext import commands
import discord
import logging
from datetime import datetime, timedelta
from NFL_Locks.utils.constants import EASTERN
from NFL_Locks.utils.data_utils import load_full_schedule
from NFL_Locks.utils.schedule_utils import get_max_week, get_current_season
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.status_tracker import set_guild_channel, get_guild_channel
from NFL_Locks.utils.command_names import CMD_START_LOCKS, CMD_END_LOCKS, CMD_SERVER_STATUS

logger = logging.getLogger('cogs.server_management')


class ServerManagement(commands.Cog):
    """Manage server participation in NFL picks."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name=CMD_START_LOCKS)
    @commands.has_permissions(administrator=True)
    async def start_server(self, ctx):
        """Start NFL picks for this server (Admin only).

        1. Sets the current channel for automated posts.
        2. Posts matchups for the current week.
        3. Enables all automated features (results, leaderboards, locks).
        """
        guild_id = ctx.guild.id
        channel_id = ctx.channel.id
        admin = ctx.author

        await ctx.send("Getting setup. Please wait while I fire up the servers!")

        # Notify if moving from a different channel
        existing_channel_id = await get_guild_channel(guild_id)
        if existing_channel_id and existing_channel_id != channel_id:
            existing_channel = self.bot.get_channel(existing_channel_id)
            if existing_channel:
                try:
                    await existing_channel.send(
                        f"📢 **NFL Picks Moving to New Channel**\n\n"
                        f"NFL picks are now being managed in {ctx.channel.mention}.\n"
                        f"Reactions in this channel will no longer be tracked.\n\n"
                        f"Please head over to {ctx.channel.mention} to continue participating!"
                    )
                    await admin.send(
                        f"📢 **Migrating from {existing_channel.mention}**\n"
                        f"Notification sent to old channel."
                    )
                except discord.Forbidden:
                    await admin.send("📢 **Changing NFL Picks Channel**\nOld channel no longer accessible.")
            else:
                await admin.send("📢 **Changing NFL Picks Channel**\nOld channel no longer accessible.")
        elif existing_channel_id == channel_id:
            await admin.send(
                "This channel is already configured for NFL picks.\nReactivating..."
            )

        await admin.send(f"Starting NFL Picks for {ctx.guild.name}...")

        # Step 1: Register the channel
        from NFL_Locks.cogs.admin import load_server_channels, save_server_channels
        channels = load_server_channels()
        channels[guild_id] = channel_id
        save_server_channels(channels)
        await set_guild_channel(guild_id, channel_id, ctx.guild.name)
        await admin.send(f"✅ Step 1/4: Set {ctx.channel.mention} as the NFL picks channel.")

        # Step 2: Find current week
        schedule = load_full_schedule()
        now = datetime.now(EASTERN)
        current_week = None

        for wk in range(1, get_max_week() + 1):
            week_games = schedule.get(str(wk))
            if not week_games:
                continue
            first_game = datetime.fromisoformat(week_games[0]["date"]).replace(tzinfo=EASTERN)
            days_since_tuesday = (first_game.weekday() - 1) % 7
            week_start = first_game - timedelta(days=days_since_tuesday)
            week_end = week_start + timedelta(days=6, hours=23, minutes=59)
            if week_start <= now <= week_end:
                current_week = wk
                break

        if not current_week:
            await admin.send("❌ Could not determine current NFL week.")
            return

        week_number = current_week

        # Step 3: Check if games are already posted for this guild/channel
        db = get_db()
        season = get_current_season()
        messages_by_channel = await db.get_messages_for_week(season, week_number, str(guild_id))

        if messages_by_channel:
            await admin.send(
                f"✅ Week {week_number} matchups are already posted in this channel.\n"
                f"NFL picks are now active! Users can start reacting to pick winners."
            )
            logger.info(
                f"Server {ctx.guild.name} ({guild_id}) reactivated with existing Week {week_number} games"
            )
            return

        # Step 4: Post games
        games_manager = self.bot.get_cog('GamesManager')
        if not games_manager:
            await admin.send("❌ GamesManager cog not found — cannot post games.")
            return

        week_games = schedule.get(str(week_number))
        if not week_games:
            await admin.send(f"❌ No games found for Week {week_number}.")
            return

        await admin.send(f"✅ Step 2/4: Posting Week {week_number} matchups...")
        success = await games_manager.post_games_to_channel(ctx.channel, week_number, week_games)

        if not success:
            await admin.send("❌ Failed to post games.")
            return

        await admin.send(f"✅ Step 3/4: All set! NFL picks are now active for {ctx.guild.name}.")

        from NFL_Locks.utils.time_utils import get_week_deadline
        deadline = get_week_deadline(week_number)
        deadline_str = deadline.strftime("%A, %B %d at %I:%M %p ET") if deadline else "TBD"

        await admin.send(
            f"\n**Setup Complete!**\n\n"
            f"**Active Week:** Week {week_number}\n"
            f"**Channel:** {ctx.channel.mention}\n"
            f"**Deadline:** {deadline_str}\n\n"
            f"**What happens next:**\n"
            f"Users react to pick winners\n"
            f"Lock summary posts at deadline\n"
            f"Results post on Tuesdays\n"
            f"New games post automatically\n\n"
            f"**Admin Commands:**\n"
            f"`!end_locks` — Disable picks for this server\n"
            f"`!post_games <week>` — Post a specific week\n"
            f"`!server_status` — View server status"
        )
        logger.info(f"Server {ctx.guild.name} ({guild_id}) started with Week {week_number}")

    @commands.command(name=CMD_END_LOCKS)
    @commands.has_permissions(administrator=True)
    async def stop_server(self, ctx):
        """Stop NFL picks for this server (Admin only).

        Removes the configured channel and disables automated posts.
        Pick data is preserved and remains queryable.
        """
        guild_id = ctx.guild.id

        if not await get_guild_channel(guild_id):
            await ctx.send(
                "This server is not currently active.\n"
                "Use `!start_locks` to begin NFL picks."
            )
            return

        from NFL_Locks.cogs.admin import load_server_channels, save_server_channels
        channels = load_server_channels()
        if guild_id in channels:
            del channels[guild_id]
            save_server_channels(channels)

        # Channel removal is handled via DB — set_guild_channel to 0 is not
        # needed here; simply not having a row means the guild is inactive.
        # (The JSON status file is no longer used.)

        await ctx.send(
            "✅ **NFL picks stopped for this server.**\n\n"
            "Automated posts are now disabled.\n"
            "Your data has been preserved and can be viewed with commands like `!mypoints`.\n\n"
            "To restart, use `!start_locks` in any channel."
        )
        logger.info(f"Server {ctx.guild.name} ({guild_id}) stopped NFL picks")

    @commands.command(name=CMD_SERVER_STATUS)
    @commands.has_permissions(administrator=True)
    async def server_status(self, ctx):
        """Check the NFL picks status for this server."""
        guild_id = ctx.guild.id
        channel_id = await get_guild_channel(guild_id)

        if not channel_id:
            await ctx.send(
                "**NFL Picks: Inactive**\n\n"
                "This server is not currently participating in NFL picks.\n"
                "Use `!start_locks` to begin."
            )
            return

        channel = self.bot.get_channel(channel_id)
        channel_mention = channel.mention if channel else f"Channel ID: {channel_id} (not found)"

        # Determine current week
        schedule = load_full_schedule()
        now = datetime.now(EASTERN)
        current_week = None

        for wk in range(1, get_max_week() + 1):
            week_games = schedule.get(str(wk))
            if not week_games:
                continue
            first_game = datetime.fromisoformat(week_games[0]["date"]).replace(tzinfo=EASTERN)
            days_since_tuesday = (first_game.weekday() - 1) % 7
            week_start = first_game - timedelta(days=days_since_tuesday)
            week_end = week_start + timedelta(days=6, hours=23, minutes=59)
            if week_start <= now <= week_end:
                current_week = wk
                break

        # Check game posting status from DB
        current_week_status = "N/A"
        if current_week:
            db = get_db()
            season = get_current_season()
            messages_by_channel = await db.get_messages_for_week(
                season, current_week, str(guild_id)
            )
            current_week_status = "✅ Games posted" if messages_by_channel else "Not yet posted"

            from NFL_Locks.utils.time_utils import get_week_deadline
            deadline = get_week_deadline(current_week)
            if deadline and now >= deadline:
                current_week_status += " (Deadline passed)"

        status_msg = (
            f"**NFL Picks Status for {ctx.guild.name}**\n\n"
            f"**Status:** Active\n"
            f"**Channel:** {channel_mention}\n"
            f"**Current Week:** Week {current_week if current_week else 'N/A'}\n"
            f"**Week Status:** {current_week_status}\n\n"
            f"**Features Enabled:**\n"
            f"Automatic game posting (Tuesdays)\n"
            f"Automatic results posting (Tuesdays)\n"
            f"Lock summaries at deadline\n"
            f"Reaction tracking\n\n"
            f"**Admin Commands:**\n"
            f"`!end_locks` — Disable picks\n"
            f"`!post_games <week>` — Post specific week\n"
            f"`!force_reaction_catchup` — Sync reactions"
        )
        await ctx.send(status_msg)


async def setup(bot):
    await bot.add_cog(ServerManagement(bot))
