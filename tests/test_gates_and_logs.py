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
        super().setUp(); clear_logs()
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
        for l in (self.L(realized_pnl=5, kind="tp"), self.L(kind="manual"), self.L(be_done=True),
                  self.L(close_avg=100.2, be_stop=100.1), self.L(realized_pnl=0)):
            write_closed([l]); self.assertFalse(closes.cooldown_check("BTC-USDT", "buy", self.now)["block"], l)
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
        self.assertIsNone(plan); self.assertTrue(why.startswith("direction cap check error"))

class TestSlippageAndBE(NoNetwork):
    def test_sizing_includes_slippage(self):
        self.assertAlmostEqual(EX.risk_per_coin(100, 98), 2 + 0.0006 * 198 + 0.0018 * 100)
        self.assertEqual(EX.size_for(1000, 100, 98, 1, "0.01"), float(int(50 / EX.risk_per_coin(100, 98) * 100) / 100))
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
