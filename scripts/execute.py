#!/usr/bin/env python3
"""Executor order script: checks setups from scan.json and opens AT MOST ONE trade. Sends real orders.

  python3 scripts/execute.py                 # try setups in scan.json, best R:R first
  python3 scripts/execute.py --inst SOL-USDT # only this pair's setup
  python3 scripts/execute.py --dry           # run every check, print the plan, place nothing

Needs BLOFIN_API_KEY / BLOFIN_API_SECRET / BLOFIN_PASSPHRASE. Holds DATA_DIR/execute.lock while running.

Per run:
  - Reject the whole scan if scan.json is older than entry.max_scan_age_min.
  - Account checks once: equity readable, overall cap, account in net (one-way) mode.
  - For each setup in order (best R:R first), skip it with a reason if any check fails, then try the next:
    side valid; direction cap; no position on the pair; stop/target rounded to tick and
    stop < entry < target (long) or target < entry < stop (short) at the live price;
    not a stock perp (entry.stock_perp_block, stockperp.py; checked first, fails closed);
    net R:R (after round-trip taker fees) >= min_rr; stop >= ATR floor; 4H trend re-checked now;
    1H trend aligned with the side (entry.trend_1h, trend1h.py; flat blocks both sides, a data error skips);
    size >= instrument min; a leverage in [min, max] that fits the margin cap; liquidation well beyond the stop.
  - First setup that passes: set leverage, market order with attached TP/SL, then a trailing stop.
Sizing: a clean stop-out loses about risk_per_trade_pct of equity INCLUDING entry and exit taker fees and a slippage
buffer (fees.slip_frac x entry): loss per coin = |entry - stop| + taker_rate x (entry + stop) + slip_frac x entry.
Direction cap: max_per_direction counts open BOT positions only (bot_positions.json matches); manual positions count
toward max_open only. Re-entry cooldown: no entry on a pair+side within risk.cooldown_hours of a losing bot stop /
trail / hard-cut close (closes.cooldown_check). Either check failing to read is a skip, never a pass.

Once an order is accepted the result is ok:true, written to DATA_DIR/exec_last.json right away, and any
later problem (position not visible yet, trailing stop rejected) is reported under "warnings".
New entries are recorded in DATA_DIR/bot_positions.json so manage.py can tell bot trades from manual ones.

The news gate (news_gate.check) and, when enabled, the macro window (news_gate.macro_check) are
applied inside evaluate() to every candidate, so falling through to the next setup can never open a
news-blocked trade.

Every news-gate, 1h-trend and stock-perp block is logged to DATA_DIR/blocked_setups.jsonl (blocklog.py, source
"execute"); --dry lines go to DATA_DIR/logs/blocked_setups_dry.jsonl. A log failure only adds a warning.

If the order request times out or the connection drops, the script looks the order up by its
clientOrderId (and checks positions) before deciding it failed.
"""
import os, sys, json, time, argparse, datetime as dt
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blofin_client as bf
import config, lock, registry, news_gate
import trend1h as T1, stockperp as SP, blocklog as BL, closes

def _load_json(path):
    with open(path) as fh:
        return json.load(fh)
from scan import candles, atr, trend, net_rr

C = config.CFG; R_ = C["risk"]; E = C["entry"]; LV = C["leverage"]; M = C["management"]
RISK = R_["risk_per_trade_pct"] / 100.0
FEE = float(C["fees"]["taker_rate"]); SLIP = float(C["fees"]["slip_frac"])
COOLDOWN_H = float(R_["cooldown_hours"])
PREFIX = C["bot"]["client_order_prefix"]
MAXDIR = int(R_["max_per_direction"]); MAXALL = int(R_["max_open"]); MINRR = float(E["min_rr"])
ATR_FLOOR = float(E["min_stop_atr"]) * float(E["atr_tolerance"])
MAX_AGE_MIN = float(E["max_scan_age_min"])
NEW_MM = LV["margin_mode"]
LEVMIN = int(LV["min"]); LEVMAX = int(LV["max"]); MARGIN_CAP = LV["margin_cap_pct"] / 100.0
FREE_BUF = float(LV["free_margin_buffer"]); MMR_DEFAULT = float(LV["mmr_default"]); MAX_STOP_TO_LIQ = float(LV["max_stop_to_liq"])
TREND1H = T1.ENABLED; STOCKPERP = bool(E["stock_perp_block"]["enabled"])
SIDES = {'buy': 'buy', 'long': 'buy', 'sell': 'sell', 'short': 'sell'}

