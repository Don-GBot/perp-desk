"""Offline tests for the 1h trend gate, stock-perp block, blocked-setups log (live/dry split), close matching,
funding logging, run records, transfers detection, slowcrowd helpers and shadow_variants. All mocked, no network.
Run: python3 -m unittest discover -s tests -v
"""
import os, sys, json, unittest, datetime as dt
from unittest import mock
import test_offline as T          # sets DATA_DIR / BOT_CONFIG to temp + example config before the scripts are imported
from test_offline import NoNetwork, INS, FakeExchange, fake_candles_up, EX, bf, registry, config
import trend1h, stockperp as SP, blocklog as BL, closes, funding, runlog, transfers, slowcrowd as SCW
import shadow_variants as SV

def fake_candles_down(inst, bar, limit=None):
    return [[100000 - i * 20, 100000 - i * 20 + 60, 100000 - i * 20 - 60, 100000 - i * 20 - 10] for i in range(200)]

def rd(p):
    try:
        with open(p) as fh: return [json.loads(x) for x in fh]
    except FileNotFoundError: return []

def clear_logs():
    for p in (BL.log_path(), BL.log_path('dry')):
        if os.path.exists(p): os.remove(p)

SETUP = dict(instId="BTC-USDT", side="buy", stop=101500, target=113000, entry=104000, rr=3.5)

class GateBase(NoNetwork):
    def setUp(self):
        super().setUp(); clear_logs(); registry.save_pending([])
        self.fx = FakeExchange(); self.ins = dict(INS)
        self.patches = [mock.patch.object(bf, "public", self.fx.public), mock.patch.object(bf, "private", self.fx.private),
                        mock.patch.object(EX, "candles", fake_candles_up), mock.patch.object(EX, "pair_positions", return_value=[]),
                        mock.patch.object(EX.news_gate, "check", return_value=dict(block=False, reason="no rating"))]
        for p in self.patches: p.start()
        self.ctx = dict(equity=1000.0, avail=1000.0, pos=[], ins=self.ins, dry=False, scan_ts="t", warnings=[])
    def tearDown(self):
        for p in self.patches: p.stop()
        super().tearDown()

class TestConfigFlags(NoNetwork):
    def test_live_values(self):
        e = config.CFG["entry"]
        self.assertTrue(e["trend_1h"]["enabled"]); self.assertTrue(e["stock_perp_block"]["enabled"])
        self.assertEqual(config.CFG["risk"]["risk_per_trade_pct"], 5); self.assertEqual(config.CFG["risk"]["hard_cut_pct"], 5.5)
        self.assertTrue(EX.TREND1H); self.assertTrue(EX.STOCKPERP)
    def test_bad_trend1h(self):
        f = os.path.join(T.TMP, "b1.yaml")
        with open(f, "w") as fh: fh.write("entry:\n  trend_1h:\n    ema_fast: 60\n    ema_slow: 50\n")
        with self.assertRaises(config.ConfigError): config.load(f)

class TestTrend1h(GateBase):
    def test_pure(self):
        self.assertEqual(trend1h.trend_from_rows(fake_candles_up(None, None))[0], "up")
        self.assertEqual(trend1h.trend_from_rows(fake_candles_down(None, None))[0], "down")
        flat = [[1, 1, 1, 1]] * 200
        self.assertEqual(trend1h.trend_from_rows(flat)[0], "flat")
        with self.assertRaises(ValueError): trend1h.trend_from_rows(flat[:10])
    def test_aligned_long_passes(self):
        plan, why = EX.evaluate(SETUP, self.ctx)
        self.assertIsNotNone(plan, why); self.assertEqual(plan["trend1h"]["trend"], "up")
    def test_misaligned_blocks_and_logs(self):
        def mixed(inst, bar, limit=None): return fake_candles_up(inst, bar) if bar == "4H" else fake_candles_down(inst, bar)
        with mock.patch.object(EX, "candles", mixed):
            plan, why = EX.evaluate(SETUP, self.ctx)
        self.assertIsNone(plan); self.assertEqual(why, "1h_trend (down)")
        l = rd(BL.log_path()); self.assertEqual([(x["gate_reason"], x["mode"], x["source"]) for x in l], [("1h_trend", "live", "execute")])
    def test_data_error_fails_closed(self):
        def broken(inst, bar, limit=None):
            if bar == "1H" and limit == trend1h.BARS: raise RuntimeError("candles 503")
            return fake_candles_up(inst, bar)
        with mock.patch.object(EX, "candles", broken):
            plan, why = EX.evaluate(SETUP, self.ctx)
        self.assertIsNone(plan); self.assertEqual(why, "1h_trend (data error)")
    def test_flag_off(self):
        def mixed(inst, bar, limit=None): return fake_candles_up(inst, bar) if bar == "4H" else fake_candles_down(inst, bar)
        with mock.patch.object(EX, "candles", mixed), mock.patch.object(EX, "TREND1H", False):
            plan, why = EX.evaluate(SETUP, self.ctx)
        self.assertIsNotNone(plan, why)   # same down 1H data, gate off: the setup passes

