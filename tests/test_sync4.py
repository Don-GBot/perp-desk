"""Offline tests for sync 4: min risk floor, single fundamentals freshness (4.5h), fee-inclusive R / R_price,
fee-inclusive break-even scratch, swapguard. All mocked, no network.
"""
import os, json, tempfile, fcntl, unittest, datetime as dt
from unittest import mock
import test_offline as T
from test_offline import NoNetwork, EX, config
from test_gates_and_logs import GateBase, SETUP
import closes, news_gate as NG, universe as UN, swapguard as SG, blocklog as BL

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
        closes.SCRATCH_FALLBACKS.clear(); closes._FALLBACK_SEEN.clear()
        c = dict(instId='A-USDT', side='buy', kind='stop', be_done=True, realized_pnl=-1.0, risk_usdt=20.0)   # no entry/stop: price fallback
        self.assertEqual(closes.scratch_risk(c), (20.0, 'price_fallback')); self.assertTrue(closes.is_scratch(c))
        self.assertEqual(closes.SCRATCH_FALLBACKS[-1]['basis'], 'price_fallback')


class TestScratchRequiresBE(NoNetwork):
    def test_no_be_no_scratch(self):
        c = dict(instId='A-USDT', side='buy', kind='stop', entry=100.0, initial_stop=98.0, size=10, ctVal=1.0, close_avg=99.9, realized_pnl=-1.0)
        self.assertFalse(closes.is_scratch(c)); self.assertTrue(closes.cooldown_trigger(c)[0])
        self.assertTrue(closes.is_scratch(dict(c, be_done=True)))
    def test_fallback_warned_once_per_line(self):
        closes.SCRATCH_FALLBACKS.clear(); closes._FALLBACK_SEEN.clear()
        c = dict(instId='B-USDT', side='buy', kind='stop', be_done=True, realized_pnl=-1.0, risk_usdt=20.0, positionId='p1', close_ms=5)
        with self.assertLogs('closes', 'WARNING') as lg:
            for _ in range(3): closes.scratch_risk(c)
            closes.scratch_risk(dict(c, positionId='p2'))
        self.assertEqual(len(closes.SCRATCH_FALLBACKS), 2); self.assertEqual(len(lg.output), 2)

class TestMinRiskUnrounded(GateBase):
    def test_compares_unrounded(self):
        # 37 contracts x 0.0999996 = 0.36999852% of equity: displays as 0.37, so a 0.37% floor must still block it
        with mock.patch.object(EX, 'loss_per_contract', return_value=0.0999996), mock.patch.object(EX, 'MIN_RISK_PCT', 0.37):
            sz = EX.fit_size(1000, 1000, 100.0, 97.0, 1.0, '1', 1.0)
        self.assertEqual((sz['size'], sz['risk_pct_actual']), (37.0, 0.37)); self.assertEqual(sz['gate'], 'below_min_risk')
    def test_unscaled_trade_gated(self):
        with mock.patch.object(EX, 'MIN_RISK_PCT', 5.5):    # an unscaled 5% trade is below a 5.5% floor
            sz = EX.fit_size(1000, 1000, 100.0, 97.0, 1.0, '0.01', 0.01)
        self.assertFalse(sz['scaled_down']); self.assertEqual(sz['gate'], 'below_min_risk')
    def test_margin_scaled_trade(self):
        sz = EX.fit_size(1000, 30, 100.0, 97.0, 1.0, '0.01', 0.01)   # tiny free margin forces a margin scale-down
        self.assertEqual(sz['scaled_by'], ['margin']); self.assertIsNone(sz['gate']); self.assertAlmostEqual(sz['risk_pct_actual'], 1.515)
        with mock.patch.object(EX, 'MIN_RISK_PCT', 1.6):
            self.assertEqual(EX.fit_size(1000, 30, 100.0, 97.0, 1.0, '0.01', 0.01)['gate'], 'below_min_risk')

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
    def hold(self, path):
        import subprocess, sys
        p = subprocess.Popen([sys.executable, '-c', 'import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR);'
                              'fcntl.flock(fd,fcntl.LOCK_EX);print("held",flush=True);time.sleep(30)', path], stdout=subprocess.PIPE)
        self.assertEqual(p.stdout.readline().strip(), b'held'); self.addCleanup(lambda: (p.kill(), p.wait(), p.stdout.close()))
        return p
    def test_real_flock_held_by_subprocess(self):
        lk = os.path.join(self.d, 'scanner.lock'); self.assertTrue(SG.lock_free(lk))
        self.hold(lk); self.assertFalse(SG.lock_free(lk))
        r = SG.guard(4, self.proc, [lk], self.ok_time, self.sleep, self.clock)
        self.assertFalse(r['ok']); self.assertIn('lock held: scanner.lock', r['busy'])
    def pair(self, n):
        src = os.path.join(self.d, f'{n}.new'); dst = os.path.join(self.d, n)
        for f, t in ((src, 'new'), (dst, 'old')):
            with open(f, 'w') as fh: fh.write(t)
        return src, dst
    def test_recheck_under_lock_scanner_and_freeze(self):
        src, dst = self.pair('a'); sl = os.path.join(self.d, 'scanner.lock'); hl = os.path.join(self.d, 'h.lock')
        real = SG.lock_free; calls = {'n': 0}
        def lf(p):   # free during guard, taken by the scanner by the time we hold execute.lock
            calls['n'] += 1; return calls['n'] <= 1 and real(p)
        with mock.patch.object(SG, 'lock_free', side_effect=lf):
            r = SG.swap([(src, dst)], 0, self.proc, [sl], self.ok_time, self.sleep, self.clock, hl)
        self.assertFalse(r['ok']); self.assertIn('lock taken', r['reason']); self.assertEqual(open(dst).read(), 'old')
        times = iter([self.ok_time, dt.datetime(2026, 1, 1, 12, 5)])   # clear at guard, freeze under the lock
        r = SG.swap([(src, dst)], 0, self.proc, [sl], lambda: next(times), self.sleep, self.clock, hl)
        self.assertFalse(r['ok']); self.assertIn('freeze', r['reason']); self.assertEqual(open(dst).read(), 'old')
    def test_partial_multi_pair(self):
        a, b = self.pair('a'), self.pair('b'); real = os.replace
        def rep(s, d):
            if d.endswith('/b'): raise OSError('disk full')
            real(s, d)
        with mock.patch.object(SG.os, 'replace', side_effect=rep):
            r = SG.swap([a, b], 0, self.proc, [], self.ok_time, self.sleep, self.clock, os.path.join(self.d, 'h.lock'))
        self.assertFalse(r['ok']); self.assertTrue(r['partial']); self.assertEqual(r['swapped'], [os.path.realpath(a[1])])
        self.assertEqual(r['failed'], os.path.realpath(b[1])); self.assertEqual(open(b[1]).read(), 'old')
        with mock.patch.object(SG, 'swap', return_value=r): self.assertEqual(SG.main(['swap', *a, *b]), 3)
    def test_validates_all_before_any_replace(self):
        a = self.pair('a'); missing = (os.path.join(self.d, 'zz.new'), os.path.join(self.d, 'zz'))
        r = SG.swap([a, missing], 0, self.proc, [], self.ok_time, self.sleep, self.clock, os.path.join(self.d, 'h.lock'))
        self.assertFalse(r['ok']); self.assertEqual(open(a[1]).read(), 'old')
    def test_argv_anywhere(self):
        self.fake_pid(77, ['/usr/bin/env', 'nice', '-n', '5', 'python3', '/opt/x/scripts/manage.py'])
        self.assertEqual([p for p, _ in SG.running(self.proc)], [77])
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
