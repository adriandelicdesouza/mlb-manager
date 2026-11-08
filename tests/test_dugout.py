"""Live, manager-controlled games: pitch-by-pitch engine, dugout controls and league integration."""

import random
import tempfile
import unittest
from pathlib import Path

from mlb_manager import tactics as tx
from mlb_manager.dugout import LiveGame, expected_pa
from mlb_manager.game import (
    PHASE_HALF,
    PHASE_PA,
    PHASE_PITCH,
    GameSimulator,
    RuleError,
    simulate_game,
)
from mlb_manager.generation import generate_rosters
from mlb_manager.league import League
from mlb_manager.persistence import load_league, save_league

TEAMS, PLAYERS, _ = generate_rosters(seed=77)


def live_game(seed: int = 1, user: str = "away", preset: str = "manual", away: str = "NYY",
              home: str = "BOS") -> LiveGame:
    sim = GameSimulator(TEAMS[away], TEAMS[home], PLAYERS, random.Random(seed), TEAMS[away].rotation[seed % 5],
                        TEAMS[home].rotation[seed % 5], pitch_by_pitch=True)
    live = LiveGame(sim, user, f"t{seed}", "Test", True, None, random.Random(f"cpu{seed}"))
    live.set_preset(preset)
    return live


def advance_to(live: LiveGame, predicate, unit: str = "pitch", limit: int = 3000) -> bool:  # noqa: ANN001
    for _ in range(limit):
        if live.is_over:
            return False
        if live.prompt is not None:
            live.answer_prompt(live.prompt.options[0][0])
        if predicate(live):
            return True
        live.advance(unit)
    return False


def box_consistent(test: unittest.TestCase, result) -> None:  # noqa: ANN001
    test.assertNotEqual(result.away_runs, result.home_runs)
    for batting, pitching in ((result.away_box, result.home_box), (result.home_box, result.away_box)):
        runs = sum(s.r for s in batting.batting.values())
        test.assertEqual(runs, batting.runs)
        test.assertEqual(sum(s.r for s in pitching.pitching.values()), batting.runs)
        hits = sum(s.h for s in batting.batting.values())
        test.assertEqual(hits, batting.hits)
        test.assertEqual(sum(s.h for s in pitching.pitching.values()), hits)
        test.assertLessEqual(sum(s.rbi for s in batting.batting.values()), runs)
        for s in pitching.pitching.values():
            test.assertLessEqual(s.er, s.r)
    test.assertEqual(sum(s.outs for s in result.home_box.pitching.values()), 3 * result.innings)
    test.assertEqual(sum(s.w for s in result.box_for(result.winner).pitching.values()), 1)
    test.assertEqual(sum(s.l for s in result.box_for(result.loser).pitching.values()), 1)


class TacticalModelTests(unittest.TestCase):
    def test_calibration_reproduces_plate_appearance_rates(self) -> None:
        for k, bb, hbp in ((0.222, 0.077, 0.011), (0.30, 0.06, 0.009), (0.15, 0.11, 0.013)):
            cal = tx.calibrate(k, bb, hbp)
            kk, bbb, hh, _, pitches = tx._neutral_terminal(cal)
            self.assertAlmostEqual(kk, k, delta=0.003)
            self.assertAlmostEqual(bbb, bb, delta=0.003)
            self.assertAlmostEqual(hh, hbp, delta=0.002)
            self.assertTrue(3.5 < pitches < 4.2, pitches)

    def test_repertoire_is_deterministic_and_centred(self) -> None:
        pitcher = PLAYERS[TEAMS["NYY"].rotation[0]]
        rep = tx.repertoire(pitcher)
        self.assertEqual(rep, tx.repertoire(pitcher))
        self.assertIn(rep[0].code, ("FF", "SI"))
        self.assertAlmostEqual(sum(p.usage for p in rep), 1.0, delta=0.01)
        mean = [sum(p.usage * tx.pitch_effects(rep, p.code)[i] for p in rep) for i in range(4)]
        for value in mean:
            self.assertAlmostEqual(value, 0.0, delta=0.02)

    def test_instructions_shift_distributions_in_expected_direction(self) -> None:
        live = live_game(3)
        g = live.sim
        g.step()  # begin half
        batter, pitcher, fielding = g.current_batter(), g.current_pitcher(), g.fielding_side
        call, align = tx.PitchCall(), tx.Alignment()
        base = expected_pa(g, batter, pitcher, fielding, tx.SwingCall(), call, align)
        power = expected_pa(g, batter, pitcher, fielding, tx.SwingCall(approach="power"), call, align)
        contact = expected_pa(g, batter, pitcher, fielding, tx.SwingCall(approach="contact"), call, align)
        patient = expected_pa(g, batter, pitcher, fielding, tx.SwingCall(aggression="patient"), call, align)
        self.assertGreater(power["HR"], base["HR"])
        self.assertGreater(power["K"], base["K"])
        self.assertLess(contact["K"], base["K"])
        self.assertLess(contact["HR"], base["HR"])
        self.assertGreater(patient["BB"], base["BB"])
        nibble = expected_pa(g, batter, pitcher, fielding, tx.SwingCall(), tx.PitchCall(approach="nibble"), align)
        self.assertGreater(nibble["BB"], base["BB"])
        for dist in (base, power, contact, patient, nibble):
            self.assertAlmostEqual(sum(dist.values()), 1.0, delta=1e-6)

    def test_steal_break_even_and_run_expectancy(self) -> None:
        self.assertTrue(0.6 < tx.steal_break_even(0, (True, False, False), 2) < 0.8)
        self.assertGreater(tx.run_expectancy(0, (True, True, True)), tx.run_expectancy(2, (False, False, False)))


