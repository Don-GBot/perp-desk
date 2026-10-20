#!/usr/bin/env python3
"""SHADOW-ONLY filter variants. Read-only: public market data plus one GET for equity; it never sends an order
(when run as a script, non-GET private calls are refused in-process) and never writes execute.py / manage.py files.

Each time DATA_DIR/scan.json gets a new ts, every variant walks the setups in scan order like execute.py does and logs
one row per setup (variant, decision TAKE / SKIP / NOT_REACHED, reason, plan). Each variant keeps its OWN virtual book
(one entry per scan, max_open / max_per_direction caps, no duplicate pair) and tracks each entry to TP / SL / BE /
trailing stop / hard cut on closed 1m bars with the live exit rules from config.yaml. CLOSE rows carry R, pnl and an
estimated funding cost (public funding-rate history x notional over the hold).

Books (config.yaml -> shadow_variants):
  BASELINE       the live rules (entry.trend_1h and entry.stock_perp_block as configured)
  BASELINE_NO1H  live rules without the 1h trend gate
  V_1H           1h trend gate always on
  V_ATR15        a stop tighter than atr_widen x 1H ATR is widened to entry -/+ atr_widen x ATR (size recomputed, R:R
                 vs the same target must still be >= min_rr)
  V_RR10         skip setups whose live R:R >= rr_cap (the walk falls through to the next setup)
  STOCKS_ONLY    live rules but ONLY stock perps (stockperp.py); everything else SKIP 'not stock_perp'
  V_SLOWTREND    + time-series momentum z (slowcrowd.tsmom_z, 168h/720h): against-sign or |z| <= slowtrend_z_min SKIP;
                 qualifying setups are walked by highest side-aligned z first
  V_CROWD        no longs in the crowd_n crypto pairs of the scan universe with the highest average funding over
                 crowd_hours, no shorts in the crowd_n lowest; a funding read failure blocks ('crowd (data error)')
Note: STOCKS_ONLY only sees stock perps if scan.py emits them; the stock universe of scan.py is crypto-only by default.

  python3 scripts/shadow_variants.py           # scheduler: poll scan.json every poll_sec seconds
  python3 scripts/shadow_variants.py --once    # one pass then exit
  python3 scripts/shadow_variants.py --stats [--by asset_class]   # graded CLOSE rows per variant

Files: DATA_DIR/logs/shadow_variants.jsonl (rows) and DATA_DIR/logs/shadow_variants_state.json (books).
"""
import os, sys, json, time, argparse, datetime as dt
from decimal import ROUND_UP, ROUND_DOWN
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blofin_client as bf
import config, news_gate as NG
import execute as EX
import trend1h as T1, stockperp as SP, slowcrowd as SCW
from scan import candles, atr, trend, net_rr

SV = config.CFG["shadow_variants"]; M = config.CFG["management"]
RR_CAP = float(SV["rr_cap"]); ATR_WIDEN = float(SV["atr_widen"]); Z_MIN = float(SV["slowtrend_z_min"])
CROWD_N = int(SV["crowd_n"]); CROWD_H = float(SV["crowd_hours"]); POLL = float(SV["poll_sec"])
HARD_CUT = config.CFG["risk"]["hard_cut_pct"] / 100.0; MANAGE_MS = 15 * 60_000
VARIANTS = ('BASELINE', 'BASELINE_NO1H', 'V_1H', 'V_ATR15', 'V_RR10', 'STOCKS_ONLY', 'V_SLOWTREND', 'V_CROWD')
OUT = config.data_path('logs'); LOG = os.path.join(OUT, 'shadow_variants.jsonl'); STATEF = os.path.join(OUT, 'shadow_variants_state.json')

_orig_private = bf.private
def _get_only(method, path, params=None, body=None):
    if str(method).upper() != 'GET': raise PermissionError(f'shadow_variants is read-only: refused {method} {path}')
    return _orig_private('GET', path, params)

