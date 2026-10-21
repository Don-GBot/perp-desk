"""Offline tests for sync 5: composite setup ranking (entry.composite), rr_cap and cost_heavy hard filters.
All mocked, no network, no orders."""
import os, unittest
from unittest import mock
import test_offline as T
from test_offline import NoNetwork, EX, config
from test_gates_and_logs import GateBase, SETUP, rd
import blocklog as BL

class TestDefaults(NoNetwork):
    def test_values(self):
        e = config.CFG["entry"]; cp = e["composite"]
        self.assertEqual((e["rr_cap_max"], e["cost_heavy_max_share"]), (8, 0.12))
        self.assertEqual((cp["enabled"], cp["target_atr_free"], cp["target_atr_pen"], cp["gap_full_pct"], cp["fee_share_free"],
                          cp["fee_share_pen"], cp["btc_bonus"]), (True, 8, 0.25, 3, 0.08, 0.25, 0.25))
        self.assertAlmostEqual(cp["gap_over_pen"], 1 / 3)
        self.assertTrue(EX.COMPOSITE); self.assertEqual(EX.FEE_HEAVY_MAX, 1 / 3)   # fee_heavy unchanged
    def test_bad_values(self):
        for txt in ("entry:\n  cost_heavy_max_share: 2\n", "entry:\n  rr_cap_max: -1\n", "entry:\n  composite:\n    gap_full_pct: 0\n",
                    "entry:\n  composite:\n    enabled: 3\n"):
            f = os.path.join(T.TMP, "s5.yaml")
            with open(f, "w") as fh: fh.write(txt)
            with self.assertRaises(config.ConfigError, msg=txt): config.load(f)

class TestFilters(NoNetwork):
    def test_rr_cap(self):
        self.assertEqual(EX.filter_gate(100, 90, 8.01)[0], "rr_cap")
        self.assertEqual(EX.filter_gate(100, 90, 8.0), (None, None))      # exactly 8 passes
        with mock.patch.object(EX, "RR_CAP", 0.0): self.assertEqual(EX.filter_gate(100, 90, 50)[0], None)   # 0 = off
    def test_cost_heavy_uses_fee_share(self):
        st = 100 - 1.25   # fees ~7.7% of the stop-out loss; fees + slippage would be > 12%
        self.assertLess(EX.fee_share(100, st), 0.12); self.assertIsNone(EX.filter_gate(100, st, 3)[0])
        st2 = 100 - 0.6
        self.assertGreaterEqual(EX.fee_share(100, st2), 0.12)
        g, why = EX.filter_gate(100, st2, 3); self.assertEqual(g, "cost_heavy"); self.assertIn("round-trip fees", why)
        with mock.patch.object(EX, "COST_HEAVY_MAX", 0.0): self.assertIsNone(EX.filter_gate(100, st2, 3)[0])
    def test_boundary_is_inclusive(self):
        with mock.patch.object(EX, "COST_HEAVY_MAX", EX.fee_share(100, 99.0)):
            self.assertEqual(EX.filter_gate(100, 99.0, 3)[0], "cost_heavy")   # >= blocks

class TestFilterInEvaluate(GateBase):
    def test_rr_cap_block_line(self):
        plan, why = EX.evaluate(dict(SETUP, target=140000, _rank=1), self.ctx)   # R:R ~ 14 at ask 104000
        self.assertIsNone(plan); self.assertTrue(why.startswith("rr_cap"), why)
        self.assertEqual(rd(BL.log_path())[-1]["gate_reason"], "rr_cap")
    def test_cost_heavy_block_line(self):
        plan, why = EX.evaluate(dict(SETUP, stop=103850, target=104600, _rank=1), self.ctx)   # 150 stop (1.5 ATR), fees ~12.4% of the loss
        self.assertIsNone(plan); self.assertTrue(why.startswith("cost_heavy"), why)
        self.assertEqual(rd(BL.log_path())[-1]["gate_reason"], "cost_heavy")
    def test_gate_of(self):
        self.assertEqual(EX.gate_of("rr_cap: planned R:R 9.00 > 8"), "rr_cap")
        self.assertEqual(EX.gate_of("cost_heavy: round-trip fees 13% of stop-out loss >= 12%"), "cost_heavy")
    def test_plan_carries_ema4h(self):
        plan, why = EX.evaluate(dict(SETUP, _rank=1), self.ctx)
        self.assertIsNotNone(plan, why); e1, e2 = plan["ema4h"]; self.assertGreater(e1, e2)

def sc(tgt_atr=6, gap=2.0, side="buy", stop=95.0, btc="flat"):
    e = 100.0; tgt = e + tgt_atr if side == "buy" else e - tgt_atr
    e20 = 100.0 * (1 + (gap if side == "buy" else -gap) / 100)
    return EX.composite_score(side, e, stop if side == "buy" else 200 - stop, tgt, 1.0, e20, 100.0, btc)

