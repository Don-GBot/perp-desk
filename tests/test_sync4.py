"""Offline tests for sync 4: min risk floor, single fundamentals freshness (4.5h), fee-inclusive R / R_price,
fee-inclusive break-even scratch, shadow cooldown after BE, swapguard. All mocked, no network.
"""
import os, json, tempfile, fcntl, unittest, datetime as dt
from unittest import mock
import test_offline as T
from test_offline import NoNetwork, EX, config
from test_gates_and_logs import GateBase, SETUP
import closes, news_gate as NG, universe as UN, swapguard as SG, blocklog as BL
import shadow_variants as SV

NOW = dt.datetime.now(dt.timezone.utc)
def iso(t): return t.strftime('%Y-%m-%dT%H:%M:%SZ')

class TestMinRisk(GateBase):
    def test_floor(self):
        self.assertEqual(EX.MIN_RISK_PCT, 1.0)
        sz = EX.fit_size(1000, 1000, 100.0, 97.0, 1.0, '0.01', 0.01, risk_cap=8.0)   # headroom 0.8%
        self.assertEqual(sz['gate'], 'below_min_risk', sz); self.assertIn('< 1%', sz['why'])
        self.assertIsNone(EX.fit_size(1000, 1000, 100.0, 97.0, 1.0, '0.01', 0.01, risk_cap=12.0)['gate'])
        with mock.patch.object(EX, 'MIN_RISK_PCT', 0.0):
            self.assertIsNone(EX.fit_size(1000, 1000, 100.0, 97.0, 1.0, '0.01', 0.01, risk_cap=8.0)['gate'])
    def test_block_line_in_execute(self):
        with mock.patch.object(EX, 'risk_assess', return_value=dict(mode='risk', headroom=5.0, total_pct=0, dir_pct=0, n_pos=0, error=None)):
            plan, why = EX.evaluate(dict(SETUP, _rank=1), self.ctx)
        self.assertIsNone(plan); self.assertTrue(why.startswith('below_min_risk'), why)
        self.assertEqual(BL.load_live()[-1]['gate_reason'], 'below_min_risk')
    def test_config_bound(self):
        f = os.path.join(T.TMP, 'mr.yaml')
        with open(f, 'w') as fh: fh.write('risk:\n  min_risk_pct: 6\n')
        with self.assertRaises(config.ConfigError): config.load(f)

class TestFreshness(NoNetwork):
    def setUp(self):
        super().setUp(); self.path = os.path.join(T.TMP, 'fund_fresh.json')
        with open(self.path, 'w') as fh:
            json.dump(dict(pairs={'BTC-USDT': dict(rating='bearish', confidence='high', ts_utc=iso(NOW - dt.timedelta(hours=4.25)))}), fh)
    def test_single_setting(self):
        self.assertEqual(NG.G['fresh_hours'], 4.5); self.assertEqual(config.CFG['risk']['cooldown_hours'], 4)
        self.assertTrue(NG.check('BTC-USDT', 'buy', self.path, NOW)['block'])           # 4.25h old: still fresh
        self.assertTrue(UN.fund_fresh('BTC-USDT', NOW, self.path)[0])
        later = NOW + dt.timedelta(hours=0.5)                                             # 4.75h old: stale everywhere
        self.assertFalse(NG.check('BTC-USDT', 'buy', self.path, later)['block'])
        self.assertFalse(UN.fund_fresh('BTC-USDT', later, self.path)[0])

class TestRMultiples(NoNetwork):
    def test_allin_and_price(self):
        c = dict(entry=100.0, initial_stop=98.0, size=10, ctVal=1.0, realized_pnl=-2.0)
        ra = 10 * EX.planned_risk_per_coin(100.0, 98.0)
        R, Rp = closes.r_multiples(c)
        self.assertAlmostEqual(R, round(-2.0 / ra, 4)); self.assertEqual(Rp, -0.1)
        self.assertLess(abs(R), abs(Rp))                                                  # fee-inclusive risk is larger
    def test_ctval_from_risk_usdt_and_unknown(self):
        c = dict(entry=100.0, initial_stop=98.0, size=10, risk_usdt=40.0, realized_pnl=-4.0)   # no ctVal: coins = 40 / 2 = 20
        self.assertEqual(closes.coins_of(c), 20.0); self.assertEqual(closes.r_multiples(c)[1], -0.1)
        self.assertEqual(closes.r_multiples(dict(realized_pnl=-1.0)), (None, None))
    def test_report_recomputes_old_lines(self):
        p = os.path.join(T.TMP, 'ct_report.jsonl')
        with open(p, 'w') as fh:
            fh.write(json.dumps(dict(entry=100.0, initial_stop=98.0, size=10, ctVal=1.0, realized_pnl=5.0, R=99)) + '\n')   # stale R ignored
            fh.write(json.dumps(dict(realized_pnl=-1.0)) + '\n')
        r = closes.report(p)
        self.assertEqual((r['n'], r['wins'], r['unknown_R']), (2, 1, 1)); self.assertAlmostEqual(r['netR_price'], 0.25)
        self.assertAlmostEqual(r['netR'], round(5.0 / (10 * EX.planned_risk_per_coin(100.0, 98.0)), 4))