def now_utc(): return dt.datetime.now(dt.timezone.utc)
def iso(t): return t.strftime('%Y-%m-%dT%H:%M:%SZ')
def ms(t): return int(t.timestamp() * 1000)
def _ts(x):
    try:
        t = dt.datetime.fromisoformat(str(x).replace('Z', '+00:00')); return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception: return None

def gated_1h(v): return v == 'V_1H' or (v != 'BASELINE_NO1H' and EX.TREND1H)
def stock_mode(v):
    if v == 'STOCKS_ONLY': return 'only'
    return 'block' if EX.STOCKPERP else None

def emit(row):
    os.makedirs(OUT, exist_ok=True)
    row = dict(ts_utc=iso(now_utc()), **row)
    with open(LOG, 'a') as fh: fh.write(json.dumps(row, default=str) + '\n')
    return row

def load_state():
    try:
        with open(STATEF) as fh: st = json.load(fh)
    except FileNotFoundError: st = {}
    st.setdefault('processed', []); books = st.setdefault('books', {})
    for v in VARIANTS: books.setdefault(v, dict(open=[], closed_n=0))
    return st

def save_state(st):
    os.makedirs(OUT, exist_ok=True); tmp = STATEF + '.tmp'
    with open(tmp, 'w') as fh: json.dump(st, fh, indent=1, default=str)
    os.replace(tmp, STATEF)

class Market:
    """Per-pass cache of public market data."""
    def __init__(s): s._ins = None; s._c = {}; s.universe = []
    def _memo(s, key, fn):
        if key not in s._c:
            try: s._c[key] = fn()
            except Exception as e: s._c[key] = e
        if isinstance(s._c[key], Exception): raise s._c[key]
        return s._c[key]
    def ins(s, inst):
        if s._ins is None: s._ins = EX.instruments()
        return s._ins.get(inst)
    def ticker(s, inst): return s._memo(('tk', inst), lambda: bf.public('/api/v1/market/tickers', {'instId': inst})['data'][0])
    def h1(s, inst): return s._memo(('h1', inst), lambda: candles(inst, T1.BAR, T1.BARS))
    def atr1h(s, inst): return atr(s.h1(inst))
    def h1long(s, inst):
        def get():
            p1 = bf.public('/api/v1/market/candles', {'instId': inst, 'bar': '1H', 'limit': '1440'})['data']
            p2 = bf.public('/api/v1/market/candles', {'instId': inst, 'bar': '1H', 'limit': '1440', 'after': p1[-1][0]})['data'] if p1 else []
            rows = {int(x[0]): float(x[4]) for x in (p1 or []) + (p2 or [])}
            return [rows[k] for k in sorted(rows)]
        return s._memo(('h1l', inst), get)
    def z(s, inst): return s._memo(('z', inst), lambda: SCW.tsmom_z(s.h1long(inst)))
    def funding(s, inst):
        def get():
            r = bf.public('/api/v1/market/funding-rate-history', {'instId': inst, 'limit': '100'})
            if str(r.get('code')) != '0': raise RuntimeError(f"funding {inst}: code {r.get('code')}")
            return r.get('data') or []
        return s._memo(('fr', inst), get)
    def crowd(s, now_ms):
        def get():
            pairs = [u for u in s.universe if SP.asset_class(u, s.ins(u)) == 'crypto'][:25]
            if len(pairs) < 2 * CROWD_N: raise ValueError(f'crowd: only {len(pairs)} crypto pairs in the scan universe')
            return SCW.crowd_sets({u: SCW.avg_funding(s.funding(u), now_ms, CROWD_H) for u in pairs}, CROWD_N)
        return s._memo('crowd', get)
    def m1(s, inst, since_ms):
        """Closed 1m bars [t, o, h, l, c] with t >= since_ms, oldest first (last 1440 bars at most)."""
        def get():
            r = bf.public('/api/v1/market/candles', {'instId': inst, 'bar': '1m', 'limit': '1440'})
            return sorted([[int(x[0])] + [float(v) for v in x[1:5]] for x in r.get('data') or [] if str(x[8]) == '1'])
        return [b for b in s._memo(('m1', inst), get) if b[0] >= since_ms]

