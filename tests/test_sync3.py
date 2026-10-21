"""Offline tests for sync 3: universe tiers, fit_size (risk as a maximum, fee_heavy, liq), risk caps (riskcap.py),
scheduled-dry block logging, unresolved-record block, registry no-overwrite, adoption from order history, log-all-skips
and the V_RR2 shadow book. All mocked, no network.
"""
import os, json, unittest, datetime as dt
from unittest import mock
import test_offline as T
from test_offline import NoNetwork, INS, FakeExchange, fake_candles_up, EX, bf, registry, config
import test_gates_and_logs as G
from test_gates_and_logs import GateBase, SETUP, rd
import universe as UN, riskcap as RC, blocklog as BL, runlog, scan as SCN, manage
import shadow_variants as SV

NOW = dt.datetime.now(dt.timezone.utc)
def iso(t): return t.strftime('%Y-%m-%dT%H:%M:%SZ')

def write_fund(pairs):
    with open(config.data_path('fundamentals.json'), 'w') as fh: json.dump(dict(pairs=pairs), fh)

class TestUniverse(NoNetwork):
    def tearDown(self):
        p = config.data_path('fundamentals.json')
        if os.path.exists(p): os.remove(p)
        super().tearDown()
    def test_tiers(self):
        self.assertEqual(UN.top_n(), 40)
        self.assertEqual(UN.tier(25), 'core'); self.assertEqual(UN.tier(26), 'ext'); self.assertIsNone(UN.tier(None))
        self.assertEqual(UN.tier_of(dict(instId='A-USDT')), 'core')                     # older scans: core
        self.assertEqual(UN.tier_of(dict(instId='A-USDT'), dict(vol_ranks={'A-USDT': 31})), 'ext')
    def test_ext_needs_fresh_rating(self):
        s = dict(instId='ABC-USDT', vol_rank=30)
        self.assertTrue(UN.block_reason(s).startswith('no_fundamentals (fundamentals unreadable'))
        write_fund({'ABC-USDT': dict(rating='neutral', confidence='low', ts_utc=iso(NOW - dt.timedelta(hours=1)))})
        self.assertIsNone(UN.block_reason(s))
        write_fund({'ABC-USDT': dict(rating='neutral', confidence='low', ts_utc=iso(NOW - dt.timedelta(hours=9)))})
        self.assertIn('old >', UN.block_reason(s))
        write_fund({})
        self.assertEqual(UN.block_reason(s), 'no_fundamentals (no rating)')
        self.assertIsNone(UN.block_reason(dict(instId='ABC-USDT', vol_rank=3)))         # core: never blocked
    def test_scan_ranks_before_spread_filter(self):
        tk = [dict(instId=f'P{i}-USDT', last='1', bidPrice='0.999' if i != 2 else '0.9', askPrice='1', volCurrency24h=str(1000 - i)) for i in range(45)]
        ins = {'code': '0', 'data': [dict(instId=x['instId'], state='live', offTime='') for x in tk]}
        def pub(path, params=None): return ins if path.endswith('instruments') else {'code': '0', 'data': tk}
        with mock.patch.object(bf, 'public', pub):
            rows, _, ranks = SCN.universe_ranked()
        self.assertEqual(len(ranks), 40); self.assertEqual(ranks['P2-USDT'], 3)          # ranked, then dropped for spread
        self.assertNotIn('P2-USDT', [r[1] for r in rows]); self.assertEqual(ranks['P39-USDT'], 40)

