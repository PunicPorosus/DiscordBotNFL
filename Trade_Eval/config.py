"""
Trade Evaluator Configuration
Update CURRENT_DRAFT_YEAR annually or use !trade.year.update command
"""

from pathlib import Path

# Current draft year - update this each year or use !trade.year.update
CURRENT_DRAFT_YEAR = 2027

# Database path - resolves relative to this file, so it always lands at
# Trade_Eval/data/trade_eval.db regardless of where the bot is launched from
DB_PATH = Path(__file__).parent / "data" / "trade_eval.db"

# Google Sheets URL for mock draft pick ownership
PICK_SHEET_URL = "https://docs.google.com/spreadsheets/d/1VtV7u6OAIhSUQ6rgijhMCD7D7SkK18tmi334AkMfhZs/edit"
