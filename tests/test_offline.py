"""Offline unit tests. No network and no orders: every exchange call is mocked.
Run: python3 -m unittest discover -s tests -v
"""
import os, sys, json, socket, tempfile, unittest, urllib.error, datetime as dt
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = TMP
os.environ["BOT_CONFIG"] = os.path.join(ROOT, "config.example.yaml")
for k in ("BLOFIN_API_KEY", "BLOFIN_API_SECRET", "BLOFIN_PASSPHRASE"): os.environ.pop(k, None)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import config, news_gate, registry, execute as EX, blofin_client as bf
import manage

def now_ms(): return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
def iso_ago(sec=0): return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=sec)).isoformat()

class NoNetwork(unittest.TestCase):
    def setUp(self):
        # any real HTTP call fails the test
        self.p = mock.patch.object(bf, "_http", side_effect=AssertionError("network call in offline test"))
        self.p.start()
    def tearDown(self): self.p.stop()

class TestConfig(NoNetwork):
    def test_example_equals_defaults(self):
        cfg = config.load(os.path.join(ROOT, "config.example.yaml"))
        a = {k: v for k, v in cfg.items() if k not in ("data_dir", "_config_path")}
        d = {k: v for k, v in config.DEFAULTS.items() if k != "data_dir"}
        self.assertEqual(a, d)

    def _bad(self, text):
        f = os.path.join(TMP, "bad.yaml")
        with open(f, "w") as fh: fh.write(text)
        with self.assertRaises(config.ConfigError): config.load(f)

    def test_risk_bounds(self):
        self._bad("risk:\n  risk_per_trade_pct: 0\n")
        self._bad("risk:\n  risk_per_trade_pct: 11\n  hard_cut_pct: 12\n")
        self._bad("risk:\n  risk_per_trade_pct: abc\n")
        self._bad("risk:\n  risk_per_trade_pct: true\n")

    def test_hard_cut_ge_risk(self):
        self._bad("risk:\n  risk_per_trade_pct: 5\n  hard_cut_pct: 4\n")

    def test_block_confidence_nonempty(self):
        self._bad("news_gate:\n  block_confidence: []\n")
        self._bad("news_gate:\n  block_confidence: [maybe]\n")

    def test_unparseable_raises(self):
        self._bad("risk:\n  this line has no colon\n")
        self._bad("just some text\n")
        self._bad("news_gate:\n  block_confidence:\n    - level: high\n")

    def test_block_list_parses(self):
        d = config._mini_yaml("a:\n  l:\n    - medium\n    - high\n  x: 1\nb: [1, 2]\n")
        self.assertEqual(d, {"a": {"l": ["medium", "high"], "x": 1}, "b": [1, 2]})

    def test_valid_override(self):
        f = os.path.join(TMP, "ok.yaml")
        with open(f, "w") as fh: fh.write("risk:\n  risk_per_trade_pct: 1\n  hard_cut_pct: 1.5\n")
        cfg = config.load(f)
        self.assertEqual(cfg["risk"]["risk_per_trade_pct"], 1); self.assertEqual(cfg["risk"]["max_open"], 4)

class TestNewsGateSide(NoNetwork):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(ROOT, "examples", "fundamentals.example.json")
        self.now = dt.datetime(2026, 1, 1, 13, 0, tzinfo=dt.timezone.utc)

    def test_normalization(self):
        for s, want in [("buy", "buy"), ("BUY", "buy"), ("Long", "buy"), (" long ", "buy"),
                        ("sell", "sell"), ("SHORT", "sell"), ("Sell", "sell")]:
            self.assertEqual(news_gate.norm_side(s), want)

    def test_invalid_side_raises(self):
        for s in ("", "up", "bull", "b", None, "longg"):
            with self.assertRaises(ValueError): news_gate.norm_side(s)

    def test_long_alias_blocks_like_buy(self):
        r1 = news_gate.check("ETH-USDT", "long", self.path, self.now)
        r2 = news_gate.check("eth-usdt", "BUY", self.path, self.now)
        self.assertTrue(r1["block"]); self.assertTrue(r2["block"])
        self.assertFalse(news_gate.check("ETH-USDT", "Short", self.path, self.now)["block"])

    def test_cli_exit_codes(self):
        import subprocess
        env = dict(os.environ)
        bad = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "news_gate.py"), "BTC-USDT", "sideways"], capture_output=True, env=env)
        self.assertEqual(bad.returncode, 2)
        none = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "news_gate.py"), "BTC-USDT"], capture_output=True, env=env)
        self.assertEqual(none.returncode, 2)
        ok = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "news_gate.py"), "BTC-USDT", "LONG"], capture_output=True, env=env)
        self.assertEqual(ok.returncode, 0); self.assertIn("block", json.loads(ok.stdout))

