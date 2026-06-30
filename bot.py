import os
import sys
import logging
import asyncio
import traceback
from pathlib import Path
from dotenv import load_dotenv

import discord
from discord.ext import commands

# -- Logging must be configured before any other import that touches logging --
from BotUtils.logging_setup import setup_logging
log_file = setup_logging()

# -- Bot-wide utilities -------------------------------------------------------
from BotUtils.notify import notify_admin

# -- Environment selection ----------------------------------------------------
# Run with no arguments to use .env (testing bot).
# Run with "prod" to use production.env (production bot).
_ENV_ARG = sys.argv[1].lower() if len(sys.argv) > 1 else ""
if _ENV_ARG == "prod":
    _ENV_FILE = Path(__file__).parent / "production.env"
    _ENV_LABEL = "PRODUCTION"
else:
    _ENV_FILE = Path(__file__).parent / ".env"
    _ENV_LABEL = "TESTING"

if not _ENV_FILE.exists():
    raise FileNotFoundError(f"Environment file not found: {_ENV_FILE}")

load_dotenv(_ENV_FILE)
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError(f"DISCORD_TOKEN not set in {_ENV_FILE}")

# Logger for bot.py startup messages — maps to STARTUP designator in log output
logger = logging.getLogger("startup")

logger.info(f"Starting in {_ENV_LABEL} mode — env file: {_ENV_FILE.name}")

# Override print() so any stray print() calls in bot.py also land in the log
_original_print = print
def print(*args, **kwargs):
    _original_print(*args, **kwargs)
    logger.info(" ".join(str(a) for a in args))

# -- Intents ------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Metadata stamped onto the bot object so any cog can read it without
# importing bot.py directly.
bot.env_label: str = _ENV_LABEL          # "TESTING" or "PRODUCTION"
bot._start_time: float | None = None     # set in on_ready (monotonic clock)

# Extensions that failed to load are collected during startup so on_ready
# can forward them to the bot-log channel once the bot is connected.
bot._failed_extensions: list[str] = []

# -- Extension list -----------------------------------------------------------
initial_extensions = [
    "NFL_Locks.cogs.admin",
    "NFL_Locks.cogs.cache",              # Load first for message cache
    "NFL_Locks.cogs.winners",            # Winner management
    "NFL_Locks.cogs.games_manager",      # Game posting
    "NFL_Locks.cogs.results_manager",    # Results posting
    "NFL_Locks.cogs.reactions",          # Updated to use Cache cog
    "NFL_Locks.cogs.games",
    "NFL_Locks.cogs.results",
    "NFL_Locks.cogs.auto_tasks",
    "NFL_Locks.cogs.locks",
    "NFL_Locks.cogs.startup_coordinator",
    "NFL_Locks.cogs.reaction_catchup",
    "NFL_Locks.cogs.schedule_update",
    "NFL_Locks.cogs.status_migration",   # Status tracker management
    "NFL_Locks.cogs.server_management",
    "NFL_Locks.cogs.test",
    "NFL_Locks.cogs.user_points",
    "NFL_Locks.cogs.season_management",
    "NFL_Locks.cogs.season_wrapup",
    "NFL_Locks.cogs.survivor",
    # Trade evaluator (standalone module)
    "Trade_Eval.trade_eval",
    # Bot-wide cogs
    "BotCogs.help",
]

# -- Events -------------------------------------------------------------------
@bot.event
async def on_ready():
    import time
    bot._start_time = time.monotonic()
    logger.info(f"Logged in as {bot.user}")
    logger.info(f"Loaded extensions: {', '.join(bot.extensions.keys())}")
    await notify_admin(bot, f"✅ **Bot Started** — {bot.user.name} is online.")

    # Report any extensions that failed to load during startup
    if bot._failed_extensions:
        lines = ["⚠️ **Extensions failed to load at startup:**"]
        lines.extend(f"• `{ext}`" for ext in bot._failed_extensions)
        await notify_admin(bot, "\n".join(lines))