class TestBEScratch(NoNetwork):
    def line(self, **k):
        return dict(dict(instId='A-USDT', side='buy', kind='stop', entry=100.0, initial_stop=98.0, size=10, ctVal=1.0,
                         be_done=True, be_stop=100.2, close_avg=99.5), **k)
    def test_fee_inclusive_half_r(self):
        ra = 10 * EX.planned_risk_per_coin(100.0, 98.0)                               # ~25.4 USDT
        self.assertTrue(closes.is_scratch(self.line(realized_pnl=-(0.5 * ra - 0.5))))   # under 0.5R fee-inclusive
        self.assertFalse(closes.is_scratch(self.line(realized_pnl=-(0.5 * ra + 0.5))))
        self.assertTrue(closes.cooldown_trigger(self.line(realized_pnl=-(0.5 * ra + 0.5)))[0])
    def test_price_fallback_logged(self):
        closes.SCRATCH_FALLBACKS.clear()
        c = dict(instId='A-USDT', side='buy', kind='stop', realized_pnl=-1.0, risk_usdt=20.0)   # no entry/stop: price fallback
        self.assertEqual(closes.scratch_risk(c), (20.0, 'price_fallback')); self.assertTrue(closes.is_scratch(c))
        self.assertEqual(closes.SCRATCH_FALLBACKS[-1]['basis'], 'price_fallback')

class TestShadowCooldownAfterBE(NoNetwork):
    def test_post_be_not_always_exempt(self):
        base = dict(kind='be', be_done=True, be_scratch=False)
        self.assertTrue(SV.cooldown_hit(dict(base, R=-0.7)))                           # BE fired, big loss: cooldown
        self.assertFalse(SV.cooldown_hit(dict(base, R=-0.2)))                          # scratch
        self.assertFalse(SV.cooldown_hit(dict(base, R=-0.7, be_scratch=True)))         # exit at the BE stop
        self.assertTrue(SV.cooldown_hit(dict(kind='stop', be_done=False, R=-1.0)))
        self.assertFalse(SV.cooldown_hit(dict(kind='tp', be_done=False, R=-1.0)))
    def test_old_rows_rescaled(self):
        r = dict(R=-1.0, entry=100.0, initial_stop=98.0)
        self.assertAlmostEqual(SV.recompute_R(r), round(-EX.risk_per_coin(100, 98) / EX.planned_risk_per_coin(100, 98), 4))
        self.assertEqual(SV.recompute_R(dict(R=-1.0, R_basis='allin')), -1.0); self.assertIsNone(SV.recompute_R(dict(R=-1.0)))

class TestSwapGuard(NoNetwork):
    def setUp(self):
        super().setUp(); self.d = tempfile.mkdtemp(); self.proc = os.path.join(self.d, 'proc'); os.makedirs(self.proc)
        self.t = [0.0]; self.clock = lambda: self.t[0]
        def sleep(s): self.t[0] += s
        self.sleep = sleep; self.ok_time = dt.datetime(2026, 1, 1, 12, 20)
    def fake_pid(self, pid, argv):
        os.makedirs(os.path.join(self.proc, str(pid)))
        with open(os.path.join(self.proc, str(pid), 'cmdline'), 'wb') as fh: fh.write(b'\0'.join(a.encode() for a in argv))
    def test_freeze_window(self):
        r = SG.guard(0, self.proc, [], dt.datetime(2026, 1, 1, 12, 7), self.sleep, self.clock)
        self.assertFalse(r['ok']); self.assertIn('freeze', r['busy'][0])
        self.assertTrue(SG.guard(0, self.proc, [], self.ok_time, self.sleep, self.clock)['ok'])
    def test_waits_then_skips_never_kills(self):
        self.fake_pid(4242, ['python3', 'scripts/execute.py', '--dry'])
        r = SG.guard(180, self.proc, [], self.ok_time, self.sleep, self.clock)
        self.assertFalse(r['ok']); self.assertGreaterEqual(r['waited_s'], 180); self.assertIn('pid 4242', r['busy'][0])
        self.assertTrue(os.path.exists(os.path.join(self.proc, '4242')))
    def test_held_lock_blocks(self):
        lk = os.path.join(self.d, 'execute.lock'); fd = os.open(lk, os.O_CREAT | os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with mock.patch.object(SG, 'lock_free', return_value=False):   # flock is per-process; simulate another holder
                self.assertFalse(SG.guard(0, self.proc, [lk], self.ok_time, self.sleep, self.clock)['ok'])
        finally: os.close(fd)
    def test_swap_atomic_same_dir(self):
        src = os.path.join(self.d, 'x.py.new'); dst = os.path.join(self.d, 'x.py')
        with open(src, 'w') as fh: fh.write('new')
        with open(dst, 'w') as fh: fh.write('old')
        r = SG.swap([(src, dst)], 0, self.proc, [], self.ok_time, self.sleep, self.clock, os.path.join(self.d, 'h.lock'))
        self.assertTrue(r['ok'], r); self.assertEqual(open(dst).read(), 'new'); self.assertFalse(os.path.exists(src))
        other = tempfile.mkdtemp(); src2 = os.path.join(other, 'y')
        with open(src2, 'w') as fh: fh.write('n')
        self.assertFalse(SG.swap([(src2, dst)], 0, self.proc, [], self.ok_time, self.sleep, self.clock, os.path.join(self.d, 'h.lock'))['ok'])
    def test_cli_exit_codes(self):
        self.assertEqual(SG.main(['bogus']), 2)
        with mock.patch.object(SG, 'guard', return_value=dict(ok=False, reason='busy')): self.assertEqual(SG.main(['check']), 3)

if __name__ == '__main__': unittest.main()