# ---- execute.py with a fake exchange ----------------------------------------------------------
INS = {"BTC-USDT": dict(instId="BTC-USDT", tickSize="0.1", lotSize="0.1", contractValue="0.001", minSize="0.1",
                        maxLeverage="100", state="live", offTime="", assetClass="Crypto")}
def fake_candles_up(inst, bar, limit=None):
    # steadily rising closed candles: trend "up", ATR about 100
    return [[100000 + i * 20, 100000 + i * 20 + 60, 100000 + i * 20 - 60, 100000 + i * 20 + 10] for i in range(200)]

class FakeExchange:
    def __init__(self, bid=103990.0, ask=104000.0):
        self.bid, self.ask = bid, ask; self.posts = []
    def public(self, path, params=None):
        if path == "/api/v1/market/tickers": return {"code": "0", "data": [dict(instId="BTC-USDT", bidPrice=str(self.bid), askPrice=str(self.ask))]}
        if path == "/api/v1/market/instruments": return {"code": "0", "data": list(INS.values())}
        raise AssertionError(path)
    def private(self, method, path, params=None, body=None):
        if method != "GET": self.posts.append((path, body)); raise AssertionError(f"order call {path} in test")
        if path == "/api/v1/account/balance": return {"code": "0", "data": {"details": [dict(currency="USDT", equity="1000", available="1000")]}}
        if path == "/api/v1/account/positions": return {"code": "0", "data": []}
        if path == "/api/v1/account/position-mode": return {"code": "0", "data": {"positionMode": "net_mode"}}
        raise AssertionError(path)