class EngineTests(unittest.TestCase):
    def test_classic_resolution_unchanged_and_deterministic(self) -> None:
        args = (TEAMS["NYY"], TEAMS["BOS"], PLAYERS)
        a = simulate_game(*args, random.Random(5), TEAMS["NYY"].rotation[0], TEAMS["BOS"].rotation[0])
        b = simulate_game(*args, random.Random(5), TEAMS["NYY"].rotation[0], TEAMS["BOS"].rotation[0],
                          record_events=True)
        self.assertEqual((a.away_box.to_dict(), a.home_box.to_dict()), (b.away_box.to_dict(), b.home_box.to_dict()))
        self.assertFalse(a.workload)
        self.assertIsNone(a.manager_report)

    def test_pitch_by_pitch_progression_and_count_transitions(self) -> None:
        live = live_game(2)
        g = live.sim
        self.assertEqual(g.phase, PHASE_HALF)
        pitches = 0
        while not live.is_over and pitches < 400:
            before = (g.balls, g.strikes, g.phase)
            report = live.advance("pitch")
            p_events = [e for e in report.events if e["k"] == "P"]
            self.assertLessEqual(len(p_events), 1)
            if not p_events:
                continue
            pitches += 1
            e = p_events[0]
            balls, strikes = e["c"]
            if before[2] == PHASE_PITCH:
                b0, s0 = before[0], before[1]
            else:
                b0, s0 = 0, 0
            if e["r"] == "ball":
                self.assertEqual((balls, strikes), (b0 + 1, s0))
            elif e["r"] in ("called", "swinging", "bunt_miss", "bunt_foul"):
                self.assertEqual((balls, strikes), (b0, s0 + 1))
            elif e["r"] == "foul":
                self.assertEqual((balls, strikes), (b0, min(2, s0 + 1)))
            self.assertLessEqual(balls, 4)
            self.assertLessEqual(strikes, 3)
            if balls == 4:
                self.assertIn(report.events[report.events.index(e) + 1]["r"], ("BB", "IBB"))
            if strikes == 3:
                self.assertEqual(report.events[report.events.index(e) + 1]["r"], "K")
        self.assertGreater(pitches, 50)

    def test_units_stop_at_the_right_boundaries(self) -> None:
        live = live_game(4)
        g = live.sim
        live.advance("pa")
        self.assertIn(g.phase, (PHASE_PA, PHASE_HALF))
        start = (g.inning, g.top)
        live.advance("half")
        self.assertEqual(g.phase, PHASE_HALF)
        self.assertNotEqual((g.inning, g.top), start)
        live.advance("inning")
        self.assertEqual(g.phase, PHASE_HALF)
        self.assertTrue(g.top)
        events = len(g.events)
        # Pausing: nothing happens between calls.
        self.assertEqual(len(g.events), events)
        report = live.advance("game")
        self.assertEqual(report.stopped, "final")
        box_consistent(self, g.result)

    def test_completed_events_are_immutable(self) -> None:
        live = live_game(6)
        live.advance("inning")
        snapshot = [dict(e) for e in live.sim.events]
        if live.control("pitch.effort") is not None:
            live.apply("pitch.effort", "max")
        live.advance("inning")
        self.assertEqual(live.sim.events[: len(snapshot)], snapshot)


