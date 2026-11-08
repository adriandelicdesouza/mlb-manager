"""Career and postseason statistics, retirements, franchise records, saves."""

import json
import tempfile
import unittest
from pathlib import Path

from mlb_manager import careers, injuries, trades, views
from mlb_manager.league import PHASE_REGULAR, League
from mlb_manager.models import BattingStats, PitchingStats, Player
from mlb_manager.persistence import load_league, save_league


def _full_season(seed: int = 21) -> League:
    league = League.create(seed=seed, user_team="NYY")
    league.sim_to_completion()
    return league


class DerivedStatsTests(unittest.TestCase):
    def test_zero_denominators_are_safe(self) -> None:
        b, p = BattingStats(), PitchingStats()
        self.assertEqual((b.avg, b.obp, b.slg, b.ops), (0.0, 0.0, 0.0, 0.0))
        self.assertEqual((p.era, p.whip, p.innings), (0.0, 0.0, 0.0))
        self.assertEqual(p.ip_display, "0.0")

    def test_derived_values(self) -> None:
        b = BattingStats(g=2, pa=10, ab=8, h=3, doubles=1, hr=1, bb=1, hbp=1)
        self.assertAlmostEqual(b.avg, 3 / 8)
        self.assertAlmostEqual(b.obp, 5 / 10)
        self.assertAlmostEqual(b.slg, (1 + 2 + 4) / 8)
        self.assertAlmostEqual(b.ops, b.obp + b.slg)
        p = PitchingStats(outs=27, er=3, h=6, bb=3)
        self.assertAlmostEqual(p.era, 3.0)
        self.assertAlmostEqual(p.whip, 1.0)

    def test_list_round_trip_and_minus(self) -> None:
        b = BattingStats(g=5, ab=20, h=7, hr=2, sb=1)
        self.assertEqual(BattingStats.from_list(b.to_list()), b)
        self.assertEqual(BattingStats.from_list(b.to_list()[:3]).g, 5)  # short lists pad with zeros
        diff = b.minus(BattingStats(g=1, ab=4, h=1))
        self.assertEqual((diff.g, diff.ab, diff.h, diff.hr), (4, 16, 6, 2))
        self.assertTrue(BattingStats().is_empty)


class StintTests(unittest.TestCase):
    def test_trade_splits_season_by_club_and_keeps_totals(self) -> None:
        player = Player(1, "A", "B", 27, "R", "R", "SS", team="NYY")
        player.batting = BattingStats(g=50, ab=200, h=60, hr=10)
        careers.close_stint(player)
        player.team = "BOS"
        player.batting.add(BattingStats(g=30, ab=100, h=25, hr=4))
        rows = careers.season_rows(player, 2026)
        self.assertEqual([(r.team, r.batting.hr) for r in rows], [("NYY", 10), ("BOS", 4)])
        careers.archive_season(player, 2026)
        self.assertEqual([(h["t"], h["y"]) for h in player.history], [("NYY", 2026), ("BOS", 2026)])
        self.assertEqual(player.stints, [])
        total, _ = careers.career_totals(player, 2026)
        self.assertEqual((total.g, total.hr, total.h), (80, 14, 85))
        # archiving twice never double counts
        careers.archive_season(player, 2026)
        self.assertEqual(careers.career_totals(player, 2026)[0].hr, 14)

    def test_live_season_counted_once(self) -> None:
        player = Player(2, "C", "D", 30, "R", "R", "SP", team="NYY")
        player.history = [{"y": 2026, "t": "NYY", "b": [], "p": PitchingStats(g=30, w=12).to_list()}]
        player.pitching = PitchingStats(g=5, w=2)
        self.assertEqual(careers.career_totals(player, 2027)[1].w, 14)
        self.assertEqual(careers.career_totals(player, 2026)[1].w, 12)  # 2026 archived: live ignored


class CareerMilestoneTests(unittest.TestCase):
    def test_crossing_career_thresholds(self) -> None:
        player = Player(3, "E", "F", 33, "L", "L", "1B", team="NYY")
        base = (BattingStats(hr=98, h=990), PitchingStats())
        player.batting = BattingStats(hr=2, h=10)
        messages = careers.career_milestones(player, base, BattingStats(hr=2, h=3), None)
        self.assertTrue(any("100 career home runs" in m for m in messages))
        self.assertTrue(any("1000 career hits" in m for m in messages))
        self.assertEqual(careers.career_milestones(player, base, BattingStats(), None), [])


