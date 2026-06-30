"""
Bot-wide logging configuration.

Sets up a consistent log format across all cogs and modules:
    2026-04-26 15:41:01 STARTUP:INFO: ✅ Loaded NFL_Locks.cogs.admin

The DESIGNATOR field identifies which part of the bot the log came from,
derived automatically from the logger name — no changes needed in individual cogs.

Designator mapping (prefix-based):
    startup.*   →  STARTUP   (bot.py lifecycle, extension loading)
    cogs.*      →  LOCKS     (NFL Locks cogs)
    trade_eval.*→  TRADE     (Trade Evaluator cog)
    discord.*   →  BOT       (discord.py framework internals)
    everything else → BOT

Log file location: data/bot.log
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Log file lives in the top-level data/ directory alongside the SQLite DB
# so all persistent bot files are in one place.
LOG_DIR = Path(__file__).parent.parent / "data"
LOG_FILE = LOG_DIR / "bot.log"

# Maps logger name prefixes to human-readable designators.
# Order matters — more specific prefixes should come first.
_DESIGNATOR_MAP = [
    ("startup",     "STARTUP"),
    ("cogs.",       "LOCKS"),
    ("trade_eval.", "TRADE"),
    ("discord.",    "BOT"),
    ("discord",     "BOT"),
]
_DEFAULT_DESIGNATOR = "BOT"


class BotLogFormatter(logging.Formatter):
    """
    Custom formatter that replaces the logger name with a short designator.

    Output format:
        2026-04-26 15:41:01 STARTUP:INFO: <message>
    """

    def _get_designator(self, name: str) -> str:
        for prefix, designator in _DESIGNATOR_MAP:
            if name == prefix or name.startswith(prefix):
                return designator
        return _DEFAULT_DESIGNATOR

    def format(self, record: logging.LogRecord) -> str:
        designator = self._get_designator(record.name)
        time_str = self.formatTime(record, datefmt="%Y-%m-%d %H:%M:%S")
        msg = record.getMessage()
        formatted = f"{time_str} {designator}:{record.levelname}: {msg}"

        # Append exception traceback if present
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            formatted += f"\n{record.exc_text}"

        return formatted


def setup_logging() -> Path:
    """
    Configure bot-wide logging. Call once at the top of bot.py before anything else.

    - File handler: INFO and above → data/bot.log (rotating, 5 MB × 5 files)
    - Console handler: INFO and above → stdout

    Returns the resolved log file path so bot.py can reference it if needed.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = BotLogFormatter()

    file_handler = RotatingFileHandler(
        filename=LOG_FILE,
        encoding="utf-8",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        mode="a",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Clear any handlers python/discord.py may have already added
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    return LOG_FILE