class TestStockPerp(GateBase):
    def test_classify(self):
        self.assertTrue(SP.is_stock("NVDA-USDT")[0]); self.assertTrue(SP.is_stock("NEW-USDT", {"assetClass": "Indices"})[0])
        self.assertFalse(SP.is_stock("BTC-USDT", {"assetClass": "Crypto"})[0]); self.assertFalse(SP.is_stock("CL-USDT", {"assetClass": "Commodities"})[0])
        self.assertEqual(SP.asset_class("XAU-USDT", {"assetClass": "Crypto"}), "commodity"); self.assertEqual(SP.asset_class("QQQ-USDT"), "index")
        self.assertEqual(SP.asset_class("EUR-USDT"), "forex"); self.assertEqual(SP.asset_class("BTC-USDT"), "crypto")
    def test_listed_blocked_first_dry_log(self):
        ins = dict(self.ins, **{"NVDA-USDT": dict(INS["BTC-USDT"], instId="NVDA-USDT", assetClass="Crypto")})
        plan, why = EX.evaluate(dict(SETUP, instId="NVDA-USDT"), dict(self.ctx, ins=ins, dry=True))
        self.assertIsNone(plan); self.assertEqual(why, "stock_perp (list)")
        self.assertEqual(rd(BL.log_path()), []); self.assertEqual([x["gate_reason"] for x in rd(BL.log_path("dry"))], ["stock_perp"])
    def test_metadata_failure_fails_closed(self):
        def pub(path, params=None):
            if path == "/api/v1/market/instruments": raise RuntimeError("503")
            return self.fx.public(path, params)
        ins = {"NEW-USDT": dict(INS["BTC-USDT"], instId="NEW-USDT", assetClass="")}
        with mock.patch.object(bf, "public", pub):
            plan, why = EX.evaluate(dict(SETUP, instId="NEW-USDT"), dict(self.ctx, ins=ins))
        self.assertIsNone(plan); self.assertEqual(why, "stock_perp (data error)")

class TestBlocklog(NoNetwork):
    def setUp(self): super().setUp(); clear_logs()
    def test_cli_and_dedupe(self):
        args = ["add", "--pair", "eth-usdt", "--side", "long", "--entry", "3000", "--sl", "2900", "--tp", "3400", "--rr", "4", "--run-id", "r1"]
        self.assertEqual(BL.main(args), 0); self.assertEqual(BL.main(args), 0)
        self.assertEqual(len(rd(BL.log_path())), 1); self.assertEqual(rd(BL.log_path())[0]["pair"], "ETH-USDT")
        self.assertEqual(BL.main(["add", "--pair", "ETH", "--side", "long", "--entry", "1", "--sl", "1", "--tp", "1", "--rr", "1"]), 2)
    def test_load_live_ignores_dry(self):
        BL.append(BL.build_line("x", "live", "A-USDT", "long", 1, 0.9, 2, 3, 1, "news_gate", fund={}))
        with open(BL.log_path(), "a") as fh: fh.write(json.dumps(dict(mode="dry", pair="B-USDT")) + "\n")
        self.assertEqual([x["pair"] for x in BL.load_live()], ["A-USDT"])

class TestCloseMatch(NoNetwork):
    REC = dict(instId="INJ-USDT", side="buy", opened_at="2026-01-01T00:00:00+00:00", position_createTime="1767225600000")
    def test_one_ms_off_matches(self):
        h = dict(instId="INJ-USDT", createTime="1767225600001", updateTime="1767229200000", openAveragePrice="10", closeAveragePrice="11", realizedPnl="1", fee="-0.01")
        self.assertEqual(closes.close_fit("INJ-USDT", self.REC, h), (1, 1))
        p = dict(instId="INJ-USDT", positions="1", createTime="1767225600001")
        self.assertTrue(registry.matches(p, self.REC))
    def test_opposite_side_and_positionid(self):
        short = dict(instId="INJ-USDT", createTime="1767225600000", openAveragePrice="10", closeAveragePrice="11", realizedPnl="-1", fee="-0.01")
        self.assertIsNone(closes.close_fit("INJ-USDT", self.REC, short))
        self.assertIsNone(closes.close_fit("INJ-USDT", dict(self.REC, positionId="1"), dict(instId="INJ-USDT", positionId="2", createTime="1767225600000")))
    def test_one_row_one_record(self):
        a = dict(self.REC); b = dict(self.REC, position_createTime="1767225660000")
        h = dict(instId="INJ-USDT", createTime="1767225660000", updateTime="1")
        consumed = set()
        self.assertIsNone(closes.pick("INJ-USDT", a, [h], consumed, [b]))    # b fits closer: left for b
        self.assertIs(closes.pick("INJ-USDT", b, [h], consumed, [a]), h)
        self.assertIsNone(closes.pick("INJ-USDT", a, [h], consumed, []))     # consumed
    def test_record_writes_line_with_funding(self):
        def get(path, params=None):
            if path == closes.PATH: return [dict(instId="INJ-USDT", positionId="9", createTime="1767225600000", updateTime="1767229200000",
                                                 openAveragePrice="10", closeAveragePrice="11", realizedPnl="1", fee="-0.01")]
            if path == funding.PATH: return [dict(instId="INJ-USDT", billId="b1", fundingTime="1767228000000", fundingFee="-0.02")]
            raise AssertionError(path)
        lines, errs = closes.record({"INJ-USDT": self.REC}, get, funding_get=get)
        self.assertEqual(errs, []); self.assertEqual(lines[0]["positionId"], "9")
        self.assertEqual(lines[0]["funding_paid"], -0.02); self.assertTrue(lines[0]["funding_final"])

