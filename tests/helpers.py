"""Shared fixtures for the test suite."""

from mlb_manager.league import PHASE_COMPLETE, League


def league_at_offseason(seed: int = 50, user: str = "NYY") -> League:
    """A fresh league fast-forwarded to 'season complete' without playing games.

    Standings are all 0-0, so the draft order falls back to the deterministic
    tiebreakers; that is fine for exercising offseason mechanics quickly.
    """
    league = League.create(seed=seed, user_team=user)
    league.phase = PHASE_COMPLETE
    return league


def set_rules(league: League, **changes: object) -> None:
    """Give ``league`` custom financial rules (from its contract season), e.g. ``cap_type="hard"``."""
    from dataclasses import replace

    from mlb_manager import finance

    season = finance.contract_season(league)
    rules = replace(finance.default_custom_rules(season), **changes)  # type: ignore[arg-type]
    league.finance.config.set_rules(season, finance.PRESET_CUSTOM, rules)
    league.finance._cache.clear()


def hard_cap(league: League, amount: float) -> None:
    set_rules(league, cap_type="hard", cap_amount=amount)
