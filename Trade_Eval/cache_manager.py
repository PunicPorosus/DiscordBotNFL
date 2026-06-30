"""
Cache Manager for Trade_Eval.

Handles syncing draft pick data from external sources into the local SQLite cache.

Mock mode — Google Sheets (one CSV fetch per round, 7 requests total).

Design decisions:
- aiohttp for all HTTP so nothing blocks the Discord event loop
- Partial mock syncs are saved; admin notified of which rounds failed
- Consecutive mock failures are tracked in DB; admin notified at 1 and 2,
  then silent until reset via !trade.execute mock reset or a successful sync
- A _syncing flag prevents overlapping syncs
"""

import logging
import aiohttp
from collections import defaultdict
from datetime import datetime, timezone

from Trade_Eval.database import Database
from Trade_Eval.pick_loader import _parse_round_sheet
from BotUtils.notify import notify_admin

logger = logging.getLogger("trade_eval.cache_manager")


class CacheManager:

    def __init__(self, db: Database, bot):
        self.db = db
        self.bot = bot
        # Prevent two syncs of the same mode running simultaneously
        self._syncing: dict[str, bool] = {"mock": False}

    # -- Public API ---------------------------------------------------------

    async def load_to_memory(self, mode: str) -> dict[str, list[int]]:
        """Load all cached teams for a mode from DB into a dict."""
        return await self.db.get_all_teams(mode)

    def is_stale(self, last_sync: datetime | None, threshold_hours: int = 24) -> bool:
        """
        Return True if last_sync is None or older than threshold_hours.
        Uses UTC throughout to avoid DST edge cases.
        """
        if last_sync is None:
            return True
        age_seconds = (datetime.now(timezone.utc) - last_sync).total_seconds()
        return age_seconds > threshold_hours * 3600

    async def sync_mock_from_sheet(self, sheet_url: str, round_gids: dict) -> bool:
        """
        Sync all teams from Google Sheets into the mock cache.

        Fetches one CSV per round (7 total), aggregates picks by team, then
        saves to DB. If some rounds fail, partial data is still saved and
        admin is notified of which rounds were lost.

        Returns True if at least one round succeeded.
        """
        if self._syncing["mock"]:
            logger.warning("Mock sync already in progress — skipping duplicate request")
            return False
        self._syncing["mock"] = True
        try:
            return await self._do_mock_sync(sheet_url, round_gids)
        finally:
            self._syncing["mock"] = False

    # -- Mock sync ----------------------------------------------------------

    async def _do_mock_sync(self, sheet_url: str, round_gids: dict) -> bool:
        logger.info("Mock sync started")

        if "/d/" not in sheet_url:
            logger.error("Mock sync aborted: invalid Google Sheets URL")
            await self.db.update_sync_status("mock", "failed", error="Invalid sheet URL")
            return False

        sheet_id = sheet_url.split("/d/")[1].split("/")[0]
        team_picks: dict[str, list[int]] = defaultdict(list)
        failed_rounds: list[int] = []
        succeeded_rounds: list[int] = []

        async with aiohttp.ClientSession() as session:
            for round_num in range(1, 8):
                gid = round_gids.get(round_num)
                if gid is None:
                    logger.warning(f"Round {round_num}: GID not configured, skipping")
                    continue

                try:
                    csv_text = await self._fetch_sheet_csv(session, sheet_id, gid)
                    round_data = _parse_round_sheet(csv_text)  # {team_name: pick_number}

                    for team, pick in round_data.items():
                        team_picks[team].append(pick)

                    succeeded_rounds.append(round_num)
                    logger.info(f"Round {round_num}: {len(round_data)} teams loaded")

                except Exception as e:
                    failed_rounds.append(round_num)
                    logger.error(f"Round {round_num} failed: {e}")

        if not team_picks:
            error_msg = f"All rounds failed: {failed_rounds}"
            logger.error(f"Mock sync failed completely: {error_msg}")
            await self.db.update_sync_status("mock", "failed", url=sheet_url, error=error_msg)
            consecutive = await self.db.increment_consecutive_failures("mock")
            if consecutive <= 2:
                await notify_admin(
                    self.bot,
                    f"⚠️ **Mock draft sync failed completely** — no picks loaded.\n"
                    f"Failed rounds: {failed_rounds}\nCheck logs for details.\n"
                    f"Consecutive failures: {consecutive}. Run `!trade.execute mock reset` after fixing."
                )
            return False

        # Save whatever landed
        for team, picks in team_picks.items():
            await self.db.save_team_picks("mock", team, sorted(picks))

        if failed_rounds:
            error_msg = f"Rounds failed: {failed_rounds}"
            await self.db.update_sync_status("mock", "partial", url=sheet_url, error=error_msg)
            consecutive = await self.db.increment_consecutive_failures("mock")
            if consecutive <= 2:
                await notify_admin(
                    self.bot,
                    f"⚠️ **Mock draft sync partial** — {len(succeeded_rounds)}/7 rounds loaded.\n"
                    f"Failed rounds: {failed_rounds} | Succeeded: {succeeded_rounds}\n"
                    f"Consecutive failures: {consecutive}. Run `!trade.execute mock reset` after fixing."
                )
            logger.warning(f"Mock sync partial — succeeded: {succeeded_rounds}, failed: {failed_rounds}")
        else:
            await self.db.update_sync_status("mock", "success", url=sheet_url)
            await self.db.reset_consecutive_failures("mock")
            logger.info(f"Mock sync complete — {len(team_picks)} teams across {len(succeeded_rounds)} rounds")

        return True

    async def _fetch_sheet_csv(
        self, session: aiohttp.ClientSession, sheet_id: str, gid: str
    ) -> str:
        """Fetch a single Google Sheets tab as CSV text."""
        url = (
            f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            f"/export?format=csv&gid={gid}"
        )
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.text()