def fs(x): return format(Decimal(str(x)).normalize(), 'f')
def q(x, step, mode=ROUND_HALF_UP): s = Decimal(step); return (Decimal(str(x)) / s).to_integral_value(mode) * s
def now_utc(): return dt.datetime.now(dt.timezone.utc)
def write_json(name, obj):
    p = config.data_path(name); tmp = p + '.tmp'
    with open(tmp, 'w') as f: json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, p)

def pick_lev(notional, equity, avail, maxlev=LEVMAX):
    cap = min(MARGIN_CAP * equity, avail * FREE_BUF)
    for L in range(LEVMIN, int(min(LEVMAX, maxlev)) + 1):
        if notional / L <= cap: return L, cap
    return None, cap

def est_liq(entry, side, L, mmr=MMR_DEFAULT):
    # isolated liq estimate: long = entry*(1-1/L+mmr), short = entry*(1+1/L-mmr) (fees ignored)
    d = entry * (1.0 / L - mmr); return entry + d if side == 'sell' else entry - d

def risk_per_coin(entry, stop, fee=FEE, slip=SLIP):
    """Loss of one coin at the stop: price distance + taker fee on entry and exit + slippage buffer."""
    return abs(entry - stop) + fee * (entry + stop) + slip * entry

def loss_per_contract(entry, stop, cv, fee=FEE, slip=SLIP):
    """Loss of one contract at the stop, including entry and exit taker fees and the slippage buffer."""
    return cv * risk_per_coin(entry, stop, fee, slip)

def size_for(equity, entry, stop, cv, lot, fee=FEE, slip=SLIP):
    per = loss_per_contract(entry, stop, cv, fee, slip)
    return float(q((equity * RISK) / per, lot, ROUND_DOWN)) if per > 0 else 0.0

def norm_side(s):
    return SIDES.get(str(s).strip().lower())

def check_geometry(side, entry, stop, target):
    """None if stop/entry/target are on the right sides, else a reason."""
    if side == 'buy' and not (stop < entry < target): return f'long needs stop < entry < target, got {stop:g} / {entry:g} / {target:g}'
    if side == 'sell' and not (target < entry < stop): return f'short needs target < entry < stop, got {target:g} / {entry:g} / {stop:g}'
    return None

def scan_age_min(scan_ts, now=None):
    t = dt.datetime.fromisoformat(str(scan_ts).replace('Z', '+00:00'))
    if t.tzinfo is None: t = t.replace(tzinfo=dt.timezone.utc)
    return ((now or now_utc()) - t).total_seconds() / 60

def check_scan_fresh(scan_ts, now=None):
    if not scan_ts: return 'scan.json has no ts'
    try: age = scan_age_min(scan_ts, now)
    except Exception: return f'scan.json ts unreadable: {scan_ts!r}'
    if age > MAX_AGE_MIN: return f'scan.json is {age:.0f} min old (> {MAX_AGE_MIN:g}); run scan.py first'
    return None

# ---- account -----------------------------------------------------------------------------------
def account():
    """(equity, available, open_positions, position_mode) via GET calls."""
    bal = bf.private('GET', '/api/v1/account/balance')
    d = [x for x in (bal.get('data') or {}).get('details', []) if x.get('currency') == 'USDT']
    if not d: raise RuntimeError(f'balance unreadable: {bf.resp_err(bal)}')
    pr = bf.private('GET', '/api/v1/account/positions')
    if str(pr.get('code')) != '0': raise RuntimeError(f'positions unreadable: {bf.resp_err(pr)}')
    pos = [p for p in pr['data'] if float(p.get('positions') or 0) != 0]
    mode = (bf.private('GET', '/api/v1/account/position-mode').get('data') or {}).get('positionMode')
    return float(d[0]['equity']), float(d[0]['available']), pos, mode

def pair_positions(inst):
    r = bf.private('GET', '/api/v1/account/positions', {'instId': inst})
    if str(r.get('code')) != '0': raise RuntimeError(f'positions unreadable: {bf.resp_err(r)}')
    return [p for p in r['data'] if float(p.get('positions') or 0) != 0]