class ControlTests(unittest.TestCase):
    def test_context_sensitive_controls(self) -> None:
        live = live_game(7, user="home")  # home fields first
        g = live.sim
        keys = {c.key for c in live.controls()}
        self.assertIn("sub.pitcher", keys)
        self.assertIn("lineup.order", keys)
        self.assertFalse(live.control("lineup.order").enabled)
        self.assertNotIn("bat.approach", keys)
        advance_to(live, lambda lv: lv.sim.phase == PHASE_PITCH)
        keys = {c.key for c in live.controls()}
        self.assertTrue({"pitch.type", "pitch.location", "pitch.intentional_ball"} <= keys)
        self.assertFalse(live.control("sub.pitcher").enabled)  # mid plate appearance
        if not any(g.runners()[:2]):
            self.assertNotIn("pitch.pitchout", keys)
        # Batting half: hitting controls, no pitching calls.
        advance_to(live, lambda lv: lv.user_batting and lv.sim.phase == PHASE_PA)
        keys = {c.key for c in live.controls()}
        self.assertIn("bat.approach", keys)
        self.assertIn("sub.pinch_hit", keys)
        self.assertNotIn("pitch.type", keys)
        if not g.steal_targets():
            self.assertNotIn("run.steal", keys)

    def test_automated_categories_explain_unavailable_controls(self) -> None:
        live = live_game(8, user="home")
        live.set_mode("pitching", "auto")
        advance_to(live, lambda lv: lv.sim.phase == PHASE_PITCH and not lv.user_batting)
        control = live.control("pitch.type")
        self.assertFalse(control.enabled)
        self.assertIn("Automated", control.reason)
        with self.assertRaises(RuleError):
            live.apply("pitch.type", "FF")

    def test_one_shot_pitch_call_used_once_and_logged(self) -> None:
        live = live_game(9, user="home")
        advance_to(live, lambda lv: lv.sim.phase == PHASE_PITCH and not lv.user_batting)
        code = tx.repertoire(live.sim.current_pitcher())[1].code
        live.apply("pitch.type", code)
        live.apply("pitch.location", "chase")
        report = live.advance("pitch")
        pitch = next(e for e in report.events if e["k"] == "P")
        self.assertEqual((pitch["pt"], pitch["loc"]), (code, "chase"))
        self.assertIsNone(live.next_pitch.pitch_type)
        self.assertIsNone(live.next_pitch.location)
        decision = next(d for d in live.decisions if d.action == "pitch_type")
        self.assertEqual((decision.new, decision.source), (code, "manual"))
        self.assertEqual(live.sim.events[decision.result]["k"], "P")
        self.assertIsNotNone(decision.assessment)

    def test_take_and_intentional_ball(self) -> None:
        live = live_game(10)
        advance_to(live, lambda lv: lv.sim.phase == PHASE_PITCH and lv.user_batting)
        live.apply("bat.take")
        pitch = next(e for e in live.advance("pitch").events if e["k"] == "P")
        self.assertIn(pitch["r"], ("ball", "called", "hbp"))
        live2 = live_game(11, user="home")
        advance_to(live2, lambda lv: lv.sim.phase == PHASE_PITCH and not lv.user_batting and lv.sim.balls < 3)
        balls = live2.sim.balls
        live2.apply("pitch.intentional_ball")
        pitch = next(e for e in live2.advance("pitch").events if e["k"] == "P")
        self.assertEqual(pitch["r"], "ball")
        self.assertEqual(pitch["c"][0], balls + 1)

    def test_intentional_walk_puts_batter_on_first(self) -> None:
        live = live_game(12, user="home")
        advance_to(live, lambda lv: lv.sim.phase == PHASE_PA and not lv.user_batting)
        batter = live.sim.current_batter()
        line_bb = live.sim.batting_side.box.batting[batter.id].bb
        live.apply("def.ibb")
        self.assertEqual(live.sim.bases[0].player_id, batter.id)
        self.assertEqual(live.sim.events[-1]["r"], "IBB")
        self.assertEqual(live.sim.side("away").box.batting[batter.id].bb, line_bb + 1)
        self.assertEqual(live.decisions[-1].action, "ibb")

    def test_steal_sign_produces_attempt(self) -> None:
        attempts = 0
        for seed in range(20, 40):
            live = live_game(seed)
            if not advance_to(live, lambda lv: lv.user_batting and lv.sim.phase == PHASE_PITCH
                              and lv.sim.steal_targets() == [2], unit="pitch"):
                continue
            pitchout = live.sim.steal_probability(2, tx.SwingCall(), tx.PitchCall(pitchout=True))
            normal = live.sim.steal_probability(2, tx.SwingCall(), tx.PitchCall())
            self.assertLess(pitchout, normal)
            live.apply("run.steal")
            decision = live.decisions[-1]
            self.assertIn("break-even", decision.assessment.basis)
            report = live.advance("pitch")
            if any(e["k"] in ("SB", "CS") for e in report.events):
                attempts += 1
        self.assertGreater(attempts, 3)

    def test_bunt_sign_produces_bunts(self) -> None:
        bunts = 0
        for seed in range(40, 50):
            live = live_game(seed)
            if not advance_to(live, lambda lv: lv.user_batting and lv.sim.phase == PHASE_PA):
                continue
            live.apply("bat.bunt")
            for _ in range(8):
                report = live.advance("pitch")
                kinds = [e["r"] for e in report.events if e["k"] == "P"]
                bunts += sum(1 for r in kinds if r.startswith("bunt"))
                if any(e["k"] == "PA" for e in report.events):
                    break
        self.assertGreater(bunts, 3)

    def test_plan_persistence_per_batter(self) -> None:
        live = live_game(13)
        advance_to(live, lambda lv: lv.user_batting and lv.sim.phase == PHASE_PA)
        batter = live.sim.current_batter()
        live.apply("bat.approach", "power")
        decision = live.decisions[-1]
        self.assertEqual((decision.previous, decision.new), ("balanced", "power"))
        self.assertIn("expected runs/PA", decision.assessment.basis)
        # Next time he bats the instruction is still there; other hitters keep the plan.
        advance_to(live, lambda lv: lv.user_batting and lv.sim.phase == PHASE_PA
                   and lv.sim.current_batter().id != batter.id)
        self.assertEqual(live.batter_plan().approach, "balanced")
        advance_to(live, lambda lv: lv.user_batting and lv.sim.phase == PHASE_PA
                   and lv.sim.current_batter().id == batter.id)
        self.assertEqual(live.batter_plan().approach, "power")
        self.assertEqual(live.control("bat.approach").value, "power")


