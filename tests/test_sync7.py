"""Offline tests for sync 7: end-to-end composite ranking through the REAL evaluate() on fake market data.
Only place() and the network are mocked; temp DATA_DIR from test_offline. No orders."""
import unittest
from collections import Counter
from unittest import mock
import test_offline as T
from test_offline import NoNetwork, EX, bf, registry

HALF = {"WIDE-USDT": 300, "MID-USDT": 60, "TIGHT-USDT": 30}   # candle half-range -> 1H ATR ~2x (target_atr ~15 / ~75 / ~150)
INS = {i: dict(instId=i, tickSize="0.1", lotSize="0.1", contractValue="0.001", minSize="0.1", maxLeverage="100",
               state="live", offTime="", assetClass="Crypto") for i in list(HALF) + ["BTC-USDT"]}

class TestRankingEndToEnd(NoNetwork):
    def setUp(self):
        super().setUp()
        self.fetch = Counter(); self.places = []; self.posts = []
        def candles(inst, bar, limit=None):
            self.fetch[(inst, bar)] += 1; h = HALF.get(inst, 60)
            return [[100000 + i * 20, 100000 + i * 20 + h, 100000 + i * 20 - h, 100000 + i * 20 + 10] for i in range(200)]
        def public(path, params=None):
            if path == "/api/v1/market/tickers":
                return {"code": "0", "data": [dict(instId=params["instId"], bidPrice="103990.0", askPrice="104000.0")]}
            if path == "/api/v1/market/instruments": return {"code": "0", "data": list(INS.values())}
            raise AssertionError(path)
        def private(method, path, params=None, body=None):
            if method != "GET": self.posts.append(path); raise AssertionError(f"order call {path} in test")
            if path == "/api/v1/account/balance": return {"code": "0", "data": {"details": [dict(currency="USDT", equity="1000", available="1000")]}}
            if path == "/api/v1/account/positions": return {"code": "0", "data": []}
            if path == "/api/v1/account/position-mode": return {"code": "0", "data": {"positionMode": "net_mode"}}
            raise AssertionError(path)
        def place(plan):
            self.places.append(plan["instId"]); return dict(ok=True, instId=plan["instId"], order_resp={"code": "0"})
        self.real_evaluate = EX.evaluate
        self.ps = [mock.patch.object(bf, "public", public), mock.patch.object(bf, "private", private),
                   mock.patch.object(EX, "candles", candles), mock.patch.object(EX, "place", place)]
        for p in self.ps: p.start()
        registry.save_pending([]); registry.save({})
        self.setups = [dict(instId=i, side="buy", stop=101500, target=113000) for i in ("TIGHT-USDT", "MID-USDT", "WIDE-USDT")]

    def tearDown(self):
        for p in self.ps: p.stop()
        super().tearDown()

    def test_real_evaluate_ranks_and_places_top_once(self):
        self.assertIs(EX.evaluate, self.real_evaluate)   # evaluate is not mocked
        self.assertTrue(EX.COMPOSITE)
        r = EX.run(self.setups, T.iso_ago(0))
        self.assertTrue(r.get("ok"), r)
        cands = r["candidates"]
        self.assertEqual(len(cands), 3, r.get("skipped"))                       # every setup qualified and was scored
        self.assertTrue(all(c["score"] is not None for c in cands))
        self.assertEqual([c["instId"] for c in cands], ["WIDE-USDT", "MID-USDT", "TIGHT-USDT"])   # smallest target in ATR first
        self.assertGreater(cands[0]["score"], cands[1]["score"])
        self.assertEqual(self.places, ["WIDE-USDT"])                            # top composite placed, one entry per run
        self.assertEqual(r["score"]["score"], cands[0]["score"]); self.assertEqual(self.posts, [])
        four = {k[0]: n for k, n in self.fetch.items() if k[1] == "4H"}
        self.assertEqual(four, {"TIGHT-USDT": 1, "MID-USDT": 1, "WIDE-USDT": 2, "BTC-USDT": 1}, self.fetch)   # first pass once each, fresh re-check for the winner
        one = {k[0]: n for k, n in self.fetch.items() if k[1] == "1H"}
        self.assertEqual(one, {"TIGHT-USDT": 1, "MID-USDT": 1, "WIDE-USDT": 2}, self.fetch)   # one 1H fetch per evaluate (ATR + 1H trend)

    def test_btc_setup_shares_btc_trend_fetch(self):
        r = EX.run([dict(instId="BTC-USDT", side="buy", stop=101500, target=113000)], T.iso_ago(0))
        self.assertTrue(r.get("ok"), r); self.assertEqual(self.places, ["BTC-USDT"])
        self.assertEqual(self.fetch[("BTC-USDT", "4H")], 2, self.fetch)   # BTC trend shares the first pass; re-check is fresh
        self.assertEqual(self.fetch[("BTC-USDT", "1H")], 2, self.fetch)

    def test_bad_atr_rejected_never_placed(self):
        for bad in (0.0, float("nan"), float("inf")):
            self.places.clear()
            with mock.patch.object(EX, "atr", return_value=bad):
                r = EX.run(self.setups, T.iso_ago(0))
            self.assertFalse(r.get("ok"), r); self.assertEqual(self.places, [])
            self.assertTrue(all("atr unreadable" in k["why"] for k in r["skipped"]), r["skipped"])
        with mock.patch.object(EX, "atr", side_effect=ValueError("not enough candles for ATR14: 3")):
            plan, why = EX.evaluate(dict(self.setups[0]), dict(equity=1000.0, avail=1000.0, pos=[], ins=INS, warnings=[]))
        self.assertIsNone(plan); self.assertIn("atr unreadable", why)

if __name__ == "__main__": unittest.main()
