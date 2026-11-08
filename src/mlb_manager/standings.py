"""Standings computation, tiebreakers and playoff seeding."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from mlb_manager.models import Team
from mlb_manager.schedule import ScheduledGame

PLAYOFF_TEAMS_PER_LEAGUE = 6


@dataclass(slots=True)
class TeamRecord:
    abbr: str
    wins: int = 0
    losses: int = 0
    home_wins: int = 0
    home_losses: int = 0
    away_wins: int = 0
    away_losses: int = 0
    runs_for: int = 0
    runs_against: int = 0
    division_wins: int = 0
    division_losses: int = 0
    results: list[bool] = field(default_factory=list)  # chronological, True = win
    head_to_head: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def pct(self) -> float:
        return self.wins / self.games if self.games else 0.0

    @property
    def run_diff(self) -> int:
        return self.runs_for - self.runs_against

    @property
    def last10(self) -> str:
        recent = self.results[-10:]
        wins = sum(recent)
        return f"{wins}-{len(recent) - wins}"

    @property
    def streak(self) -> str:
        if not self.results:
            return "-"
        last = self.results[-1]
        count = 0
        for result in reversed(self.results):
            if result != last:
                break
            count += 1
        return f"{'W' if last else 'L'}{count}"


def compute_records(games: Iterable[ScheduledGame], teams: Mapping[str, Team]) -> dict[str, TeamRecord]:
    records = {abbr: TeamRecord(abbr) for abbr in teams}
    for g in games:
        if not g.played:
            continue
        away, home = records[g.away], records[g.home]
        assert g.away_runs is not None and g.home_runs is not None
        home_won = g.home_runs > g.away_runs
        away.runs_for += g.away_runs
        away.runs_against += g.home_runs
        home.runs_for += g.home_runs
        home.runs_against += g.away_runs
        same_division = teams[g.away].division_key == teams[g.home].division_key
        if home_won:
            home.wins += 1
            home.home_wins += 1
            away.losses += 1
            away.away_losses += 1
            home.head_to_head[g.away][0] += 1
            away.head_to_head[g.home][1] += 1
            if same_division:
                home.division_wins += 1
                away.division_losses += 1
        else:
            away.wins += 1
            away.away_wins += 1
            home.losses += 1
            home.home_losses += 1
            away.head_to_head[g.home][0] += 1
            home.head_to_head[g.away][1] += 1
            if same_division:
                away.division_wins += 1
                home.division_losses += 1
        home.results.append(home_won)
        away.results.append(not home_won)
    return records


def _h2h_pct(record: TeamRecord, opponents: Iterable[str]) -> float:
    wins = losses = 0
    for opp in opponents:
        w, l_ = record.head_to_head.get(opp, (0, 0))
        wins += w
        losses += l_
    return wins / (wins + losses) if wins + losses else 0.5


def rank(abbrs: Iterable[str], records: Mapping[str, TeamRecord]) -> list[str]:
    """Order clubs by winning percentage with MLB-style tiebreakers.

    Ties are broken by head-to-head record among the tied clubs, then by
    division record, then run differential, then alphabetically (stable and
    deterministic).
    """
    abbrs = list(abbrs)
    groups: dict[float, list[str]] = defaultdict(list)
    for abbr in abbrs:
        groups[round(records[abbr].pct, 9)].append(abbr)
    ordered: list[str] = []
    for pct in sorted(groups, reverse=True):
        tied = groups[pct]
        if len(tied) == 1:
            ordered.extend(tied)
            continue

        def key(abbr: str, tied: list[str] = tied) -> tuple[float, float, int, str]:
            rec = records[abbr]
            others = [t for t in tied if t != abbr]
            div_games = rec.division_wins + rec.division_losses
            div_pct = rec.division_wins / div_games if div_games else 0.5
            return (-_h2h_pct(rec, others), -div_pct, -rec.run_diff, abbr)

        ordered.extend(sorted(tied, key=key))
    return ordered


def games_back(leader: TeamRecord, record: TeamRecord) -> float:
    return ((leader.wins - record.wins) + (record.losses - leader.losses)) / 2


def division_standings(
    teams: Mapping[str, Team], records: Mapping[str, TeamRecord], league: str, division: str
) -> list[str]:
    members = [t.abbr for t in teams.values() if t.league == league and t.division == division]
    return rank(members, records)


def playoff_seeds(
    teams: Mapping[str, Team], records: Mapping[str, TeamRecord], league: str
) -> list[str]:
    """Return the 6 playoff clubs of a league in seed order (index 0 = seed 1)."""
    divisions = sorted({t.division for t in teams.values() if t.league == league})
    winners = [division_standings(teams, records, league, d)[0] for d in divisions]
    winners = rank(winners, records)
    others = [t.abbr for t in teams.values() if t.league == league and t.abbr not in winners]
    wild_cards = rank(others, records)[: PLAYOFF_TEAMS_PER_LEAGUE - len(winners)]
    return winners + wild_cards


def wild_card_standings(
    teams: Mapping[str, Team], records: Mapping[str, TeamRecord], league: str
) -> list[str]:
    """Non-division-leaders ranked for the wild card race."""
    divisions = sorted({t.division for t in teams.values() if t.league == league})
    leaders = {division_standings(teams, records, league, d)[0] for d in divisions}
    others = [t.abbr for t in teams.values() if t.league == league and t.abbr not in leaders]
    return rank(others, records)