class TestFitSize(NoNetwork):
    def test_full_size_fits(self):
        sz = EX.fit_size(1000, 1000, 100.0, 97.0, 1.0, '0.01', 0.01)
        self.assertIsNone(sz['gate']); self.assertFalse(sz['scaled_down']); self.assertEqual(sz['size'], EX.size_for(1000, 100.0, 97.0, 1.0, '0.01'))
        self.assertAlmostEqual(sz['risk_pct_actual'], 5.0, delta=0.01)
    def test_scaled_to_margin_cap(self):
        # 0.5% stop: the full 5% size (~51 coins) needs ~5100 notional; 20x x 250 margin = 5000 max
        sz = EX.fit_size(1000, 1000, 100.0, 99.5, 1.0, '0.01', 0.01)
        self.assertIsNone(sz['gate'], sz); self.assertTrue(sz['scaled_down']); self.assertIn('margin', sz['scaled_by'])
        self.assertLessEqual(sz['notional'] / sz['lev'], 250.0 + 1e-9); self.assertLess(sz['risk_pct_actual'], 5.0)
    def test_fee_heavy(self):
        sz = EX.fit_size(1000, 1000, 100.0, 99.95, 1.0, '0.01', 0.01)   # 0.05% stop: fees > 1/3 of the loss, scaled -> skip
        self.assertEqual(sz['gate'], 'fee_heavy', sz)
        self.assertGreater(EX.fee_share(100.0, 99.95), EX.FEE_HEAVY_MAX); self.assertLess(EX.fee_share(100.0, 99.5), EX.FEE_HEAVY_MAX)
    def test_liq_bounds_leverage(self):
        self.assertEqual(EX.lev_liq_max(100.0, 90.0), 4)          # 10% stop: liq dist 100 x (1/L - 0.005) must be > 20
        self.assertIsNone(EX.lev_liq_max(100.0, 80.0))
        self.assertEqual(EX.fit_size(1000, 1000, 100.0, 80.0, 1.0, '0.01', 0.01)['gate'], 'liq')
    def test_cap_risk(self):
        sz = EX.fit_size(1000, 1000, 100.0, 97.0, 1.0, '0.01', 0.01, risk_cap=20.0)
        self.assertIsNone(sz['gate']); self.assertIn('cap_risk', sz['scaled_by']); self.assertAlmostEqual(sz['risk_pct_actual'], 2.0, delta=0.01)
        self.assertEqual(EX.fit_size(1000, 1000, 100.0, 97.0, 1.0, '1', 1, risk_cap=0.5)['gate'], 'cap_risk')
    def test_scale_down_off(self):
        with mock.patch.object(EX, 'SCALE_DOWN', False):
            self.assertEqual(EX.fit_size(1000, 1000, 100.0, 99.5, 1.0, '0.01', 0.01)['gate'], 'no_leverage_fit')

def P(inst, q, avg, mark, ct=None):
    return dict(instId=inst, positions=str(q), averagePrice=str(avg), markPrice=str(mark), createTime=str(ct or T.now_ms()))

class TestRiskCap(NoNetwork):
    F, S = 0.0006, 0.0018
    def a(self, pos, rows, bot=lambda p: True, side='buy', rec=None):
        return RC.assess(side, pos, rows, lambda i: 1.0, 1000.0, bot, self.F, self.S, rec)
    def test_open_risk_formula_and_headroom(self):
        r = self.a([P('A-USDT', 10, 100, 101)], [dict(instId='A-USDT', slTriggerPrice='97')])
        want = 10 * 4 + 10 * 97 * self.F + 10 * 101 * self.S
        self.assertEqual(r['mode'], 'risk'); self.assertAlmostEqual(r['total_open'], want, places=3)
        self.assertAlmostEqual(r['headroom'], 150 - want, places=3)                    # direction cap 15% binds
        self.assertAlmostEqual(self.a([P('A-USDT', 10, 100, 101)], [dict(instId='A-USDT', slTriggerPrice='97')], side='sell')['headroom'], 150.0, places=3)   # other side: no same-dir risk, 15% headroom
    def test_be_counts_zero_only_net_of_fees(self):
        self.assertEqual(self.a([P('A-USDT', 10, 100, 105)], [dict(instId='A-USDT', slTriggerPrice='100.4')])['total_open'], 0.0)
        self.assertGreater(self.a([P('A-USDT', 10, 100, 105)], [dict(instId='A-USDT', slTriggerPrice='100.1')])['total_open'], 0.0)   # BE-ish, not net >= 0
    def test_fallbacks(self):
        self.assertEqual(self.a([P('A-USDT', 10, 100, 0)], [dict(instId='A-USDT', slTriggerPrice='97')])['mode'], 'slots')          # no mark
        self.assertEqual(self.a([P('A-USDT', 10, 100, 101)], [dict(instId='A-USDT', slTriggerPrice='97', size='4')])['mode'], 'slots')  # partial SL
        self.assertEqual(self.a([P('A-USDT', 10, 100, 101)], [])['mode'], 'slots')                                                  # bot, no stop
        self.assertEqual(self.a([P('A-USDT', 10, 100, 101)], [], bot=lambda p: False, rec=lambda i: True)['mode'], 'slots')         # ambiguous
        self.assertEqual(RC.assess('buy', [], None, None, 1000.0, None, self.F, self.S)['mode'], 'slots')                           # orders unknown
    def test_manual_no_stop_and_loosest(self):
        r = self.a([P('A-USDT', 10, 100, 101)], [], bot=lambda p: False)
        self.assertAlmostEqual(r['total_open'], 50.0); self.assertEqual(r['dir_open'], 0.0)
        r = self.a([P('A-USDT', 10, 100, 101)], [dict(instId='A-USDT', slTriggerPrice='97'), dict(instId='A-USDT', slTriggerPrice='95')])
        self.assertEqual(r['rows'][0]['stop'], 95.0)