def evaluate(v, s, book, mk, equity, scan_ts, now):
    """One setup through execute.py's checks for variant v against its own book. Returns (decision, reason, plan)."""
    inst = s.get('instId'); side = EX.norm_side(s.get('side')); pl = {}
    if not inst or not side: return 'SKIP', 'bad setup', pl
    why = EX.check_scan_fresh(scan_ts, now)
    if why: return 'SKIP', why, pl
    ins = mk.ins(inst); pl['asset_class'] = SP.asset_class(inst, ins)
    sm = stock_mode(v)
    if sm:
        try: ist, how = SP.is_stock(inst, ins) if (ins or inst in SP.STOCK_PERPS) else SP.lookup(inst, bf.public)
        except Exception as e: return 'SKIP', 'stock_perp (data error)', dict(pl, stock_error=str(e)[:160])
        if sm == 'block' and ist: return 'SKIP', f'stock_perp ({how})', pl
        if sm == 'only' and not ist: return 'SKIP', 'not stock_perp', pl
    if sum(1 for p in book['open'] if p['side'] == side) >= EX.MAXDIR: return 'SKIP', 'direction cap (variant book)', pl
    if len(book['open']) >= EX.MAXALL: return 'SKIP', 'overall cap (variant book)', pl
    if any(p['inst'] == inst for p in book['open']): return 'SKIP', 'duplicate pair (variant book)', pl
    ng = NG.check(inst, side); pl['news_gate'] = ng.get('reason')
    if ng.get('block'): return 'SKIP', 'news gate: ' + ng['reason'], pl
    if gated_1h(v):
        try: t1, ef, es, p1 = T1.trend_from_rows(mk.h1(inst))
        except Exception as e: return 'SKIP', '1h_trend (data error)', dict(pl, trend1h_error=str(e)[:160])
        pl['trend1h'] = t1
        if t1 != T1.want(side): return 'SKIP', f'1h_trend ({t1})', pl
    if v == 'V_SLOWTREND':
        try: zz = mk.z(inst)
        except Exception as e: return 'SKIP', 'slowtrend (data error)', dict(pl, slowtrend_error=str(e)[:160])
        pl['tsmom_z'] = round(zz, 4); az = SCW.aligned(zz, side)
        if az <= 0: return 'SKIP', f'slowtrend: against z {zz:+.2f}', pl
        if az <= Z_MIN: return 'SKIP', f'slowtrend: no signal |z| {abs(zz):.2f} <= {Z_MIN:g}', pl
    if v == 'V_CROWD':
        try: nl, nsh = mk.crowd(ms(now))
        except Exception as e: return 'SKIP', 'crowd (data error)', dict(pl, crowd_error=str(e)[:160])
        if side == 'buy' and inst in nl: return 'SKIP', f'crowd: long in a top-{CROWD_N} funding pair', pl
        if side == 'sell' and inst in nsh: return 'SKIP', f'crowd: short in a bottom-{CROWD_N} funding pair', pl
    if not ins: return 'SKIP', 'instrument not found', pl
    tick = ins['tickSize']; lot = ins['lotSize']; cv = float(ins['contractValue']); mins = float(ins['minSize'])
    tk = mk.ticker(inst); entry = float(tk['askPrice']) if side == 'buy' else float(tk['bidPrice'])
    stop = float(EX.q(s['stop'], tick)); target = float(EX.q(s['target'], tick)); a1 = mk.atr1h(inst)
    if v == 'V_ATR15' and a1 and abs(entry - stop) < ATR_WIDEN * a1:
        ns = entry - ATR_WIDEN * a1 if side == 'buy' else entry + ATR_WIDEN * a1
        stop = float(EX.q(ns, tick, ROUND_DOWN if side == 'buy' else ROUND_UP)); pl['widened'] = True
    why = EX.check_geometry(side, entry, stop, target)
    if why: return 'SKIP', why, pl
    risk = abs(entry - stop); rr = net_rr(side, entry, stop, target)
    pl.update(entry=entry, stop=stop, target=target, rr=round(rr, 4), atr1h=a1, tick=tick)
    if rr < EX.MINRR: return 'SKIP', f'net R:R {rr:.2f} < {EX.MINRR:g}', pl
    if v == 'V_RR10' and rr >= RR_CAP: return 'SKIP', f'rr>={RR_CAP:g} (R:R {rr:.2f})', pl
    if risk < EX.ATR_FLOOR * a1: return 'SKIP', f'stop {risk/a1:.2f}x ATR too tight', pl
    tf = config.CFG['entry']['trend_filter']
    if tf['enabled']:
        tr = trend([x[3] for x in candles(inst, tf['timeframe'])])[0]
        if tr != ('up' if side == 'buy' else 'down'): return 'SKIP', f'{tf["timeframe"]} trend now {tr}', pl
    size = EX.size_for(equity, entry, stop, cv, lot)
    if size < mins: return 'SKIP', 'size below min', pl
    notional = size * cv * entry; avail = max(0.0, equity - sum(p['margin'] for p in book['open']))
    lev, cap = EX.pick_lev(notional, equity, avail, float(ins.get('maxLeverage') or EX.LEVMAX))
    if lev is None: return 'SKIP', 'no leverage fits the margin cap', pl
    liq = EX.est_liq(entry, side, lev, float(ins.get('maintenanceMarginRate') or EX.MMR_DEFAULT))
    if risk >= EX.MAX_STOP_TO_LIQ * abs(liq - entry): return 'SKIP', f'liq too close at {lev}x', pl
    pl.update(coins=size * cv, lev=lev, margin=notional / lev, notional=notional)
    return 'TAKE', 'passes every check', pl