class TestFunding(NoNetwork):
    def test_paged_dedup_and_window(self):
        calls = []
        def get(path, params):
            calls.append(params)
            return [dict(instId="X-USDT", billId="1", fundingTime="1000", fundingFee="-0.5"),
                    dict(instId="X-USDT", billId="2", fundingTime=str(9 * 86_400_000), fundingFee="0.1"),
                    dict(instId="Y-USDT", billId="3", fundingTime="1000", fundingFee="-9")]
        rec = dict(position_createTime="1")
        funding.apply(rec, "X-USDT", get, 10 * 86_400_000)
        self.assertEqual(rec["funding_paid"], -0.4); self.assertEqual(rec["funding_n"], 2); self.assertEqual(len(calls), 2)
    def test_update_never_raises(self):
        n, errs = funding.update({"X-USDT": dict()}, lambda *a: [])
        self.assertEqual(n, 0); self.assertTrue(errs and "no open time" in errs[0])

class TestRunlog(NoNetwork):
    def test_append_annotate(self):
        rid = runlog.new_run_id()
        self.assertTrue(runlog.append(dict(run_id=rid, source="manage.py", ok=True)))
        self.assertFalse(runlog.append(dict(run_id=rid, source="manage.py")))
        self.assertEqual(runlog.main(["annotate", "--run-id", rid, "--json", '{"note": "x"}']), 0)
        self.assertEqual(runlog.main(["annotate", "--run-id", rid, "--json", '{"ok": false}']), 2)
        self.assertEqual(runlog.main(["annotate", "--run-id", "nope", "--json", '{"a": 1}']), 3)
        self.assertEqual(runlog._load()[-1]["note"], "x")

class TestTransfers(NoNetwork):
    def test_detect_and_dedupe(self):
        bills = {"toAccount": [dict(ts="1767225600000", amount="100", currency="USDT", fromAccount="funding", toAccount="futures", transferId="t1")],
                 "fromAccount": [dict(ts="1767225700000", amount="40", currency="USDT", fromAccount="futures", toAccount="funding", transferId="t2")]}
        get = lambda p: dict(code="0", data=bills["toAccount" if "toAccount" in p else "fromAccount"])
        found, new, added = transfers.detect("2026-01-01T00:00:00Z", record=True, get=get)
        self.assertEqual([r["type"] for r in added], ["deposit", "withdrawal"])
        found, new, added = transfers.detect("2026-01-01T00:00:00Z", record=True, get=get)
        self.assertEqual(new, []); self.assertEqual(len(transfers.load_list()), 2)
        self.assertEqual(transfers.main(["add", "--type", "gift", "--amount", "1", "--ts-utc", "2026-01-01T00:00:00Z"]), 2)

class TestSlowCrowd(NoNetwork):
    def test_z_and_crowd(self):
        up = [100 * (1.0005 ** i) * (1 + 0.001 * ((-1) ** i)) for i in range(SCW.NEED)]
        self.assertGreater(SCW.tsmom_z(up), 1)
        with self.assertRaises(ValueError): SCW.tsmom_z(up[:100])
        nl, ns = SCW.crowd_sets({"A": 0.003, "B": 0.001, "C": -0.002, "D": 0.0}, 1)
        self.assertEqual((nl, ns), ({"A"}, {"C"}))
        self.assertEqual(SCW.funding_paid([dict(fundingRate="0.001", fundingTime="5")], "buy", 1000, 0, 10), 1.0)