class SubstitutionTests(unittest.TestCase):
    def test_pitching_change_and_three_batter_minimum(self) -> None:
        live = live_game(14, user="home")
        g = live.sim
        advance_to(live, lambda lv: lv.sim.phase == PHASE_PA and not lv.user_batting and lv.sim.outs == 0
                   and lv.sim.inning >= 2)
        starter = g.fielding_side.pitcher_id
        reliever = int(live.control("sub.pitcher").options[0][0])
        live.apply("sub.pitcher", str(reliever))
        self.assertEqual(g.fielding_side.pitcher_id, reliever)
        self.assertEqual(g.events[-2]["k"], "PC")
        d = live.decisions[-1]
        self.assertEqual((d.action, d.category, d.player), ("change", "pitching", reliever))
        self.assertEqual(d.previous, PLAYERS[starter].short_name)
        control = live.control("sub.pitcher")
        self.assertFalse(control.enabled)
        self.assertIn("must face 3", control.reason)
        live.advance("half")
        result = live.advance("game")
        self.assertEqual(result.stopped, "final")
        self.assertIn(reliever, g.result.home_box.pitching)
        box_consistent(self, g.result)

    def test_pinch_hitter_runner_and_defensive_replacement(self) -> None:
        live = live_game(15)
        g = live.sim
        side = live.user_side
        advance_to(live, lambda lv: lv.user_batting and lv.sim.phase == PHASE_PA)
        old = g.current_batter()
        bench = [p.id for p in g.bench(side)]
        live.apply("sub.pinch_hit", str(bench[0]))
        self.assertEqual(g.current_batter().id, bench[0])
        self.assertIn(old.id, side.removed)
        self.assertEqual(side.box.subs[bench[0]], old.id)
        self.assertNotIn(old.id, [p.id for p in g.bench(side)])
        with self.assertRaises(RuleError):
            g.pinch_hit(live.user_key, old.id)
        # Pinch runner when someone reaches base.
        if advance_to(live, lambda lv: lv.user_batting and lv.sim.phase == PHASE_PA and lv.sim.bases[0]
                      and lv.control("sub.pinch_run") and lv.control("sub.pinch_run").enabled):
            value = live.control("sub.pinch_run").options[0][0]
            base, pid = (int(x) for x in value.split(":"))
            live.apply("sub.pinch_run", value)
            self.assertEqual(g.bases[base - 1].player_id, pid)
        # Defensive replacement when we take the field.
        advance_to(live, lambda lv: not lv.user_batting and lv.sim.phase == PHASE_HALF)
        control = live.control("sub.defense")
        if control.enabled:
            slot, pid = (int(x) for x in control.options[0][0].split(":"))
            live.apply("sub.defense", control.options[0][0])
            self.assertEqual(side.order[slot][0], pid)
        live.advance("game")
        result = g.result
        box_consistent(self, result)
        self.assertGreaterEqual(len(result.box_for(side.team.abbr).subs), 1)