def instruments():
    return {x['instId']: x for x in bf.public('/api/v1/market/instruments')['data']}

# ---- shared gates (also used by shadow_swap.py / shadow_variants.py) ---------------------------
def stockperp_check(inst, ins=None):
    """(reason, detail) if inst is a stock perp or cannot be classified (fail closed), else (None, how). No-op when off."""
    if not STOCKPERP: return None, None
    try:
        stock, how = SP.is_stock(inst, ins) if ins else SP.lookup(inst, bf.public)
        if inst not in SP.STOCK_PERPS and ins and not ins.get('assetClass'): stock, how = SP.lookup(inst, bf.public)
    except Exception as e: return 'stock_perp (data error)', str(e)[:160]
    return (f'stock_perp ({how})', how) if stock else (None, how)

def trend1h_check(inst, side, rows=None):
    """(reason, detail) if the 1H trend does not agree with side or cannot be read (fail closed), else (None, detail)."""
    if not TREND1H: return None, None
    try:
        t, ef, es, p = T1.trend_from_rows(rows if rows is not None else candles(inst, T1.BAR, T1.BARS))
    except Exception as e: return '1h_trend (data error)', str(e)[:160]
    d = dict(trend=t, ema_fast=ef, ema_slow=es, close=p)
    return (None, d) if t == T1.want(side) else (f'1h_trend ({t})', d)

def log_block(setup, gate_reason, reason, ctx, extra=None):
    """Append one blocked_setups line. Never raises: a failure becomes a warning in ctx."""
    if not BL.ENABLED: return
    try:
        side = norm_side(setup.get('side'))
        line = BL.build_line('execute', 'dry' if ctx.get('dry') else 'live', setup.get('instId'), 'long' if side == 'buy' else 'short',
                             setup.get('entry'), setup.get('stop'), setup.get('target'), setup.get('rr'), setup.get('_rank'),
                             gate_reason, run_id=os.environ.get('RUN_ID'), scan_ts=ctx.get('scan_ts'), reason=reason, **(extra or {}))
        BL.append(line)
    except Exception as e: ctx.setdefault('warnings', []).append(f'blocked_setups log failed: {str(e)[:160]}')

def cooldown_block(inst, side, now=None):
    """None if the pair+side may be entered, else the SKIP reason. A read error is a skip."""
    if not COOLDOWN_H: return None
    try: cd = closes.cooldown_check(inst, side, now, COOLDOWN_H)
    except Exception as e: return f'cooldown check error: {str(e)[:160]}'
    return cd['reason'] if cd['block'] else None

def bot_side_count(side, pos, reg=None):
    """Open BOT positions on side ('buy'/'sell'): exchange positions with a matching bot_positions.json record."""
    reg = registry.load_strict() if reg is None else reg
    return sum(1 for p in pos if (float(p['positions']) > 0) == (side == 'buy') and registry.match(p, reg))

def dircap_block(side, pos):
    try: n = bot_side_count(side, pos)
    except Exception as e: return f'direction cap check error: {str(e)[:160]}'
    return f'direction cap {n}/{MAXDIR}' if n >= MAXDIR else None