class TestShadowVariants(GateBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(SV, "candles", fake_candles_up); p.start(); self.patches.append(p)
        self.mk = SV.Market(); self.mk.ins = lambda i: self.ins.get(i)
        self.mk.h1 = lambda i: fake_candles_up(i, "1H"); self.mk.ticker = lambda i: dict(askPrice="104000", bidPrice="103990", last="104000")
        self.now = dt.datetime.now(dt.timezone.utc); self.ts = self.now.isoformat()
    def book(self): return dict(open=[], closed_n=0)
    def test_books_differ(self):
        for v in ("BASELINE", "V_1H", "BASELINE_NO1H", "V_ATR15"):
            d, why, pl = SV.evaluate(v, SETUP, self.book(), self.mk, 1000.0, self.ts, self.now)
            self.assertEqual(d, "TAKE", (v, why))
        d, why, _ = SV.evaluate("STOCKS_ONLY", SETUP, self.book(), self.mk, 1000.0, self.ts, self.now)
        self.assertEqual(why, "not stock_perp")
        with mock.patch.object(SV, "RR_CAP", 3.0):
            d, why, _ = SV.evaluate("V_RR10", SETUP, self.book(), self.mk, 1000.0, self.ts, self.now)
        self.assertTrue(why.startswith("rr>="))
        self.mk.h1 = lambda i: fake_candles_down(i, "1H")
        self.assertEqual(SV.evaluate("V_1H", SETUP, self.book(), self.mk, 1000.0, self.ts, self.now)[1], "1h_trend (down)")
        self.assertNotIn("1h_trend", SV.evaluate("BASELINE_NO1H", SETUP, self.book(), self.mk, 1000.0, self.ts, self.now)[1])
    def test_crowd_and_slowtrend(self):
        self.mk.crowd = lambda now_ms: ({"BTC-USDT"}, set())
        self.assertTrue(SV.evaluate("V_CROWD", SETUP, self.book(), self.mk, 1000.0, self.ts, self.now)[1].startswith("crowd: long"))
        self.mk.z = lambda i: -2.0
        self.assertTrue(SV.evaluate("V_SLOWTREND", SETUP, self.book(), self.mk, 1000.0, self.ts, self.now)[1].startswith("slowtrend: against"))
        self.mk.z = lambda i: 2.0
        self.assertEqual(SV.evaluate("V_SLOWTREND", SETUP, self.book(), self.mk, 1000.0, self.ts, self.now)[0], "TAKE")
    def test_exit_tracking(self):
        d, why, pl = SV.evaluate("BASELINE", SETUP, self.book(), self.mk, 1000.0, self.ts, self.now)
        pos = SV.open_pos("BASELINE", SETUP, 1, pl, self.ts, self.now)
        t = pos["cursor"]
        self.assertEqual(SV.step_bars(pos, [[t, 104000, 113500, 103900, 113000]])[1], "tp")
        pos = SV.open_pos("BASELINE", SETUP, 1, pl, self.ts, self.now)
        fill, kind, _ = SV.step_bars(pos, [[t, 104000, 104100, 101000, 101200]])
        self.assertEqual((fill, kind), (pl["stop"], "stop"))
    def test_never_posts(self):
        with self.assertRaises(PermissionError): SV._get_only("POST", "/api/v1/trade/order")

class TestScannerGuards(NoNetwork):
    def _run(self, env):
        import subprocess
        return subprocess.run([sys.executable, os.path.join(T.ROOT, "scripts", "scanner.py")], capture_output=True, text=True, env=env, timeout=30)
    def test_locked_and_recent_skip(self):
        import fcntl, tempfile
        d = tempfile.mkdtemp(); env = dict(os.environ, DATA_DIR=d, BLOFIN_BASE_URL="http://127.0.0.1:9")   # unreachable: any HTTP call would fail
        fd = os.open(os.path.join(d, "scanner.lock"), os.O_CREAT | os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX)
        r = self._run(env); os.close(fd)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "SKIP: locked"))
        with open(os.path.join(d, "scanner_state.json"), "w") as fh: json.dump(dict(last_run=dt.datetime.now(dt.timezone.utc).isoformat()), fh)
        r = self._run(env)
        self.assertEqual(r.returncode, 0); self.assertTrue(r.stdout.startswith("SKIP: ran"), r.stdout + r.stderr)

class TestManageBookkeeping(NoNetwork):
    def test_close_funding_and_run_record(self):
        import manage
        rec = dict(instId="INJ-USDT", side="buy", opened_at="2026-01-01T00:00:00+00:00", position_createTime="1767225600000")
        def get(path, params=None):
            if path == closes.PATH: return [dict(instId="INJ-USDT", positionId="9", createTime="1767225600001", updateTime="1767229200000",
                                                 openAveragePrice="10", closeAveragePrice="11", realizedPnl="1", fee="-0.01")]
            if path == funding.PATH: return []
            raise AssertionError(path)
        registry.save({"INJ-USDT": rec}); registry.save_pending([])
        registry.prune_full([])                       # position gone -> queued in closed_pending.json
        with mock.patch.object(manage, "_get", get):
            book = manage.bookkeeping({"INJ-USDT": rec}, {})
        self.assertEqual(registry.load_pending(), [])
        self.assertEqual(book["closes"][0]["positionId"], "9"); self.assertEqual(book["errors"], [])
        res = dict(ok=True); rid = manage.log_run("status", res, book)
        self.assertTrue(rid); self.assertEqual(runlog._load()[runlog.find(runlog._load(), rid)]["closes"][0]["positionId"], "9")

# ---- live rules ported 2026-10-20: cooldown, direction cap 3, BE at round-trip fees, slippage, commodities ----------
def write_closed(lines):
    with open(config.data_path(closes.FILE), "w") as fh:
        for l in lines: fh.write(json.dumps(l) + "\n")