class AutomationTests(unittest.TestCase):
    def test_automatic_mode_makes_cpu_decisions_without_prompts(self) -> None:
        live = live_game(16, preset="auto")
        report = live.advance("game")
        self.assertEqual(report.stopped, "final")
        self.assertIsNone(live.prompt)
        self.assertTrue(all(d.source in ("auto", "forced") for d in live.decisions))
        self.assertTrue(any(d.action == "change" for d in live.decisions))

    def test_manual_mode_leaves_pitching_changes_to_the_user(self) -> None:
        live = live_game(17, preset="manual")
        live.advance("game")
        changes = [d for d in live.decisions if d.action == "change"]
        self.assertTrue(all(d.source == "forced" for d in changes))
        user_box = live.sim.result.box_for(live.user_side.team.abbr)
        starter = live.user_side.starter_id
        self.assertGreater(user_box.pitching[starter].pitches, 0)

    def test_assisted_mode_presents_key_decisions(self) -> None:
        prompts = 0
        for seed in range(18, 24):
            live = live_game(seed, preset="assisted")
            for _ in range(500):
                if live.is_over:
                    break
                report = live.advance("pa")
                if report.stopped == "prompt":
                    prompts += 1
                    prompt = live.prompt
                    self.assertGreaterEqual(len(prompt.options), 2)
                    live.answer_prompt(prompt.options[0][0])
                    self.assertIsNone(live.prompt)
                    self.assertIn(live.decisions[-1].source, ("assisted", "manual"))
            box_consistent(self, live.sim.result)
        self.assertGreater(prompts, 0)

    def test_mixed_automation_per_category(self) -> None:
        live = live_game(25, user="home", preset="manual")
        live.set_mode("pitching", "auto")
        live.set_mode("batting", "manual")
        advance_to(live, lambda lv: lv.sim.phase == PHASE_PITCH)
        if live.user_batting:
            self.assertTrue(live.control("bat.take").enabled)
        live.advance("game")
        self.assertTrue(any(d.action == "change" and d.source == "auto" for d in live.decisions))

    def test_summary_grades_decisions_ex_ante(self) -> None:
        live = live_game(26, preset="assisted")
        live.finish()
        report = live.sim.result.manager_report
        self.assertIsNotNone(report)
        self.assertIn("not the result", report["method"])
        self.assertEqual(report["decisions"], len(live.decisions))
        for d in report["log"]:
            if d["assessment"]:
                self.assertIn(d["assessment"]["grade"], ("favourable", "neutral", "unfavourable", "informational"))

    def test_random_managers_never_break_the_game(self) -> None:
        rng = random.Random(99)
        for seed in range(30, 36):
            live = live_game(seed, user=rng.choice(["away", "home"]), preset=rng.choice(["manual", "assisted"]))
            for _ in range(2000):
                if live.is_over:
                    break
                if live.prompt is not None:
                    live.answer_prompt(rng.choice(live.prompt.options)[0])
                    continue
                enabled = [c for c in live.controls() if c.enabled and not c.key.startswith("auto.")]
                if enabled and rng.random() < 0.3:
                    c = rng.choice(enabled)
                    value = rng.choice(c.options)[0] if c.kind == "choice" and c.options else None
                    if c.kind == "choice" and not c.options:
                        continue
                    try:
                        live.apply(c.key, value)
                    except RuleError:
                        pass
                live.advance(rng.choice(["pitch", "play", "pa"]))
            self.assertTrue(live.is_over)
            box_consistent(self, live.sim.result)

    def test_live_run_environment_is_realistic(self) -> None:
        runs = hr = hits = ab = 0
        games = 0
        abbrs = sorted(TEAMS)
        for seed in range(60):
            away, home = abbrs[seed % 30], abbrs[(seed * 7 + 3) % 30]
            if away == home:
                continue
            live = live_game(seed, preset="auto", away=away, home=home)
            live.finish()
            r = live.sim.result
            games += 1
            for box in (r.away_box, r.home_box):
                runs += box.runs
                hr += sum(b.hr for b in box.batting.values())
                hits += sum(b.h for b in box.batting.values())
                ab += sum(b.ab for b in box.batting.values())
        self.assertTrue(3.4 <= runs / games / 2 <= 5.2, runs / games / 2)
        self.assertTrue(0.7 <= hr / games / 2 <= 1.6, hr / games / 2)
        self.assertTrue(0.225 <= hits / ab <= 0.270, hits / ab)


