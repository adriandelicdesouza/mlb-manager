"""Twelve-team postseason bracket.

Per league: seeds 1-3 are division winners, 4-6 are wild cards.

* Wild Card Series (best of 3, all games at the higher seed): 3 v 6, 4 v 5
* Division Series (best of 5, 2-2-1): 1 v winner(4/5), 2 v winner(3/6)
* League Championship Series (best of 7, 2-3-2)
* World Series (best of 7, 2-3-2), home field to the better regular-season record
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ROUND_NAMES: tuple[str, ...] = (
    "Wild Card Series",
    "Division Series",
    "Championship Series",
    "World Series",
)
ROUND_BEST_OF: tuple[int, ...] = (3, 5, 7, 7)
# True = higher seed hosts that game.
HOME_PATTERNS: dict[int, tuple[bool, ...]] = {
    3: (True, True, True),
    5: (True, True, False, False, True),
    7: (True, True, False, False, False, True, True),
}


@dataclass(slots=True)
class SeriesGame:
    away: str
    home: str
    away_runs: int
    home_runs: int
    innings: int

    @property
    def winner(self) -> str:
        return self.home if self.home_runs > self.away_runs else self.away

    def to_dict(self) -> dict[str, Any]:
        return {
            "away": self.away,
            "home": self.home,
            "away_runs": self.away_runs,
            "home_runs": self.home_runs,
            "innings": self.innings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SeriesGame:
        return cls(**data)


@dataclass(slots=True)
class Series:
    series_id: str
    round_index: int
    league: str  # "AL", "NL" or "MLB" for the World Series
    high: str  # club holding home-field advantage
    low: str
    best_of: int
    games: list[SeriesGame] = field(default_factory=list)

    @property
    def round_name(self) -> str:
        return ROUND_NAMES[self.round_index]

    @property
    def wins_needed(self) -> int:
        return self.best_of // 2 + 1

    def wins(self, abbr: str) -> int:
        return sum(1 for g in self.games if g.winner == abbr)

    @property
    def winner(self) -> str | None:
        for abbr in (self.high, self.low):
            if self.wins(abbr) >= self.wins_needed:
                return abbr
        return None

    @property
    def loser(self) -> str | None:
        winner = self.winner
        if winner is None:
            return None
        return self.low if winner == self.high else self.high

    @property
    def is_over(self) -> bool:
        return self.winner is not None

    def next_matchup(self) -> tuple[str, str]:
        """Return ``(away, home)`` for the next game."""
        if self.is_over:
            raise ValueError(f"Series {self.series_id} is already decided")
        high_home = HOME_PATTERNS[self.best_of][len(self.games)]
        return (self.low, self.high) if high_home else (self.high, self.low)

    def record(self, game: SeriesGame) -> None:
        if self.is_over:
            raise ValueError(f"Series {self.series_id} is already decided")
        if {game.away, game.home} != {self.high, self.low}:
            raise ValueError("Game does not belong to this series")
        self.games.append(game)

    def summary(self) -> str:
        hw, lw = self.wins(self.high), self.wins(self.low)
        if self.winner:
            w, l_ = (self.high, self.low) if self.winner == self.high else (self.low, self.high)
            return f"{w} def. {l_} {max(hw, lw)}-{min(hw, lw)}"
        if hw == lw:
            return f"{self.high} vs {self.low}, tied {hw}-{lw}"
        leader = self.high if hw > lw else self.low
        return f"{self.high} vs {self.low}, {leader} leads {max(hw, lw)}-{min(hw, lw)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "series_id": self.series_id,
            "round_index": self.round_index,
            "league": self.league,
            "high": self.high,
            "low": self.low,
            "best_of": self.best_of,
            "games": [g.to_dict() for g in self.games],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Series:
        return cls(
            series_id=data["series_id"],
            round_index=int(data["round_index"]),
            league=data["league"],
            high=data["high"],
            low=data["low"],
            best_of=int(data["best_of"]),
            games=[SeriesGame.from_dict(g) for g in data["games"]],
        )


@dataclass(slots=True)
class Playoffs:
    seeds: dict[str, list[str]]  # league -> clubs in seed order
    rounds: list[list[Series]] = field(default_factory=list)

    @classmethod
    def create(cls, seeds: dict[str, list[str]]) -> Playoffs:
        for league, clubs in seeds.items():
            if len(clubs) != 6:
                raise ValueError(f"{league} needs 6 playoff seeds, got {len(clubs)}")
        bracket = cls(seeds={k: list(v) for k, v in seeds.items()})
        first: list[Series] = []
        for league in sorted(seeds):
            s = seeds[league]
            first.append(Series(f"{league}-WC1", 0, league, s[2], s[5], ROUND_BEST_OF[0]))
            first.append(Series(f"{league}-WC2", 0, league, s[3], s[4], ROUND_BEST_OF[0]))
        bracket.rounds.append(first)
        return bracket

    def seed_of(self, abbr: str) -> int:
        for clubs in self.seeds.values():
            if abbr in clubs:
                return clubs.index(abbr) + 1
        raise KeyError(abbr)

    @property
    def current_round(self) -> list[Series]:
        return self.rounds[-1]

    @property
    def round_index(self) -> int:
        return len(self.rounds) - 1

    @property
    def champion(self) -> str | None:
        if len(self.rounds) == len(ROUND_NAMES):
            return self.rounds[-1][0].winner
        return None

    @property
    def is_complete(self) -> bool:
        return self.champion is not None

    def active_series(self) -> list[Series]:
        return [s for s in self.current_round if not s.is_over]

    def eliminated(self) -> set[str]:
        return {s.loser for rnd in self.rounds for s in rnd if s.loser}

    def advance_round(self, better_record: Callable[[str, str], str]) -> bool:
        """Create the next round once the current one is finished.

        ``better_record(a, b)`` must return whichever club had the better
        regular-season record (used for World Series home field).
        Returns True if a new round was created.
        """
        if self.active_series() or self.is_complete:
            return False
        nxt = self.round_index + 1
        by_id = {s.series_id: s for s in self.current_round}
        new: list[Series] = []
        if nxt == 1:
            for league in sorted(self.seeds):
                s = self.seeds[league]
                wc1 = by_id[f"{league}-WC1"].winner  # winner of 3 v 6
                wc2 = by_id[f"{league}-WC2"].winner  # winner of 4 v 5
                assert wc1 and wc2
                new.append(Series(f"{league}-DS1", 1, league, s[0], wc2, ROUND_BEST_OF[1]))
                new.append(Series(f"{league}-DS2", 1, league, s[1], wc1, ROUND_BEST_OF[1]))
        elif nxt == 2:
            for league in sorted(self.seeds):
                a = by_id[f"{league}-DS1"].winner
                b = by_id[f"{league}-DS2"].winner
                assert a and b
                high, low = (a, b) if self.seed_of(a) < self.seed_of(b) else (b, a)
                new.append(Series(f"{league}-CS", 2, league, high, low, ROUND_BEST_OF[2]))
        elif nxt == 3:
            al = by_id["AL-CS"].winner
            nl = by_id["NL-CS"].winner
            assert al and nl
            high = better_record(al, nl)
            low = nl if high == al else al
            new.append(Series("WS", 3, "MLB", high, low, ROUND_BEST_OF[3]))
        else:  # pragma: no cover - guarded by is_complete
            return False
        self.rounds.append(new)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "seeds": {k: list(v) for k, v in self.seeds.items()},
            "rounds": [[s.to_dict() for s in rnd] for rnd in self.rounds],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Playoffs:
        return cls(
            seeds={k: list(v) for k, v in data["seeds"].items()},
            rounds=[[Series.from_dict(s) for s in rnd] for rnd in data["rounds"]],
        )