class TestCooldown(GateBase):
    def setUp(self):
        super().setUp(); self.now = dt.datetime.now(dt.timezone.utc); self.nm = int(self.now.timestamp() * 1000)
    def tearDown(self):
        write_closed([]); super().tearDown()
    def L(self, **k):
        d = dict(instId="BTC-USDT", side="buy", realized_pnl=-10, kind="stop", close_ms=self.nm - 3_600_000, be_done=False)
        d.update(k); return d
    def test_values(self):
        self.assertEqual(config.CFG["risk"]["cooldown_hours"], 4); self.assertEqual(EX.MAXDIR, 3)
        self.assertEqual(EX.SLIP, 0.0018); self.assertEqual(config.CFG["management"]["breakeven_fee_legs"], 2)
    def test_losing_stop_blocks_same_side_4h(self):
        write_closed([self.L()])
        plan, why = EX.evaluate(SETUP, self.ctx)
        self.assertIsNone(plan); self.assertTrue(why.startswith("cooldown until"), why)
        self.assertFalse(closes.cooldown_check("BTC-USDT", "sell", self.now)["block"])                 # opposite side ok
        write_closed([self.L(close_ms=self.nm - 5 * 3_600_000)])
        self.assertFalse(closes.cooldown_check("BTC-USDT", "buy", self.now)["block"])                  # after 4h
    def test_kinds_that_do_not_trigger(self):
        for l in (self.L(realized_pnl=5, kind="tp"), self.L(kind="manual"), self.L(close_avg=100.2, be_stop=100.1),
                  self.L(be_done=True, close_avg=100.0, be_stop=100.1),         # BE fired, within the 0.18% slippage tolerance
                  self.L(be_done=True, realized_pnl=-20, equity_at_entry=1000), self.L(realized_pnl=0)):   # BE fired, 20 < 0.5R (R = 50)
            write_closed([l]); self.assertFalse(closes.cooldown_check("BTC-USDT", "buy", self.now)["block"], l)
        for l in (self.L(be_done=True), self.L(be_done=True, close_avg=99.5, be_stop=100.1),   # BE marked, filled far below
                  self.L(realized_pnl=-30, equity_at_entry=1000),                                 # 30 >= 0.5R
                  self.L(close_avg=100.0, be_stop=100.1),                                         # sync 4 (as live): no BE, slip tolerance does not apply
                  self.L(realized_pnl=-20, equity_at_entry=1000)):                                # sync 4 (as live): no BE, 0.5R does not apply
            write_closed([l]); self.assertTrue(closes.cooldown_check("BTC-USDT", "buy", self.now)["block"], l)
        for k in ("stop", "trail", "cut"):
            write_closed([self.L(kind=k)]); self.assertTrue(closes.cooldown_check("BTC-USDT", "buy", self.now)["block"], k)
    def test_unreadable_fails_closed(self):
        with open(config.data_path(closes.FILE), "w") as fh: fh.write("{broken\n")
        plan, why = EX.evaluate(SETUP, self.ctx)
        self.assertIsNone(plan); self.assertTrue(why.startswith("cooldown check error"))
    def test_close_kind(self):
        self.assertEqual(closes.close_kind(dict(close_by="cut"), -5), "cut")
        self.assertEqual(closes.close_kind({}, -5), "stop"); self.assertEqual(closes.close_kind({}, 5), "tp")

class TestDirCap(GateBase):
    def pos(self, inst, n="1", ct=None):
        return dict(instId=inst, positions=n, createTime=str(ct or T.now_ms()))
    def tearDown(self): registry.save({}); super().tearDown()
    def test_three_bot_longs_block_manual_not_counted(self):
        ps = [self.pos(f"P{i}-USDT") for i in range(3)]
        registry.save({p["instId"]: dict(instId=p["instId"], side="buy", position_createTime=p["createTime"]) for p in ps})
        plan, why = EX.evaluate(SETUP, dict(self.ctx, pos=ps))
        self.assertIsNone(plan); self.assertEqual(why, "direction cap 3/3")
        registry.save({p["instId"]: dict(instId=p["instId"], side="buy", position_createTime=p["createTime"]) for p in ps[:2]})
        plan, why = EX.evaluate(SETUP, dict(self.ctx, pos=ps))   # third long is manual: 2/3 bot, passes
        self.assertIsNotNone(plan, why)
        self.assertEqual(EX.bot_side_count("sell", ps), 0)
    def test_unreadable_registry_fails_closed(self):
        with open(config.data_path(registry.FILE), "w") as fh: fh.write("nope")
        plan, why = EX.evaluate(SETUP, self.ctx)
        self.assertIsNone(plan); self.assertTrue(why.startswith("unresolved_record check error"), why)   # first registry reader (runs before caps)
        self.assertTrue(EX.dircap_block("buy", []).startswith("direction cap check error"))                # the cap check still fails closed