class TestExecuteValidation(NoNetwork):
    def setUp(self):
        super().setUp()
        self.fx = FakeExchange()
        self.patches = [mock.patch.object(bf, "public", self.fx.public), mock.patch.object(bf, "private", self.fx.private),
                        mock.patch.object(EX, "candles", fake_candles_up)]
        for p in self.patches: p.start()
        self.ctx = dict(equity=1000.0, avail=1000.0, pos=[], ins=INS)
        registry.save_pending([]); registry.save({})   # no leftover bot record (unresolved_record_block)
        self.fresh = dt.datetime.now(dt.timezone.utc).isoformat()
    def tearDown(self):
        for p in self.patches: p.stop()
        super().tearDown()

    def test_geometry_pure(self):
        self.assertIsNone(EX.check_geometry("buy", 100, 90, 130))
        self.assertIsNotNone(EX.check_geometry("buy", 100, 105, 130))   # stop above entry
        self.assertIsNotNone(EX.check_geometry("buy", 100, 90, 95))     # target below entry
        self.assertIsNone(EX.check_geometry("sell", 100, 110, 70))
        self.assertIsNotNone(EX.check_geometry("sell", 100, 95, 70))    # short stop below entry

    def test_wrong_side_stop_long_rejected(self):
        plan, why = EX.evaluate(dict(instId="BTC-USDT", side="buy", stop=104500, target=110000), self.ctx)
        self.assertIsNone(plan); self.assertIn("stop < entry < target", why)

    def test_wrong_side_stop_short_rejected(self):
        plan, why = EX.evaluate(dict(instId="BTC-USDT", side="sell", stop=103000, target=98000), self.ctx)
        self.assertIsNone(plan); self.assertIn("target < entry < stop", why)

    def test_bad_side_rejected(self):
        plan, why = EX.evaluate(dict(instId="BTC-USDT", side="up", stop=103000, target=110000), self.ctx)
        self.assertIsNone(plan); self.assertIn("bad setup", why)

    def test_stale_scan_rejected_before_any_account_call(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=36)).isoformat()
        with mock.patch.object(EX, "account", side_effect=AssertionError("account read on stale scan")):
            r = EX.run([dict(instId="BTC-USDT", side="buy", stop=103500, target=110000)], old)
        self.assertFalse(r["ok"]); self.assertIn("min old", r["why"])
        self.assertFalse(EX.run([], None)["ok"])

    def test_fresh_scan_ok(self):
        self.assertIsNone(EX.check_scan_fresh(self.fresh))

    def test_trend_recheck_rejects_countertrend(self):
        plan, why = EX.evaluate(dict(instId="BTC-USDT", side="sell", stop=104600, target=100000), self.ctx)
        self.assertIsNone(plan); self.assertIn("trend now up", why)

    def test_falls_through_to_next_setup_and_never_posts_on_failure(self):
        setups = [dict(instId="BTC-USDT", side="buy", stop=104500, target=110000),   # wrong-side stop
                  dict(instId="BTC-USDT", side="sell", stop=103000, target=98000)]   # wrong-side stop
        r = EX.run(setups, self.fresh)
        self.assertFalse(r["ok"]); self.assertEqual(len(r["skipped"]), 2); self.assertEqual(self.fx.posts, [])

    def test_valid_setup_plans_fee_inclusive_size(self):
        with mock.patch.object(EX, "pair_positions", return_value=[]):
            plan, why = EX.evaluate(dict(instId="BTC-USDT", side="buy", stop=101500, target=113000), self.ctx)
        self.assertIsNone(why, why)
        self.assertLessEqual(plan["est_loss_at_stop"], 1000 * EX.RISK + 1e-9)   # loss incl. fees <= 5% of equity
        self.assertGreater(plan["est_loss_at_stop"], 1000 * EX.RISK * 0.95)
        self.assertGreaterEqual(plan["rr"], EX.MINRR); self.assertLess(plan["rr_net"], plan["rr"])
        self.assertTrue(plan["order_req"]["clientOrderId"].startswith(config.CFG["bot"]["client_order_prefix"]))

    def test_min_rr_is_price_only(self):
        from scan import net_rr, rr
        self.assertEqual(rr("buy", 104000, 103500, 110000), 12.0); self.assertEqual(rr("sell", 100, 101, 97), 3.0)
        self.assertLess(net_rr("buy", 104000, 103500, 110000), 12.0)   # informational only
        # gross 3.0 passes even though net of fees it is below 3
        plan, why = EX.evaluate(dict(instId="BTC-USDT", side="buy", stop=101000, target=113000), self.ctx)
        self.assertIsNotNone(plan, why); self.assertEqual(plan["rr"], 3.0); self.assertLess(plan["rr_net"], 3.0)

    def test_accepted_order_is_ok_even_if_later_steps_fail(self):
        posts = []
        def priv(method, path, params=None, body=None):
            if method == "POST":
                posts.append(path)
                if path == "/api/v1/trade/order-algo": return {"code": "0", "data": {"algoId": None, "code": "148083", "msg": "Callback constant range"}}
                return {"code": "0", "data": [{"code": "0"}]} if path == "/api/v1/trade/order" else {"code": "0", "data": {"code": "0"}}
            if path == "/api/v1/account/positions":
                return {"code": "0", "data": [dict(instId="BTC-USDT", positions="1", averagePrice="104000", marginMode="isolated", positionSide="net")]}
            raise RuntimeError("network hiccup")
        plan = dict(instId="BTC-USDT", side="buy", stop=103500.0, target=110000.0, entry=104000.0, stop_dist=500.0, atr1h=100.0,
                    tick="0.1", lev_req={}, order_req=dict(clientOrderId="bfx1"))
        with mock.patch.object(bf, "private", priv):
            r = EX.place(plan)
        self.assertTrue(r["ok"]); self.assertTrue(any("trailing stop rejected" in w for w in r["warnings"]))
        self.assertTrue(any("TP/SL check failed" in w for w in r["warnings"]))
        self.assertTrue(os.path.exists(config.data_path("exec_last.json")))

    def test_stock_blocked_commodity_allowed(self):
        ins = {"BTC-USDT": dict(INS["BTC-USDT"], assetClass="Stocks")}
        plan, why = EX.evaluate(dict(instId="BTC-USDT", side="buy", stop=101500, target=113000), dict(self.ctx, ins=ins))
        self.assertIsNone(plan); self.assertIn("stock_perp", why)          # stock-perp block fires first
        ins = {"BTC-USDT": dict(INS["BTC-USDT"], assetClass="Commodities")}
        plan, why = EX.evaluate(dict(instId="BTC-USDT", side="buy", stop=101500, target=113000), dict(self.ctx, ins=ins))
        self.assertIsNotNone(plan, why); self.assertEqual(plan["asset_class"], "commodity")  # commodities allowed, tagged

    def test_news_blocked_candidate_skipped_in_evaluate(self):
        with mock.patch.object(EX.news_gate, "check", return_value=dict(block=True, reason="fresh bearish rating, high confidence")), \
             mock.patch.object(EX, "pair_positions", return_value=[]):
            plan, why = EX.evaluate(dict(instId="BTC-USDT", side="buy", stop=101500, target=113000), self.ctx)
        self.assertIsNone(plan); self.assertIn("news gate", why)

    def test_run_falls_through_news_block_without_posting(self):
        seen = []
        def gate(inst, side, *a, **k): seen.append(side); return dict(block=True, reason="blocked")
        with mock.patch.object(EX.news_gate, "check", gate), mock.patch.object(EX, "pair_positions", return_value=[]):
            r = EX.run([dict(instId="BTC-USDT", side="buy", stop=101500, target=113000)] * 2, self.fresh)
        self.assertFalse(r["ok"]); self.assertEqual(len(r["skipped"]), 2); self.assertEqual(len(seen), 2); self.assertEqual(self.fx.posts, [])

    def test_macro_window_blocks_in_evaluate(self):
        with mock.patch.object(EX.news_gate, "macro_check", return_value=dict(block=True, reason="macro window: US CPI")), \
             mock.patch.object(EX, "pair_positions", return_value=[]):
            plan, why = EX.evaluate(dict(instId="BTC-USDT", side="buy", stop=101500, target=113000), self.ctx)
        self.assertIsNone(plan); self.assertIn("macro window", why)

    def _priv_uncertain(self, detail, positions, posts):
        def priv(method, path, params=None, body=None):
            if method == "POST":
                posts.append(path)
                if path == "/api/v1/trade/order": raise bf.RequestUncertain("timeout after 20s")
                return {"code": "0", "data": {"code": "0"}}
            if path == "/api/v1/trade/order-detail": return detail
            if path == "/api/v1/account/positions": return {"code": "0", "data": positions}
            return {"code": "0", "data": [dict(tpslId="1")]}
        return priv

    def _plan(self):
        return dict(instId="BTC-USDT", side="buy", stop=103500.0, target=110000.0, entry=104000.0, stop_dist=500.0, atr1h=100.0,
                    tick="0.1", equity=1000.0, lev_req={}, order_req=dict(clientOrderId=bf.client_order_id("bfx")))

    def test_timeout_on_order_recovered_when_accepted(self):
        posts = []; ct = str(now_ms())
        pos = [dict(instId="BTC-USDT", positions="1", averagePrice="104000", marginMode="isolated", positionSide="net", createTime=ct)]
        priv = self._priv_uncertain({"code": "0", "data": {"state": "filled", "filledSize": "1"}}, pos, posts)
        with mock.patch.object(bf, "private", priv), mock.patch.object(EX.time, "sleep"):
            r = EX.place(self._plan())
        self.assertTrue(r["ok"], r); self.assertTrue(any("order response was lost" in w for w in r["warnings"]))
        self.assertIn("/api/v1/trade/order-algo", posts)
        rec = registry.load()["BTC-USDT"]
        self.assertEqual(rec["position_createTime"], ct); self.assertEqual(rec["equity_at_entry"], 1000.0)

    def test_timeout_on_order_unknown_when_not_found(self):
        posts = []
        priv = self._priv_uncertain({"code": "0", "data": []}, [], posts)
        with mock.patch.object(bf, "private", priv), mock.patch.object(EX.time, "sleep"):
            r = EX.place(self._plan())
        self.assertFalse(r["ok"]); self.assertIn("ORDER OUTCOME UNKNOWN", r["why"])
        self.assertEqual(posts, ["/api/v1/account/set-leverage", "/api/v1/trade/order"])   # nothing after the order

    def test_timeout_on_order_canceled_is_rejected(self):
        posts = []
        priv = self._priv_uncertain({"code": "0", "data": {"state": "canceled", "filledSize": "0"}}, [], posts)
        with mock.patch.object(bf, "private", priv), mock.patch.object(EX.time, "sleep"):
            r = EX.place(self._plan())
        self.assertFalse(r["ok"]); self.assertIn("not found", r["why"]); self.assertEqual(r["order_resp"]["resolution"], "rejected")

