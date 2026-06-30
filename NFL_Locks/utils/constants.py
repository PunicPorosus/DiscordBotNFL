from BotUtils.time import EASTERN  # noqa: F401 — re-exported for existing cog imports
from BotUtils.constants import OWNER_ID  # noqa: F401 — re-exported for existing cog imports

NFL_TEAMS = {
    "ARI": "<:Cardinals:1438626449631346839>",
    "ATL": "<:Falcons:1438626450453299311>",
    "BAL": "<:Ravens:1438626451166597263>",
    "BUF": "<:Bills:1438626451711725670>",
    "CAR": "<:Panthers:1438626452705906850>",
    "CHI": "<:Bears:1438626453762605087>",
    "CIN": "<:Bengals:1438626454656123125>",
    "CLE": "<:Browns:1438626455582937159>",
    "DAL": "<:Cowboys:1438626456916852989>",
    "DEN": "<:Broncos:1438626457768427702>",
    "DET": "<:Lions:1438626459735560293>",
    "GB": "<:Packers:1438626460461174925>",
    "HOU": "<:Texans:1438626461228466306>",
    "IND": "<:Colts:1438626462327505029>",
    "JAX": "<:Jaguars:1438626463770480661>",
    "KC": "<:Chiefs:1438626464797819002>",
    "LV": "<:Raiders:1438626467876569212>",
    "LAC": "<:Chargers:1438626466408431777>",
    "LAR": "<:Rams:1438626467344027709>",
    "MIA": "<:Dolphins:1438626469554294794>",
    "MIN": "<:Vikings:1438626470611390514>",
    "NE": "<:Patriots:1438626471949107327>",
    "NO": "<:Saints:1438626473266249738>",
    "NYG": "<:Giants:1438626474197516449>",
    "NYJ": "<:Jets:1438626475459739689>",
    "PHI": "<:Eagles:1438626476655251516>",
    "PIT": "<:Steelers:1438626477821268019>",
    "SEA": "<:Seahawks:1438626479050067968>",
    "SF": "<:49ers:1438626480383987742>",
    "TB": "<:Buccaneers:1438626481353003049>",
    "TEN": "<:Titans:1438626482279944382>",
    "WSH": "<:Commanders:1438626483051692155>"
}

# Pre-built O(1) reverse lookup tables — built once at import time.
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