class TestSlippageAndBE(NoNetwork):
    def test_sizing_includes_slippage(self):
        self.assertAlmostEqual(EX.risk_per_coin(100, 98), 2 + 0.0006 * 198 + 0.0018 * 100)
        self.assertAlmostEqual(EX.planned_risk_per_coin(100, 98), 2 + 0.0018 * 100 + 0.0006 * 198 + 0.0018 * 100)   # + entry slip
        self.assertEqual(EX.size_for(1000, 100, 98, 1, "0.01"), float(int(50 / EX.planned_risk_per_coin(100, 98) * 100) / 100))
    def test_hard_cut_distance_solves_all_in(self):
        import manage
        for long_ in (True, False):
            d = manage.hard_cut_distance(100.0, 10.0, long_, 1000.0)
            stop = 100 - d if long_ else 100 + d
            self.assertAlmostEqual(manage.loss_at(100.0, stop, 10.0), 0.055 * 1000, places=6)
    def test_breakeven_price_round_trip_fees(self):
        import manage
        self.assertEqual(manage.breakeven_price(dict(averagePrice="8.4689", positions="1"), "0.0001"), 8.4791)   # 8.4689 x 1.0012 = 8.47906 -> up
        self.assertEqual(manage.breakeven_price(dict(averagePrice="8.4689", positions="-1"), "0.0001"), 8.4587)  # x 0.9988 = 8.45874 -> down
        self.assertEqual(manage.breakeven_price(dict(averagePrice="10", positions="1"), "0.01", dict(be_stop=10.05)), 10.05)  # recorded wins

class TestCommodities(GateBase):
    def test_commodity_allowed_stock_blocked_universe_any_class(self):
        ins = dict(self.ins, **{"XAU-USDT": dict(INS["BTC-USDT"], instId="XAU-USDT", assetClass="Crypto")})
        plan, why = EX.evaluate(dict(SETUP, instId="XAU-USDT"), dict(self.ctx, ins=ins))
        self.assertIsNotNone(plan, why); self.assertEqual(plan["asset_class"], "commodity")
        import scan
        data = [dict(instId=i, state="live", offTime="", assetClass=c) for i, c in (("A-USDT", "Commodities"), ("B-USDT", "Stocks"), ("C-USDT", "Forex"))]
        with mock.patch.object(bf, "public", return_value=dict(code="0", data=data)):
            self.assertEqual(sorted(scan.tradable_instruments()), ["A-USDT", "B-USDT", "C-USDT"])

# ---- reviewer fixes ---------------------------------------------------------------------------------------------
import hashlib
def snapshot(d):
    out = {}
    for root, _, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            with open(p, "rb") as fh: out[p] = hashlib.sha256(fh.read()).hexdigest()
    return out

class TestPendingCooldown(GateBase):
    def setUp(self):
        super().setUp(); registry.save({}); registry.save_pending([]); write_closed([])
        self.fresh = dt.datetime.now(dt.timezone.utc).isoformat()
    def tearDown(self):
        registry.save({}); registry.save_pending([]); write_closed([]); super().tearDown()
    def test_execute_prune_then_same_pair_side_refused(self):
        registry.save({"BTC-USDT": dict(instId="BTC-USDT", side="buy", opened_at=self.fresh, position_createTime=str(T.now_ms() - 3_600_000))})
        with mock.patch.object(EX, "place", side_effect=AssertionError("must not place")):
            r = EX.run([SETUP], self.fresh)          # exchange shows no position -> execute prunes the record
        self.assertEqual(len(registry.load_pending()), 1); self.assertFalse(r["ok"])
        self.assertTrue(r["skipped"][0]["why"].startswith("cooldown: pending close not logged yet"), r)
        self.assertFalse(closes.cooldown_check("BTC-USDT", "sell")["block"])        # other side free
    def test_pending_kinds_and_expiry(self):
        now = dt.datetime.now(dt.timezone.utc)
        for rec, block in ((dict(close_by="cut"), True), (dict(close_by="stop"), True), (dict(close_by="manual"), False),
                           (dict(be_done=True), True), (dict(pending_since=(now - dt.timedelta(hours=5)).isoformat()), False)):
            registry.save_pending([dict(dict(instId="BTC-USDT", side="buy", pending_since=now.isoformat()), **rec)])
            self.assertEqual(closes.cooldown_check("BTC-USDT", "buy", now)["block"], block, rec)
    def test_close_ms_zero_uses_logged_utc(self):
        write_closed([dict(instId="BTC-USDT", side="buy", realized_pnl=-1, kind="stop", close_ms=0,
                           logged_utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))])
        self.assertTrue(closes.cooldown_check("BTC-USDT", "buy")["block"])
    def test_corrupt_line_number_in_reason(self):
        with open(config.data_path(closes.FILE), "w") as fh: fh.write(json.dumps(dict(a=1)) + "\n{bad\n")
        plan, why = EX.evaluate(SETUP, self.ctx)
        self.assertIsNone(plan); self.assertIn("line 2 is corrupt", why)
    def test_append_line_locked_fsync(self):
        closes.append_line(dict(instId="X-USDT")); closes.append_line(dict(instId="Y-USDT"))
        self.assertEqual([x["instId"] for x in closes.load_closed()], ["X-USDT", "Y-USDT"])
        self.assertTrue(os.path.exists(config.data_path(closes.FILE) + ".lock"))

