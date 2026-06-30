"""
Survivor game mode cog.

Rules
-----
- Each week a player picks exactly ONE team from the week's matchups.
- A team may only be used once per season — the bot removes the reaction
  and warns the user if they attempt a repeat.
- Failing to pick before the deadline = elimination.
- A player is eliminated when their picked team loses.
- Win by:  (a) 17 consecutive correct picks, OR
           (b) being the last player(s) alive.
- If all remaining players are eliminated simultaneously, all are declared
  winners (they were the last finalists).

Pick mechanism
--------------
Survivor matchups are posted in a dedicated survivor channel (separate from
the main locks channel). Reacting to a game post with a team emoji records
that team as the player's pick. Reacting to a second game replaces the first
pick (old reaction is removed from Discord automatically).

Enrollment
----------
First reaction during the start week auto-enrolls a player. After the start
week's deadline, no new enrollments are accepted.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands

from NFL_Locks.utils.command_names import (
    CMD_SURVIVOR_SETUP,
    CMD_SURVIVOR_START,
    CMD_SURVIVOR_STANDINGS,
    CMD_SURVIVOR_MYPICKS,
    CMD_SURVIVOR_FIX_PICK,
    CMD_SURVIVOR_ELIMINATE,
    CMD_SURVIVOR_PROCESS,
    CMD_SURVIVOR_STATUS,
)
from NFL_Locks.utils.constants import NFL_TEAMS, EASTERN, emoji_to_team
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.data_utils import load_full_schedule
from NFL_Locks.utils.schedule_utils import get_current_season, get_max_week
from NFL_Locks.utils.time_utils import is_deadline_passed, get_week_deadline

logger = logging.getLogger("cogs.survivor")

# A player wins outright after this many consecutive correct picks.
SURVIVOR_WIN_STREAK = 17


class SurvivorGame(commands.Cog):
    """Handles the NFL Survivor pick-em game mode."""

    def __init__(self, bot):
        self.bot = bot
        # Channel IDs where survivor messages live — {channel_id (int)}
        self.survivor_channel_ids: set[int] = set()
        # In-memory message cache: {message_id (int): week_num (int)}
        self._msg_cache: dict[int, int] = {}
        # DM cooldown: {user_id (str): datetime}
        self._dm_cooldowns: dict[str, datetime] = {}
        # Prevent reaction loops
        self._processing: set = set()
        # -- Reconciliation gate (mirrors reactions.py pattern) ----------------
        # When True, live reaction events are buffered instead of written.
        # process_week_survivor_reactions() sets/clears this flag and flushes
        # the buffer after the API rebuild completes.
        self.survivor_reconciliation_active: bool = False
        self._pending_survivor_reactions: list[dict] = []

    async def cog_load(self):
        """Populate in-memory state from DB after the connection is live."""
        db = get_db()
        configs = await db.get_all_survivor_configs()
        for cfg in configs:
            self.survivor_channel_ids.add(cfg["channel_id"])

        season = get_current_season()
        msg_map = await db.get_all_survivor_messages(season)
        self._msg_cache.update(msg_map)

        logger.info(
            f"[SURVIVOR] Loaded {len(self.survivor_channel_ids)} channel(s), "
            f"{len(self._msg_cache)} cached message(s)"
        )

    # -- DM helpers ------------------------------------------------------------

    def _dm_allowed(self, user_id: int | str, cooldown_secs: int = 60) -> bool:
        key = str(user_id)
        last = self._dm_cooldowns.get(key)
        now = datetime.now(EASTERN)
        if last and (now - last).total_seconds() < cooldown_secs:
            return False
        self._dm_cooldowns[key] = now
        return True

    async def _dm(self, user, message: str):
        try:
            await user.send(message)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # -- Reaction routing ------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        if payload.member and payload.member.bot:
            return
        if payload.channel_id not in self.survivor_channel_ids:
            return

        reaction_key = (payload.message_id, payload.user_id, str(payload.emoji))
        if reaction_key in self._processing:
            return
        self._processing.add(reaction_key)

        try:
            await self._handle_reaction_add(payload)
        except Exception as e:
            logger.error(f"[SURVIVOR] Error handling reaction add: {e}", exc_info=True)
        finally:
            await asyncio.sleep(2)
            self._processing.discard(reaction_key)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        if payload.channel_id not in self.survivor_channel_ids:
            return

        reaction_key = (payload.message_id, payload.user_id, str(payload.emoji))
        if reaction_key in self._processing:
            return

        await self._handle_reaction_remove(payload)

    # -- Core reaction logic ---------------------------------------------------

    async def _handle_reaction_add(self, payload: discord.RawReactionActionEvent):
        db = get_db()
        guild_id = str(payload.guild_id)
        user_id = str(payload.user_id)
        member = payload.member
        user_name = member.name if member else user_id

        # Identify which week this message belongs to
        week_num = self._msg_cache.get(payload.message_id)
        if not week_num:
            week_num = await db.get_week_for_survivor_message(payload.message_id)
            if not week_num:
                return  # Not a tracked survivor message (e.g. header)
            self._msg_cache[payload.message_id] = week_num

        # Confirm this is actually a matchup message (has team_a/team_b)
        matchup = await db.get_survivor_matchup_teams(payload.message_id)
        if not matchup:
            return  # Header / footer message — ignore

        # Resolve the reacted team
        team = emoji_to_team(payload.emoji)
        if not team:
            return

        # Deadline-blocked reactions are rejected immediately — never buffered.
        # This mirrors the behaviour in reactions.py.
        if is_deadline_passed(week_num):
            await self._remove_reaction(payload, member)
            if self._dm_allowed(user_id):
                deadline = get_week_deadline(week_num)
                deadline_str = (
                    deadline.strftime("%A, %B %d at %I:%M %p ET")
                    if deadline else "the deadline"
                )
                if member:
                    await self._dm(
                        member,
                        f"Survivor submissions for Week {week_num} closed at {deadline_str}."
                    )
            return

        # Buffer during reconciliation — week_num already resolved above.
        if self.survivor_reconciliation_active:
            self._pending_survivor_reactions.append({
                "type": "add",
                "guild_id": guild_id,
                "user_id": user_id,
                "user_name": user_name,
                "team": team,
                "week_num": week_num,
                "message_id": payload.message_id,
                "channel_id": payload.channel_id,
                "member": member,
            })
            logger.debug(
                f"[SURVIVOR BUFFER] Queued add for {user_name} "
                f"Week {week_num} (reconciliation active)"
            )
            return

        await self._apply_pick(
            guild_id=guild_id,
            user_id=user_id,
            user_name=user_name,
            team=team,
            week_num=week_num,
            channel_id=payload.channel_id,
            message_id=payload.message_id,
            member=member,
        )

    async def _apply_pick(
        self,
        guild_id: str,
        user_id: str,
        user_name: str,
        team: str,
        week_num: int,
        channel_id: int | None = None,
        message_id: int | None = None,
        member=None,
    ) -> None:
        """
        Validate and record a survivor pick.

        Called from both the live reaction handler and the reconciliation
        flush so validation logic lives in exactly one place.

        channel_id / message_id / member are used to strip Discord reactions
        (swap path) and are omitted by the reconciliation flush path.
        """
        db = get_db()
        season = get_current_season()

        # -- Enrollment gate ---------------------------------------------------
        config = await db.get_survivor_config(guild_id)
        if not config:
            return

        is_enrolled = await db.is_survivor_enrolled(season, guild_id, user_id)

        if not is_enrolled:
            # Only auto-enroll during the start week
            if week_num != config["start_week"]:
                if member and self._dm_allowed(user_id):
                    await self._dm(
                        member,
                        f"Survivor enrollment is closed. It locked after Week {config['start_week']}."
                    )
                # Remove the reaction if we have Discord context
                if channel_id and message_id and member:
                    await self._remove_reaction_by_ids(channel_id, message_id, team, member)
                return
            # Auto-enroll on first valid pick during start week
            await db.enroll_survivor_player(season, guild_id, user_id, user_name)
            logger.info(f"[SURVIVOR] Enrolled {user_name} ({user_id}) in guild {guild_id}")

        # -- Alive check -------------------------------------------------------
        player = await db.get_survivor_player(season, guild_id, user_id)
        if player and player["eliminated"]:
            if member and self._dm_allowed(user_id):
                await self._dm(
                    member,
                    f"You were eliminated from Survivor in Week {player['eliminated_week']}. "
                    f"Better luck next season!"
                )
            if channel_id and message_id and member:
                await self._remove_reaction_by_ids(channel_id, message_id, team, member)
            return

        # -- Team-already-used check -------------------------------------------
        used_teams = await db.get_teams_used_in_survivor(season, guild_id, user_id)
        current_pick = await db.get_survivor_pick(season, week_num, guild_id, user_id)

        # Exclude current week's existing pick from "used" — swapping is allowed
        if current_pick:
            used_teams.discard(current_pick)

        if team in used_teams:
            if member and self._dm_allowed(user_id):
                used_wk = await self._find_week_team_was_used(
                    db, season, guild_id, user_id, team, exclude_week=week_num
                )
                wk_str = f" in Week {used_wk}" if used_wk else ""
                await self._dm(
                    member,
                    f"You already used **{team}**{wk_str} this season. Pick a different team!"
                )
            if channel_id and message_id and member:
                await self._remove_reaction_by_ids(channel_id, message_id, team, member)
            return

        # -- Swap: remove existing pick for this week if different -------------
        if current_pick and current_pick != team:
            await db.remove_survivor_pick(season, week_num, guild_id, user_id)
            if channel_id and member:
                await self._remove_old_reaction(
                    channel_id, message_id, current_pick, member, int(user_id)
                )
            logger.info(
                f"[SURVIVOR] Swapped {current_pick} → {team} "
                f"for {user_name} Week {week_num}"
            )

        # -- Record the pick ---------------------------------------------------
        await db.set_survivor_pick(season, week_num, guild_id, user_id, user_name, team)
        logger.info(f"[SURVIVOR] {user_name} picked {team} for Week {week_num}")

    async def _handle_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Remove a survivor pick when a user un-reacts (pre-deadline only)."""
        week_num = self._msg_cache.get(payload.message_id)
        if not week_num:
            db_lookup = get_db()
            week_num = await db_lookup.get_week_for_survivor_message(payload.message_id)
            if not week_num:
                return
            self._msg_cache[payload.message_id] = week_num

        # Deadline-blocked removals are rejected immediately — never buffered.
        if is_deadline_passed(week_num):
            if self._dm_allowed(payload.user_id):
                try:
                    user = await self.bot.fetch_user(payload.user_id)
                    deadline = get_week_deadline(week_num)
                    deadline_str = (
                        deadline.strftime("%A, %B %d at %I:%M %p ET")
                        if deadline else "the deadline"
                    )
                    await self._dm(
                        user,
                        f"Your Survivor pick for Week {week_num} is locked — "
                        f"submissions closed at {deadline_str}."
                    )
                except Exception:
                    pass
            return

        team = emoji_to_team(payload.emoji)
        if not team:
            return

        # Buffer during reconciliation.
        if self.survivor_reconciliation_active:
            self._pending_survivor_reactions.append({
                "type": "remove",
                "guild_id": str(payload.guild_id),
                "user_id": str(payload.user_id),
                "team": team,
                "week_num": week_num,
            })
            logger.debug(
                f"[SURVIVOR BUFFER] Queued remove for user {payload.user_id} "
                f"Week {week_num} (reconciliation active)"
            )
            return

        db = get_db()
        season = get_current_season()
        current = await db.get_survivor_pick(
            season, week_num, str(payload.guild_id), str(payload.user_id)
        )
        if current == team:
            await db.remove_survivor_pick(
                season, week_num, str(payload.guild_id), str(payload.user_id)
            )
            logger.info(
                f"[SURVIVOR] user {payload.user_id} removed pick {team} Week {week_num}"
            )

    # -- Reaction removal helpers ----------------------------------------------

    async def _remove_reaction(self, payload: discord.RawReactionActionEvent, member):
        """Remove the offending reaction from Discord (live-event path)."""
        try:
            channel = self.bot.get_channel(payload.channel_id)
            if not channel:
                return
            message = await channel.fetch_message(payload.message_id)
            target = member
            if target is None:
                target = await self.bot.fetch_user(payload.user_id)
            await message.remove_reaction(payload.emoji, target)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
            logger.debug(f"[SURVIVOR] Could not remove reaction: {e}")

    async def _remove_reaction_by_ids(
        self,
        channel_id: int,
        message_id: int,
        team: str,
        member,
    ) -> None:
        """Remove a specific team's reaction by channel/message IDs (_apply_pick path)."""
        emoji_str = NFL_TEAMS.get(team)
        if not emoji_str:
            return
        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                return
            message = await channel.fetch_message(message_id)
            await message.remove_reaction(emoji_str, member)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
            logger.debug(f"[SURVIVOR] Could not remove reaction for {team}: {e}")

    async def _remove_old_reaction(
        self,
        channel_id: int,
        message_id: int,
        team: str,
        member,
        user_id: int,
    ):
        """Remove a previously placed reaction (team swap)."""
        emoji_str = NFL_TEAMS.get(team)
        if not emoji_str:
            return
        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                return
            # The old reaction could be on any matchup message this week — find it
            # by fetching the message and scanning for the right reaction.
            # We don't have the old message_id here, so we fetch from DB.
            db = get_db()
            season = get_current_season()
            cfg = await db.get_survivor_config(str(channel.guild.id))
            if not cfg:
                return
            messages_by_channel = await db.get_survivor_messages_for_week(
                season, self._msg_cache.get(message_id, 0), str(channel.guild.id)
            )
            target = member
            if target is None:
                try:
                    target = await self.bot.fetch_user(user_id)
                except Exception:
                    return

            for ch_id_str, msg_ids in messages_by_channel.items():
                ch = self.bot.get_channel(int(ch_id_str))
                if not ch:
                    continue
                for mid in msg_ids:
                    try:
                        msg = await ch.fetch_message(mid)
                        for reaction in msg.reactions:
                            if str(reaction.emoji) == emoji_str:
                                async for u in reaction.users():
                                    if u.id == user_id:
                                        await msg.remove_reaction(reaction.emoji, target)
                                        return
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"[SURVIVOR] Could not remove old reaction for {team}: {e}")

    # -- Reconciliation buffer helpers (mirrors reactions.py) -----------------

    async def flush_pending_survivor_reactions(self) -> None:
        """
        Drain the buffered reaction queue after a rebuild completes.

        Add entries call _apply_pick (same validation path as live reactions).
        Remove entries call remove_survivor_pick directly.
        """
        entries = list(self._pending_survivor_reactions)
        self._pending_survivor_reactions.clear()

        if not entries:
            return

        db = get_db()
        season = get_current_season()
        flushed = 0

        for entry in entries:
            try:
                if entry["type"] == "add":
                    await self._apply_pick(
                        guild_id=entry["guild_id"],
                        user_id=entry["user_id"],
                        user_name=entry["user_name"],
                        team=entry["team"],
                        week_num=entry["week_num"],
                        channel_id=entry.get("channel_id"),
                        message_id=entry.get("message_id"),
                        member=entry.get("member"),
                    )
                    flushed += 1
                elif entry["type"] == "remove":
                    current = await db.get_survivor_pick(
                        season, entry["week_num"], entry["guild_id"], entry["user_id"]
                    )
                    if current == entry["team"]:
                        await db.remove_survivor_pick(
                            season, entry["week_num"], entry["guild_id"], entry["user_id"]
                        )
                    flushed += 1
            except Exception as e:
                logger.error(
                    f"[SURVIVOR BUFFER] Error flushing entry for guild "
                    f"{entry.get('guild_id')}: {e}",
                    exc_info=True,
                )

        logger.info(f"[SURVIVOR BUFFER] Flushed {flushed} buffered reactions")

    # -- Survivor reaction rebuild (called by reaction_catchup) ----------------

    async def process_week_survivor_reactions(self, week_number: int) -> None:
        """
        Rebuild survivor picks for a week from live Discord reactions.

        Mirrors the locks reconciliation flow in reaction_catchup._reconcile_guild:
          1. Set reconciliation gate so live events buffer during rebuild.
          2. Fetch messages from Discord via fetch_messages_bulk (shared util).
          3. Build a survivor-specific state snapshot (one pick per user per week).
          4. Diff against DB and apply delta.
          5. Flush buffered live events and release the gate.

        One-pick-per-week is enforced by taking the first valid team encountered
        per user across all matchup messages. Under normal operation the live
        handler strips extra reactions, so duplicates should be rare.

        Called by reaction_catchup.process_week_reactions() so startup catchup,
        pre-deadline sync, and !force_reaction_catchup all cover survivor too.
        """
        from NFL_Locks.utils.message_fetcher import get_message_fetcher

        db = get_db()
        season = get_current_season()
        configs = await db.get_all_survivor_configs()
        message_fetcher = get_message_fetcher(self.bot)

        relevant = [c for c in configs if c["season"] == season and c["start_week"] <= week_number]
        if not relevant:
            return

        logger.info(
            f"[SURVIVOR REBUILD] Starting Week {week_number} "
            f"for {len(relevant)} guild(s)"
        )

        self.survivor_reconciliation_active = True
        try:
            for cfg in relevant:
                guild_id = cfg["guild_id"]
                await self._rebuild_guild_survivor(
                    guild_id, week_number, season, db, message_fetcher, cfg
                )
        finally:
            self.survivor_reconciliation_active = False
            await self.flush_pending_survivor_reactions()
            logger.info(f"[SURVIVOR REBUILD] Gate released, buffer flushed for Week {week_number}")

    async def _rebuild_guild_survivor(
        self,
        guild_id: str,
        week_number: int,
        season: int,
        db,
        message_fetcher,
        cfg: dict,
    ) -> None:
        """Fetch and diff survivor reactions for a single guild."""
        import discord as _discord

        messages_by_channel = await db.get_survivor_messages_for_week(
            season, week_number, guild_id
        )
        if not messages_by_channel:
            logger.debug(
                f"[SURVIVOR REBUILD] No messages for guild {guild_id} Week {week_number}"
            )
            return

        int_keyed = {int(ch): mids for ch, mids in messages_by_channel.items()}

        BACKOFF_SECS = [30, 60, 120]

        for attempt, backoff_secs in enumerate(BACKOFF_SECS, start=1):
            try:
                fetched = await message_fetcher.fetch_messages_bulk(
                    int_keyed, batch_size=3, delay_between_batches=1.0
                )

                # -- Build survivor state: {user_id: {name, team}} ------------
                # One pick per user per week. First valid team wins in the rare
                # case where two reactions survived (e.g. bot was offline when
                # user swapped mid-week).
                discord_state: dict[str, dict] = {}

                for ch_messages in fetched.values():
                    for message in ch_messages:
                        matchup = await db.get_survivor_matchup_teams(message.id)
                        if not matchup:
                            continue
                        team_a, team_b = matchup

                        for reaction in message.reactions:
                            team = emoji_to_team(reaction.emoji)
                            if not team or team not in (team_a, team_b):
                                continue
                            async for user in reaction.users():
                                if user.bot:
                                    continue
                                uid = str(user.id)
                                # Skip if user already has a pick this week
                                if uid not in discord_state:
                                    discord_state[uid] = {
                                        "name": user.name,
                                        "team": team,
                                    }

                # -- Fetch DB state --------------------------------------------
                db_picks = {
                    p["user_id"]: p["team"]
                    for p in await db.get_survivor_picks_for_week(
                        season, week_number, guild_id
                    )
                }

                # -- Apply delta -----------------------------------------------
                added = removed = 0

                for user_id, info in discord_state.items():
                    if db_picks.get(user_id) == info["team"]:
                        continue  # Already correct in DB

                    # Validate: enrolled, alive, team not used earlier
                    if not await db.is_survivor_enrolled(season, guild_id, user_id):
                        if week_number == cfg["start_week"]:
                            await db.enroll_survivor_player(
                                season, guild_id, user_id, info["name"]
                            )
                        else:
                            continue

                    player = await db.get_survivor_player(season, guild_id, user_id)
                    if player and player["eliminated"]:
                        continue

                    used = await db.get_teams_used_in_survivor(season, guild_id, user_id)
                    used.discard(db_picks.get(user_id, ""))  # allow swap
                    if info["team"] in used:
                        continue

                    await db.set_survivor_pick(
                        season, week_number, guild_id, user_id, info["name"], info["team"]
                    )
                    added += 1

                for user_id in list(db_picks.keys()):
                    if user_id not in discord_state:
                        await db.remove_survivor_pick(
                            season, week_number, guild_id, user_id
                        )
                        removed += 1

                logger.info(
                    f"[SURVIVOR REBUILD] Guild {guild_id} Week {week_number} "
                    f"attempt {attempt} OK — +{added} added, -{removed} removed"
                )
                return

            except _discord.HTTPException as e:
                sleep_secs = max(backoff_secs, getattr(e, "retry_after", backoff_secs))
                logger.warning(
                    f"[SURVIVOR REBUILD] Guild {guild_id} attempt {attempt} "
                    f"HTTP {e.status} — sleeping {sleep_secs:.1f}s"
                )
                if attempt < len(BACKOFF_SECS):
                    await asyncio.sleep(sleep_secs)
            except Exception as e:
                logger.warning(
                    f"[SURVIVOR REBUILD] Guild {guild_id} attempt {attempt} "
                    f"failed: {e} — sleeping {backoff_secs}s"
                )
                if attempt < len(BACKOFF_SECS):
                    await asyncio.sleep(backoff_secs)

        logger.error(
            f"[SURVIVOR REBUILD] Guild {guild_id} exhausted all attempts for Week {week_number}"
        )

    async def _find_week_team_was_used(
        self,
        db,
        season: int,
        guild_id: str,
        user_id: str,
        team: str,
        exclude_week: int,
    ) -> "int | None":
        """Return the week number a player used a particular team (excluding current week)."""
        async with db._conn.execute(
            """SELECT week FROM survivor_picks
               WHERE season=? AND guild_id=? AND user_id=? AND team=? AND week!=?""",
            (season, guild_id, user_id, team, exclude_week),
        ) as cur:
            row = await cur.fetchone()
        return row["week"] if row else None

    # -- Week processing -------------------------------------------------------

    async def process_survivor_week(self, week: int) -> "dict[str, list[dict]]":
        """
        Evaluate survivor picks for a completed week across all configured guilds.

        For each guild:
          - Players with a correct pick → streak incremented
          - Players with a wrong pick   → eliminated
          - Players with no pick        → eliminated (missed = out)
          - Win conditions checked:
              (a) streak reaches SURVIVOR_WIN_STREAK
              (b) all remaining players eliminated simultaneously

        Returns {guild_id: [result_dict, ...]} where each result_dict has:
          user_id, user_name, outcome ('survived'|'eliminated'|'no_pick'|'winner'),
          team (or None), streak.
        """
        db = get_db()
        season = get_current_season()
        winners_list = await db.get_winners(season, week)
        if not winners_list:
            logger.warning(f"[SURVIVOR] No winners set for Week {week} — cannot process")
            return {}

        winners_set = set(winners_list)
        configs = await db.get_all_survivor_configs()
        guild_results: dict[str, list[dict]] = {}

        for cfg in configs:
            if cfg["season"] != season:
                continue
            if cfg["start_week"] > week:
                continue  # Game hasn't started yet for this guild

            guild_id = cfg["guild_id"]
            alive_players = await db.get_alive_survivor_players(season, guild_id)

            if not alive_players:
                continue

            picks_this_week = {
                p["user_id"]: p["team"]
                for p in await db.get_survivor_picks_for_week(season, week, guild_id)
            }

            results: list[dict] = []
            newly_eliminated: list[str] = []
            survivors: list[str] = []

            for player in alive_players:
                uid = player["user_id"]
                pick = picks_this_week.get(uid)

                if not pick:
                    # Missed the week — eliminate
                    await db.eliminate_survivor_player(season, guild_id, uid, week)
                    newly_eliminated.append(uid)
                    results.append({
                        "user_id": uid,
                        "user_name": player["user_name"],
                        "outcome": "no_pick",
                        "team": None,
                        "streak": player["correct_streak"],
                    })
                    logger.info(
                        f"[SURVIVOR] {player['user_name']} eliminated (no pick) "
                        f"Week {week} guild {guild_id}"
                    )

                elif pick not in winners_set:
                    # Wrong pick — eliminate
                    await db.eliminate_survivor_player(season, guild_id, uid, week)
                    newly_eliminated.append(uid)
                    results.append({
                        "user_id": uid,
                        "user_name": player["user_name"],
                        "outcome": "eliminated",
                        "team": pick,
                        "streak": player["correct_streak"],
                    })
                    logger.info(
                        f"[SURVIVOR] {player['user_name']} eliminated ({pick} lost) "
                        f"Week {week} guild {guild_id}"
                    )

                else:
                    # Correct pick — increment streak
                    new_streak = await db.increment_survivor_streak(season, guild_id, uid)
                    survivors.append(uid)
                    won_by_streak = new_streak >= SURVIVOR_WIN_STREAK
                    results.append({
                        "user_id": uid,
                        "user_name": player["user_name"],
                        "outcome": "winner" if won_by_streak else "survived",
                        "team": pick,
                        "streak": new_streak,
                    })
                    logger.info(
                        f"[SURVIVOR] {player['user_name']} survived ({pick} won) "
                        f"Week {week} streak={new_streak} guild {guild_id}"
                    )

            # -- Win condition: last survivors standing -------------------------
            # Scenario A: everyone eliminated simultaneously → all were finalists
            if not survivors and newly_eliminated:
                for r in results:
                    if r["outcome"] in ("eliminated", "no_pick"):
                        r["outcome"] = "winner"
                        logger.info(
                            f"[SURVIVOR] {r['user_name']} declared winner "
                            f"(all-eliminated scenario) Week {week}"
                        )

            # Scenario B: some survived and now nobody else is left alive
            elif survivors:
                remaining_alive = await db.get_alive_survivor_players(season, guild_id)
                if len(remaining_alive) == len(survivors):
                    # Check if previous alive count was larger (i.e. some just got eliminated)
                    if newly_eliminated:
                        for r in results:
                            if r["outcome"] == "survived":
                                r["outcome"] = "winner"
                                logger.info(
                                    f"[SURVIVOR] {r['user_name']} declared winner "
                                    f"(last standing) Week {week}"
                                )

            guild_results[guild_id] = results

        return guild_results

    async def post_survivor_results(
        self, channel: discord.TextChannel, week: int, results: "list[dict]"
    ) -> None:
        """Post formatted survivor results to the survivor channel."""
        if not results:
            return

        winners = [r for r in results if r["outcome"] == "winner"]
        survived = [r for r in results if r["outcome"] == "survived"]
        eliminated = [r for r in results if r["outcome"] == "eliminated"]
        no_pick = [r for r in results if r["outcome"] == "no_pick"]

        lines = [f"**Week {week} Survivor Results**"]

        if winners:
            names = ", ".join(f"**{r['user_name']}**" for r in winners)
            streaks = ", ".join(str(r["streak"]) for r in winners)
            lines.append(f"\nTROPHY **SURVIVOR WINNER(S):** {names} (streak: {streaks})")

        if survived:
            lines.append("\nSURVIVED:")
            for r in survived:
                lines.append(f"  {r['user_name']} — picked {r['team']} (streak: {r['streak']})")

        if eliminated:
            lines.append("\nELIMINATED:")
            for r in eliminated:
                lines.append(f"  {r['user_name']} — picked {r['team']} (wrong)")

        if no_pick:
            lines.append("\nELIMINATED (no pick):")
            for r in no_pick:
                lines.append(f"  {r['user_name']}")

        await channel.send("\n".join(lines))

    # -- Admin commands --------------------------------------------------------

    @commands.command(name=CMD_SURVIVOR_SETUP)
    @commands.has_permissions(administrator=True)
    async def survivor_setup(self, ctx):
        """Set the current channel as the survivor channel for this guild.

        Usage: !survivor_setup
        Run this in the channel where survivor matchups should be posted.
        """
        db = get_db()
        season = get_current_season()
        guild_id = str(ctx.guild.id)
        channel_id = str(ctx.channel.id)

        existing = await db.get_survivor_config(guild_id)
        start_week = existing["start_week"] if existing else 1

        # Evict the old channel from in-memory structures before writing the new one
        if existing:
            old_ch_id = existing["channel_id"]
            self.survivor_channel_ids.discard(old_ch_id)
            # Drop any cached message -> week mappings that belonged to the old channel
            stale_msgs = [
                mid for mid, wk in list(self._msg_cache.items())
                if mid in self._msg_cache
            ]
            # Can't map message -> channel without a DB lookup, so just clear the whole
            # cache — it will repopulate from DB on next reaction event.
            if old_ch_id != ctx.channel.id:
                self._msg_cache.clear()
                logger.info(
                    f"[SURVIVOR] Guild {guild_id} reassigned from channel "
                    f"{old_ch_id} to {ctx.channel.id} — msg cache cleared"
                )

        await db.set_survivor_config(guild_id, channel_id, start_week, season)
        self.survivor_channel_ids.add(ctx.channel.id)

        # Reload the cache for the new channel from DB
        all_msgs = await db.get_all_survivor_messages(guild_id)
        for msg in all_msgs:
            self._msg_cache[msg["message_id"]] = msg["week"]

        await ctx.send(
            f"Survivor channel set to **#{ctx.channel.name}**.\n"
            f"Run `!survivor_start` here when you're ready to open the first week."
        )

    @commands.command(name=CMD_SURVIVOR_START)
    @commands.has_permissions(administrator=True)
    async def survivor_start(self, ctx, week_num: int = None):
        """Post this week's matchups in the survivor channel and open enrollment.

        Usage: !survivor_start [week]
        Omit week to use the current NFL week.
        """
        db = get_db()
        season = get_current_season()
        guild_id = str(ctx.guild.id)

        config = await db.get_survivor_config(guild_id)
        if not config:
            await ctx.send("No survivor channel configured. Run `!survivor_setup` first.")
            return

        if ctx.channel.id != config["channel_id"]:
            ch_mention = f"<#{config['channel_id']}>"
            await ctx.send(f"Run this command in {ch_mention}.")
            return

        schedule = load_full_schedule()

        if week_num is None:
            today = datetime.now(EASTERN)
            for wk in range(1, get_max_week() + 1):
                week_games = schedule.get(str(wk))
                if not week_games:
                    continue
                first_game = datetime.fromisoformat(
                    week_games[0]["date"].replace("Z", "+00:00")
                ).astimezone(EASTERN)
                days_since_tuesday = (first_game.weekday() - 1) % 7
                week_start = first_game - timedelta(days=days_since_tuesday)
                week_end = week_start + timedelta(days=6, hours=23, minutes=59)
                if week_start <= today <= week_end:
                    week_num = wk
                    break

        if week_num is None:
            await ctx.send("Could not determine the current week. Pass a week number explicitly.")
            return

        week_games = schedule.get(str(week_num))
        if not week_games:
            await ctx.send(f"No games found for Week {week_num}.")
            return

        await db.set_survivor_config(guild_id, str(ctx.channel.id), week_num, season)
        await db.clear_survivor_messages_for_week(season, week_num, guild_id)

        deadline = get_week_deadline(week_num)
        deadline_str = deadline.strftime("%A, %B %d at %I:%M %p ET") if deadline else ""

        await ctx.send(
            f"**Survivor Week {week_num}**\n"
            f"React with ONE team emoji to make your pick.\n"
            f"You cannot pick a team you've already used this season.\n"
            + (f"Picks lock at **{deadline_str}**\n" if deadline_str else "")
            + f"*First reaction this week auto-enrolls you.*"
        )

        channel_id_str = str(ctx.channel.id)
        for game in week_games:
            away, home = game["away"], game["home"]
            msg = await ctx.send(f"{away} @ {home}")
            await msg.add_reaction(NFL_TEAMS[away])
            await msg.add_reaction(NFL_TEAMS[home])

            await db.add_survivor_message(
                message_id=msg.id,
                guild_id=guild_id,
                channel_id=channel_id_str,
                season=season,
                week=week_num,
                team_a=away,
                team_b=home,
            )
            self._msg_cache[msg.id] = week_num

        await ctx.send(f"Week {week_num} survivor matchups posted! Good luck.")
        logger.info(
            f"[SURVIVOR] Posted Week {week_num} games in guild {ctx.guild.name} "
            f"({len(week_games)} matchups)"
        )

    @commands.command(name=CMD_SURVIVOR_PROCESS)
    @commands.has_permissions(administrator=True)
    async def survivor_process(self, ctx, week_num: int):
        """Manually process survivor results for a completed week.

        Usage: !survivor_process <week>
        Requires winners to be set first (!set_winners or !fetch_winners).
        """
        db = get_db()
        season = get_current_season()

        if not await db.has_winners(season, week_num):
            await ctx.send(f"No winners set for Week {week_num}. Set them first.")
            return

        guild_id = str(ctx.guild.id)
        config = await db.get_survivor_config(guild_id)
        if not config:
            await ctx.send("Survivor is not configured for this server.")
            return

        await ctx.send(f"Processing Survivor Week {week_num}...")
        guild_results = await self.process_survivor_week(week_num)

        results = guild_results.get(guild_id, [])
        if not results:
            await ctx.send("No alive survivors to process.")
            return

        survivor_ch = self.bot.get_channel(config["channel_id"])
        if survivor_ch:
            await self.post_survivor_results(survivor_ch, week_num, results)
            await db.mark_survivor_results_posted(season, week_num, guild_id)
        else:
            await ctx.send("Survivor channel not accessible — cannot post results.")

        await ctx.send(f"Survivor Week {week_num} processed.")

    @commands.command(name=CMD_SURVIVOR_STATUS)
    @commands.has_permissions(administrator=True)
    async def survivor_status(self, ctx):
        """Show survivor config and game state for this guild."""
        db = get_db()
        season = get_current_season()
        guild_id = str(ctx.guild.id)

        config = await db.get_survivor_config(guild_id)
        if not config:
            await ctx.send("Survivor is not configured. Run `!survivor_setup`.")
            return

        survivor_ch = self.bot.get_channel(config["channel_id"])
        ch_name = f"#{survivor_ch.name}" if survivor_ch else f"<unknown {config['channel_id']}>"

        alive = await db.get_alive_survivor_players(season, guild_id)
        all_players = await db.get_all_survivor_players(season, guild_id)

        await ctx.send(
            f"**Survivor Config**\n"
            f"Channel: {ch_name}\n"
            f"Start week: {config['start_week']}\n"
            f"Season: {config['season']}\n"
            f"Total enrolled: {len(all_players)}\n"
            f"Still alive: {len(alive)}"
        )

    @commands.command(name=CMD_SURVIVOR_STANDINGS)
    async def survivor_standings(self, ctx):
        """Show current survivor standings for this guild."""
        db = get_db()
        season = get_current_season()
        guild_id = str(ctx.guild.id)

        config = await db.get_survivor_config(guild_id)
        if not config:
            await ctx.send("Survivor is not running in this server.")
            return

        all_players = await db.get_all_survivor_players(season, guild_id)
        if not all_players:
            await ctx.send("No players enrolled in Survivor yet.")
            return

        alive = [p for p in all_players if not p["eliminated"]]
        eliminated = [p for p in all_players if p["eliminated"]]

        lines = ["**Survivor Standings**\n"]

        if alive:
            lines.append(f"**Alive ({len(alive)})**")
            for p in alive:
                lines.append(f"  {p['user_name']} — {p['correct_streak']} correct")
        else:
            lines.append("**No players remaining alive.**")

        if eliminated:
            lines.append(f"\n**Eliminated ({len(eliminated)})**")
            for p in sorted(eliminated, key=lambda x: x["eliminated_week"] or 0, reverse=True):
                lines.append(
                    f"  {p['user_name']} — out Week {p['eliminated_week']}, "
                    f"streak was {p['correct_streak']}"
                )

        await ctx.send("\n".join(lines))

    @commands.command(name=CMD_SURVIVOR_MYPICKS)
    async def survivor_mypicks(self, ctx):
        """Show your pick history and teams used in Survivor this season."""
        db = get_db()
        season = get_current_season()
        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)

        config = await db.get_survivor_config(guild_id)
        if not config:
            await ctx.send("Survivor is not running in this server.")
            return

        player = await db.get_survivor_player(season, guild_id, user_id)
        if not player:
            await ctx.send("You are not enrolled in Survivor this season.")
            return

        async with db._conn.execute(
            """SELECT week, team FROM survivor_picks
               WHERE season=? AND guild_id=? AND user_id=?
               ORDER BY week ASC""",
            (season, guild_id, user_id),
        ) as cur:
            pick_rows = await cur.fetchall()

        winners_by_week: dict[int, set[str]] = {}
        for row in pick_rows:
            wk = row["week"]
            if wk not in winners_by_week:
                w = await db.get_winners(season, wk)
                winners_by_week[wk] = set(w) if w else set()

        lines = [f"**Your Survivor Season** ({ctx.author.display_name})\n"]

        if player["eliminated"]:
            lines.append(f"STATUS: Eliminated in Week {player['eliminated_week']}")
        else:
            lines.append(f"STATUS: Alive — {player['correct_streak']} correct picks")

        lines.append("\n**Pick History:**")
        for row in pick_rows:
            wk = row["week"]
            team = row["team"]
            wset = winners_by_week.get(wk, set())
            if not wset:
                result = "(pending)"
            elif team in wset:
                result = "WIN"
            else:
                result = "LOSS"
            lines.append(f"  Week {wk}: {team} — {result}")

        if not pick_rows:
            lines.append("  No picks recorded yet.")

        await ctx.send("\n".join(lines))

    @commands.command(name=CMD_SURVIVOR_FIX_PICK)
    @commands.has_permissions(administrator=True)
    async def survivor_fix_pick(self, ctx, action: str, username: str, team: str, week_num: int):
        """Manually add or remove a survivor pick for a user.

        Usage:
          !survivor_fix_pick add    porosus KC 15
          !survivor_fix_pick remove porosus KC 15
        """
        db = get_db()
        season = get_current_season()
        guild_id = str(ctx.guild.id)

        action = action.lower()
        team = team.upper()

        if team not in NFL_TEAMS:
            await ctx.send(f"Unknown team: `{team}`")
            return

        if action not in ("add", "remove"):
            await ctx.send("Action must be `add` or `remove`.")
            return

        async with db._conn.execute(
            """SELECT user_id FROM survivor_status
               WHERE season=? AND guild_id=? AND user_name=?""",
            (season, guild_id, username),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await ctx.send(f"Player `{username}` not found in survivor this season.")
            return

        user_id = row["user_id"]

        if action == "add":
            await db.set_survivor_pick(season, week_num, guild_id, user_id, username, team)
            await ctx.send(f"Set survivor pick: **{username}** → `{team}` Week {week_num}")
        else:
            removed = await db.remove_survivor_pick(season, week_num, guild_id, user_id)
            if removed:
                await ctx.send(f"Removed survivor pick: **{username}** → `{removed}` Week {week_num}")
            else:
                await ctx.send(f"No survivor pick found for **{username}** in Week {week_num}.")

    @commands.command(name=CMD_SURVIVOR_ELIMINATE)
    @commands.has_permissions(administrator=True)
    async def survivor_eliminate(self, ctx, username: str, week_num: int):
        """Manually eliminate a survivor player.

        Usage: !survivor_eliminate porosus 7
        """
        db = get_db()
        season = get_current_season()
        guild_id = str(ctx.guild.id)

        async with db._conn.execute(
            """SELECT user_id, eliminated FROM survivor_status
               WHERE season=? AND guild_id=? AND user_name=?""",
            (season, guild_id, username),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await ctx.send(f"Player `{username}` not found in survivor this season.")
            return

        if row["eliminated"]:
            await ctx.send(f"**{username}** is already eliminated.")
            return

        await db.eliminate_survivor_player(season, guild_id, row["user_id"], week_num)
        await ctx.send(f"Eliminated **{username}** from Survivor (Week {week_num}).")


async def setup(bot):
    await bot.add_cog(SurvivorGame(bot))