def open_pos(v, s, rank, pl, scan_ts, now):
    lg = s['side'] in ('buy', 'long'); e = pl['entry']; d = abs(e - pl['stop'])
    return dict(variant=v, inst=s['instId'], side='buy' if lg else 'sell', rank=rank, scan_ts=scan_ts, entry=e, sl=pl['stop'], stop0=pl['stop'],
                target=pl['target'], coins=pl['coins'], margin=pl['margin'], tick=pl['tick'], d=d, t_entry=ms(now), cursor=ms(now),
                last_manage=ms(now), be=False, best=e, trig=e + M['breakeven_at_r'] * d if lg else e - M['breakeven_at_r'] * d,
                act=e + M['trail_activate_r'] * d if lg else e - M['trail_activate_r'] * d,
                cb=float(EX.q(M['trail_callback_atr'] * pl['atr1h'], pl['tick'])), trail_on=False, tbest=None,
                asset_class=pl.get('asset_class'), widened=pl.get('widened'))

def step_bars(pos, bars):
    """Exchange-side SL / TP / trailing stop over closed 1m bars. Returns (fill, kind, t_ms) or None."""
    lg = pos['side'] == 'buy'
    for t, o, h, l, c in bars:
        lv = pos['sl']
        if pos['trail_on']: lv = max(lv, pos['tbest'] - pos['cb']) if lg else min(lv, pos['tbest'] + pos['cb'])
        kind = 'be' if pos['be'] and lv == pos['sl'] else ('trail' if lv != pos['sl'] else 'stop')
        if (lg and l <= lv) or (not lg and h >= lv): pos['cursor'] = t + 60_000; return (min(o, lv) if lg else max(o, lv)), kind, t
        if (lg and h >= pos['target']) or (not lg and l <= pos['target']): pos['cursor'] = t + 60_000; return (max(o, pos['target']) if lg else min(o, pos['target'])), 'tp', t
        pos['best'] = max(pos['best'], h) if lg else min(pos['best'], l)
        if not pos['trail_on'] and ((lg and h >= pos['act']) or (not lg and l <= pos['act'])): pos['trail_on'] = True; pos['tbest'] = pos['act']
        if pos['trail_on']: pos['tbest'] = max(pos['tbest'], h) if lg else min(pos['tbest'], l)
        pos['cursor'] = t + 60_000
    return None