class TestDryRunReadOnly(GateBase):
    def test_dry_leaves_data_byte_identical(self):
        fresh = dt.datetime.now(dt.timezone.utc).isoformat()
        registry.save({"OLD-USDT": dict(instId="OLD-USDT", side="buy", opened_at=fresh, position_createTime="1")})   # would be pruned live
        registry.save_pending([])
        before = snapshot(T.TMP)
        r = EX.run([SETUP], fresh, dry=True)
        self.assertTrue(r["ok"] and r["dry"], r)
        self.assertEqual(snapshot(T.TMP), before)
        registry.save({})

class TestPositionIdStored(NoNetwork):
    def test_place_records_position_id(self):
        def priv(method, path, params=None, body=None):
            if method == "POST": return {"code": "0", "data": [{"code": "0"}]} if path == "/api/v1/trade/order" else {"code": "0", "data": {"code": "0"}}
            if path == "/api/v1/account/positions":
                return {"code": "0", "data": [dict(instId="BTC-USDT", positions="1", averagePrice="104000", marginMode="isolated",
                                                    positionSide="net", createTime="123", positionId="777")]}
            return {"code": "0", "data": []}
        plan = dict(instId="BTC-USDT", side="buy", stop=103500.0, target=110000.0, entry=104000.0, stop_dist=500.0, atr1h=100.0,
                    tick="0.1", lev_req={}, order_req=dict(clientOrderId="bfx1"))
        with mock.patch.object(bf, "private", priv): EX.place(plan)
        self.assertEqual(registry.load()["BTC-USDT"]["positionId"], "777"); registry.save({})

class TestBreakevenPriority(NoNetwork):
    def test_recorded_be_stop_wins_over_formula(self):
        import manage
        p = dict(averagePrice="100", positions="-1")
        self.assertEqual(manage.breakeven_price(p, "0.01"), 99.88)
        self.assertEqual(manage.breakeven_price(p, "0.01", dict(be_stop=99.95)), 99.95)
        self.assertEqual(manage.breakeven_price(p, "0.01", dict(be_stop=None)), 99.88)

class TestShadowManageStep(NoNetwork):
    def pos(self, **k):
        d = dict(side="buy", entry=100.0, sl=98.0, coins=10.0, tick="0.01", be=False, best=102.0, trig=101.5, cb=1.0,
                 trail_on=False, tbest=None, equity_at_entry=1000.0); d.update(k); return d
    def test_be_only_when_stop_moves(self):
        p = self.pos(); SV.manage_step(p, 101.0, 1000.0, 1.0, 0)
        self.assertTrue(p["be"]); self.assertEqual(p["sl"], 100.12)
        p = self.pos(sl=100.5); SV.manage_step(p, 101.0, 1000.0, 1.0, 0)        # stop already past BE: not moved
        self.assertFalse(p["be"]); self.assertEqual(p["sl"], 100.5)
    def test_hard_cut_uses_equity_at_entry(self):
        p = self.pos(coins=100.0, best=100.0, equity_at_entry=1000.0)   # loss at 99.6 ~ 70 > 55 (5.5% of 1000)
        self.assertEqual(SV.manage_step(p, 99.6, 100000.0, 1.0, 5)[1], "cut")
        p = self.pos(coins=100.0, best=100.0, equity_at_entry=100000.0)
        self.assertIsNone(SV.manage_step(p, 99.6, 1000.0, 1.0, 5))

class TestScannerImport(NoNetwork):
    def test_import_has_no_side_effects(self):
        import importlib
        lockp = config.data_path("scanner.lock")
        if os.path.exists(lockp): os.remove(lockp)
        importlib.import_module("scanner")
        self.assertFalse(os.path.exists(lockp))


# ---- second review fixes ----------------------------------------------------------------------------------------
PLAN = dict(instId="BTC-USDT", side="buy", stop=103500.0, target=110000.0, entry=104000.0, stop_dist=500.0, atr1h=100.0,
            tick="0.1", equity=1000.0, lev_req={}, order_req=dict(clientOrderId="bfxprov1"))

