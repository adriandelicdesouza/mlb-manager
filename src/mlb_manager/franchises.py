"""Static franchise, league and division data for the 30 MLB clubs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Franchise:
    abbr: str
    city: str
    name: str
    league: str  # "AL" or "NL"
    division: str  # "East", "Central" or "West"

    @property
    def full_name(self) -> str:
        return f"{self.city} {self.name}"

    @property
    def division_key(self) -> str:
        return f"{self.league} {self.division}"


LEAGUES: tuple[str, ...] = ("AL", "NL")
DIVISIONS: tuple[str, ...] = ("East", "Central", "West")
LEAGUE_NAMES: dict[str, str] = {"AL": "American League", "NL": "National League"}

FRANCHISES: tuple[Franchise, ...] = (
    # American League East
    Franchise("BAL", "Baltimore", "Orioles", "AL", "East"),
    Franchise("BOS", "Boston", "Red Sox", "AL", "East"),
    Franchise("NYY", "New York", "Yankees", "AL", "East"),
    Franchise("TB", "Tampa Bay", "Rays", "AL", "East"),
    Franchise("TOR", "Toronto", "Blue Jays", "AL", "East"),
    # American League Central
    Franchise("CWS", "Chicago", "White Sox", "AL", "Central"),
    Franchise("CLE", "Cleveland", "Guardians", "AL", "Central"),
    Franchise("DET", "Detroit", "Tigers", "AL", "Central"),
    Franchise("KC", "Kansas City", "Royals", "AL", "Central"),
    Franchise("MIN", "Minnesota", "Twins", "AL", "Central"),
    # American League West
    Franchise("ATH", "Sacramento", "Athletics", "AL", "West"),
    Franchise("HOU", "Houston", "Astros", "AL", "West"),
    Franchise("LAA", "Los Angeles", "Angels", "AL", "West"),
    Franchise("SEA", "Seattle", "Mariners", "AL", "West"),
    Franchise("TEX", "Texas", "Rangers", "AL", "West"),
    # National League East
    Franchise("ATL", "Atlanta", "Braves", "NL", "East"),
    Franchise("MIA", "Miami", "Marlins", "NL", "East"),
    Franchise("NYM", "New York", "Mets", "NL", "East"),
    Franchise("PHI", "Philadelphia", "Phillies", "NL", "East"),
    Franchise("WSH", "Washington", "Nationals", "NL", "East"),
    # National League Central
    Franchise("CHC", "Chicago", "Cubs", "NL", "Central"),
    Franchise("CIN", "Cincinnati", "Reds", "NL", "Central"),
    Franchise("MIL", "Milwaukee", "Brewers", "NL", "Central"),
    Franchise("PIT", "Pittsburgh", "Pirates", "NL", "Central"),
    Franchise("STL", "St. Louis", "Cardinals", "NL", "Central"),
    # National League West
    Franchise("ARI", "Arizona", "Diamondbacks", "NL", "West"),
    Franchise("COL", "Colorado", "Rockies", "NL", "West"),
    Franchise("LAD", "Los Angeles", "Dodgers", "NL", "West"),
    Franchise("SD", "San Diego", "Padres", "NL", "West"),
    Franchise("SF", "San Francisco", "Giants", "NL", "West"),
)

FRANCHISES_BY_ABBR: dict[str, Franchise] = {f.abbr: f for f in FRANCHISES}

# Interleague "natural rivals" (AL, NL). Each club appears exactly once.
NATURAL_RIVALS: tuple[tuple[str, str], ...] = (
    ("BAL", "WSH"),
    ("BOS", "ATL"),
    ("NYY", "NYM"),
    ("TB", "MIA"),
    ("TOR", "PHI"),
    ("CWS", "CHC"),
    ("CLE", "CIN"),
    ("DET", "PIT"),
    ("KC", "STL"),
    ("MIN", "MIL"),
    ("ATH", "SF"),
    ("HOU", "COL"),
    ("LAA", "LAD"),
    ("SEA", "SD"),
    ("TEX", "ARI"),
)


def get_franchise(abbr: str) -> Franchise:
    try:
        return FRANCHISES_BY_ABBR[abbr.upper()]
    except KeyError:
        raise KeyError(f"Unknown franchise abbreviation: {abbr!r}") from None


def teams_in_division(league: str, division: str) -> list[str]:
    return [f.abbr for f in FRANCHISES if f.league == league and f.division == division]


def teams_in_league(league: str) -> list[str]:
    return [f.abbr for f in FRANCHISES if f.league == league]


def natural_rival(abbr: str) -> str:
    for al, nl in NATURAL_RIVALS:
        if abbr == al:
            return nl
        if abbr == nl:
            return al
    raise KeyError(abbr)
