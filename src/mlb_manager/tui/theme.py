"""Visual theme: a dark, understated palette with a subtle per-club accent."""

from __future__ import annotations

# Muted versions of each club's primary colour (used sparingly: club tag, active nav).
TEAM_ACCENTS: dict[str, str] = {
    "BAL": "#d98b4a", "BOS": "#c76b6b", "NYY": "#7f9cc9", "TB": "#7fa3cf", "TOR": "#6f9bd6",
    "CWS": "#a9adb3", "CLE": "#c76b6b", "DET": "#d98b5a", "KC": "#6f9bd6", "MIN": "#7f9cc9",
    "ATH": "#7fae86", "HOU": "#e09a5f", "LAA": "#c76b6b", "SEA": "#5fae9f", "TEX": "#7f9cc9",
    "ATL": "#c76b6b", "MIA": "#5fb3c9", "NYM": "#e09a5f", "PHI": "#c76b6b", "WSH": "#c76b6b",
    "CHC": "#6f8fd6", "CIN": "#c76b6b", "MIL": "#d6b46b", "PIT": "#d6c06b", "STL": "#c76b6b",
    "ARI": "#b8737d", "COL": "#9a8bc4", "LAD": "#6f9bd6", "SD": "#c4a77a", "SF": "#e09a5f",
}
DEFAULT_ACCENT = "#8fa7c4"


def accent(abbr: str | None) -> str:
    return TEAM_ACCENTS.get(abbr or "", DEFAULT_ACCENT)


CSS = """
Screen {
    background: #101315;
    color: #cfd4d9;
}
#topbar {
    height: 1;
    background: #181c20;
    color: #b9c0c7;
    padding: 0 1;
}
#body {
    height: 1fr;
}
#sidebar {
    width: 24;
    background: #14181b;
    border-right: solid #262b30;
}
#main {
    width: 1fr;
    padding: 0 1;
}
.section-label {
    color: #69727b;
    text-style: bold;
    padding: 1 1 0 1;
}
Button {
    min-width: 6;
}
Button.nav, Button.sim {
    width: 100%;
    height: 1;
    border: none;
    background: #14181b;
    color: #9aa3ac;
    content-align: left middle;
    text-align: left;
    padding: 0 1;
    text-style: none;
}
Button.nav:hover, Button.sim:hover {
    background: #1d2226;
    color: #e3e7ea;
}
Button.nav:focus, Button.sim:focus {
    color: #e3e7ea;
    text-style: bold;
}
Button.nav.-current {
    background: #20262b;
    color: #e8ebee;
    text-style: bold;
}
Button.act {
    height: 1;
    border: none;
    background: #232a30;
    color: #d3d8dc;
    margin: 0 1 0 0;
    padding: 0 1;
    text-style: none;
}
Button.act:hover {
    background: #2e363d;
}
Button.act:focus {
    background: #34404a;
    text-style: bold;
}
Button.act.-danger {
    color: #e3a0a0;
}
Button.act:disabled {
    color: #59616a;
    background: #1a1f23;
}
.actions {
    height: 1;
    margin: 1 0 0 0;
    overflow-x: auto;
    scrollbar-size-horizontal: 0;
}
FranchiseScreen.-narrow #sidebar {
    width: 18;
}
.toolbar {
    height: auto;
}
.toolbar > Static {
    padding: 1 2;
}
.pane-title {
    color: #e3e7ea;
    text-style: bold;
    height: 1;
    margin: 0 0 0 0;
}
.hint {
    color: #6f7881;
    height: auto;
}
.panel {
    border: round #2a3036;
    border-title-color: #8b949d;
    border-title-style: bold;
    padding: 0 1;
    height: auto;
}
.mono {
    height: auto;
}
.status-line {
    color: #a7c0a0;
    height: auto;
    margin: 1 0 0 0;
}
DataTable {
    background: #121619;
    height: 1fr;
    scrollbar-size: 1 1;
}
DataTable > .datatable--header {
    background: #1a1f23;
    color: #8b949d;
    text-style: bold;
}
DataTable > .datatable--cursor {
    background: #2b3a46;
    color: #f0f3f5;
}
TabbedContent {
    height: 1fr;
}
VerticalScroll {
    scrollbar-size: 1 1;
}
Select {
    width: 34;
}
#trade-partner, #title-team {
    width: 56;
}
Input {
    width: 30;
}
#dash-grid {
    grid-size: 3;
    grid-gutter: 0 1;
    height: auto;
}
.split {
    height: 1fr;
}
.half {
    width: 1fr;
}
ModalScreen {
    align: center middle;
    background: #0b0d0f 70%;
}
.dialog {
    width: 64;
    height: auto;
    max-height: 90%;
    background: #171b1f;
    border: round #3a434b;
    padding: 1 2;
}
.dialog-wide {
    width: 120;
    height: 90%;
    background: #171b1f;
    border: round #3a434b;
    padding: 0 1;
}
.dialog-buttons {
    height: 1;
    margin: 1 0 0 0;
}
#title-box {
    width: 72;
    height: auto;
    border: round #2f363d;
    padding: 1 2;
    background: #14181b;
}
TitleScreen {
    align: center middle;
}
#dug-top {
    height: auto;
}
#dug-score {
    width: 1fr;
    min-width: 30;
}
#dug-situation {
    width: 38;
}
#dug-matchup {
    width: 1fr;
    min-width: 40;
}
#dug-prompt {
    height: auto;
    border: round #a98f5a;
    border-title-color: #d6b46b;
}
#dug-tabs {
    width: 1fr;
}
#dug-controls-box {
    width: 52;
    padding: 0 0 0 1;
}
#dug-controls {
    height: 1fr;
}
#dug-hint {
    height: 3;
}
#dug-unit {
    width: 18;
    height: 1;
    margin: 0 1 0 0;
}
#dug-unit > SelectCurrent {
    border: none;
    height: 1;
    padding: 0 1;
}
#dug-situation {
    min-width: 34;
}
FranchiseScreen.-narrow #dug-controls-box {
    width: 36;
}
FranchiseScreen.-narrow #dug-score, FranchiseScreen.-narrow #dug-matchup {
    display: none;
}
FranchiseScreen.-narrow #dug-situation {
    width: 1fr;
}
#choice-list {
    height: auto;
    max-height: 20;
    margin: 1 0 0 0;
}
"""