class TestClient(unittest.TestCase):
    def test_timeout_raises_uncertain(self):
        for exc in (socket.timeout("timed out"), urllib.error.URLError(socket.timeout("timed out")), ConnectionResetError("reset")):
            with mock.patch.object(bf.urllib.request, "urlopen", side_effect=exc):
                with self.assertRaises(bf.RequestUncertain): bf._http("POST", "https://example.invalid/x", body="{}")

    def test_dns_failure_is_not_uncertain(self):
        with mock.patch.object(bf.urllib.request, "urlopen", side_effect=urllib.error.URLError("Name or service not known")):
            with self.assertRaises(RuntimeError) as cm: bf._http("GET", "https://example.invalid/x")
        self.assertNotIsInstance(cm.exception, bf.RequestUncertain)

    def test_client_order_ids_unique_alnum_short(self):
        ids = [bf.client_order_id("bfx", k) for k in ("", "t", "s", "c") for _ in range(500)]
        self.assertEqual(len(set(ids)), len(ids))
        for i in ids: self.assertTrue(i.isalnum() and len(i) <= 32 and i.startswith("bfx"), i)

class TestConfigLookup(unittest.TestCase):
    def test_cwd_config_ignored(self):
        d = tempfile.mkdtemp(); bad = os.path.join(d, "config.yaml")
        with open(bad, "w") as fh: fh.write("risk:\n  risk_per_trade_pct: 0\n")
        old = os.getcwd()
        try:
            os.chdir(d)
            with mock.patch.dict(os.environ):
                os.environ.pop("BOT_CONFIG", None)
                cfg = config.load()   # would raise ConfigError if the cwd file were read
        finally: os.chdir(old)
        self.assertNotEqual(os.path.realpath(cfg["_config_path"] or ""), os.path.realpath(bad))
        self.assertEqual(cfg["risk"]["risk_per_trade_pct"], config.DEFAULTS["risk"]["risk_per_trade_pct"])

    def test_bot_config_missing_file_raises(self):
        with mock.patch.dict(os.environ, BOT_CONFIG=os.path.join(TMP, "nope.yaml")):
            with self.assertRaises(config.ConfigError): config.load()