class TestProvisionalRecord(NoNetwork):
    def setUp(self): super().setUp(); registry.save({})
    def tearDown(self): registry.save({}); super().tearDown()
    def priv(self, order):
        def priv(method, path, params=None, body=None):
            if path == "/api/v1/account/set-leverage": return {"code": "0", "data": {"code": "0"}}
            if path == "/api/v1/trade/order": return order()
            return {"code": "0", "data": []}
        return priv
    def test_unknown_outcome_keeps_provisional_record_and_it_counts(self):
        def boom(): raise TimeoutError("read timed out")
        with mock.patch.object(bf, "private", self.priv(boom)), mock.patch.object(EX, "resolve_uncertain_order", return_value=("unknown", [])):
            r = EX.place(dict(PLAN))
        self.assertFalse(r["ok"]); self.assertIn("ORDER OUTCOME UNKNOWN", r["why"])
        rec = registry.load()["BTC-USDT"]
        self.assertTrue(rec["provisional"]); self.assertEqual(rec["clientOrderId"], "bfxprov1")
        self.assertEqual(rec["initial_stop"], 103500.0); self.assertEqual(rec["equity_at_entry"], 1000.0)
        p = dict(instId="BTC-USDT", positions="1", createTime=str(T.now_ms()))   # position shows up later
        self.assertEqual(registry.match(p)["clientOrderId"], "bfxprov1")         # adopted as the bot's
        self.assertEqual(EX.bot_side_count("buy", [p]), 1)
    def test_rejected_drops_record(self):
        with mock.patch.object(bf, "private", self.priv(lambda: {"code": "1", "msg": "no"})):
            r = EX.place(dict(PLAN))
        self.assertFalse(r["ok"]); self.assertNotIn("BTC-USDT", registry.load())
    def test_put_failure_aborts_before_order(self):
        sent = []
        def order(): sent.append(1); return {"code": "0", "data": [{"code": "0"}]}
        with mock.patch.object(bf, "private", self.priv(order)), mock.patch.object(registry, "put", side_effect=OSError("disk full")):
            r = EX.place(dict(PLAN))
        self.assertFalse(r["ok"]); self.assertIn("order NOT sent", r["why"]); self.assertEqual(sent, [])

class TestDirCapHistory(NoNetwork):
    def test_history_owned_position_counts(self):
        import manage
        p = dict(instId="ETH-USDT", positions="1", createTime=str(T.now_ms()))
        with mock.patch.object(manage, "history_says_bot", return_value=True):
            self.assertEqual(EX.bot_side_count("buy", [p], {}), 1)
        with mock.patch.object(manage, "history_says_bot", return_value=False):
            self.assertEqual(EX.bot_side_count("buy", [p], {}), 0)

class TestHardCutFallback(NoNetwork):
    def setUp(self):
        super().setUp()
        import manage; self.m = manage
        self.p1 = dict(instId="A-USDT", positions="10", averagePrice="100", markPrice="90", marginMode="isolated", positionSide="net")
        self.p2 = dict(instId="B-USDT", positions="10", averagePrice="100", markPrice="100", marginMode="isolated", positionSide="net")
        self.ps = [mock.patch.object(manage, "wallet_balance", return_value=1000.0),
                   mock.patch.object(manage, "positions", return_value=[self.p1, self.p2]),
                   mock.patch.object(manage, "owner", return_value=(True, dict(equity_at_entry=1000.0))),
                   mock.patch.object(manage, "_instrument", return_value=dict(tickSize="0.01", contractValue="1")),
                   mock.patch.object(manage, "pending_tpsl", return_value=[])]
        for x in self.ps: x.start()
    def tearDown(self):
        for x in self.ps: x.stop()
        super().tearDown()
    def test_close_fails_then_tightest_valid_stop(self):
        stops = []
        with mock.patch.object(self.m, "close_position", return_value=dict(ok=False, why="exchange 500")), \
             mock.patch.object(self.m, "replace_stop", side_effect=lambda p, sl, keep_tp=True: stops.append((p["instId"], sl)) or dict(ok=True)):
            out = self.m.hard_cut()
        self.assertIn(("A-USDT", 89.99), stops)                         # mark 90 is past the cut: one tick inside the mark
        self.assertTrue(any(s[0] == "B-USDT" for s in stops))             # second position still handled
        self.assertTrue(any(e["instId"] == "A-USDT" for e in out["errors"]))
    def test_close_raises_and_one_error_does_not_skip_rest(self):
        stops = []
        def rs(p, sl, keep_tp=True):
            if p["instId"] == "A-USDT": raise RuntimeError("boom")
            stops.append(p["instId"]); return dict(ok=True)
        with mock.patch.object(self.m, "close_position", side_effect=RuntimeError("timeout")), mock.patch.object(self.m, "replace_stop", side_effect=rs):
            out = self.m.hard_cut()
        self.assertEqual(stops, ["B-USDT"]); self.assertTrue(any("A-USDT" == e["instId"] for e in out["errors"]))

class TestEnsureStopsManualPriceOnly(NoNetwork):
    def test_manual_uses_price_only(self):
        import manage
        p = dict(instId="M-USDT", positions="10", averagePrice="100", markPrice="100", marginMode="isolated", positionSide="net")
        seen = {}
        real = manage.hard_cut_distance
        def hcd(*a, **k): seen.update(k); return real(*a, **k)
        with mock.patch.object(manage, "positions", return_value=[p]), mock.patch.object(manage, "pending_tpsl", return_value=[]), \
             mock.patch.object(manage, "owner", return_value=(False, None)), mock.patch.object(manage, "PROTECT_MANUAL", False), \
             mock.patch.object(manage, "wallet_balance", return_value=1000.0), mock.patch.object(manage, "_instrument", return_value=dict(tickSize="0.01", contractValue="1")), \
             mock.patch.object(manage, "hard_cut_distance", side_effect=hcd), mock.patch.object(manage, "place_tpsl", return_value=dict(ok=True)):
            manage.ensure_stops()
        self.assertEqual((seen.get("fee"), seen.get("slip")), (0.0, 0.0))