class TestScore(NoNetwork):
    def test_target(self):
        self.assertEqual((sc(4)["s_target"], sc(8)["s_target"], sc(10)["s_target"]), (0, 0, -0.5))
    def test_gap(self):
        self.assertEqual((sc(gap=1.5)["s_gap"], sc(gap=3)["s_gap"], sc(gap=4.5)["s_gap"]), (0.5, 1.0, 0.5))
        self.assertLess(sc(gap=-1)["s_gap"], 0)
        self.assertEqual(sc(side="sell", gap=2.0)["gap4h_pct"], 2.0)   # direction-aware
    def test_cost(self):
        self.assertEqual(sc(stop=90.0)["s_cost"], 0)
        c = sc(stop=99.0); self.assertAlmostEqual(c["s_cost"], -0.25 * (EX.fee_share(100, 99.0) * 100 - 8), places=4)
        self.assertLess(c["s_cost"], 0)
    def test_btc(self):
        self.assertEqual((sc(btc="up")["s_btc"], sc(btc="down")["s_btc"], sc(side="sell", btc="down")["s_btc"], sc(btc=None)["s_btc"]),
                         (0.25, 0, 0.25, 0))
    def test_sum_and_missing_inputs(self):
        x = sc(10, 4.5, stop=99.0, btc="up")
        self.assertAlmostEqual(x["score"], x["s_target"] + x["s_gap"] + x["s_cost"] + x["s_btc"], places=3)
        y = EX.composite_score("buy", 100.0, 95.0, 106.0, None, None, None, None)
        self.assertEqual((y["s_target"], y["s_gap"]), (0, 0))

PLANS = {"HIRR-USDT": dict(entry=100.0, stop=95.0, target=140.0, atr1h=1.0, rr=8.0),   # 40 ATR target
         "GOOD-USDT": dict(entry=100.0, stop=95.0, target=106.0, atr1h=1.0, rr=1.2),
         "MID-USDT": dict(entry=100.0, stop=95.0, target=110.0, atr1h=1.0, rr=2.0)}

class TestWalk(NoNetwork):
    def setUp(self):
        super().setUp(); self.calls = []; self.places = []; self.recheck_fail = set(); self.lev_fail = set()
        def ev(s, ctx):
            inst = s["instId"]; self.calls.append(inst)
            if inst == "FAIL-USDT": return None, "R:R 1.00 < 3"
            if inst in self.recheck_fail and self.calls.count(inst) > 1: return None, "R:R 2.50 < 3"   # passes the walk, fails the pre-order re-check
            return dict(instId=inst, side="buy", ema4h=(102.0, 100.0), **PLANS[inst]), None
        def pl(plan):
            self.places.append(plan["instId"])
            if plan["instId"] in self.lev_fail: return dict(ok=False, why="leverage refused")
            return dict(ok=True, instId=plan["instId"], order_resp={"code": "0"})
        self.ps = [mock.patch.object(EX, "evaluate", ev), mock.patch.object(EX, "place", pl),
                   mock.patch.object(EX, "account", return_value=(1000.0, 1000.0, [], "net_mode")),
                   mock.patch.object(EX, "instruments", return_value={}), mock.patch.object(EX, "btc_trend4h", return_value="up"),
                   mock.patch.object(EX, "log_block", lambda *a, **k: None)]
        for p in self.ps: p.start()
        self.setups = [dict(instId=i, side="buy") for i in ("HIRR-USDT", "FAIL-USDT", "MID-USDT", "GOOD-USDT")]   # scan = R:R order
        self.ts = T.iso_ago(0)
    def tearDown(self):
        for p in self.ps: p.stop()
        super().tearDown()
    def test_dry_picks_top_score(self):
        r = EX.run(self.setups, self.ts, dry=True)
        self.assertTrue(r["ok"]); self.assertEqual(r["plan"]["instId"], "GOOD-USDT")
        self.assertEqual([c["instId"] for c in r["candidates"]], ["GOOD-USDT", "MID-USDT", "HIRR-USDT"])
        self.assertEqual([s["instId"] for s in r["skipped"]], ["FAIL-USDT"]); self.assertEqual(self.places, [])
    def test_live_one_entry(self):
        r = EX.run(self.setups, self.ts)
        self.assertEqual(self.places, ["GOOD-USDT"]); self.assertEqual(r["score"]["score"], r["candidates"][0]["score"])
        self.assertEqual(self.calls.count("GOOD-USDT"), 2)   # top candidate re-checked right before its order
        self.assertEqual(self.calls.count("HIRR-USDT"), 1)
    def test_live_falls_through_on_failed_leverage_and_recheck(self):
        self.lev_fail = {"GOOD-USDT"}; self.recheck_fail = {"MID-USDT"}
        r = EX.run(self.setups, self.ts)
        self.assertEqual(self.places, ["GOOD-USDT", "HIRR-USDT"]); self.assertEqual(r["instId"], "HIRR-USDT")
        self.assertEqual(self.calls.count("MID-USDT"), 2)   # re-checked at the live price before placing
        self.assertIn("MID-USDT", [s["instId"] for s in r["skipped"]])
    def test_nothing_passes(self):
        r = EX.run([dict(instId="FAIL-USDT", side="buy")], self.ts)
        self.assertFalse(r["ok"]); self.assertEqual(self.places, [])
    def test_unscorable_last(self):
        with mock.patch.object(EX, "score_plan", lambda p, b: dict(score=None) if p["instId"] == "GOOD-USDT" else EX.composite_score(
                "buy", p["entry"], p["stop"], p["target"], p["atr1h"], 102.0, 100.0, b)):
            r = EX.run(self.setups, self.ts, dry=True)
        self.assertEqual(r["candidates"][-1]["instId"], "GOOD-USDT")
    def test_missing_ema_is_unscorable(self):
        self.assertIsNone(EX.score_plan(dict(PLANS["GOOD-USDT"], side="buy", ema4h=None), "up")["score"])
    def test_composite_off_is_scan_order(self):
        with mock.patch.object(EX, "COMPOSITE", False):
            r = EX.run(self.setups, self.ts, dry=True)
        self.assertEqual(r["plan"]["instId"], "HIRR-USDT"); self.assertNotIn("candidates", r)

if __name__ == "__main__": unittest.main()