class TestEvaluateRiskAndUniverse(GateBase):
    def tearDown(self):
        p = config.data_path('fundamentals.json')
        if os.path.exists(p): os.remove(p)
        registry.save({}); registry.save_pending([]); super().tearDown()
    def test_ext_without_rating_skipped_and_logged(self):
        s = dict(SETUP, vol_rank=33, universe_tier='ext', _rank=1)
        plan, why = EX.evaluate(s, self.ctx)
        self.assertIsNone(plan); self.assertTrue(why.startswith('no_fundamentals'))
        l = BL.load_live()[-1]; self.assertEqual((l['gate_reason'], l['universe_tier'], l['vol_rank']), ('no_fundamentals', 'ext', 33))
    def test_risk_mode_skips_slot_cap_and_scales(self):
        tp = dict(code='0', data=[])
        with mock.patch.object(EX, 'risk_assess', return_value=dict(mode='risk', headroom=20.0, total_pct=0, dir_pct=0, n_pos=0, error=None)):
            plan, why = EX.evaluate(dict(SETUP, _rank=1), self.ctx)
        self.assertIsNotNone(plan, why); self.assertTrue(plan['scaled_down']); self.assertIn('cap_risk', plan['scaled_by'])
        self.assertAlmostEqual(plan['risk_pct_actual'], 2.0, delta=0.05)   # headroom 20 USDT = 2% (above min_risk_pct)
    def test_slot_fallback_overall_cap(self):
        ctx = dict(self.ctx, pos=[P(f'X{i}-USDT', 1, 1, 1) for i in range(4)])
        with mock.patch.object(EX, 'risk_assess', return_value=dict(mode='slots', error='x')):
            self.assertEqual(EX.evaluate(dict(SETUP, _rank=1), ctx)[1], 'overall cap')
    def test_unresolved_record_blocks_either_side(self):
        registry.save({'BTC-USDT': dict(instId='BTC-USDT', side='sell', opened_at=iso(NOW), position_createTime='1')})
        plan, why = EX.evaluate(dict(SETUP, _rank=1), self.ctx)
        self.assertIsNone(plan); self.assertTrue(why.startswith('unresolved_record'), why)
        registry.save({}); registry.save_pending([dict(instId='BTC-USDT', side='sell', close_by='manual', pending_since=iso(NOW))])
        self.assertTrue(EX.evaluate(dict(SETUP, _rank=1), self.ctx)[1].startswith('unresolved_record'))
    def test_run_logs_every_skip(self):
        registry.save_pending([dict(instId='BTC-USDT', side='buy', close_by='stop', pending_since=iso(NOW))])
        r = EX.run([SETUP], iso(NOW))
        self.assertFalse(r['ok']); l = BL.load_live()[-1]
        self.assertEqual(l['gate_reason'], 'cooldown'); self.assertEqual(l['pair'], 'BTC-USDT')

class TestRegistryNoOverwrite(NoNetwork):
    def tearDown(self): registry.save({}); super().tearDown()
    def test_put(self):
        registry.save({}); registry.put(dict(instId='A-USDT', clientOrderId='x1', side='buy'))
        registry.put(dict(instId='A-USDT', clientOrderId='x1', side='buy', avg=1))           # same trade: update
        with self.assertRaises(registry.UnresolvedRecord): registry.put(dict(instId='A-USDT', clientOrderId='x2', side='buy'))
        self.assertEqual(registry.load()['A-USDT']['avg'], 1)