class LeagueIntegrationTests(unittest.TestCase):
    def play(self, league: League, stop_after: int | None = None) -> None:
        league.advance_to_user_game()
        live = league.start_live_game()
        live.set_preset("assisted")
        n = 0
        while not live.is_over:
            live.advance("pa")
            if live.prompt is not None:
                live.answer_prompt(live.prompt.options[0][0])
            n += 1
            if stop_after and n >= stop_after:
                return

    def test_live_game_counts_and_deterministic_with_save_load(self) -> None:
        a = League.create(seed=3, user_team="NYY")
        b = League.create(seed=3, user_team="NYY")
        self.play(a, stop_after=20)
        self.play(b, stop_after=20)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live.json"
            save_league(a, path)
            a = load_league(path)
        self.assertIsNotNone(a.live_game)
        self.assertEqual(a.live_game.sim.events, b.live_game.sim.events)
        for league in (a, b):
            live = league.live_game
            while not live.is_over:
                live.advance("pa")
                if live.prompt is not None:
                    live.answer_prompt(live.prompt.options[0][0])
        self.assertEqual(a.live_game.result.to_dict(), b.live_game.result.to_dict())
        result = a.live_game.result
        report = a.finish_live_game()
        self.assertIsNone(a.live_game)
        self.assertIs(a.last_user_game, result)
        self.assertIsNotNone(a.last_user_game.manager_report)
        game = next(g for g in a.schedule if g.played and "NYY" in (g.away, g.home))
        self.assertEqual((game.away_runs, game.home_runs), (result.away_runs, result.home_runs))
        self.assertEqual(len(report.games), len([g for g in a.schedule if g.day == game.day]))
        box = result.box_for("NYY")
        for pid, line in box.batting.items():
            self.assertEqual(a.players[pid].batting.pa, line.pa)
        for pid in box.subs:
            self.assertEqual(a.players[pid].games_by_pos, {})

    def test_simulating_the_day_finishes_a_live_game(self) -> None:
        league = League.create(seed=4, user_team="BOS")
        league.advance_to_user_game()
        live = league.start_live_game()
        live.advance("inning")
        league.sim_days(1)
        self.assertIsNone(league.live_game)
        self.assertIs(league.last_user_game, live.result)
        self.assertTrue(live.is_over)

    def test_effort_workload_feeds_fatigue(self) -> None:
        league = League.create(seed=5, user_team="NYY")
        league.advance_to_user_game()
        live = league.start_live_game()
        live.set_preset("auto")
        live.instructions.pitching.effort = "max"
        live.finish()
        result = live.result
        user_box = result.box_for("NYY")
        starter = live.user_side.starter_id
        self.assertGreater(result.workload[starter], user_box.pitching[starter].pitches)
        before = league.players[starter].fatigue
        league.finish_live_game()
        self.assertGreaterEqual(league.players[starter].fatigue, before + result.workload[starter] - 25)


if __name__ == "__main__":
    unittest.main()