# ---- per-setup evaluation ----------------------------------------------------------------------
def evaluate(setup, ctx):
    """Returns (plan, None) if this setup can be traded now, else (None, reason)."""
    inst = setup.get('instId'); side = norm_side(setup.get('side'))
    if not inst or not side: return None, f'bad setup: instId {inst!r} side {setup.get("side")!r}'
    try: stop_raw = float(setup['stop']); target_raw = float(setup['target'])
    except Exception: return None, 'bad setup: stop/target missing or not numeric'
    pos = ctx['pos']
    if any(p['instId'] == inst for p in pos): return None, 'pair already has a position'
    ins = ctx['ins'].get(inst)
    sw, how = stockperp_check(inst, ins)
    if sw:
        log_block(setup, 'stock_perp (data error)' if 'data error' in sw else 'stock_perp', sw, ctx, dict(detail=how))
        return None, sw
    why = cooldown_block(inst, side)
    if why: return None, why
    why = dircap_block(side, pos)
    if why: return None, why
    if not ins: return None, 'instrument not found'
    if ins.get('state') not in (None, 'live') or ins.get('offTime'): return None, 'instrument not live or scheduled for delisting'
    ng = news_gate.check(inst, side)
    if ng.get('block'):
        log_block(setup, 'news_gate', ng['reason'], ctx)
        return None, f'news gate: {ng["reason"]}'
    mc = news_gate.macro_check()
    if mc.get('block'): return None, mc['reason']
    tick = ins['tickSize']; lot = ins['lotSize']; cv = float(ins['contractValue']); mins = float(ins['minSize'])
    tk = bf.public('/api/v1/market/tickers', {'instId': inst})['data'][0]
    entry = float(tk['askPrice']) if side == 'buy' else float(tk['bidPrice'])
    stop = float(q(stop_raw, tick)); target = float(q(target_raw, tick))
    why = check_geometry(side, entry, stop, target)
    if why: return None, why
    risk = abs(entry - stop)
    rr = net_rr(side, entry, stop, target)
    if rr < MINRR: return None, f'net R:R {rr:.2f} < {MINRR:g}'
    a1 = atr(candles(inst, '1H'))
    if risk < ATR_FLOOR * a1: return None, f'stop {risk/a1:.2f}x ATR < {E["min_stop_atr"]:g}'
    tf = E['trend_filter']
    if tf['enabled']:
        tr = trend([x[3] for x in candles(inst, tf['timeframe'])])[0]
        want = 'up' if side == 'buy' else 'down'
        if tr != want: return None, f'{tf["timeframe"]} trend now {tr}, need {want}'
    tw, t1d = trend1h_check(inst, side)
    if tw:
        log_block(setup, '1h_trend (data error)' if 'data error' in tw else '1h_trend', tw, ctx, dict(detail=t1d))
        return None, tw
    equity, avail = ctx['equity'], ctx['avail']
    size = size_for(equity, entry, stop, cv, lot)
    if size < mins: return None, f'size {size:g} below min {mins:g}'
    notional = size * cv * entry
    lev, cap = pick_lev(notional, equity, avail, float(ins.get('maxLeverage') or LEVMAX))
    if lev is None: return None, f'no leverage <= {LEVMAX} fits: notional {notional:.2f}, margin cap {cap:.2f}'
    liq = est_liq(entry, side, lev, float(ins.get('maintenanceMarginRate') or MMR_DEFAULT))
    if risk >= MAX_STOP_TO_LIQ * abs(liq - entry):
        return None, f'liq {liq:.8g} too close: stop dist {risk:.8g} >= {MAX_STOP_TO_LIQ:g} x liq dist {abs(liq-entry):.8g} at {lev}x'
    live = pair_positions(inst)
    if live: return None, 'existing position on pair; leverage untouched'
    coid = bf.client_order_id(PREFIX)
    plan = dict(instId=inst, side=side, news=ng, trend1h=t1d, asset_class=SP.asset_class(inst, ins), macro=mc, equity=equity, avail=avail, entry=entry, stop=stop, target=target, rr_net=rr,
                atr1h=a1, size=size, notional=notional, lev=lev, margin=notional / lev, margin_cap=cap, liq_est=liq,
                stop_dist=risk, fee_rate=FEE, slip_frac=SLIP, est_loss_at_stop=size * loss_per_contract(entry, stop, cv), tick=tick,
                lev_req=dict(instId=inst, leverage=str(lev), marginMode=NEW_MM, positionSide='net'),
                order_req=dict(instId=inst, marginMode=NEW_MM, positionSide='net', side=side, orderType='market',
                               size=fs(size), tpTriggerPrice=fs(target), tpOrderPrice='-1', slTriggerPrice=fs(stop),
                               slOrderPrice='-1', clientOrderId=coid))
    return plan, None

# ---- placing -----------------------------------------------------------------------------------
def trail_body(p, callback, active=None):
    b = dict(instId=p['instId'], marginMode=p['marginMode'], positionSide=p.get('positionSide') or 'net',
             side='sell' if float(p['positions']) > 0 else 'buy', orderType='trailing_stop', size='-1',
             callbackSpread=fs(callback), reduceOnly='true', clientOrderId=bf.client_order_id(PREFIX, 't'))
    if active is not None: b['activePrice'] = fs(active)
    return b