class TestAdoptFromHistory(NoNetwork):
    def tearDown(self): registry.save({}); super().tearDown()
    def test_rebuilds_missing_record_only(self):
        registry.save({}); p = P('A-USDT', 2, 100, 101)
        tp = [dict(slTriggerPrice='95', tpTriggerPrice='120', tpslId='1')]
        with mock.patch.object(manage, 'history_says_bot', return_value=True), mock.patch.object(manage, 'pending_tpsl', return_value=tp):
            reg = {}; ad, notes = manage.adopt_from_history([p], reg)
            self.assertEqual(ad, ['A-USDT']); rec = registry.load()['A-USDT']
            self.assertEqual((rec['initial_stop'], rec['target'], rec['avg']), (95.0, 120.0, 100.0))
            self.assertTrue(registry.matches(p, rec))
            p2 = P('A-USDT', 2, 100, 101, ct=T.now_ms() - 10_000_000)                          # another position, record held
            ad, notes = manage.adopt_from_history([p2], registry.load())
            self.assertEqual(ad, []); self.assertIn('not overwritten', notes[0])
        with mock.patch.object(manage, 'history_says_bot', return_value=False):
            self.assertEqual(manage.adopt_from_history([P('B-USDT', 1, 1, 1)], {})[0], [])

class TestScheduledDry(NoNetwork):
    def setUp(self):
        super().setUp(); BL._SCHED.clear(); G.clear_logs()
        with open(config.data_path(runlog.FILE), 'w') as fh:
            json.dump([dict(ts_utc=runlog.now_z(), run_id='rid-1', source='manage.py'),
                       dict(ts_utc=iso(NOW - dt.timedelta(hours=2)), run_id='rid-old', source='manage.py')], fh)
    def tearDown(self): os.remove(config.data_path(runlog.FILE)); BL._SCHED.clear(); super().tearDown()
    def test_modes(self):
        self.assertEqual(BL.mode_for(False), 'live'); self.assertTrue(BL.scheduled_dry('rid-1'))
        self.assertFalse(BL.scheduled_dry('rid-old')); self.assertFalse(BL.scheduled_dry('nope')); self.assertFalse(BL.scheduled_dry(''))
        with mock.patch.dict(os.environ, {'RUN_ID': 'rid-1'}): self.assertEqual(BL.mode_for(True), 'scheduled_dry')
        with mock.patch.dict(os.environ, {'RUN_ID': ''}): self.assertEqual(BL.mode_for(True), 'dry')
    def test_scheduled_dry_line_goes_live(self):
        with mock.patch.dict(os.environ, {'RUN_ID': 'rid-1'}):
            ctx = dict(dry=True, scan_ts='t', warnings=[])
            EX.log_block(dict(SETUP, _rank=1), 'news_gate', 'x', ctx)
        live = BL.load_live(); self.assertEqual(live[-1]['mode'], 'scheduled_dry'); self.assertEqual(rd(BL.log_path('dry')), [])

class TestRR2(NoNetwork):
    def test_near_misses_and_bucket(self):
        sc = dict(rejected=['A-USDT: buy R:R 2.50 <3', 'B-USDT: sell R:R 1.90 <3', 'C-USDT: 4H flat', 'D-USDT: sell R:R 2.00 <3'])
        self.assertEqual(SV.near_misses(sc), [('A-USDT', 'buy', 2.5), ('D-USDT', 'sell', 2.0)])
        self.assertEqual(SV.rr_bucket(2.4), '2-3'); self.assertEqual(SV.rr_bucket(3.0), '3+'); self.assertIn('V_RR2', SV.VARIANTS)
    def test_geometry_matches_scan(self):
        h = fake_candles_up(None, None)
        g, note = SCN.geometry('BTC-USDT', 103990.0, 104000.0, h, h)
        self.assertIsNone(note); self.assertEqual(g['side'], 'buy'); self.assertEqual(g['entry'], 104000.0)
        self.assertLess(g['stop'], g['entry']); self.assertEqual(g['target'], max(x[1] for x in h[-60:]))

