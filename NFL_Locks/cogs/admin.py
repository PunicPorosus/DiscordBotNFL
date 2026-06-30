from discord.ext import commands
import logging
import time
from pathlib import Path
from NFL_Locks.utils.constants import OWNER_ID, NFL_TEAMS
from NFL_Locks.utils.schedule_utils import get_max_week, get_current_season
from NFL_Locks.utils.database import get_db, DB_PATH
from NFL_Locks.utils.status_tracker import set_guild_channel, get_guild_channel
from NFL_Locks.utils.command_names import (
    CMD_SET_CHANNEL, CMD_CHECK_CHANNEL, CMD_LIST_SERVERS,
    CMD_FIX_PICK, CMD_SHOW_PICKS, CMD_BOT_STATUS,
    CMD_SET_SCORING_SCHEME, CMD_CHECK_SCORING_SCHEME,
)
from NFL_Locks.utils.scoring import VALID_SCHEMES, SCHEME_LABELS

logger = logging.getLogger('cogs.admin')


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name=CMD_SET_CHANNEL)
    @commands.has_permissions(administrator=True)
    async def set_channel(self, ctx):
        """Set the current channel for automated results posting."""
        await set_guild_channel(ctx.guild.id, ctx.channel.id, ctx.guild.name)
        await ctx.send(f"✅ Set **#{ctx.channel.name}** as the results channel for this server.")

    @commands.command(name=CMD_CHECK_CHANNEL)
    @commands.has_permissions(administrator=True)
    async def check_channel(self, ctx):
        """Show which channel is set for this server."""
        channel_id = await get_guild_channel(ctx.guild.id)
        if channel_id:
            channel = self.bot.get_channel(channel_id)
            if channel:
                await ctx.send(f"Results will post in **#{channel.name}**")
            else:
                await ctx.send("❌ Stored channel not found. Run `!set_channel` to fix.")
        else:
            await ctx.send("❌ No results channel configured. Run `!set_channel` here.")

    @commands.command(name=CMD_LIST_SERVERS)
    @commands.has_permissions(administrator=True)
    async def list_servers(self, ctx):
        """List all servers and their configured channels (owner only)."""
        if ctx.author.id != OWNER_ID:
            await ctx.send("❌ Only the bot owner can use this command.")
            return

        guilds = await get_db().get_all_configured_guilds()
        if not guilds:
            await ctx.send("No servers configured yet.")
            return

        text = []
        for gid, cid in guilds.items():
            guild = self.bot.get_guild(int(gid))
            channel = self.bot.get_channel(cid)
            if guild and channel:
                text.append(f"**{guild.name}** -> #{channel.name}")
        await ctx.send("\n".join(text) or "No valid entries found.")

    # -- Pick correction ----------------------------------------------------

    @commands.command(name=CMD_FIX_PICK)
    @commands.has_permissions(administrator=True)
    async def fix_pick(self, ctx, action: str, username: str, team: str, week_num: int):
        """
        Manually add or remove a pick for a user by display name.

        Usage:
          !fix_pick add    porosus KC 15
          !fix_pick remove porosus KC 15

        team     - 2-3 letter abbreviation (KC, SF, NE, WSH, etc.)
        week_num - 1 through 18

        Picks are stored by username (legacy style) so they coexist with
        reaction-sourced picks without colliding on the UNIQUE constraint.
        """
        action = action.lower()
        if action not in ("add", "remove"):
            await ctx.send(
                "❌ First argument must be `add` or `remove`.\n"
                "Example: `!fix_pick add porosus KC 15`"
            )
            return

        team = team.upper()
        if team not in NFL_TEAMS:
            valid = ", ".join(sorted(NFL_TEAMS.keys()))
            await ctx.send(
                f"❌ Unknown team `{team}`.\n"
                f"Valid abbreviations: {valid}"
            )
            return

        max_week = get_max_week()
        if not (1 <= week_num <= max_week):
            await ctx.send(f"❌ Week must be between 1 and {max_week}.")
            return

        db = get_db()
        season = get_current_season()
        guild_id = ctx.guild.id

        if action == "add":
            success, message = await db.admin_add_pick(
                season=season,
                week=week_num,
                guild_id=guild_id,
                user_name=username,
                team=team,
            )
            prefix = "✅ " if success else ""
            await ctx.send(f"{prefix}{message}")
        else:
            success, message = await db.admin_remove_pick(
                season=season,
                week=week_num,
                guild_id=guild_id,
                user_name=username,
                team=team,
            )
            prefix = "✅ " if success else ""
            await ctx.send(f"{prefix}{message}")

    @commands.command(name=CMD_SHOW_PICKS)
    @commands.has_permissions(administrator=True)
    async def show_picks(self, ctx, username: str, week_num: int):
        """
        Show all picks recorded for a user in a given week.

        Usage: !show_picks porosus 15
        """
        max_week = get_max_week()
        if not (1 <= week_num <= max_week):
            await ctx.send(f"❌ Week must be between 1 and {max_week}.")
            return

        db = get_db()
        season = get_current_season()
        guild_id = ctx.guild.id

        user_picks = await db.get_user_picks_by_name(
            season=season,
            week=week_num,
            guild_id=guild_id,
            user_name=username,
        )

        if not user_picks:
            await ctx.send(f"**{username}** has no picks recorded for Week {week_num}.")
            return

        picks_str = ", ".join(f"`{t}`" for t in sorted(user_picks))
        await ctx.send(f"**{username}** — Week {week_num} picks: {picks_str}")


    # -- Scoring scheme -----------------------------------------------------

    @commands.command(name=CMD_SET_SCORING_SCHEME)
    @commands.has_permissions(administrator=True)
    async def set_scoring_scheme(self, ctx, scheme: str):
        """
        Set the scoring scheme for this server.

        Available schemes:
          all_or_nothing  — correct picks only count if you had zero wrong picks
          additive        — +1 per correct pick, -1 per wrong pick (can go negative)

        Usage: !set_scoring_scheme additive
        """
        scheme = scheme.lower()
        if scheme not in VALID_SCHEMES:
            valid_list = ", ".join(f"`{s}`" for s in sorted(VALID_SCHEMES))
            await ctx.send(
                f"❌ Unknown scheme `{scheme}`. Valid options: {valid_list}"
            )
            return

        db = get_db()
        channel_id = await db.get_guild_channel(ctx.guild.id)
        if not channel_id:
            await ctx.send(
                "❌ This server has no channel configured yet. "
                "Run `!set_channel` first."
            )
            return

        await db.set_scoring_scheme(ctx.guild.id, scheme)
        label = SCHEME_LABELS.get(scheme, scheme)
        await ctx.send(
            f"✅ Scoring scheme for **{ctx.guild.name}** set to **{label}**.\n"
            f"This will take effect on the next results post."
        )

    @commands.command(name=CMD_CHECK_SCORING_SCHEME)
    @commands.has_permissions(administrator=True)
    async def check_scoring_scheme(self, ctx):
        """Show the current scoring scheme for this server."""
        db = get_db()
        scheme = await db.get_scoring_scheme(ctx.guild.id)
        label = SCHEME_LABELS.get(scheme, scheme)
        await ctx.send(f"**{ctx.guild.name}** is using **{label}** scoring.")

    @commands.command(name=CMD_BOT_STATUS)
    @commands.is_owner()
    async def botstatus(self, ctx):
        """
        Owner-only diagnostic snapshot of the bot's live state.

        Reports: environment, uptime, season/week, DB size, configured guilds,
        in-memory cache sizes, cooldown dict sizes, and AutoTasks loop state.
        """
        lines = []

        # -- Environment & uptime ----------------------------------------------
        env = getattr(self.bot, "env_label", "UNKNOWN")
        start = getattr(self.bot, "_start_time", None)
        if start is not None:
            elapsed = time.monotonic() - start
            h, rem = divmod(int(elapsed), 3600)
            m, s = divmod(rem, 60)
            uptime_str = f"{h}h {m}m {s}s"
        else:
            uptime_str = "unknown"

        lines.append(f"**Environment:** {env}")
        lines.append(f"**Uptime:** {uptime_str}")
        lines.append("")

        # -- Season / week -----------------------------------------------------
        season = get_current_season()
        max_week = get_max_week()
        lines.append(f"**Season:** {season}  |  **Max week:** {max_week}")
        lines.append("")

        # -- Database ----------------------------------------------------------
        db_path = DB_PATH
        if db_path.exists():
            size_kb = db_path.stat().st_size / 1024
            lines.append(f"**DB file:** {size_kb:.1f} KB  (`{db_path.name}`)")
        else:
            lines.append("**DB file:** not found")

        # -- Configured guilds -------------------------------------------------
        try:
            guilds = await get_db().get_all_configured_guilds()
            lines.append(f"**Configured guilds:** {len(guilds)}")
            for gid, cid in guilds.items():
                guild_obj = self.bot.get_guild(int(gid))
                channel_obj = self.bot.get_channel(cid)
                g_name = guild_obj.name if guild_obj else f"id:{gid}"
                c_name = f"#{channel_obj.name}" if channel_obj else f"id:{cid}"
                lines.append(f"  • {g_name} → {c_name}")
        except Exception as e:
            lines.append(f"**Configured guilds:** error ({e})")
        lines.append("")

        # -- Cache cog ---------------------------------------------------------
        cache_cog = self.bot.get_cog("Cache")
        if cache_cog:
            cache_size = len(getattr(cache_cog, "message_to_week_cache", {}))
            lines.append(f"**Cache cog** — message→week entries: {cache_size}")
        else:
            lines.append("**Cache cog:** not loaded")

        # -- Reactions cog -----------------------------------------------------
        reactions_cog = self.bot.get_cog("Reactions")
        if reactions_cog:
            msg_cd  = len(getattr(reactions_cog, "message_cooldowns", {}))
            dm_cd   = len(getattr(reactions_cog, "dm_cooldowns", {}))
            in_prog = len(getattr(reactions_cog, "processing_reactions", set()))
            lines.append(
                f"**Reactions cog** — msg cooldowns: {msg_cd} | "
                f"DM cooldowns: {dm_cd} | in-flight: {in_prog}"
            )
        else:
            lines.append("**Reactions cog:** not loaded")

        # -- Rate limiter ------------------------------------------------------
        try:
            from NFL_Locks.utils.rate_limiter import rate_limiter
            channel_count = len(rate_limiter._last_send)
            lines.append(f"**Rate limiter** — tracked channels: {channel_count}")
        except Exception:
            lines.append("**Rate limiter:** unavailable")

        # -- AutoTasks cog -----------------------------------------------------
        auto_cog = self.bot.get_cog("AutoTasks")
        if auto_cog:
            loop_running = auto_cog.dynamic_weekly_tasks.is_running()
            last_week    = auto_cog.last_posted_week or "none this session"
            lines.append(
                f"**AutoTasks** — loop running: {loop_running} | "
                f"last posted week: {last_week}"
            )
        else:
            lines.append("**AutoTasks cog:** not loaded")

        # -- Loaded extensions -------------------------------------------------
        lines.append("")
        lines.append(f"**Loaded extensions:** {len(self.bot.extensions)}")

        await ctx.send("\n".join(lines))


async def setup(bot):
    await bot.add_cog(Admin(bot))