def wait_position(inst, timeout=10.0, step=1.0):
    t0 = time.time()
    while True:
        p = pair_positions(inst)
        if p or time.time() - t0 >= timeout: return p
        time.sleep(step)

def order_detail(inst, coid):
    r = bf.private('GET', '/api/v1/trade/order-detail', {'instId': inst, 'clientOrderId': coid})
    d = r.get('data') if isinstance(r, dict) else None
    if isinstance(d, list): d = d[0] if d else None
    return d if str(r.get('code')) == '0' and isinstance(d, dict) and d else None

def resolve_uncertain_order(plan, tries=3, wait=2.0):
    """After a timeout/dropped connection on the order POST: was the order accepted?
    Returns ('accepted', evidence) | ('rejected', evidence) | ('unknown', evidence)."""
    inst = plan['instId']; coid = plan['order_req']['clientOrderId']; ev = []
    for i in range(tries):
        if i: time.sleep(wait)
        try:
            d = order_detail(inst, coid)
            if d:
                ev.append(dict(order_detail=d))
                if d.get('state') in ('canceled', 'order_failed') and float(d.get('filledSize') or 0) == 0: return 'rejected', ev
                return 'accepted', ev
        except Exception as e: ev.append(dict(order_detail_error=str(e)[:200]))
        try:
            p = pair_positions(inst)
            if p and (float(p[0]['positions']) > 0) == (plan['side'] == 'buy'):
                ev.append(dict(position=p)); return 'accepted', ev   # pair was flat before the order
        except Exception as e: ev.append(dict(positions_error=str(e)[:200]))
    return 'unknown', ev

def place(plan):
    inst = plan['instId']; side = plan['side']
    lr = bf.private('POST', '/api/v1/account/set-leverage', body=plan['lev_req'])
    if not bf.resp_ok(lr): return dict(ok=False, why=f'set-leverage failed: {bf.resp_err(lr)}', plan=plan)
    try:
        r = bf.private('POST', '/api/v1/trade/order', body=plan['order_req'])
        if not bf.resp_ok(r): return dict(ok=False, why=f'order rejected: {bf.resp_err(r)}', plan=plan, order_resp=r)
    except Exception as e:
        state, ev = resolve_uncertain_order(plan)
        if state != 'accepted':
            res = dict(ok=False, plan=plan, order_resp=dict(error=str(e)[:300], resolution=state, evidence=ev),
                       why=(f'order request failed ({str(e)[:120]}); order not found on the exchange' if state == 'rejected' else
                            f'ORDER OUTCOME UNKNOWN after {str(e)[:120]}: check the exchange for {plan["order_req"]["clientOrderId"]} now'))
            write_json('exec_last.json', res); return res
        r = dict(recovered=True, error=str(e)[:300], evidence=ev)
    # ---- order accepted: from here on the result is ok:true; problems become warnings ----
    out = dict(ok=True, accepted_at=now_utc().isoformat(), plan=plan, order_resp=r, warnings=[])
    if isinstance(r, dict) and r.get('recovered'): out['warnings'].append('order response was lost; acceptance confirmed by order lookup')
    write_json('exec_last.json', out)
    rec = dict(instId=inst, side=side, clientOrderId=plan['order_req']['clientOrderId'], opened_at=out['accepted_at'],
               entry_quote=plan['entry'], initial_stop=plan['stop'], target=plan['target'], stop_dist=plan['stop_dist'],
               atr1h=plan['atr1h'], equity_at_entry=plan.get('equity'), be_done=False, asset_class=plan.get('asset_class'))
    try: registry.put(rec)
    except Exception as e: out['warnings'].append(f'bot_positions.json not updated: {e}')
    p = []
    try:
        p = wait_position(inst)
        out['position'] = p
        if not p: out['warnings'].append('position not visible within 10s; check manually and add the trailing stop')
    except Exception as e: out['warnings'].append(f'position fetch failed: {str(e)[:200]}')
    if p:
        try:
            avg = float(p[0]['averagePrice']); out['avg'] = avg; out['liq'] = p[0].get('liquidationPrice')
            rec.update(avg=avg, stop_dist=abs(avg - plan['stop']), position_createTime=p[0].get('createTime')); registry.put(rec)
            Rd = abs(avg - plan['stop']) * M['trail_activate_r']
            act = float(q(avg + Rd if side == 'buy' else avg - Rd, plan['tick']))
            cb = float(q(M['trail_callback_atr'] * plan['atr1h'], plan['tick']))
            tr = trail_body(p[0], cb, act); out['trail_req'] = tr
            resp = bf.private('POST', '/api/v1/trade/order-algo', body=tr); out['trail_resp'] = resp
            if not bf.resp_ok(resp): out['warnings'].append(f'trailing stop rejected: {bf.resp_err(resp)}')
        except Exception as e: out['warnings'].append(f'trailing stop failed: {str(e)[:200]}')
    try:
        tp = bf.private('GET', '/api/v1/trade/orders-tpsl-pending', {'instId': inst}); out['tpsl_pending'] = tp
        if not bf.resp_ok(tp): out['warnings'].append(f'TP/SL check failed: {bf.resp_err(tp)}')
        elif not tp.get('data'): out['warnings'].append('no pending TP/SL found for the new position; add a stop now')
    except Exception as e: out['warnings'].append(f'TP/SL check failed: {str(e)[:200]}')
    try:
        al = bf.private('GET', '/api/v1/trade/orders-algo-pending', {'instId': inst, 'orderType': 'trailing_stop'}); out['algo_pending'] = al
        if not bf.resp_ok(al): out['warnings'].append(f'trailing stop check failed: {bf.resp_err(al)}')
    except Exception as e: out['warnings'].append(f'trailing stop check failed: {str(e)[:200]}')
    write_json('exec_last.json', out)
    return out