@bot.event
async def on_command_error(ctx, error):
    """
    Global command error handler.

    - CommandNotFound: silently ignored (noisy, happens on typos).
    - User errors (bad args, missing permissions): reply to the user.
    - Everything else: log the full traceback and notify the bot-log channel.
    """
    # Unwrap the wrapper discord puts around exceptions raised inside commands
    if isinstance(error, commands.CommandInvokeError):
        original = error.original
    else:
        original = error

    # -- Ignore ------------------------------------------------------------
    if isinstance(original, commands.CommandNotFound):
        return

    # -- User-facing errors — reply in channel, no admin alert -------------
    if isinstance(original, commands.MissingRequiredArgument):
        await ctx.send(
            f"❌ Missing argument: `{original.param.name}`\n"
            f"Run `!help {ctx.invoked_with}` for usage."
        )
        return

    if isinstance(original, commands.BadArgument):
        await ctx.send(f"❌ Bad argument: {original}")
        return

    if isinstance(original, (commands.MissingPermissions, commands.NotOwner,
                              commands.CheckFailure)):
        await ctx.send("❌ You don't have permission to use that command.")
        return

    # -- Unexpected errors — log fully and notify admin ---------------------
    tb = "".join(traceback.format_exception(type(original), original, original.__traceback__))
    logger.error(
        f"Unhandled error in command '{ctx.command}' "
        f"(invoked by {ctx.author} in {ctx.guild}/{ctx.channel}):\n{tb}"
    )

    # Keep the Discord notification concise — full details are in the log file
    short = str(original)[:200]
    guild_name = ctx.guild.name if ctx.guild else "DM"
    await notify_admin(
        bot,
        f"❌ **Command error** — `!{ctx.invoked_with}` in **{guild_name}**\n"
        f"`{type(original).__name__}: {short}`"
    )

    # Let the user know something went wrong
    await ctx.send("❌ An unexpected error occurred. The bot owner has been notified.")


# -- Development commands -----------------------------------------------------
@bot.command()
@commands.is_owner()
async def reload(ctx):
    """Reload all cogs without restarting the bot."""
    errors = []
    reloaded = []
    loaded_extensions = list(bot.extensions.keys())

    for ext in initial_extensions:
        if ext in loaded_extensions:
            try:
                await bot.reload_extension(ext)
                reloaded.append(ext)
                logger.info(f"[RELOAD] Reloaded {ext}")
            except Exception as e:
                errors.append(f"{ext}: {str(e)[:50]}")
                logger.error(f"[RELOAD] Error reloading {ext}: {e}", exc_info=True)
        else:
            try:
                await bot.load_extension(ext)
                reloaded.append(ext)
                logger.info(f"[RELOAD] Loaded {ext}")
            except Exception as e:
                errors.append(f"{ext}: {str(e)[:50]}")
                logger.error(f"[RELOAD] Error loading {ext}: {e}", exc_info=True)

    if errors:
        await ctx.send(f"✅ Reloaded: {len(reloaded)}\n❌ Errors: {len(errors)}")
        for err in errors:
            await notify_admin(bot, f"⚠️ **Reload error:** `{err}`")
    else:
        await ctx.send(f"✅ Successfully reloaded all {len(reloaded)} cogs!")


# -- Entry point --------------------------------------------------------------
async def main():
    from NFL_Locks.utils.database import get_db

    # Connect the DB singleton BEFORE any extension loads.
    # This guarantees every cog receives a live connection from the moment
    # cog_load (or any command) first calls get_db().
    db = get_db()
    await db.connect()
    logger.info("Database connected")

    try:
        for ext in initial_extensions:
            try:
                await bot.load_extension(ext)
                logger.info(f"✅ Loaded {ext}")
            except Exception as e:
                logger.error(f"❌ Failed to load {ext}: {e}", exc_info=True)
                bot._failed_extensions.append(f"{ext} — {e}")

        if bot._failed_extensions:
            logger.warning(
                f"Extensions that failed to load: "
                f"{', '.join(bot._failed_extensions)}"
            )

        await bot.start(TOKEN)
    finally:
        # Runs on clean shutdown, KeyboardInterrupt, or unhandled exception.
        await db.close()
        logger.info("Database closed")

asyncio.run(main())
