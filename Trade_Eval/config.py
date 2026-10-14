"""
Trade Evaluator Configuration
Update CURRENT_DRAFT_YEAR annually or use !trade.year.update command
"""

from datetime import time as _time
from pathlib import Path

from BotUtils.time import EASTERN

# Current draft year - update this each year or use !trade.year.update
CURRENT_DRAFT_YEAR = 2027

# Database path - resolves relative to this file, so it always lands at
# Trade_Eval/data/trade_eval.db regardless of where the bot is launched from
DB_PATH = Path(__file__).parent / "data" / "trade_eval.db"

# Google Sheets URL for mock draft pick ownership
PICK_SHEET_URL = "https://docs.google.com/spreadsheets/d/1VtV7u6OAIhSUQ6rgijhMCD7D7SkK18tmi334AkMfhZs/edit"


# -- Scheduled clock times (ET) ------------------------------------------------
# Mirrors NFL_Locks/utils/config.py. Kept separate on purpose: Trade Eval is a
# standalone module and should not have to import NFL Locks config to know when
# it syncs. Same timezone, same pattern, its own value.
#
# @tasks.loop(time=...) fires at the wall-clock time whatever time the process
# started. The sync was an hourly interval loop that threw away 23 of every 24
# ticks to act only in hour 3, landing on whatever minute the last restart fell
# on.
AUTO_SYNC_HOUR = 3                 # Hour (ET) the nightly cache sync runs
AUTO_SYNC_TIME = _time(AUTO_SYNC_HOUR, 0, tzinfo=EASTERN)