class SeasonIntegrationTests(unittest.TestCase):
    league: League

    @classmethod
    def setUpClass(cls) -> None:
        cls.league = _full_season()

    def test_regular_and_postseason_never_mixed(self) -> None:
        lg = self.league
        regular_runs = sum(g.away_runs + g.home_runs for g in lg.schedule)
        self.assertEqual(sum(p.batting.r for p in lg.players.values()), regular_runs)
        post_runs = sum(sg.away_runs + sg.home_runs for rnd in lg.playoffs.rounds for s in rnd for sg in s.games)
        recorded = sum(line.r for p in lg.players.values() for line in p.post_batting.values())
        self.assertEqual(recorded, post_runs)
        self.assertGreater(post_runs, 0)
        ws_games = len(lg.playoffs.rounds[-1][0].games)
        ws_starts = sum(p.post_pitching["WS"].gs for p in lg.players.values() if "WS" in p.post_pitching)
        self.assertEqual(ws_starts, 2 * ws_games)

    def test_postseason_awards(self) -> None:
        lg = self.league
        awards = {a["award"]: a for a in lg.postseason_award_list(lg.year)}
        self.assertIn("World Series MVP", awards)
        self.assertEqual(awards["World Series MVP"]["team"], lg.playoffs.champion)
        self.assertIn("ALCS MVP", awards)
        self.assertIn("NLCS MVP", awards)
        self.assertEqual(lg.history[-1]["ws_mvp"].split(" (")[0], awards["World Series MVP"]["name"])
        self.assertEqual(len(lg.history[-1]["series"]), 4 + 4 + 2 + 1)

    def test_world_series_subset_of_postseason(self) -> None:
        lg = self.league
        for p in lg.players.values():
            if "WS" in p.post_batting:
                total, _ = careers.postseason_totals(p, lg.year)
                ws, _ = careers.world_series_totals(p, lg.year)
                self.assertLessEqual(ws.ab, total.ab)
                self.assertLessEqual(ws.h, total.h)

    def test_views_render(self) -> None:
        lg = self.league
        hitter = max(lg.players.values(), key=lambda p: p.batting.hr)
        card = views.player_card(lg, hitter)
        self.assertIn("Career (regular season)", card)
        self.assertIn("Morale", card)
        for text in (views.career_leaders_view(lg), views.records_view(lg, "NYY"),
                     views.postseason_history_view(lg), views.championships_view(lg),
                     views.career_leaders_view(lg, postseason=True), views.career_milestones_view(lg)):
            self.assertTrue(text)
        self.assertIn(hitter.name, views.career_leaders_view(lg))


class MultiSeasonCareerTests(unittest.TestCase):
    def test_careers_persist_across_seasons_trades_retirement_and_saves(self) -> None:
        lg = _full_season(seed=33)
        year1 = lg.year
        snapshot = {pid: careers.career_totals(p, lg.year) for pid, p in lg.players.items()
                    if p.batting.g or p.pitching.g}
        lg.start_new_season()
        # archived seasons equal the live totals they replaced
        for pid, (bat, pit) in snapshot.items():
            if pid in lg.players:
                after = careers.career_totals(lg.players[pid], lg.year)
                self.assertEqual(after[0], bat)
                self.assertEqual(after[1], pit)
            else:
                self.assertIn(pid, lg.retired)  # retired with his record
                self.assertEqual(careers.career_totals(lg.retired[pid])[0], bat)
        self.assertTrue(lg.retired)
        self.assertTrue(views.retired_view(lg))

        # trade a regular mid-season: his stats follow him and stay credited to both clubs
        lg.sim_days(30)
        nyy = lg.teams["NYY"]
        mover = max((lg.players[pid] for pid in nyy.active if not lg.players[pid].is_pitcher),
                    key=lambda p: p.batting.pa)
        before = careers.career_totals(mover, lg.year)[0]
        lg.note_departure(mover)
        nyy.remove_player(mover.id)
        lg.teams["BOS"].reserves.append(mover.id)
        mover.team = "BOS"
        self.assertEqual(careers.career_totals(mover, lg.year)[0], before)
        rows = careers.season_rows(mover, lg.year)
        self.assertEqual(rows[-1].team, "NYY")
        self.assertEqual(rows[-1].year, lg.year)

        with tempfile.TemporaryDirectory() as tmp:
            path = save_league(lg, Path(tmp) / "c.json")
            loaded = load_league(path)
        self.assertEqual(careers.career_totals(loaded.players[mover.id], loaded.year),
                         careers.career_totals(mover, lg.year))
        self.assertEqual(set(loaded.retired), set(lg.retired))
        self.assertEqual(loaded.postseason_awards, lg.postseason_awards)
        self.assertEqual(views.records_view(loaded, "NYY"), views.records_view(lg, "NYY"))
        self.assertGreater(lg.year, year1)