def manage_step(pos, last, equity, a1_now, now_ms):
    """manage.py at a 15-min step: fee-inclusive hard cut, then break-even (stop to entry +/- buffer, trail to N x ATR)."""
    lg = pos['side'] == 'buy'; e = pos['entry']
    if pos['coins'] * (((e - last) if lg else (last - e)) + EX.FEE * (e + last)) >= HARD_CUT * equity: return last, 'cut', now_ms
    if not pos['be'] and ((lg and pos['best'] >= pos['trig']) or (not lg and pos['best'] <= pos['trig'])):
        b = M['breakeven_buffer_pct'] / 100.0; bes = float(EX.q(e * (1 + b) if lg else e * (1 - b), pos['tick']))
        if (lg and pos['sl'] < bes < last) or (not lg and last < bes < pos['sl']):
            pos['sl'] = bes; pos['cb'] = float(EX.q(M['breakeven_trail_atr'] * a1_now, pos['tick'])) if a1_now else pos['cb']
            pos['trail_on'] = True; pos['tbest'] = last
        pos['be'] = True
    return None

def close_row(pos, fill, kind, t_ms, mk):
    lg = pos['side'] == 'buy'; e = pos['entry']
    pnl = ((fill - e) if lg else (e - fill)) - EX.FEE * (e + fill); rpc = pos['d'] + EX.FEE * (e + pos['stop0'])
    row = dict(variant=pos['variant'], decision='CLOSE', scan_ts=pos['scan_ts'], rank=pos['rank'], pair=pos['inst'], side=pos['side'],
               kind=kind, entry=e, exit=fill, initial_stop=pos['stop0'], target=pos['target'], R=round(pnl / rpc, 4) if rpc else None,
               pnl_usdt=round(pnl * pos['coins'], 4), be_done=pos['be'], hold_min=round((t_ms - pos['t_entry']) / 60_000, 1),
               widened=pos.get('widened'), asset_class=pos.get('asset_class') or SP.asset_class(pos['inst']))
    try: row['funding_paid_est'] = SCW.funding_paid(mk.funding(pos['inst']), pos['side'], e * pos['coins'], pos['t_entry'], t_ms)
    except Exception as x: row['funding_paid_est'] = None; row['funding_error'] = str(x)[:120]
    return row

def track(st, mk, equity, now):
    nowm = ms(now); n = 0
    for v in VARIANTS:
        book = st['books'][v]
        for pos in list(book['open']):
            try:
                ex = step_bars(pos, mk.m1(pos['inst'], pos['cursor']))
                if ex is None and nowm - pos['last_manage'] >= MANAGE_MS:
                    pos['last_manage'] = nowm
                    ex = manage_step(pos, float(mk.ticker(pos['inst'])['last']), equity, mk.atr1h(pos['inst']), nowm)
            except Exception as e: emit(dict(variant=v, decision='TRACK_ERROR', pair=pos['inst'], reason=str(e)[:200])); continue
            if ex:
                emit(close_row(pos, *ex, mk)); n += 1
                book['open'].remove(pos); book['closed_n'] = book.get('closed_n', 0) + 1
    return n

