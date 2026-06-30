"""
Schedule Updater
================
Automatically re-fetches the NFL schedule once per year on August 15th.

Design decisions:
- Uses tasks.loop(time=...) so discord.py handles daily alignment natively —
  no manual sleep-until-9-AM math needed, no drift.
- Passes the current calendar year to NFLschedulePuller.py as a CLI argument
  so the script never fetches the wrong season.
- After a successful update, checks schedule metadata so a bot restart on
  August 15th doesn't trigger a redundant re-download.
- schedule_updated_this_year is a belt-and-suspenders in-memory guard; the
  metadata check is the persistent guard.
"""

import asyncio
import os
import sys
from datetime import datetime, time
from pathlib import Path
from discord.ext import commands, tasks

from NFL_Locks.utils.constants import EASTERN
from NFL_Locks.utils.config import (
    SCHEDULE_UPDATE_HOUR,
    SCHEDULE_UPDATE_MONTH,
    SCHEDULE_UPDATE_DAY,
    SCHEDULE_RESET_MONTH,
    SCHEDULE_RESET_DAY,
    SCHEDULE_UPDATE_TIMEOUT_SECONDS,
    SEASON_ARCHIVE_DAY,
)
from NFL_Locks.utils.command_names import CMD_UPDATE_SCHEDULE_NOW, CMD_CHECK_SCHEDULE_STATUS


_TRIGGER_TIME = time(SCHEDULE_UPDATE_HOUR, 0, tzinfo=EASTERN)


