"""Versioned JSON franchise saves."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from mlb_manager import __version__, frontoffice
from mlb_manager.league import League

SAVE_FORMAT_VERSION = 3
SUPPORTED_VERSIONS = (1, 2, 3)
SAVE_SUFFIX = ".json"
SAVE_DIR_ENV = "MLB_MANAGER_SAVE_DIR"


class SaveError(Exception):
    """Raised when a save file cannot be written or read."""


def default_save_dir() -> Path:
    return Path(os.environ.get(SAVE_DIR_ENV) or Path.cwd() / "saves")


def default_save_name(league: League) -> str:
    return f"{league.user_team.lower()}_{league.year}"


def resolve_save_path(name_or_path: str | os.PathLike[str], directory: Path | None = None) -> Path:
    """Turn a bare save name like ``my_club`` into ``<dir>/my_club.json``."""
    raw = str(name_or_path).strip()
    if not raw:
        raise SaveError("Save name cannot be empty.")
    path = Path(raw).expanduser()
    if path.parent == Path(".") and not raw.startswith("."):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.name)
        path = (directory or default_save_dir()) / safe
    if path.suffix != SAVE_SUFFIX:
        path = path.with_name(path.name + SAVE_SUFFIX)
    return path


def save_league(league: League, path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    payload: dict[str, Any] = {
        "format_version": SAVE_FORMAT_VERSION,
        "app_version": __version__,
        "league": league.to_dict(),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=SAVE_SUFFIX)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise SaveError(f"Could not write save file {target}: {exc}") from exc
    return target


def load_league(path: str | os.PathLike[str]) -> League:
    source = Path(path)
    try:
        with source.open(encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        raise SaveError(f"Save file not found: {source}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise SaveError(f"Could not read save file {source}: {exc}") from exc
    if not isinstance(payload, dict) or "league" not in payload:
        raise SaveError(f"{source} is not an MLB Manager save file.")
    version = payload.get("format_version")
    if version not in SUPPORTED_VERSIONS:
        raise SaveError(
            f"Unsupported save format version {version!r} (supported: {SUPPORTED_VERSIONS})."
        )
    try:
        league = League.from_dict(payload["league"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"Save file {source} is corrupt: {exc!r}") from exc
    if version == 1:
        migrate_v1_to_v2(league)
    if version in (1, 2):
        migrate_v2_to_v3(league)
    return league


def migrate_v1_to_v2(league: League) -> None:
    """v1 saves predate the front office: add contracts, potential and draft picks.

    The initialisation is seeded from the league seed, so migrating the same
    save twice gives identical results.
    """
    frontoffice.initialize(league)
    league.add_news("Save upgraded: contracts, salary cap, draft picks and scouting are now active.")


def migrate_v2_to_v3(league: League) -> None:
    """v2 saves predate injuries, service time and position games.

    Everyone starts healthy with an empty IL (the defaults); service time is
    estimated from age and games-by-position from games played this season.
    """
    league.assign_initial_service()
    for team in league.teams.values():
        for slot in team.lineup:
            player = league.players[slot.player_id]
            if player.batting.g and not player.games_by_pos:
                player.games_by_pos = {slot.position: player.batting.g}
    for player in league.players.values():
        if player.batting.g and not player.games_by_pos and not player.is_pitcher:
            player.games_by_pos = {player.position: player.batting.g}
    league.add_news("Save upgraded: injuries, in-season development and awards are now active.")


def list_saves(directory: Path | None = None) -> list[Path]:
    folder = directory or default_save_dir()
    if not folder.is_dir():
        return []
    return sorted(
        (p for p in folder.glob(f"*{SAVE_SUFFIX}") if not p.name.startswith(".")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