class MigrationTests(unittest.TestCase):
    def test_v3_save_loads_with_defaults(self) -> None:
        lg = League.create(seed=5, user_team="SEA")
        lg.sim_days(12)
        payload = {"format_version": 3, "app_version": "0.3.0", "league": lg.to_dict()}
        for key in ("retired", "postseason_awards", "career_milestones"):
            payload["league"].pop(key)
        for p in payload["league"]["players"]:
            for key in ("post_batting", "post_pitching", "stints", "history", "injury_total",
                        "injury_history", "rehab", "rust_days", "morale"):
                p.pop(key)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.json"
            path.write_text(json.dumps(payload))
            loaded = load_league(path)
        self.assertEqual(loaded.retired, {})
        self.assertTrue(all(p.morale == 50 and p.history == [] for p in loaded.players.values()))
        injured = [p for p in loaded.players.values() if p.is_injured]
        self.assertTrue(all(p.injury_total == p.injury_days for p in injured))
        self.assertIn("Save upgraded", loaded.news[-1])
        # the current season's live stats count toward careers immediately
        hitter = max(loaded.players.values(), key=lambda p: p.batting.pa)
        self.assertEqual(careers.career_totals(hitter, loaded.year)[0].pa, hitter.batting.pa)
        self.assertEqual(loaded.phase, PHASE_REGULAR)
        loaded.sim_days(3)  # keeps playing


class TradeFollowTests(unittest.TestCase):
    def test_execute_closes_stint(self) -> None:
        lg = League.create(seed=8, user_team="NYY")
        lg.sim_days(20)
        bos = lg.teams["BOS"]
        a = next(pid for pid in lg.user.active if not lg.players[pid].is_pitcher
                 and lg.players[pid].batting.g and lg.players[pid].contract_years > 0)
        b = next(pid for pid in bos.active if not lg.players[pid].is_pitcher and lg.players[pid].contract_years > 0)
        prop = trades.TradeProposal("NYY", "BOS", [a], [], [b], [])
        if trades.validate(lg, prop):
            self.skipTest("trade not legal in this seed")
        games_before = lg.players[a].batting.g
        trades.execute(lg, prop)
        self.assertEqual(lg.players[a].stints[0]["t"], "NYY")
        lg.sim_days(5)
        rows = careers.season_rows(lg.players[a], lg.year)
        self.assertEqual(rows[0].team, "NYY")
        self.assertEqual(rows[0].batting.g, games_before)
        self.assertEqual(sum(r.batting.g for r in rows), lg.players[a].batting.g)


if __name__ == "__main__":
    unittest.main()


class CareerCliTests(unittest.TestCase):
    def test_season_center_and_injury_menus(self) -> None:
        from mlb_manager.cli import App
        from tests.test_cli import ScriptedIO

        lg = _full_season(seed=44)
        lg.start_new_season()
        lg.sim_days(20)
        team = lg.user
        pid = next(p for p in team.active if not lg.players[p].is_pitcher and p not in
                   {s.player_id for s in team.lineup[:1]})
        injuries.apply_injury(lg.players[pid], "knee sprain", 20, lg.year, lg.day)
        answers = [
            "16", "5", "6", "", "6", "BOS", "7", "8", "9", "10", "11", "0",  # Season center screens
            "15", "1", str(pid), "3", str(pid), "4", "0",                    # IL, early rehab (refused), morale
            "6", str(pid),                                                   # player card
        ]
        io_ = ScriptedIO(answers)
        with tempfile.TemporaryDirectory() as tmp:
            App(io_.input, io_.output, save_dir=Path(tmp)).run(lg, None)
        text = io_.text
        self.assertIn("All-time career leaders", text)
        self.assertIn("New York Yankees franchise single-season records", text)
        self.assertIn("Boston Red Sox franchise single-season records", text)
        self.assertIn("postseason leaders", text)
        self.assertIn("Retired players", text)
        self.assertIn("Postseason history", text)
        self.assertIn("Championship history", text)
        self.assertIn("rehab assignments start", text)  # refused: too early
        self.assertIn("clubhouse morale", text)
        self.assertIn("Injury history", text)
        self.assertIn(pid, team.injured_list)
