"""
Pick nudges: a courtesy DM to players with no pick recorded for the week.

Sent once per player per guild per week, NUDGE_HOURS_BEFORE_DEADLINE before the
week's first kickoff. One DM covers both contests in that guild, so a player
missing locks and survivor gets a single message, not two.

Who gets one
------------
NFL Locks: anyone with zero picks this week who picked at least once in the
previous NUDGE_LOOKBACK_WEEKS weeks in that guild. One reaction is the
engagement bar, so a partial slate counts as picked. There is no enrollment for
locks, so prior picks are the only membership signal available: a player who
sits out two weeks running drops off the list until they pick again, and nobody
is eligible in Week 1.

Survivor: enrolled, not eliminated, no pick this week. Survivor has real
enrollment, so no lookback is needed.

Why the catchup runs first
--------------------------
Picks are read from the DB, but the DB and Discord are only reconciled by the
pre-deadline sync 10 to 20 minutes before kickoff, hours after this runs. If
reactions were missed during an outage earlier in the week, the DB shows no
pick for someone whose reaction is sitting in the channel, and the bot would
tell a player they had not picked while their pick was visible above. So the
week's reaction catchup runs first and the recipient list is built from what it
leaves behind.

Failures
--------
A player with DMs closed cannot be reached, and that is accepted: the attempt is
banked either way so it is not retried, and the bounce is logged. Nothing is
posted to the channel about it. The lock summary at kickoff is still the record
of what the bot saw, and responsibility for picking stays with the player.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from NFL_Locks.utils.config import (
    LOCK_CHECK_INTERVAL_MIN,
    NUDGE_DM_DELAY_SECONDS,
    NUDGE_HOURS_BEFORE_DEADLINE,
    NUDGE_LOOKBACK_WEEKS,
)
from NFL_Locks.utils.command_names import CMD_NUDGE_PREVIEW
from NFL_Locks.utils.constants import EASTERN
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.schedule_utils import get_current_season, get_current_week_info
from NFL_Locks.utils.time_utils import get_week_deadline

logger = logging.getLogger("cogs.nudges")


class PickNudges(commands.Cog):
    """DMs players who have no pick recorded, once per week, before kickoff."""

    def __init__(self, bot):
        self.bot = bot
        self._batch_task: "asyncio.Task | None" = None
        self.check_nudge_window.start()

    def cog_unload(self):
        self.check_nudge_window.cancel()
        if self._batch_task and not self._batch_task.done():
            self._batch_task.cancel()

    # -- Timer -----------------------------------------------------------------

    @tasks.loop(minutes=LOCK_CHECK_INTERVAL_MIN)
    async def check_nudge_window(self):
        """Start the DM batch once the window opens, in its own task.

        The batch paces itself at NUDGE_DM_DELAY_SECONDS per DM and can easily
        run longer than this loop's interval. discord.py starts the next
        iteration only after the current one returns, so running the batch
        inline would hold up every later tick. It goes in a separate task and
        this loop stays free.
        """
        try:
            if self._batch_task and not self._batch_task.done():
                return

            now = datetime.now(EASTERN)
            week, _prev, in_season = get_current_week_info(now)
            if not in_season or week is None:
                return

            deadline = get_week_deadline(week)
            if not deadline:
                return

            # Catchup rule: send any time between the window opening and the
            # deadline, so an outage across the 6-hour mark does not eat the
            # week's nudges. Past the deadline there is nothing to nudge about.
            window_opens = deadline - timedelta(hours=NUDGE_HOURS_BEFORE_DEADLINE)
            if now < window_opens or now >= deadline:
                return

            self._batch_task = asyncio.create_task(self.run_nudges(week))
        except Exception as e:
            logger.error(f"[NUDGE] Window check failed: {e}", exc_info=True)

    @check_nudge_window.before_loop
    async def _before_check_nudge_window(self):
        await self.bot.wait_until_ready()

    # -- Batch -----------------------------------------------------------------

    async def run_nudges(self, week: int, dry_run: bool = False) -> "list[dict]":
        """Reconcile reactions, work out who is missing, DM them one at a time.

        Returns the recipient list, so !nudge_preview can show it without
        sending anything.
        """
        season = get_current_season()
        db = get_db()

        if not dry_run:
            catchup_cog = self.bot.get_cog("ReactionCatchup")
            if catchup_cog:
                try:
                    await catchup_cog.process_week_reactions(week)
                except Exception as e:
                    logger.error(
                        f"[NUDGE] Reaction catchup failed for Week {week}, "
                        f"skipping this run rather than DM from stale picks: {e}",
                        exc_info=True,
                    )
                    return []
            else:
                logger.warning("[NUDGE] ReactionCatchup cog missing, skipping run")
                return []

        recipients = await self._build_recipients(db, season, week)
        if not recipients:
            logger.info(f"[NUDGE] Week {week}: nobody to nudge")
            return []

        if dry_run:
            return recipients

        deadline = get_week_deadline(week)
        sent = bounced = skipped = 0

        for r in recipients:
            if await db.was_nudge_sent(season, week, r["guild_id"], r["user_id"]):
                continue

            guild = self.bot.get_guild(int(r["guild_id"]))
            member = guild.get_member(int(r["user_id"])) if guild else None
            if member is None and guild is not None:
                try:
                    member = await guild.fetch_member(int(r["user_id"]))
                except discord.NotFound:
                    member = None
                except Exception as e:
                    logger.warning(
                        f"[NUDGE] Could not resolve {r['user_id']} in guild "
                        f"{r['guild_id']}: {e!r}"
                    )
                    member = None

            if member is None:
                # Left the server, or the guild is unreachable. Bank it so the
                # loop does not retry them every 5 minutes for the rest of the day.
                skipped += 1
                await db.mark_nudge_sent(season, week, r["guild_id"], r["user_id"])
                continue

            try:
                await member.send(self._build_message(r, week, deadline))
                sent += 1
                logger.info(
                    f"[NUDGE] Week {week} DM sent to {member.name} "
                    f"({r['guild_name']}): {', '.join(r['missing'])}"
                )
            except discord.Forbidden:
                bounced += 1
                logger.info(
                    f"[NUDGE] Week {week} DM blocked by {member.name} "
                    f"({r['guild_name']}), DMs closed"
                )
            except Exception as e:
                bounced += 1
                logger.warning(
                    f"[NUDGE] Week {week} DM to {member.name} failed: {e!r}"
                )

            # Banked after every attempt, delivered or not, and per player rather
            # than at the end of the batch: a restart partway through re-DMs
            # nobody who has already had their attempt.
            await db.mark_nudge_sent(season, week, r["guild_id"], r["user_id"])
            await asyncio.sleep(NUDGE_DM_DELAY_SECONDS)

        logger.info(
            f"[NUDGE] Week {week} complete: {sent} sent, {bounced} bounced, "
            f"{skipped} skipped"
        )
        return recipients

    # -- Recipient list --------------------------------------------------------

    async def _build_recipients(self, db, season: int, week: int) -> "list[dict]":
        """One entry per player per guild, merging both contests."""
        merged: dict[tuple[str, str], dict] = {}

        def entry(guild_id: str, guild_name: str, user_id: str):
            key = (guild_id, str(user_id))
            if key not in merged:
                merged[key] = {
                    "guild_id": guild_id,
                    "guild_name": guild_name,
                    "user_id": str(user_id),
                    "missing": [],
                    "locks_link": None,
                    "survivor_link": None,
                    "teams_used": [],
                }
            return merged[key]

        # -- NFL Locks ---------------------------------------------------------
        for guild_id_int in await db.get_all_configured_guilds():
            guild_id = str(guild_id_int)
            guild = self.bot.get_guild(guild_id_int)
            if not guild:
                continue

            picked = set(await db.get_picks_by_user_id(season, week, guild_id))

            eligible: set[str] = set()
            for back in range(1, NUDGE_LOOKBACK_WEEKS + 1):
                prior = week - back
                if prior < 1:
                    continue
                eligible |= set(await db.get_picks_by_user_id(season, prior, guild_id))

            missing = eligible - picked
            if not missing:
                continue

            link = self._message_link(
                guild_id, await db.get_first_message_for_week(season, week, guild_id)
            )
            for user_id in missing:
                e = entry(guild_id, guild.name, user_id)
                e["missing"].append("NFL Locks")
                e["locks_link"] = link

        # -- Survivor ----------------------------------------------------------
        for cfg in await db.get_all_survivor_configs():
            if cfg["season"] != season or cfg["start_week"] > week:
                continue

            guild_id = str(cfg["guild_id"])
            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                continue

            picked = {
                p["user_id"]
                for p in await db.get_survivor_picks_for_week(season, week, guild_id)
            }
            alive = await db.get_alive_survivor_players(season, guild_id)
            missing = [p for p in alive if p["user_id"] not in picked]
            if not missing:
                continue

            link = self._message_link(
                guild_id,
                await db.get_first_survivor_message_for_week(season, week, guild_id),
            )
            for player in missing:
                e = entry(guild_id, guild.name, player["user_id"])
                e["missing"].append("Survivor")
                e["survivor_link"] = link
                used = await db.get_teams_used_in_survivor(
                    season, guild_id, player["user_id"]
                )
                e["teams_used"] = sorted(used)

        return sorted(merged.values(), key=lambda r: (r["guild_id"], r["user_id"]))

    @staticmethod
    def _message_link(guild_id: str, row) -> "str | None":
        if not row:
            return None
        channel_id, message_id = row
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    @staticmethod
    def _build_message(r: dict, week: int, deadline) -> str:
        """Discord timestamps render in each reader's own timezone and language,
        so the message never has to guess at 'tonight' or an offset."""
        lines = [f"**{r['guild_name']}, Week {week}**"]

        if deadline:
            stamp = int(deadline.timestamp())
            lines.append(f"Picks lock <t:{stamp}:R>, at <t:{stamp}:f>.")
        lines.append("")

        if "NFL Locks" in r["missing"]:
            line = "**NFL Locks:** no picks recorded."
            if r["locks_link"]:
                line += f" {r['locks_link']}"
            lines.append(line)

        if "Survivor" in r["missing"]:
            line = "**Survivor:** no pick recorded. No pick means elimination."
            if r["teams_used"]:
                line += f" Teams already used: {', '.join(r['teams_used'])}."
            if r["survivor_link"]:
                line += f" {r['survivor_link']}"
            lines.append(line)

        return "\n".join(lines)

    # -- Admin commands --------------------------------------------------------

    @commands.command(name=CMD_NUDGE_PREVIEW)
    @commands.has_permissions(administrator=True)
    async def nudge_preview(self, ctx, week_num: int = None):
        """Show who would be nudged this week without sending anything."""
        if week_num is None:
            week_num, _prev, _in_season = get_current_week_info(datetime.now(EASTERN))
        if week_num is None:
            await ctx.send("Could not determine the current week.")
            return

        recipients = await self.run_nudges(week_num, dry_run=True)
        guild_recipients = [
            r for r in recipients if r["guild_id"] == str(ctx.guild.id)
        ]
        if not guild_recipients:
            await ctx.send(f"Week {week_num}: nobody would be nudged here.")
            return

        db = get_db()
        season = get_current_season()
        lines = [f"**Week {week_num} nudge list ({len(guild_recipients)})**"]
        for r in guild_recipients:
            member = ctx.guild.get_member(int(r["user_id"]))
            name = member.name if member else f"(left server) {r['user_id']}"
            already = await db.was_nudge_sent(
                season, week_num, r["guild_id"], r["user_id"]
            )
            suffix = " [already sent]" if already else ""
            lines.append(f"  {name}: {', '.join(r['missing'])}{suffix}")

        await ctx.send("\n".join(lines)[:1900])


async def setup(bot):
    await bot.add_cog(PickNudges(bot))
