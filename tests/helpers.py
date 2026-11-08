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
