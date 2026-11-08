import unittest

from mlb_manager import views
from mlb_manager.league import League


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.league = League.create(seed=12, user_team="CLE")
        cls.league.sim_days(5)

    def test_table_alignment(self) -> None:
        text = views.table(["Name", "N"], [["a", 1], ["bbb", 22]], "lr")
        lines = text.splitlines()
        self.assertEqual(lines[0], "Name   N")
        self.assertEqual(lines[3], "bbb   22")

    def test_rate_formatting(self) -> None:
        self.assertEqual(views.pct(0.3456), ".346")
        self.assertEqual(views.pct(1.0), "1.000")
        self.assertEqual(views.gb(0), "-")
        self.assertEqual(views.gb(2.5), "2.5")

    def test_player_card_shows_position_specific_ratings(self) -> None:
        players = self.league.players
        catcher = next(p for p in players.values() if p.position == "C")
        pitcher = next(p for p in players.values() if p.position == "SP")
        self.assertIn("Framing", views.player_card(self.league, catcher))
        card = views.player_card(self.league, pitcher)
        self.assertIn("Stamina", card)
        self.assertNotIn("Framing", card)

    def test_status_and_dashboard(self) -> None:
        status = views.status_line(self.league)
        self.assertIn("Cleveland Guardians", status)
        self.assertIn("AL Central", status)
        self.assertIn("Next starter", views.dashboard(self.league))

    def test_standings_mark_user_team(self) -> None:
        self.assertIn("*CLE", views.standings(self.league, "AL"))
        self.assertNotIn("*CLE", views.standings(self.league, "NL"))

    def test_bracket_before_postseason(self) -> None:
        self.assertIn("has not started", views.bracket_view(self.league))
