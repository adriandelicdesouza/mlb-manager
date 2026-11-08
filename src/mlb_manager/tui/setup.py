"""League setup and financial-rules screens.

* :class:`LeagueSetupScreen` - before a new league is generated: starting
  season, Standard MLB or custom financial rules, and a summary.
* :class:`LoadReviewScreen` - after a save is read: its financial rules (and
  any upgrade notes) for review, with continue / edit / cancel.
* :class:`RulesEditScreen` - change a loaded league's rules, with a preview of
  what the change affects.

The screens only collect input; validation and the rules themselves live in
``finance.py`` and text in ``views.py``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Input, Select, Static

from mlb_manager import finance, views
from mlb_manager.league import DEFAULT_YEAR, League
from mlb_manager.tui.modals import ConfirmScreen

if TYPE_CHECKING:
    from mlb_manager.tui.app import MLBManagerApp

PRESET_OPTIONS = [(finance.PRESET_LABELS[p], p) for p in finance.PRESETS]
CAP_OPTIONS = [("No salary cap (unlimited payroll)", finance.CAP_NONE),
               ("Soft cap (exceptions + penalty)", finance.CAP_SOFT),
               ("Hard cap", finance.CAP_HARD)]


def _amount_text(value: float) -> str:
    return finance.format_amount(value)


def _rate_text(value: float) -> str:
    return finance.format_rate(value)


class RulesForm(Vertical):
    """Editable custom rules; inputs appear only where the chosen options need them."""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="form-row"):
            yield Static("Salary cap", classes="form-label")
            yield Select(CAP_OPTIONS, allow_blank=False, value=finance.CAP_NONE, id="rf-cap-type")
            yield Input(placeholder="Cap amount, e.g. $200M", id="rf-cap-amount", classes="money")
        with Vertical(id="rf-soft"):
            with Horizontal(classes="form-row"):
                yield Static("Soft-cap penalty", classes="form-label")
                yield Input(placeholder="per $ over, e.g. 100%", id="rf-soft-rate", classes="money")
                yield Static("Trade matching", classes="form-label-short")
                yield Input(placeholder="e.g. 125%", id="rf-trade-match", classes="money")
            for key, label in finance.SOFT_CAP_EXCEPTION_SHORT.items():
                yield Checkbox(f"Exception: {label}", id=f"rf-exc-{key}", classes="form-check")
        with Horizontal(classes="form-row"):
            yield Checkbox("Salary floor", id="rf-floor", classes="form-toggle")
            yield Input(placeholder="Floor amount, e.g. $110M", id="rf-floor-amount", classes="money")
        with Horizontal(classes="form-row"):
            yield Checkbox("Competitive Balance Tax", id="rf-cbt", classes="form-toggle")
            yield Input(placeholder="Threshold, e.g. $244M", id="rf-cbt-threshold", classes="money")
            yield Input(placeholder="Rates 20/30/50", id="rf-cbt-rates", classes="money")
        with Horizontal(classes="form-row"):
            yield Checkbox("Revenue sharing", id="rf-rs", classes="form-toggle")
            yield Input(placeholder="Pooled share, e.g. 48%", id="rf-rs-rate", classes="money")
        with Horizontal(classes="form-row"):
            yield Static("League minimum salary", classes="form-label")
            yield Input(placeholder="e.g. $780,000", id="rf-min", classes="money")

    # -- loading and reading -------------------------------------------------------

    def load(self, r: finance.FinancialRules) -> None:
        self.query_one("#rf-cap-type", Select).value = r.cap_type
        self.query_one("#rf-cap-amount", Input).value = _amount_text(r.cap_amount or finance.SUGGESTED_CAP)
        self.query_one("#rf-soft-rate", Input).value = _rate_text(r.soft_cap_tax_rate)
        self.query_one("#rf-trade-match", Input).value = _rate_text(r.trade_match_pct)
        for key in finance.SOFT_CAP_EXCEPTIONS:
            self.query_one(f"#rf-exc-{key}", Checkbox).value = key in r.soft_cap_exceptions
        self.query_one("#rf-floor", Checkbox).value = r.floor_enabled
        self.query_one("#rf-floor-amount", Input).value = _amount_text(r.floor_amount or finance.SUGGESTED_FLOOR)
        self.query_one("#rf-cbt", Checkbox).value = r.cbt_enabled
        self.query_one("#rf-cbt-threshold", Input).value = _amount_text(r.cbt_threshold)
        self.query_one("#rf-cbt-rates", Input).value = "/".join(f"{x * 100:g}" for x in r.cbt_rates)
        self.query_one("#rf-rs", Checkbox).value = r.revenue_sharing
        self.query_one("#rf-rs-rate", Input).value = _rate_text(r.revenue_sharing_rate)
        self.query_one("#rf-min", Input).value = _amount_text(r.min_salary)
        self.sync()

    def read(self, base: finance.FinancialRules) -> tuple[finance.FinancialRules | None, list[str]]:
        """The rules in the form on top of ``base``, or the input errors."""
        errors: list[str] = []

        def amount(widget_id: str, label: str, fallback: float) -> float:
            try:
                return finance.parse_amount(self.query_one(widget_id, Input).value)
            except ValueError:
                errors.append(f"{label}: enter an amount such as $200M or 780k.")
                return fallback

        def rate(widget_id: str, label: str, fallback: float) -> float:
            try:
                return finance.parse_rate(self.query_one(widget_id, Input).value)
            except ValueError:
                errors.append(f"{label}: enter a percentage such as 50%.")
                return fallback

        cap_type = cast(str, self.query_one("#rf-cap-type", Select).value)
        values: dict[str, object] = {"cap_type": cap_type}
        if cap_type != finance.CAP_NONE:
            values["cap_amount"] = amount("#rf-cap-amount", "Salary cap", base.cap_amount)
        if cap_type == finance.CAP_SOFT:
            values["soft_cap_tax_rate"] = rate("#rf-soft-rate", "Soft-cap penalty", base.soft_cap_tax_rate)
            values["trade_match_pct"] = rate("#rf-trade-match", "Trade matching", base.trade_match_pct)
            values["soft_cap_exceptions"] = tuple(
                k for k in finance.SOFT_CAP_EXCEPTIONS if self.query_one(f"#rf-exc-{k}", Checkbox).value)
        values["floor_enabled"] = self.query_one("#rf-floor", Checkbox).value
        if values["floor_enabled"]:
            values["floor_amount"] = amount("#rf-floor-amount", "Salary floor", base.floor_amount)
        values["cbt_enabled"] = self.query_one("#rf-cbt", Checkbox).value
        if values["cbt_enabled"]:
            values["cbt_threshold"] = amount("#rf-cbt-threshold", "CBT threshold", base.cbt_threshold)
            parts = [p for p in self.query_one("#rf-cbt-rates", Input).value.replace(",", "/").split("/") if p.strip()]
            try:
                rates = tuple(finance.parse_rate(p if p.strip().endswith("%") else p.strip() + "%") for p in parts)
                if len(rates) != 3:
                    raise ValueError
                values["cbt_rates"] = rates
            except ValueError:
                errors.append("CBT rates: enter three percentages such as 20/30/50.")
        values["revenue_sharing"] = self.query_one("#rf-rs", Checkbox).value
        if values["revenue_sharing"]:
            values["revenue_sharing_rate"] = rate("#rf-rs-rate", "Revenue sharing", base.revenue_sharing_rate)
        values["min_salary"] = amount("#rf-min", "League minimum salary", base.min_salary)
        rules = replace(base, **values)  # type: ignore[arg-type]
        errors += finance.validate_rules(rules)
        return (None, errors) if errors else (rules, [])

    def sync(self) -> None:
        """Show only the inputs the selected options use."""
        cap_type = self.query_one("#rf-cap-type", Select).value
        self.query_one("#rf-cap-amount", Input).display = cap_type != finance.CAP_NONE
        self.query_one("#rf-soft", Vertical).display = cap_type == finance.CAP_SOFT
        self.query_one("#rf-floor-amount", Input).display = self.query_one("#rf-floor", Checkbox).value
        cbt = self.query_one("#rf-cbt", Checkbox).value
        self.query_one("#rf-cbt-threshold", Input).display = cbt
        self.query_one("#rf-cbt-rates", Input).display = cbt
        self.query_one("#rf-rs-rate", Input).display = self.query_one("#rf-rs", Checkbox).value

    def set_locked(self, locked: bool) -> None:
        for widget in self.query("Input, Select, Checkbox"):
            widget.disabled = locked


class _RulesScreen(Screen[None]):
    """Shared behaviour: a preset selector, the form and a live summary."""

    BINDINGS = [("escape", "back", "Back")]

    @property
    def mlb(self) -> MLBManagerApp:
        return cast("MLBManagerApp", self.app)

    def season(self) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    def preset(self) -> str:
        return cast(str, self.query_one("#setup-preset", Select).value)

    def base_rules(self) -> finance.FinancialRules:
        return finance.default_custom_rules(self.season())

    def current(self) -> tuple[finance.FinancialRules | None, list[str]]:
        if self.preset() == finance.PRESET_MLB:
            return finance.mlb_rules(self.season()), []
        return self.query_one(RulesForm).read(self.base_rules())

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "setup-preset":
            form = self.query_one(RulesForm)
            if self.preset() == finance.PRESET_MLB:
                form.load(finance.mlb_rules(self.season()))
            form.set_locked(self.preset() == finance.PRESET_MLB)
        self.query_one(RulesForm).sync()
        self.refresh_summary()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self.query_one(RulesForm).sync()
        self.refresh_summary()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.refresh_summary()

    def refresh_summary(self) -> None:
        rules, errors = self.current()
        self.query_one("#setup-errors", Static).update("\n".join(f"! {e}" for e in errors))
        if rules is not None:
            title = f"Financial structure: {finance.PRESET_LABELS[self.preset()]}"
            self.query_one("#setup-summary", Static).update(views.rules_view(replace(rules, season=self.season()),
                                                                             title))

    def action_back(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class LeagueSetupScreen(_RulesScreen):
    """New league: starting season and financial rules before generation."""

    def __init__(self, team: str, seed: int, salary_cap: float | None = None) -> None:
        super().__init__()
        self.team = team
        self.seed = seed
        self.salary_cap = salary_cap

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-box"):
            with VerticalScroll(id="setup-scroll"):
                yield Static("New league setup", classes="pane-title")
                yield Static(f"Club {self.team} · seed {self.seed}. Choose the season and the league's financial "
                             "structure; defaults follow the actual MLB rules.", classes="hint")
                with Horizontal(classes="form-row"):
                    yield Static("Starting season", classes="form-label")
                    yield Input(str(DEFAULT_YEAR), id="setup-season", type="integer", classes="money")
                with Horizontal(classes="form-row"):
                    yield Static("Financial structure", classes="form-label")
                    yield Select(PRESET_OPTIONS, allow_blank=False, value=finance.PRESET_MLB, id="setup-preset")
                yield RulesForm(id="rules-form")
                yield Static("", id="setup-errors", classes="form-errors")
                yield Static("", id="setup-summary", classes="panel mono")
            with Horizontal(classes="actions setup-actions"):
                yield Button("Start league", id="setup-start", classes="act")
                yield Button("Back", id="setup-back", classes="act")

    def on_mount(self) -> None:
        form = self.query_one(RulesForm)
        if self.salary_cap is not None:
            self.query_one("#setup-preset", Select).value = finance.PRESET_CUSTOM
            form.load(replace(self.base_rules(), cap_type=finance.CAP_HARD, cap_amount=self.salary_cap))
            form.set_locked(False)
        else:
            form.load(finance.mlb_rules(self.season()))
            form.set_locked(True)
        self.refresh_summary()

    def season(self) -> int:
        raw = self.query_one("#setup-season", Input).value.strip()
        try:
            year = int(raw)
        except ValueError:
            return DEFAULT_YEAR
        return year if not finance.validate_season(year) else DEFAULT_YEAR

    def season_errors(self) -> list[str]:
        raw = self.query_one("#setup-season", Input).value.strip()
        if not raw.lstrip("-").isdigit():
            return ["Starting season: enter a year."]
        return finance.validate_season(int(raw))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "setup-season" and self.preset() == finance.PRESET_MLB:
            self.query_one(RulesForm).load(finance.mlb_rules(self.season()))
        self.refresh_summary()

    def refresh_summary(self) -> None:
        super().refresh_summary()
        extra = self.season_errors()
        if extra:
            errors = self.query_one("#setup-errors", Static)
            errors.update("\n".join(f"! {e}" for e in extra))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "setup-back":
            self.action_back()
            return
        if event.button.id != "setup-start":
            return
        rules, errors = self.current()
        errors = self.season_errors() + errors
        if errors or rules is None:
            self.app.notify(errors[0] if errors else "Invalid settings.", severity="error")
            return
        year = self.season()
        config = finance.new_config(year, self.preset(), rules if self.preset() == finance.PRESET_CUSTOM else None)
        self.mlb.create_league(self.team, self.seed, year, config)

    def action_back(self) -> None:
        self.mlb.back_to_title()


class LoadReviewScreen(Screen[None]):
    """A save's financial rules before play continues."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, league: League, path: Path) -> None:
        super().__init__()
        self.league = league
        self.path = path
        self.changed = False

    @property
    def mlb(self) -> MLBManagerApp:
        return cast("MLBManagerApp", self.app)

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-box"):
            with VerticalScroll(id="setup-scroll"):
                yield Static(f"Load {self.path.name}", classes="pane-title")
                yield Static("", id="review-info", classes="hint")
                yield Static("", id="review-notes", classes="form-errors")
                yield Static("", id="review-rules", classes="panel mono")
                yield Static("", id="review-payroll", classes="mono")
            with Horizontal(classes="actions setup-actions"):
                yield Button("Continue", id="review-continue", classes="act")
                yield Button("Edit financial rules", id="review-edit", classes="act")
                yield Button("Cancel", id="review-cancel", classes="act")

    def on_mount(self) -> None:
        self.refresh_view()

    def on_screen_resume(self) -> None:
        self.refresh_view()

    def refresh_view(self) -> None:
        lg = self.league
        self.query_one("#review-info", Static).update(
            f"{lg.user.full_name} · {lg.year} · {views.phase_label(lg)} · seed {lg.seed}")
        notes = finance.migration_notes(lg) if lg.finance.migrated else []
        if self.changed:
            notes = ["Financial rules changed (save the league to keep them)."] + notes
        self.query_one("#review-notes", Static).update(
            ("Upgraded save: " + " ".join(notes)) if lg.finance.migrated else "\n".join(notes))
        self.query_one("#review-rules", Static).update(
            views.rules_view(finance.rules(lg), "Current financial rules"))
        self.query_one("#review-payroll", Static).update(views.payroll_status(lg, lg.user))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "review-continue":
            self.league.finance.migrated = False
            self.mlb.finish_load(self.league, self.path, self.changed)
        elif event.button.id == "review-edit":
            self.app.push_screen(RulesEditScreen(self.league), self._edited)
        elif event.button.id == "review-cancel":
            self.action_cancel()

    def _edited(self, changed: bool | None) -> None:
        if changed:
            self.changed = True
        self.refresh_view()

    def action_cancel(self) -> None:
        self.mlb.back_to_title()