def run(setups, scan_ts, now=None, dry=False):
    """Try setups in order; open at most one (none with dry=True: the first passing plan is returned). Returns a result dict."""
    why = check_scan_fresh(scan_ts, now)
    if why: return dict(ok=False, why=why)
    equity, avail, pos, mode = account()
    try: registry.prune(pos)
    except Exception: pass
    if not equity > 0: return dict(ok=False, why='equity unreadable')
    if len(pos) >= MAXALL: return dict(ok=False, why='overall cap')
    if mode != 'net_mode': return dict(ok=False, why=f'account position mode {mode} != net_mode')
    ctx = dict(equity=equity, avail=avail, pos=pos, ins=instruments(), dry=dry, scan_ts=scan_ts, warnings=[])
    skipped = []
    for i, s in enumerate(setups):
        s = dict(s, _rank=i + 1)
        try: plan, why = evaluate(s, ctx)
        except Exception as e: plan, why = None, f'error: {str(e)[:200]}'
        if plan is None: skipped.append(dict(instId=s.get('instId'), why=why)); continue
        if dry: return dict(ok=True, dry=True, plan=plan, skipped=skipped, warnings=ctx['warnings'])
        out = place(plan); out['skipped'] = skipped
        if ctx['warnings']: out.setdefault('warnings', []).extend(ctx['warnings'])
        if out.get('ok') or out.get('order_resp') is not None: return out   # accepted, rejected or unknown: stop here
        skipped.append(dict(instId=s.get('instId'), why=out.get('why'))); continue  # leverage refused: try next
    return dict(ok=False, why='no setup passed the checks', skipped=skipped, warnings=ctx['warnings'], **(dict(dry=True) if dry else {}))

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--inst', help='only consider this instId from scan.json')
    ap.add_argument('--dry', action='store_true', help='run every check and print the plan; place nothing (still needs read keys)')
    a = ap.parse_args()
    bf.require_keys('execute.py')
    try:
        scan = _load_json(config.data_path('scan.json'))
        setups = scan.get('setups') or []
        if a.inst: setups = [s for s in setups if s.get('instId') == a.inst]
        with lock.exclusive():
            res = run(setups, scan.get('ts'), dry=a.dry) if setups else dict(ok=False, why='no matching setup in scan.json')
    except lock.LockBusy as e: res = dict(ok=False, why=str(e))
    except FileNotFoundError: res = dict(ok=False, why='scan.json not found; run scan.py first')
    except Exception as e: res = dict(ok=False, why=f'error: {str(e)[:300]}')
    res['finished_at'] = now_utc().isoformat()
    if not a.dry: write_json('exec_last.json', res)   # a dry run never overwrites the live result
    print(json.dumps(res, indent=1, default=str))