class TestShadowRR2AndRisk(GateBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(SV, "candles", fake_candles_up); p.start(); self.patches.append(p)
        self.mk = SV.Market(); self.mk.ins = lambda i: self.ins.get(i)
        self.mk.h1 = lambda i: fake_candles_up(i, "1H"); self.mk.ticker = lambda i: dict(askPrice="104000", bidPrice="103990", last="104000")
        self.now = dt.datetime.now(dt.timezone.utc); self.ts = self.now.isoformat()
    def book(self): return dict(open=[], closed_n=0)
    def test_rr2_takes_2_to_3(self):
        s = dict(SETUP, target=110000)   # R:R about 2.4 at the live ask
        self.assertEqual(SV.evaluate('BASELINE', s, self.book(), self.mk, 1000.0, self.ts, self.now)[0], 'SKIP')
        d, why, pl = SV.evaluate('V_RR2', s, self.book(), self.mk, 1000.0, self.ts, self.now)
        self.assertEqual(d, 'TAKE', why); self.assertEqual(pl['rr_bucket'], '2-3')
        pos = SV.open_pos('V_RR2', s, 1, pl, self.ts, self.now); self.assertEqual((pos['rr_bucket'], pos['universe_tier']), ('2-3', 'core'))
    def test_book_risk_caps(self):
        d, why, pl = SV.evaluate('BASELINE', SETUP, self.book(), self.mk, 1000.0, self.ts, self.now)
        self.assertEqual(pl['risk_caps']['mode'], 'risk')
        full = dict(open=[dict(inst=f'X{i}-USDT', side='buy', coins=1.0, entry=100.0, sl=95.0, margin=0.0) for i in range(8)], closed_n=0)
        self.assertTrue(SV.evaluate('BASELINE', SETUP, full, self.mk, 1000.0, self.ts, self.now)[1].startswith('max positions'))
    def test_ext_tier_filtered(self):
        d, why, _ = SV.evaluate('BASELINE', dict(SETUP, vol_rank=35), self.book(), self.mk, 1000.0, self.ts, self.now)
        self.assertTrue(why.startswith('no_fundamentals'), why)

if __name__ == '__main__': unittest.main()

class TestReviewFollowups(GateBase):
    def test_run_level_max_positions_backstop(self):
        pos = [dict(instId=f'X{i}-USDT', positions='1', averagePrice='1', markPrice='1') for i in range(RC.MAX_POS)]
        n0 = len(BL.load_live())
        with mock.patch.object(EX, 'account', return_value=(1000.0, 1000.0, pos, 'net_mode')), \
             mock.patch.object(EX, 'evaluate', side_effect=AssertionError('no setup may be walked')):
            r = EX.run([SETUP], iso(NOW))
        self.assertFalse(r['ok']); self.assertEqual(r['gate_reason'], 'max_positions')
        self.assertEqual(len(BL.load_live()), n0)                                   # run-level: no block line
    def test_fee_heavy_not_applied_unscaled(self):
        # with the default 1/3 a fee-heavy stop can never fit unscaled, so lower the threshold: a 3% stop's full size fits
        with mock.patch.object(EX, 'FEE_HEAVY_MAX', 0.01):
            self.assertGreater(EX.fee_share(100.0, 97.0), EX.FEE_HEAVY_MAX)
            sz = EX.fit_size(1000, 1000, 100.0, 97.0, 1.0, '0.01', 0.01)
            self.assertFalse(sz['scaled_down'], sz); self.assertIsNone(sz['gate'], sz)
            self.assertEqual(EX.fit_size(1000, 1000, 100.0, 99.5, 1.0, '0.01', 0.01)['gate'], 'fee_heavy')   # scaled -> applies
    def test_gate_of_mapping(self):
        for why, g in (('cooldown: x', 'cooldown'), ('overall cap', 'overall_cap'), ('direction cap 3/3', 'dir_cap'),
                       ('unresolved_record (X)', 'unresolved_record'), ('pair already has a position', 'duplicate'),
                       ('R:R 2.1 < 3', 'rr'), ('stop 0.5x ATR < 1', 'atr'), ('4H trend now down, need up', 'trend'),
                       ('news gate: x', 'news_gate'), ('macro window: CPI', 'macro_window'), ('instrument not found', 'instrument'),
                       ('long needs stop < entry < target', 'geometry'), ('error: boom', 'error'), ('???', 'other'), (None, 'other')):
            self.assertEqual(EX.gate_of(why), g, why)