class RulesEditScreen(_RulesScreen):
    """Change a loaded league's financial rules (from the current contract season on)."""

    def __init__(self, league: League) -> None:
        super().__init__()
        self.league = league

    def compose(self) -> ComposeResult:
        season = finance.contract_season(self.league)
        with Vertical(id="setup-box"):
            with VerticalScroll(id="setup-scroll"):
                yield Static(f"Edit financial rules (from the {season} season)", classes="pane-title")
                yield Static("Contracts, payrolls and history are preserved; earlier seasons keep their rules.",
                             classes="hint")
                with Horizontal(classes="form-row"):
                    yield Static("Financial structure", classes="form-label")
                    yield Select(PRESET_OPTIONS, allow_blank=False, id="setup-preset",
                                 value=self.league.finance.config.preset_for(season))
                yield RulesForm(id="rules-form")
                yield Static("", id="setup-errors", classes="form-errors")
                yield Static("", id="edit-impact", classes="panel mono")
                yield Static("", id="setup-summary", classes="panel mono")
            with Horizontal(classes="actions setup-actions"):
                yield Button("Apply", id="edit-apply", classes="act")
                if self.league.finance.config.legacy_salary_cap is not None:
                    yield Button(f"Restore previous {finance.format_amount(self.league.finance.config.legacy_salary_cap)}"
                                 " hard cap", id="edit-legacy", classes="act")
                yield Button("Back", id="edit-back", classes="act")

    def season(self) -> int:
        return finance.contract_season(self.league)

    def on_mount(self) -> None:
        form = self.query_one(RulesForm)
        form.load(finance.rules(self.league))
        form.set_locked(self.preset() == finance.PRESET_MLB)
        self.refresh_summary()

    def refresh_summary(self) -> None:
        super().refresh_summary()
        rules, errors = self.current()
        impact = self.query_one("#edit-impact", Static)
        if rules is None:
            impact.update("Fix the highlighted settings to see what changes.")
            return
        custom = rules if self.preset() == finance.PRESET_CUSTOM else None
        lines = finance.preview_change(self.league, self.preset(), custom)
        impact.update("What changes\n" + "\n".join(f"- {line}" for line in lines))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "edit-back":
            self.dismiss(False)
        elif event.button.id == "edit-legacy":
            legacy = finance.restore_legacy_cap_rules(self.league)
            if legacy is not None:
                self.query_one("#setup-preset", Select).value = finance.PRESET_CUSTOM
                form = self.query_one(RulesForm)
                form.load(legacy)
                form.set_locked(False)
                self.refresh_summary()
        elif event.button.id == "edit-apply":
            rules, errors = self.current()
            if rules is None:
                self.app.notify(errors[0], severity="error")
                return
            preset = self.preset()
            custom = rules if preset == finance.PRESET_CUSTOM else None
            lines = finance.preview_change(self.league, preset, custom)

            def confirmed(ok: bool | None) -> None:
                if ok:
                    result = finance.change_rules(self.league, preset, custom)
                    self.app.notify(result[0])
                    self.dismiss(True)

            self.app.push_screen(ConfirmScreen("Apply these financial rules?\n\n" + "\n".join(
                f"- {line}" for line in lines), yes="Apply"), confirmed)

    def action_back(self) -> None:
        self.dismiss(False)
