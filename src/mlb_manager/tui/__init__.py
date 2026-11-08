"""Interactive, mouse-enabled terminal interface built on Textual.

The simulation engine never imports this package; it only uses the public
``League`` API and the domain modules. ``available()`` lets the entry point
fall back to the classic CLI when Textual is not installed.
"""

from __future__ import annotations


def available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def run(**kwargs: object) -> int:
    from mlb_manager.tui.app import run as _run

    return _run(**kwargs)  # type: ignore[arg-type]