class ScheduleUpdater(commands.Cog):
    """Automatically updates the NFL schedule on August 15th each year."""

    def __init__(self, bot):
        self.bot = bot
        self.schedule_updated_this_year = False
        self.season_archived_this_year = False   # in-memory guard; bot_meta is the durable guard
        self.check_schedule_update.start()

    def cog_unload(self):
        self.check_schedule_update.cancel()

    # -- Main task -------------------------------------------------------------

    @tasks.loop(time=_TRIGGER_TIME)
    async def check_schedule_update(self):
        """
        Fires once per day at SCHEDULE_UPDATE_HOUR ET.
        Runs the schedule puller on SCHEDULE_UPDATE_MONTH / SCHEDULE_UPDATE_DAY.
        """
        now = datetime.now(EASTERN)

        # Reset in-memory flags on January 1st
        if now.month == SCHEDULE_RESET_MONTH and now.day == SCHEDULE_RESET_DAY:
            self.schedule_updated_this_year = False
            self.season_archived_this_year = False

        # Only act during the target month (August)
        if now.month != SCHEDULE_UPDATE_MONTH:
            return

        # -- Aug 1+: archive last season's DB data -------------------------
        if now.day >= SEASON_ARCHIVE_DAY and not self.season_archived_this_year:
            await self._maybe_archive(now)

        # -- Aug 15+: pull the new season's schedule ------------------------
        # In-memory short-circuit (cleared on restart, but that's fine —
        # the metadata check below is the durable guard)
        if self.schedule_updated_this_year:
            return

        if now.day < SCHEDULE_UPDATE_DAY:
            return

        # Durable guard: if metadata shows the schedule was already updated
        # for this year's season, skip (handles restart-on-Aug-15 scenario)
        if self._already_updated_this_season(now.year):
            self.schedule_updated_this_year = True
            return

        await self._run_update(now.year)

    @check_schedule_update.before_loop
    async def before_check_schedule_update(self):
        await self.bot.wait_until_ready()

    # -- Core update logic ------------------------------------------------------

    def _already_updated_this_season(self, year: int) -> bool:
        """
        Return True if the schedule metadata shows it was updated for `year`.
        Reads the on-disk schedule file — no imports from schedule_utils to
        avoid circular-dependency issues at cog load time.
        """
        from NFL_Locks.utils.data_utils import get_schedule_metadata
        metadata = get_schedule_metadata()
        if not metadata:
            return False
        if metadata.get("season") != year:
            return False
        # last_updated is an ISO string; check if it falls in the current year
        last_updated_str = metadata.get("last_updated", "")
        try:
            last_updated = datetime.fromisoformat(last_updated_str)
            return last_updated.year == year
        except (ValueError, TypeError):
            return False

    # -- Archival helpers -------------------------------------------------------

    async def _maybe_archive(self, now: datetime):
        """
        Archive last season's DB data if it hasn't been done yet.

        Uses bot_meta as the durable idempotency guard so a bot restart on
        Aug 1 doesn't double-archive.  get_current_season() still returns the
        old season year on Aug 1 (the schedule file hasn't been updated yet),
        so that's the correct season to archive.
        """
        from NFL_Locks.utils.database import get_db
        from NFL_Locks.utils.schedule_utils import get_current_season

        season_to_archive = get_current_season()
        meta_key = f"season_{season_to_archive}_db_archived"

        db = get_db()
        already_done = await db.get_bot_meta(meta_key)
        if already_done:
            self.season_archived_this_year = True
            return

        await self._run_archival(season_to_archive)
        self.season_archived_this_year = True

    async def _run_archival(self, season_year: int):
        """Export season data to CSVs, purge live DB rows, update bot_meta."""
        from NFL_Locks.utils.database import get_db
        from NFL_Locks.utils.data_utils import DATA_DIR
        from datetime import timezone

        archive_dir = DATA_DIR / "archives" / f"season_{season_year}"
        db = get_db()

        try:
            counts = await db.archive_and_purge_season(season_year, archive_dir)

            ts = datetime.now(timezone.utc).isoformat()
            await db.set_bot_meta(f"season_{season_year}_db_archived", ts)
            await db.set_bot_meta("off_season", "true")

            total_rows = sum(counts.values())
            row_detail = ", ".join(f"{t}: {n}" for t, n in counts.items())
            msg = (
                f"**Season {season_year} DB Archive Complete**\n"
                f"Archived to: `data/archives/season_{season_year}/`\n"
                f"Total rows exported & purged: {total_rows}\n"
                f"Breakdown: {row_detail}\n\n"
                f"Bot is in off-season mode. The {season_year + 1} schedule "
                f"pulls on {SCHEDULE_UPDATE_MONTH}/{SCHEDULE_UPDATE_DAY}."
            )
            print(f"[ARCHIVE] Season {season_year} archived — {total_rows} rows")
            await self._notify_owner(msg)

        except Exception as e:
            msg = f"❌ Season {season_year} DB archival failed: {e!s:.500}"
            print(f"[ARCHIVE] {msg}")
            await self._notify_owner(msg)

    async def _run_update(self, season_year: int):
        """Run NFLschedulePuller.py for season_year, notify owner of outcome."""
        script_path = str(Path(__file__).parent.parent.parent / "NFLschedulePuller.py")

        if not os.path.exists(script_path):
            msg = f"❌ Schedule update failed: {script_path} not found"
            print(f"[SCHEDULE] {msg}")
            await self._notify_owner(msg)
            return

        print(f"[SCHEDULE] Running {script_path} for season {season_year}...")

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, script_path, str(season_year),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=SCHEDULE_UPDATE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                process.kill()
                msg = f"❌ Schedule update timed out after {SCHEDULE_UPDATE_TIMEOUT_SECONDS}s"
                print(f"[SCHEDULE] {msg}")
                await self._notify_owner(msg)
                return

            stdout_text = stdout.decode() if stdout else ""
            stderr_text = stderr.decode() if stderr else ""

        except Exception as e:
            msg = f"❌ Schedule update error: {e!s:.500}"
            print(f"[SCHEDULE] {msg}")
            await self._notify_owner(msg)
            return

        if process.returncode == 0:
            print(f"[SCHEDULE] ✅ Schedule updated successfully\n{stdout_text}")
            self.schedule_updated_this_year = True

            # Flush the metadata cache so the rest of the bot sees the new data
            from NFL_Locks.utils.schedule_utils import refresh_metadata_cache
            metadata = refresh_metadata_cache()

            # Clear the off-season flag now that the new schedule is live
            from NFL_Locks.utils.database import get_db
            await get_db().set_bot_meta("off_season", "false")

            total_weeks = metadata.get("total_weeks", "?") if metadata else "?"
            await self._notify_owner(
                f"**NFL Schedule Updated — {season_year} season**\n"
                f"Total weeks: {total_weeks}\n"
                f"Updated: {datetime.now(EASTERN).strftime('%B %d, %Y at %I:%M %p ET')}\n"
                f"Off-season mode cleared. Bot is ready for {season_year}."
            )
        else:
            msg = f"❌ Schedule update failed (exit {process.returncode})\n{stderr_text[:500]}"
            print(f"[SCHEDULE] {msg}")
            await self._notify_owner(msg)

    async def _notify_owner(self, message: str):
        """Send a DM to the bot owner."""
        try:
            from NFL_Locks.utils.constants import OWNER_ID
            owner = await self.bot.fetch_user(OWNER_ID)
            await owner.send(message)
        except Exception as e:
            print(f"[SCHEDULE] Could not notify owner: {e}")

    # -- Manual commands --------------------------------------------------------

    @commands.command(name=CMD_UPDATE_SCHEDULE_NOW)
    @commands.is_owner()
    async def update_schedule_now(self, ctx):
        """Force a schedule update for the current season year (owner only)."""
        season_year = datetime.now(EASTERN).year
        await ctx.send(f"Running schedule update for {season_year}...")
        await self._run_update(season_year)
        await ctx.send("✅ Schedule update complete — check your DMs for details.")

    @commands.command(name=CMD_CHECK_SCHEDULE_STATUS)
    @commands.is_owner()
    async def check_schedule_status(self, ctx):
        """Show schedule update status and what's currently on disk (owner only)."""
        from NFL_Locks.utils.data_utils import get_schedule_metadata
        now = datetime.now(EASTERN)
        metadata = get_schedule_metadata()

        in_memory = "✅ Updated this session" if self.schedule_updated_this_year else "❌ Not updated this session"

        if metadata:
            on_disk = (
                f"Season {metadata.get('season', '?')} | "
                f"{metadata.get('total_weeks', '?')} weeks | "
                f"Last updated: {metadata.get('last_updated', 'unknown')}"
            )
        else:
            on_disk = "❌ No metadata found — schedule may be missing or in old format"

        await ctx.send(
            f"**Schedule Status**\n"
            f"In-memory flag: {in_memory}\n"
            f"On-disk: {on_disk}\n"
            f"Update target: {SCHEDULE_UPDATE_MONTH}/{SCHEDULE_UPDATE_DAY} at {SCHEDULE_UPDATE_HOUR}:00 AM ET\n"
            f"Current date: {now.strftime('%B %d, %Y')}"
        )


async def setup(bot):
    await bot.add_cog(ScheduleUpdater(bot))
