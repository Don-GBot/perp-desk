"""Offline tests for sync 10: BTC 4h veto (entry.btc_4h_veto) and positionId-first ownership (registry.matches).
Network mocked; temp DATA_DIR from test_offline. No orders."""
import os, unittest, datetime as dt
from collections import Counter
from unittest import mock
import test_offline as T
from test_offline import NoNetwork, EX, bf, registry, config
import test_sync7 as S7

NOW = dt.datetime(2026, 10, 22, 16, 0, 30, tzinfo=dt.timezone.utc)
LAST = int(dt.datetime(2026, 10, 22, 16, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)

def rows(last_close, ref_close=100000.0, last=LAST, n=60, drop_ref=False):
    out = []
    for i in range(n):
        ts = last - i * 300_000
        if drop_ref and ts == last - 4 * 3600_000: continue
        c = last_close if i == 0 else ref_close if ts == last - 4 * 3600_000 else 100500.0
        out.append([str(ts), "0", "0", "0", str(c), "0", "0", "0", "0" if i == 0 else "1"])
    return out   # newest first, like the exchange

def pub(data, calls=None):
    def f(path, params=None):
        if calls is not None: calls.append((path, dict(params or {})))
        if path == "/api/v1/market/candles": return {"code": "0", "data": data}
        raise AssertionError(path)
    return f

class TestBtc4hFetchCheck(NoNetwork):
    def setUp(self):
        super().setUp(); self.on = mock.patch.object(EX, "BTC4H", True); self.on.start()
    def tearDown(self): self.on.stop(); super().tearDown()
    def chk(self, side, last_close, **kw):
        with mock.patch.object(bf, "public", pub(rows(last_close, **kw))), mock.patch.object(EX, "now_utc", return_value=NOW):
            return EX.btc4h_check(side, dict())
    def test_config_live_values(self):
        b = config.CFG["entry"]["btc_4h_veto"]
        self.assertEqual((b["enabled"], b["max_move"], b["stale_min"]), (True, 0.01, 15))
        self.assertEqual((EX.BTC4H_MAX, EX.BTC4H_STALE_MIN, EX.BTC4H_INST, EX.BTC4H_BAR, EX.BTC4H_LIMIT), (0.01, 15.0, "BTC-USDT", "5m", 60))
    def test_fetch_ret_and_params(self):
        calls = []
        with mock.patch.object(bf, "public", pub(rows(101000.0), calls)):
            b = EX.btc4h_fetch(now=NOW)
        self.assertEqual(b["state"], "ok"); self.assertAlmostEqual(b["ret"], 0.01); self.assertEqual(b["ref_close"], 100000.0)
        self.assertEqual(b["last_ts_utc"], "2026-10-22T16:00:00Z"); self.assertEqual(b["age_min"], 0.5)
        self.assertEqual(calls, [("/api/v1/market/candles", dict(instId="BTC-USDT", bar="5m", limit=60))])
    def test_long_blocked_below_minus_1pct(self):
        w, d, b = self.chk("buy", 98990.0)
        self.assertTrue(w.startswith("btc_4h_veto: BTC 4h -1.01%")); self.assertIn("long blocked", w)
    def test_long_allowed_at_minus_099(self):
        self.assertIsNone(self.chk("buy", 99010.0)[0])
    def test_exact_minus_1pct_passes_long(self):
        self.assertIsNone(self.chk("buy", 99000.0)[0])
    def test_short_blocked_above_plus_1pct(self):
        w = self.chk("sell", 101010.0)[0]; self.assertIn("short blocked", w)
    def test_short_allowed_at_plus_099_and_exact(self):
        self.assertIsNone(self.chk("sell", 100990.0)[0]); self.assertIsNone(self.chk("sell", 101000.0)[0])
    def test_drop_does_not_block_short_and_rise_not_long(self):
        self.assertIsNone(self.chk("sell", 95000.0)[0]); self.assertIsNone(self.chk("buy", 105000.0)[0])
    def test_stale_blocks_both(self):
        old = LAST - 16 * 60_000
        for side in ("buy", "sell"):
            with mock.patch.object(bf, "public", pub(rows(100000.0, last=old))), mock.patch.object(EX, "now_utc", return_value=NOW):
                w, d, b = EX.btc4h_check(side, dict())
            self.assertTrue(w.startswith("btc_4h_veto (stale BTC data, fail closed)"), w); self.assertEqual(b["state"], "stale")
    def test_fifteen_min_old_is_not_stale(self):
        with mock.patch.object(bf, "public", pub(rows(100000.0, last=LAST - 14 * 60_000 - 30_000))), mock.patch.object(EX, "now_utc", return_value=NOW):
            self.assertIsNone(EX.btc4h_check("buy", dict())[0])
    def test_missing_variants_block_both(self):
        for data in ([], None, rows(100000.0, drop_ref=True)):
            for side in ("buy", "sell"):
                with mock.patch.object(bf, "public", pub(data)), mock.patch.object(EX, "now_utc", return_value=NOW):
                    w = EX.btc4h_check(side, dict())[0]
                self.assertTrue(w.startswith("btc_4h_veto (missing BTC data, fail closed)"), w)
    def test_fetch_error_blocks(self):
        with mock.patch.object(bf, "public", side_effect=RuntimeError("boom")):
            w, d, b = EX.btc4h_check("buy", dict())
        self.assertIn("missing BTC data, fail closed", w); self.assertIn("boom", b["error"])
    def test_ctx_caches_one_fetch(self):
        calls = []; ctx = {}
        with mock.patch.object(bf, "public", pub(rows(98000.0), calls)), mock.patch.object(EX, "now_utc", return_value=NOW):
            for _ in range(3): EX.btc4h_check("buy", ctx)
            ctx["_fresh"] = True; EX.btc4h_check("sell", ctx)
        self.assertEqual(len(calls), 1)
    def test_off_is_noop(self):
        with mock.patch.object(EX, "BTC4H", False), mock.patch.object(bf, "public", side_effect=AssertionError("fetched")):
            self.assertEqual(EX.btc4h_check("buy", {}), (None, None, None))
    def test_gate_of(self):
        self.assertEqual(EX.gate_of("btc_4h_veto: BTC 4h -1.20% ..."), "btc_4h_veto")
    def test_bad_config_rejected(self):
        for body in ("    max_move: 0\n", "    max_move: 1.5\n", "    stale_min: 0\n"):
            f = os.path.join(T.TMP, "bv.yaml")
            with open(f, "w") as fh: fh.write("entry:\n  btc_4h_veto:\n" + body)
            with self.assertRaises(config.ConfigError): config.load(f)

class TestBtc4hEndToEnd(S7.TestRankingEndToEnd):
    """The sync-7 harness (real evaluate, composite run) with BTC 5m candles added."""
    def setUp(self):
        super().setUp()
        self.btc = rows(98000.0)   # BTC -2.00% over 4h
        self.btc_calls = 0
        inner = bf.public
        def public(path, params=None):
            if path == "/api/v1/market/candles" and (params or {}).get("bar") == "5m":
                self.btc_calls += 1; return {"code": "0", "data": self.btc}
            return inner(path, params)
        self.ps2 = [mock.patch.object(EX, "BTC4H", True), mock.patch.object(bf, "public", public),
                    mock.patch.object(EX, "now_utc", return_value=NOW)]
        for p in self.ps2: p.start()
    def tearDown(self):
        for p in reversed(self.ps2): p.stop()
        super().tearDown()
    def test_longs_blocked_falls_through_to_short(self):
        setups = self.setups + [dict(instId="BTC-USDT", side="sell", stop=106500, target=95000)]
        with mock.patch.object(EX, "trend", return_value=("down", 0, 0, 0)), mock.patch.object(EX, "trend1h_check", return_value=(None, {})):
            r = EX.run(setups, NOW.isoformat(), now=NOW, dry=True)
        why = {s["instId"]: s["why"] for s in r["skipped"]}
        for i in ("TIGHT-USDT", "MID-USDT", "WIDE-USDT"):
            self.assertTrue(why[i].startswith("btc_4h_veto: BTC 4h -2.00%"), why)
        self.assertTrue(r["ok"], r); self.assertEqual(r["plan"]["instId"], "BTC-USDT"); self.assertEqual(r["plan"]["btc4h"]["state"], "ok")
        self.assertEqual(self.btc_calls, 1)
    def test_all_blocked_live_no_order_one_fetch(self):
        r = EX.run(self.setups, NOW.isoformat(), now=NOW)
        self.assertFalse(r["ok"]); self.assertEqual(self.places, []); self.assertEqual(self.btc_calls, 1)
        self.assertEqual(len(r["skipped"]), 3)
    def test_stale_blocks_live_run(self):
        self.btc = rows(100000.0, last=LAST - 20 * 60_000)
        r = EX.run(self.setups, NOW.isoformat(), now=NOW)
        self.assertFalse(r["ok"]); self.assertEqual(self.places, [])
        self.assertTrue(all("stale BTC data, fail closed" in s["why"] for s in r["skipped"]), r["skipped"])
    def test_flat_btc_places_and_recheck_reuses_fetch(self):
        self.btc = rows(100200.0)   # +0.20%
        r = EX.run(self.setups, NOW.isoformat(), now=NOW)
        self.assertTrue(r.get("ok"), r); self.assertEqual(self.places, ["WIDE-USDT"]); self.assertEqual(self.btc_calls, 1)
    def test_block_line_logged(self):
        import blocklog as BL
        seen = []
        with mock.patch.object(BL, "append", side_effect=lambda line, *a, **k: seen.append(line)):
            EX.run(self.setups, NOW.isoformat(), now=NOW, dry=True)
        self.assertEqual(sorted(l["gate_reason"] for l in seen), ["btc_4h_veto"] * 3)

for _n in [n for n in dir(S7.TestRankingEndToEnd) if n.startswith("test")]:
    setattr(TestBtc4hEndToEnd, _n, None)   # reuse only the sync-7 harness, not its tests (they run in test_sync7)

class TestPositionIdOwnership(NoNetwork):
    REC = dict(instId="ENA-USDT", side="sell", opened_at=dt.datetime.fromtimestamp(1761040800, dt.timezone.utc).isoformat(), position_createTime="1761040800000", positionId="777")
    def pos(self, **kw):
        return dict(dict(instId="ENA-USDT", positions="-10", createTime="1761040800000", positionId="777"), **kw)
    def test_same_id_moved_createtime_matches(self):
        self.assertTrue(registry.matches(self.pos(createTime="1761040800008"), self.REC))
        self.assertTrue(registry.matches(self.pos(createTime="1761050000000"), self.REC))   # id decides, not time
    def test_different_id_never_matches(self):
        self.assertFalse(registry.matches(self.pos(positionId="778"), self.REC))
    def test_side_still_required(self):
        self.assertFalse(registry.matches(self.pos(positions="10"), self.REC))
    def test_fallback_createtime_1s(self):
        rec = dict(self.REC, positionId=None)
        self.assertTrue(registry.matches(self.pos(createTime="1761040801000"), rec))
        self.assertFalse(registry.matches(self.pos(createTime="1761040801001"), rec))
        self.assertTrue(registry.matches(self.pos(positionId="", createTime="1761040799000"), self.REC))
    def test_fallback_opened_at_120s(self):
        rec = dict(self.REC, positionId=None, position_createTime=None)
        self.assertTrue(registry.matches(self.pos(createTime=str(1761040800000 + 119_000)), rec))
        self.assertFalse(registry.matches(self.pos(createTime=str(1761040800000 + 121_000)), rec))
    def test_match_and_manage_owner(self):
        import manage
        reg = {"ENA-USDT": self.REC}
        self.assertIs(registry.match(self.pos(createTime="1761040800008"), reg), self.REC)
        self.assertEqual(registry.match(self.pos(positionId="9"), reg), None)
        with mock.patch.object(manage, "history_says_bot", return_value=False):
            self.assertTrue(manage.owner(self.pos(createTime="1761040800008"), reg)[0])

if __name__ == "__main__": unittest.main()