class TestMacroCheck(unittest.TestCase):
    path = os.path.join(ROOT, "examples", "fundamentals.example.json")
    def test_disabled_allows(self):
        self.assertFalse(news_gate.macro_check(dt.datetime(2026, 1, 14, 13, 30, tzinfo=dt.timezone.utc), self.path)["block"])
    def test_enabled_blocks_inside_window(self):
        with mock.patch.dict(config.CFG["macro_window"], enabled=True):
            self.assertTrue(news_gate.macro_check(dt.datetime(2026, 1, 14, 12, 0, tzinfo=dt.timezone.utc), self.path)["block"])
            self.assertFalse(news_gate.macro_check(dt.datetime(2026, 1, 14, 10, 0, tzinfo=dt.timezone.utc), self.path)["block"])
    def test_enabled_without_events_fails_open_with_warning(self):
        f = os.path.join(TMP, "f_noevents.json")
        with open(f, "w") as fh: json.dump({"pairs": {}}, fh)
        with mock.patch.dict(config.CFG["macro_window"], enabled=True):
            r = news_gate.macro_check(dt.datetime(2026, 1, 14, 12, 0, tzinfo=dt.timezone.utc), f)
        self.assertFalse(r["block"]); self.assertTrue(r.get("warning"))

class TestRegistry(NoNetwork):
    def test_match_rules(self):
        ct = now_ms(); p = dict(instId="BTC-USDT", positions="1", createTime=str(ct))
        self.assertTrue(registry.matches(p, dict(instId="BTC-USDT", side="buy", opened_at=iso_ago(30))))
        self.assertTrue(registry.matches(p, dict(instId="BTC-USDT", side="buy", opened_at=iso_ago(86400), position_createTime=str(ct))))
        self.assertFalse(registry.matches(p, dict(instId="BTC-USDT", side="buy", opened_at=iso_ago(86400))))   # stale record
        self.assertFalse(registry.matches(p, dict(instId="BTC-USDT", side="sell", opened_at=iso_ago(30))))    # other side
        self.assertFalse(registry.matches(p, dict(instId="BTC-USDT", side="buy")))                            # pair + side only

    def test_prune_drops_stale(self):
        ct = now_ms()
        registry.save({"BTC-USDT": dict(instId="BTC-USDT", side="buy", opened_at=iso_ago(86400)),
                       "ETH-USDT": dict(instId="ETH-USDT", side="sell", opened_at=iso_ago(10)),
                       "SOL-USDT": dict(instId="SOL-USDT", side="buy", opened_at=iso_ago(10))})
        pos = [dict(instId="BTC-USDT", positions="1", createTime=str(ct)), dict(instId="ETH-USDT", positions="-3", createTime=str(ct))]
        reg, removed = registry.prune(pos)
        self.assertEqual(removed, ["BTC-USDT", "SOL-USDT"]); self.assertEqual(list(registry.load()), ["ETH-USDT"])

