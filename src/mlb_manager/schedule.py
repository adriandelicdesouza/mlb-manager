"""Balanced 162-game regular-season schedule generation.

Matchup counts follow the modern MLB format:

* 13 games against each of the 4 division rivals            = 52
* 6 or 7 games against each of the 10 other league clubs    = 64
* 4 games against the natural interleague rival             =  4
* 3 games against each of the other 14 interleague clubs    = 42
                                                              ----
                                                               162
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from mlb_manager.franchises import DIVISIONS, FRANCHISES, NATURAL_RIVALS, get_franchise

GAMES_PER_TEAM = 162
SERIES_SPLITS: dict[int, tuple[int, ...]] = {
    13: (4, 3, 3, 3),
    7: (4, 3),
    6: (3, 3),
    4: (2, 2),
    3: (3,),
}


@dataclass(slots=True)
class ScheduledGame:
    game_id: int
    day: int
    away: str
    home: str
    away_runs: int | None = None
    home_runs: int | None = None
    innings: int | None = None
    winning_pitcher: int | None = None
    losing_pitcher: int | None = None
    save_pitcher: int | None = None

    @property
    def played(self) -> bool:
        return self.away_runs is not None

    @property
    def winner(self) -> str | None:
        if not self.played:
            return None
        return self.home if self.home_runs > self.away_runs else self.away  # type: ignore[operator]

    @property
    def loser(self) -> str | None:
        if not self.played:
            return None
        return self.away if self.winner == self.home else self.home

    def involves(self, abbr: str) -> bool:
        return abbr in (self.away, self.home)

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "day": self.day,
            "away": self.away,
            "home": self.home,
            "away_runs": self.away_runs,
            "home_runs": self.home_runs,
            "innings": self.innings,
            "winning_pitcher": self.winning_pitcher,
            "losing_pitcher": self.losing_pitcher,
            "save_pitcher": self.save_pitcher,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledGame:
        return cls(**data)


def matchup_counts() -> dict[tuple[str, str], int]:
    """Number of games between each unordered pair (keys sorted alphabetically)."""
    counts: dict[tuple[str, str], int] = {}
    division_index = {d: i for i, d in enumerate(DIVISIONS)}
    # Position of each club inside its division (0-4), used for the 7-game pattern.
    slot: dict[str, int] = {}
    per_div: dict[tuple[str, str], int] = defaultdict(int)
    for f in FRANCHISES:
        slot[f.abbr] = per_div[(f.league, f.division)]
        per_div[(f.league, f.division)] += 1
    rivals = {frozenset(pair) for pair in NATURAL_RIVALS}

    abbrs = [f.abbr for f in FRANCHISES]
    for i, a in enumerate(abbrs):
        fa = get_franchise(a)
        for b in abbrs[i + 1 :]:
            fb = get_franchise(b)
            key = (a, b) if a < b else (b, a)
            if fa.league != fb.league:
                counts[key] = 4 if frozenset((a, b)) in rivals else 3
            elif fa.division == fb.division:
                counts[key] = 13
            else:
                da, db = division_index[fa.division], division_index[fb.division]
                # Team k in division d plays 7 games vs slots k, k+1 of division d+1
                # (and therefore vs slots k, k-1 of division d-1). Symmetric, 4 per club.
                if (db - da) % 3 == 1:
                    seven = (slot[b] - slot[a]) % 5 in (0, 1)
                else:
                    seven = (slot[a] - slot[b]) % 5 in (0, 1)
                counts[key] = 7 if seven else 6
    return counts


def _build_series(rng: random.Random) -> list[tuple[str, str, int]]:
    """Split matchups into (away, home, length) series with balanced home games."""
    pairs = sorted(matchup_counts().items())
    rng.shuffle(pairs)
    home_games: dict[str, int] = defaultdict(int)
    series: list[tuple[str, str, int]] = []
    for (a, b), total in pairs:
        if home_games[a] != home_games[b]:
            first_home = a if home_games[a] < home_games[b] else b
        else:
            first_home = a if rng.random() < 0.5 else b
        other = b if first_home == a else a
        for index, length in enumerate(SERIES_SPLITS[total]):
            home = first_home if index % 2 == 0 else other
            away = other if home == first_home else first_home
            series.append((away, home, length))
            home_games[home] += length
    _balance_home_games(series, home_games)
    return series


def _balance_home_games(series: list[tuple[str, str, int]], home_games: dict[str, int]) -> None:
    """Flip series venues while doing so moves both clubs closer to 81 home games."""
    target = GAMES_PER_TEAM / 2
    improved = True
    while improved:
        improved = False
        for index, (away, home, length) in enumerate(series):
            before = (home_games[home] - target) ** 2 + (home_games[away] - target) ** 2
            after = (home_games[home] - length - target) ** 2 + (
                home_games[away] + length - target
            ) ** 2
            if after < before:
                series[index] = (home, away, length)
                home_games[home] -= length
                home_games[away] += length
                improved = True


def generate_schedule(seed: int | str) -> list[ScheduledGame]:
    """Create the full regular-season schedule, sorted by day."""
    rng = random.Random(f"{seed}:schedule")
    remaining = _build_series(rng)
    load: dict[str, int] = defaultdict(int)
    for away, home, length in remaining:
        load[away] += length
        load[home] += length

    games: list[ScheduledGame] = []
    day = 0
    while remaining:
        rng.shuffle(remaining)
        remaining.sort(key=lambda s: -(load[s[0]] + load[s[1]]))
        busy: set[str] = set()
        chosen: list[tuple[str, str, int]] = []
        leftover: list[tuple[str, str, int]] = []
        for s in remaining:
            away, home, _ = s
            if away in busy or home in busy:
                leftover.append(s)
            else:
                busy.update((away, home))
                chosen.append(s)
        span = max(length for _, _, length in chosen)
        for away, home, length in chosen:
            for offset in range(length):
                games.append(ScheduledGame(0, day + offset, away, home))
            load[away] -= length
            load[home] -= length
        day += span
        remaining = leftover

    games.sort(key=lambda g: (g.day, g.home))
    for game_id, game in enumerate(games, start=1):
        game.game_id = game_id
    return games