def process_scan(st, mk, sc, equity, now):
    scan_ts = sc.get('ts'); setups = sc.get('setups') or []; mk.universe = sc.get('universe') or [x.get('instId') for x in setups]
    def zkey(s):
        try: return -SCW.aligned(mk.z(s.get('instId')), EX.norm_side(s.get('side')))
        except Exception: return float('inf')
    for v in VARIANTS:
        book = st['books'][v]; taken = None; order = list(enumerate(setups, 1))
        if v == 'V_SLOWTREND': order = sorted(order, key=lambda rs: zkey(rs[1]))
        for rank, s in order:
            base = dict(variant=v, scan_ts=scan_ts, rank=rank, pair=s.get('instId'), side=s.get('side'), scan_rr=s.get('rr'))
            if taken: emit(dict(base, decision='NOT_REACHED', reason=f'walk stopped at rank {taken} (one entry per scan)')); continue
            try: dec, why, pl = evaluate(v, s, book, mk, equity, scan_ts, now)
            except Exception as e: dec, why, pl = 'SKIP', f'error: {str(e)[:200]}', {}
            emit(dict(base, decision=dec, reason=why, plan=pl))
            if dec == 'TAKE': book['open'].append(open_pos(v, s, rank, pl, scan_ts, now)); taken = rank
        if not setups: emit(dict(variant=v, scan_ts=scan_ts, decision='NO_SETUPS', reason='scan.json has no setups'))
    st['processed'] = (st['processed'] + [scan_ts])[-200:]

def account_equity():
    r = bf.private('GET', '/api/v1/account/balance')
    d = [x for x in (r.get('data') or {}).get('details', []) if x.get('currency') == 'USDT']
    if not d: raise RuntimeError(f'balance unreadable: {bf.resp_err(r)}')
    return float(d[0]['equity'])

def one_pass(now=None, equity=None):
    now = now or now_utc(); st = load_state(); mk = Market(); did = dict(scan=False, closes=0)
    equity = equity if equity is not None else account_equity()
    did['closes'] = track(st, mk, equity, now)
    try:
        with open(config.data_path('scan.json')) as fh: sc = json.load(fh)
    except Exception: sc = None
    if sc and sc.get('ts') and sc['ts'] not in st['processed']:
        process_scan(st, mk, sc, equity, now); did['scan'] = True
    save_state(st); return did

def stats(path=None, by=None):
    out = {}
    try: fh = open(path or LOG)
    except FileNotFoundError: return out
    with fh:
        for x in fh:
            try: r = json.loads(x)
            except Exception: continue
            if r.get('decision') != 'CLOSE' or r.get('R') is None: continue
            k = r['variant'] if not by else f"{r['variant']}|{r.get(by)}"
            o = out.setdefault(k, dict(n=0, wins=0, sum_R=0.0, pnl_usdt=0.0))
            o['n'] += 1; o['wins'] += r['R'] > 0; o['sum_R'] = round(o['sum_R'] + r['R'], 4); o['pnl_usdt'] = round(o['pnl_usdt'] + (r.get('pnl_usdt') or 0), 4)
    for o in out.values(): o['avg_R'] = round(o['sum_R'] / o['n'], 4); o['win_rate'] = round(o['wins'] / o['n'], 3)
    return out

def main():
    ap = argparse.ArgumentParser(description='shadow filter variants (read-only)')
    ap.add_argument('--once', action='store_true'); ap.add_argument('--stats', action='store_true'); ap.add_argument('--by')
    a = ap.parse_args()
    if a.stats: print(json.dumps(stats(by=a.by), indent=1)); return 0
    if not SV['enabled']: print('shadow_variants disabled in config.yaml'); return 0
    bf.require_keys('shadow_variants.py')
    bf.private = _get_only   # installed for this process only; importing the module (tests) leaves bf untouched
    while True:
        try: print(json.dumps(one_pass()))
        except Exception as e: print(f'pass failed: {str(e)[:200]}', file=sys.stderr)
        if a.once: return 0
        time.sleep(POLL)

if __name__ == '__main__': sys.exit(main())
