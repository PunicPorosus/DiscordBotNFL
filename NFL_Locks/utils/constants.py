from BotUtils.time import EASTERN  # noqa: F401, re-exported for existing cog imports
from BotUtils.constants import OWNER_ID  # noqa: F401, re-exported for existing cog imports

# Team reaction emoji: APPLICATION emoji owned by the bot, not guild emoji.
#
# These are listed under Emojis in the Discord Developer Portal, not in any
# server's emoji list, so they do not appear in discord.py's bot.emojis and
# they work in every guild the app is installed to without Use External Emoji.
#
# The previous IDs here (1438...) were stale: every add_reaction returned
# "400 Bad Request (10014): Unknown Emoji", so no matchup could be picked and
# the hourly catchup reposted the week forever. Re-check these against the
# portal if reactions ever stop appearing.
NFL_TEAMS = {
    "ARI": "<:cardinals:1413172503534239917>",
    "ATL": "<:falcons:1413172385913245749>",
    "BAL": "<:ravens:1413172539240349859>",
    "BUF": "<:bills:1413172397569216582>",
    "CAR": "<:panthers:1413172338014556230>",
    "CHI": "<:bears:1413172415457918996>",
    "CIN": "<:bengals:1413172767238393987>",
    "CLE": "<:browns:1413172435015962684>",
    "DAL": "<:cowboys:1413172876529373204>",
    "DEN": "<:broncos:1413172361514979338>",
    "DET": "<:lions:1413172695415128165>",
    "GB": "<:packers:1413172714239168682>",
    "HOU": "<:texans:1413172459393388748>",
    "IND": "<:colts:1413174176985907260>",
    "JAX": "<:jaguars:1413172373355630612>",
    "KC": "<:chiefs:1413172736796135445>",
    "LV": "<:raiders:1413172349691232296>",
    "LAC": "<:chargers:1413172704814694430>",
    "LAR": "<:rams:1413172847026765894>",
    "MIA": "<:dolphins:1413172635021344858>",
    "MIN": "<:vikings:1413172448555175956>",
    "NE": "<:patriots:1413172568067670097>",
    "NO": "<:saints:1413172863145476137>",
    "NYG": "<:giants:1413172481576931348>",
    "NYJ": "<:jets:1413172470705295420>",
    "PHI": "<:eagles:1413172669758570566>",
    "PIT": "<:steelers:1413172647566508062>",
    "SEA": "<:seahawks:1413174223161004034>",
    "SF": "<:49ers:1413172524585455676>",
    "TB": "<:buccaneers:1413172292141187102>",
    "TEN": "<:titans:1413172723928137829>",
    "WSH": "<:commanders:1413172328128446474>",
}

# Pre-built O(1) reverse lookup tables, built once at import time.
# Maps emoji ID string (e.g. "1438626449631346839") → team abbreviation for
# custom Discord emojis (<:Name:ID>), and raw emoji string → abbreviation for
# standard unicode emojis.
_EMOJI_ID_TO_TEAM: dict[str, str] = {}
_EMOJI_STR_TO_TEAM: dict[str, str] = {}

for _abbr, _emoji_str in NFL_TEAMS.items():
    if _emoji_str.startswith('<:') and _emoji_str.endswith('>'):
        _parts = _emoji_str.split(':')
        if len(_parts) >= 3:
            _EMOJI_ID_TO_TEAM[_parts[2].rstrip('>')] = _abbr
    else:
        _EMOJI_STR_TO_TEAM[_emoji_str] = _abbr


def emoji_to_team(emoji) -> str | None:
    """Convert an emoji (str, PartialEmoji, or Emoji) to an NFL team abbreviation.

    Uses pre-built lookup dicts for O(1) resolution.
    Returns None if the emoji does not match any known team.
    """
    reaction_str = str(emoji)
    if reaction_str.startswith('<:') and reaction_str.endswith('>'):
        parts = reaction_str.split(':')
        if len(parts) >= 3:
            return _EMOJI_ID_TO_TEAM.get(parts[2].rstrip('>'))
        return None
    return _EMOJI_STR_TO_TEAM.get(reaction_str)