class TestRespOk(NoNetwork):
    def test_nested_codes(self):
        self.assertTrue(bf.resp_ok({"code": "0", "data": {"code": "0"}}))
        self.assertFalse(bf.resp_ok({"code": "0", "data": {"code": "148083"}}))
        self.assertFalse(bf.resp_ok({"code": "0", "data": [{"code": "0"}, {"code": "500"}]}))
        self.assertFalse(bf.resp_ok({"code": "152422"}))

# ---- manage.py with a fake exchange ----------------------------------------------------------
MINS = dict(tickSize="0.1", contractValue="0.001", lotSize="0.1", minSize="0.1")

class FakeAccount:
    """Mocked private(): one position, pending TP/SL list, history, scripted POST results."""
    def __init__(self, pos, tpsl=(), history=(), wallet="1000", post_results=None):
        self.pos = pos; self.tpsl = list(tpsl); self.history = list(history); self.wallet = wallet
        self.post_results = post_results or {}; self.calls = []
    def posts(self): return [p for m, p, b in self.calls if m == "POST"]
    def bodies(self, path): return [b for m, p, b in self.calls if m == "POST" and p == path]
    def __call__(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if method == "POST":
            res = self.post_results.get(path)
            if isinstance(res, list): res = res.pop(0) if res else None
            if res is not None: return res
            return {"code": "0", "data": [{"code": "0"}]} if path.endswith("cancel-tpsl") else {"code": "0", "data": {"code": "0"}}
        if path == "/api/v1/account/positions": return {"code": "0", "data": [self.pos] if self.pos else []}
        if path == "/api/v1/trade/orders-tpsl-pending": return {"code": "0", "data": self.tpsl}
        if path == "/api/v1/trade/orders-algo-pending": return {"code": "0", "data": []}
        if path == "/api/v1/trade/orders-history": return {"code": "0", "data": self.history}
        if path == "/api/v1/account/balance": return {"code": "0", "data": {"details": [dict(currency="USDT", equity="999", balance=self.wallet, available="900")]}}
        raise AssertionError(path)

ERR = {"code": "0", "data": {"code": "102015", "msg": "rejected"}}
ERR_LIST = {"code": "0", "data": [{"code": "102015", "msg": "rejected"}]}

def long_pos(mark="101000", avg="100000", contracts="10", ct=None):
    return dict(instId="BTC-USDT", positions=contracts, averagePrice=avg, markPrice=mark, marginMode="isolated",
                positionSide="net", createTime=str(ct or now_ms()), unrealizedPnl="0")

def bot_rec(**kw):
    r = dict(instId="BTC-USDT", side="buy", opened_at=iso_ago(20), initial_stop=97000.0, target=110000.0,
             stop_dist=1000.0, equity_at_entry=1000.0, be_done=False)
    r.update(kw); return r

class TestManage(NoNetwork):
    def setUp(self):
        super().setUp()
        self.patches = [mock.patch.object(manage, "_instrument", return_value=MINS), mock.patch.object(manage, "candles", fake_candles_up)]
        for p in self.patches: p.start()
        registry.save({})
    def tearDown(self):
        for p in self.patches: p.stop()
        super().tearDown()
    def run_with(self, fa, fn, *a, **k):
        with mock.patch.object(bf, "private", fa): return fn(*a, **k)

    def test_breakeven_cancels_old_before_placing_new(self):
        registry.save({"BTC-USDT": bot_rec()})
        fa = FakeAccount(long_pos(), tpsl=[dict(tpslId="7", slTriggerPrice="99000", tpTriggerPrice="106000")])
        r = self.run_with(fa, manage.move_stop_to_breakeven, "BTC-USDT")
        self.assertTrue(r["ok"], r)
        self.assertEqual(fa.posts()[:2], ["/api/v1/trade/cancel-tpsl", "/api/v1/trade/order-tpsl"])
        self.assertEqual(r["place"]["req"]["tpTriggerPrice"], "106000")        # target kept
        self.assertEqual(r["place"]["req"]["slTriggerPrice"], "100120")       # entry + 2 x 0.06% taker fee (round trip)
        self.assertTrue(registry.load()["BTC-USDT"]["be_done"])

    def test_breakeven_ignores_stale_record_values(self):
        registry.save({"BTC-USDT": bot_rec(opened_at=iso_ago(86400), stop_dist=10.0)})   # stale: must not be used
        fa = FakeAccount(long_pos(), tpsl=[dict(tpslId="7", slTriggerPrice="99000")])
        r = self.run_with(fa, manage.move_stop_to_breakeven, "BTC-USDT")
        self.assertFalse(r["ok"]); self.assertIn("manual", r["why"]); self.assertEqual(fa.posts(), [])

    def test_manual_position_not_closed(self):
        pos = dict(instId="ETH-USDT", positions="-2", averagePrice="3000", marginMode="cross", positionSide="net", createTime=str(now_ms()))
        fa = FakeAccount(pos, history=[dict(clientOrderId="manual1", side="sell", state="filled", reduceOnly="false", createTime=pos["createTime"])])
        r = self.run_with(fa, manage.close_position, "ETH-USDT")
        self.assertFalse(r["ok"]); self.assertIn("manual", r["why"]); self.assertEqual(fa.posts(), [])

    # -- replace_stop safety --
    def test_cancel_fails_nothing_placed(self):
        fa = FakeAccount(long_pos(), tpsl=[dict(tpslId="7", slTriggerPrice="99000")], post_results={"/api/v1/trade/cancel-tpsl": ERR_LIST})
        r = self.run_with(fa, manage.replace_stop, fa.pos, 99500.0)
        self.assertFalse(r["ok"]); self.assertIn("nothing placed", r["why"]); self.assertEqual(fa.posts(), ["/api/v1/trade/cancel-tpsl"])

    def test_place_fails_restore_attempted(self):
        fa = FakeAccount(long_pos(), tpsl=[dict(tpslId="7", slTriggerPrice="99000", tpTriggerPrice="106000")],
                         post_results={"/api/v1/trade/order-tpsl": [ERR, None]})
        r = self.run_with(fa, manage.replace_stop, fa.pos, 99500.0)
        self.assertFalse(r["ok"]); self.assertTrue(r["restore"]["ok"]); self.assertFalse(r.get("unprotected"))
        b = fa.bodies("/api/v1/trade/order-tpsl")
        self.assertEqual(len(b), 2); self.assertEqual(b[1]["slTriggerPrice"], "99000"); self.assertEqual(b[1]["tpTriggerPrice"], "106000")

    def test_restore_fails_reports_unprotected(self):
        fa = FakeAccount(long_pos(), tpsl=[dict(tpslId="7", slTriggerPrice="99000")], post_results={"/api/v1/trade/order-tpsl": [ERR, ERR]})
        r = self.run_with(fa, manage.replace_stop, fa.pos, 99500.0)
        self.assertFalse(r["ok"]); self.assertTrue(r["unprotected"]); self.assertIn("UNPROTECTED", r["why"])

    def test_tighten_never_widens(self):
        registry.save({"BTC-USDT": bot_rec()})
        fa = FakeAccount(long_pos(), tpsl=[dict(tpslId="7", slTriggerPrice="99000")])
        r = self.run_with(fa, manage.tighten_stop, "BTC-USDT", 98000.0)
        self.assertFalse(r["ok"]); self.assertIn("widen", r["why"]); self.assertEqual(fa.posts(), [])
        r = self.run_with(fa, manage.tighten_stop, "BTC-USDT", 99500.0)
        self.assertTrue(r["ok"], r)

    # -- hard cut --
    def test_hard_cut_distance_includes_fees(self):
        for long_ in (True, False):
            d = manage.hard_cut_distance(100000, 0.01, long_, 1000, hc=0.055, fee=0.0006)
            stop = 100000 - d if long_ else 100000 + d
            self.assertAlmostEqual(manage.loss_at(100000, stop, 0.01, fee=0.0006), 55.0, places=6)
        self.assertLessEqual(manage.hard_cut_distance(100000, 1.0, True, 1000, hc=0.055, fee=0.0006), 0)   # fees alone exceed the cut

    def test_hard_cut_uses_equity_at_entry_not_wallet(self):
        registry.save({"BTC-USDT": bot_rec(equity_at_entry=1000.0)})
        fa = FakeAccount(long_pos(mark="99000"), tpsl=[dict(tpslId="7", slTriggerPrice="90000")], wallet="500")
        r = self.run_with(fa, manage.hard_cut)
        self.assertTrue(r["ok"], r); self.assertEqual(len(r["tightened"]), 1)
        new = r["tightened"][0]["new"]; L = manage.HARD_CUT * 1000
        self.assertLessEqual(manage.loss_at(100000, new, 0.01), L + 1e-9); self.assertGreater(manage.loss_at(100000, new, 0.01), L * 0.999)

    def test_hard_cut_falls_back_to_wallet_balance(self):
        pos = long_pos(mark="99000")
        fa = FakeAccount(pos, tpsl=[dict(tpslId="7", slTriggerPrice="90000")], wallet="500",
                         history=[dict(clientOrderId="bfx123", side="buy", state="filled", reduceOnly="false", filledSize="10", createTime=pos["createTime"])])
        r = self.run_with(fa, manage.hard_cut)
        new = r["tightened"][0]["new"]
        self.assertAlmostEqual(manage.loss_at(100000, new, 0.01), manage.HARD_CUT * 500, delta=0.01)

    def test_hard_cut_breached_closes(self):
        registry.save({"BTC-USDT": bot_rec()})
        fa = FakeAccount(long_pos(mark="94000"), tpsl=[dict(tpslId="7", slTriggerPrice="90000")])
        r = self.run_with(fa, manage.hard_cut)
        self.assertEqual(len(r["closed"]), 1); self.assertIn("/api/v1/trade/close-position", fa.posts())
        self.assertEqual(registry.load()["BTC-USDT"]["close_by"], "cut")   # kept for the closed-trade log, pruned next run

    def test_hard_cut_never_widens(self):
        registry.save({"BTC-USDT": bot_rec()})
        fa = FakeAccount(long_pos(mark="99000"), tpsl=[dict(tpslId="7", slTriggerPrice="98000")])
        r = self.run_with(fa, manage.hard_cut)
        self.assertTrue(r["ok"]); self.assertEqual(r["tightened"], []); self.assertEqual(fa.posts(), [])

    # -- ownership --
    def test_stale_record_vs_new_manual_position(self):
        registry.save({"BTC-USDT": bot_rec(opened_at=iso_ago(3 * 86400))})
        fa = FakeAccount(long_pos(), history=[dict(clientOrderId="bfx1", side="buy", state="filled", reduceOnly="false",
                                                   createTime=str(now_ms() - 3 * 86400 * 1000))])
        self.assertEqual(self.run_with(fa, manage.owner, fa.pos), (False, None))
        r = self.run_with(fa, manage.ensure_stops)
        self.assertEqual(r["alerts"], ["BTC-USDT: manual position has NO STOP"]); self.assertEqual(fa.posts(), [])

    def test_history_reduce_only_bot_order_ignored(self):
        pos = long_pos()
        ro = dict(clientOrderId="bfxc123", side="buy", state="filled", reduceOnly="true", filledSize="10", createTime=pos["createTime"])
        self.assertFalse(self.run_with(FakeAccount(pos, history=[ro]), manage.is_bot_position, pos))
        self.assertFalse(self.run_with(FakeAccount(pos, history=[dict(ro, reduceOnly="false", state="canceled", filledSize="0")]), manage.is_bot_position, pos))
        self.assertFalse(self.run_with(FakeAccount(pos, history=[dict(ro, reduceOnly="false", side="sell")]), manage.is_bot_position, pos))
        self.assertTrue(self.run_with(FakeAccount(pos, history=[dict(ro, reduceOnly="false")]), manage.is_bot_position, pos))

    # -- ensure-stops --
    def test_ensure_stops_places_initial_stop_and_target(self):
        registry.save({"BTC-USDT": bot_rec()})
        fa = FakeAccount(long_pos(mark="99000"))
        r = self.run_with(fa, manage.ensure_stops)
        self.assertTrue(r["ok"], r); b = fa.bodies("/api/v1/trade/order-tpsl")[0]
        self.assertEqual(b["slTriggerPrice"], "97000"); self.assertEqual(b["tpTriggerPrice"], "110000")

    def test_ensure_stops_closes_if_mark_past_stop(self):
        registry.save({"BTC-USDT": bot_rec()})
        fa = FakeAccount(long_pos(mark="96000"))
        r = self.run_with(fa, manage.ensure_stops)
        self.assertEqual(len(r["closed"]), 1); self.assertIn("CLOSED AT MARKET", r["closed"][0]["why"])
        self.assertEqual(fa.posts()[0], "/api/v1/trade/close-position"); self.assertNotIn("/api/v1/trade/order-tpsl", fa.posts())

    # -- exit codes --
    def test_exit_codes(self):
        self.assertEqual(manage.exit_code(dict(ok=True)), 0)
        self.assertEqual(manage.exit_code(dict(ok=False)), 1)
        self.assertFalse(manage._ok(dict(errors=["x"]))["ok"]); self.assertTrue(manage._ok(dict(errors=[]))["ok"])
        import subprocess
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "manage.py"), "status"], capture_output=True, env=dict(os.environ))
        self.assertNotEqual(r.returncode, 0)   # no keys in the test env

if __name__ == "__main__":
    unittest.main()
