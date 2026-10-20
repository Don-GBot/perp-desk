#!/usr/bin/env python3
"""Deterministic position-management helpers for the executor. Sends real orders. Needs API keys.

  python3 scripts/manage.py status                 # read-only: positions, stops, flags (start here)
  python3 scripts/manage.py ensure-stops           # bot trades: add missing stops; manual trades: alert only
  python3 scripts/manage.py hard-cut               # bot trades: tighten stops to the hard cut, close if breached
  python3 scripts/manage.py breakeven INST         # move stop to break-even if the trigger is reached
  python3 scripts/manage.py tighten INST PRICE     # move the stop closer (refuses to widen)
  python3 scripts/manage.py close INST [--force]   # market-close one position
  python3 scripts/manage.py cancel-tpsl INST       # cancel all pending TP/SL orders on INST
  python3 scripts/manage.py cancel-trail INST      # cancel the trailing stop(s) on INST

Output is one JSON object. Exit code 0 if the result is ok, 1 otherwise (2 for bad arguments).
Every action that changes something holds DATA_DIR/execute.lock (shared with execute.py).
Every exchange response is checked at the top level and in data.code.
Each run first prunes DATA_DIR/bot_positions.json of records that match no open position. Pruned records are looked up
in the positions history and logged to DATA_DIR/closed_trades.jsonl (closes.py: positionId first, else createTime within
120 s, one history row per record). Logging only, never an order:
  - funding.py writes funding_paid / funding_n on open bot records each run (logging.funding) and on closed-trade lines;
  - runlog.py appends one run record per invocation to DATA_DIR/runs.json (logging.run_records); its run_id is in the output.

Bot vs manual positions: a position is the bot's if
  (a) a bot_positions.json record MATCHES it (same pair and side, and the position's createTime equals
      the recorded one or is within 120 s of the recorded opened_at; pair + side alone never counts), or
  (b) the exchange order history has a FILLED, non-reduce-only opening order on that side whose
      clientOrderId starts with bot.client_order_prefix, created within 120 s of the position.
Only values from a matching record (initial_stop, target, stop_dist, equity_at_entry) are ever used.
With bot.protect_manual_trades: true (default), the hard cut, break-even, tighten and close only act
on bot positions; manual positions only get a missing-stop alert. `close --force` overrides for a human.

Hard cut: loss at the stop INCLUDING entry and exit taker fees may not exceed hard_cut_pct of the
equity at entry (from the record; wallet balance without unrealized PnL as fallback).

Endpoints (Blofin public API docs):
  POST /api/v1/trade/close-position   {instId, marginMode, positionSide, clientOrderId}
  POST /api/v1/trade/cancel-tpsl      [{instId, tpslId}]  (array body)
  POST /api/v1/trade/cancel-algo      {instId, algoId, orderType: "trailing_stop"}
  POST /api/v1/trade/order-tpsl       {instId, marginMode, positionSide, side, slTriggerPrice, slOrderPrice: "-1", tp..., size: "-1", reduceOnly}
  POST /api/v1/trade/order-algo       trailing stop, see execute.trail_body
"""
import os, sys, json, argparse
from decimal import ROUND_DOWN, ROUND_UP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blofin_client as bf
import config, lock, registry, closes, funding, runlog
import execute as EX
from scan import candles, atr

C = config.CFG; M = C["management"]; PREFIX = C["bot"]["client_order_prefix"]
PROTECT_MANUAL = bool(C["bot"]["protect_manual_trades"])
HARD_CUT = C["risk"]["hard_cut_pct"] / 100.0
FEE = EX.FEE
WINDOW_MS = registry.MATCH_WINDOW_MS

def _get(path, params=None):
    r = bf.private('GET', path, params)
    if str(r.get('code')) != '0': raise RuntimeError(f'GET {path} failed: {bf.resp_err(r)}')
    return r.get('data') or []

def positions():
    return [p for p in _get('/api/v1/account/positions') if float(p.get('positions') or 0) != 0]

def position(inst):
    p = [x for x in _get('/api/v1/account/positions', {'instId': inst}) if float(x.get('positions') or 0) != 0]
    return p[0] if p else None

def pending_tpsl(inst):
    return _get('/api/v1/trade/orders-tpsl-pending', {'instId': inst})

def pending_trails(inst):
    return _get('/api/v1/trade/orders-algo-pending', {'instId': inst, 'orderType': 'trailing_stop'})

def is_long(p): return float(p['positions']) > 0

def _instrument(inst):
    return [x for x in bf.public('/api/v1/market/instruments', {'instId': inst})['data'] if x['instId'] == inst][0]

# ---- ownership ----------------------------------------------------------------------------------
def history_says_bot(p):
    """Fallback (b): a filled, non-reduce-only opening order with the bot prefix near the position's creation."""
    try: ct = int(p.get('createTime') or 0)
    except Exception: return False
    if not ct: return False
    open_side = 'buy' if is_long(p) else 'sell'
    try: hist = _get('/api/v1/trade/orders-history', {'instId': p['instId'], 'limit': 100})
    except Exception: return False
    for o in hist:
        if not str(o.get('clientOrderId') or '').startswith(PREFIX): continue
        if o.get('side') != open_side or o.get('state') not in ('filled', 'partially_filled'): continue
        if str(o.get('reduceOnly', 'false')).lower() == 'true': continue
        if float(o.get('filledSize') or 0) <= 0 and o.get('state') != 'filled': continue
        try: ot = int(o.get('createTime') or 0)
        except Exception: continue
        if abs(ot - ct) <= WINDOW_MS: return True
    return False

def owner(p, reg=None):
    """Returns (is_bot, matching_record_or_None)."""
    rec = registry.match(p, reg)
    if rec: return True, rec
    return history_says_bot(p), None

def is_bot_position(p, reg=None):
    return owner(p, reg)[0]

def _stop_of(tpsl_list):
    s = [float(t['slTriggerPrice']) for t in tpsl_list if t.get('slTriggerPrice') not in (None, '', '0')]
    return s[0] if s else None

def _tp_of(tpsl_list):
    s = [float(t['tpTriggerPrice']) for t in tpsl_list if t.get('tpTriggerPrice') not in (None, '', '0')]
    return s[0] if s else None

def _ok(r): return dict(r, ok=r.get('ok', not r.get('errors')))

# ---- primitive actions (callers hold the lock) -------------------------------------------------
def cancel_tpsl(inst, tpsl_ids=None):
    ids = tpsl_ids or [t['tpslId'] for t in pending_tpsl(inst)]
    if not ids: return dict(ok=True, cancelled=[], note='nothing to cancel')
    r = bf.private('POST', '/api/v1/trade/cancel-tpsl', body=[dict(instId=inst, tpslId=str(i)) for i in ids])
    return dict(ok=bf.resp_ok(r), cancelled=ids if bf.resp_ok(r) else [], resp=r, why=None if bf.resp_ok(r) else bf.resp_err(r))

def cancel_trail(inst, algo_ids=None):
    ids = algo_ids or [a['algoId'] for a in pending_trails(inst)]
    out = dict(ok=True, cancelled=[], errors=[])
    for i in ids:
        r = bf.private('POST', '/api/v1/trade/cancel-algo', body=dict(instId=inst, algoId=str(i), orderType='trailing_stop'))
        if bf.resp_ok(r): out['cancelled'].append(i)
        else: out['ok'] = False; out['errors'].append(f'{i}: {bf.resp_err(r)}')
    return out

def place_tpsl(p, sl=None, tp=None):
    b = dict(instId=p['instId'], marginMode=p['marginMode'], positionSide=p.get('positionSide') or 'net',
             side='sell' if is_long(p) else 'buy', size='-1', reduceOnly='true',
             clientOrderId=bf.client_order_id(PREFIX, 's'))
    if sl is not None: b.update(slTriggerPrice=EX.fs(sl), slOrderPrice='-1')
    if tp is not None: b.update(tpTriggerPrice=EX.fs(tp), tpOrderPrice='-1')
    try: r = bf.private('POST', '/api/v1/trade/order-tpsl', body=b)
    except Exception as e: return dict(ok=False, req=b, resp=None, why=f'request failed: {str(e)[:200]}')
    return dict(ok=bf.resp_ok(r), req=b, resp=r, why=None if bf.resp_ok(r) else bf.resp_err(r))

def place_trail(p, callback, active=None):
    b = EX.trail_body(p, callback, active)
    r = bf.private('POST', '/api/v1/trade/order-algo', body=b)
    return dict(ok=bf.resp_ok(r), req=b, resp=r, why=None if bf.resp_ok(r) else bf.resp_err(r))

def replace_stop(p, new_sl, keep_tp=True):
    """Cancel the old TP/SL first, then place the new stop (keeping the old target).
    If the cancel fails, nothing is placed. If the new order fails, restore the old stop/target."""
    inst = p['instId']; old = pending_tpsl(inst); old_sl = _stop_of(old); old_tp = _tp_of(old) if keep_tp else None
    out = dict(instId=inst, old_stop=old_sl, old_target=old_tp, new_stop=new_sl)
    c = cancel_tpsl(inst, [t['tpslId'] for t in old]) if old else dict(ok=True, cancelled=[])
    out['cancel'] = c
    if not c['ok']: out.update(ok=False, why=f'could not cancel old TP/SL: {c.get("why")}; nothing placed'); return out
    pl = place_tpsl(p, sl=new_sl, tp=old_tp); out['place'] = pl
    if pl['ok']: out['ok'] = True; return out
    out['ok'] = False; out['why'] = f'new stop rejected: {pl["why"]}'
    if old_sl is not None or old_tp is not None:
        rs = place_tpsl(p, sl=old_sl, tp=old_tp); out['restore'] = rs
        if not rs['ok']: out['why'] += f'; RESTORE FAILED ({rs["why"]}): POSITION IS UNPROTECTED'; out['unprotected'] = True
    else:
        out['why'] += '; there was no old stop: POSITION IS UNPROTECTED'; out['unprotected'] = True
    return out

def close_position(inst, force=False, reg=None):
    p = position(inst)
    if not p: return dict(ok=False, instId=inst, why='no open position')
    if PROTECT_MANUAL and not force and not is_bot_position(p, reg):
        return dict(ok=False, instId=inst, why='manual position (protect_manual_trades); use --force only on the owner\'s instruction')
    b = dict(instId=inst, marginMode=p['marginMode'], positionSide=p.get('positionSide') or 'net',
             clientOrderId=bf.client_order_id(PREFIX, 'c'))
    r = bf.private('POST', '/api/v1/trade/close-position', body=b)
    out = dict(ok=bf.resp_ok(r), instId=inst, req=b, resp=r, why=None if bf.resp_ok(r) else bf.resp_err(r))
    if out['ok']:
        try: cancel_trail(inst)  # the exchange usually cancels it; this is a cleanup
        except Exception: pass
        rg = registry.load()
        if inst in rg: rg.pop(inst); registry.save(rg)
    return out

def breakeven_price(p, tick):
    avg = float(p['averagePrice']); buf = M['breakeven_buffer_pct'] / 100.0
    if is_long(p): return float(EX.q(avg * (1 + buf), tick, ROUND_UP))
    return float(EX.q(avg * (1 - buf), tick, ROUND_DOWN))

def _past(long_, stop, mark):
    """True if the mark is already at or beyond the stop (the stop would trigger instantly)."""
    return (long_ and mark <= stop) or (not long_ and mark >= stop)

def move_stop_to_breakeven(inst, check_trigger=True, reg=None):
    p = position(inst)
    if not p: return dict(ok=False, instId=inst, why='no open position')
    bot, rec = owner(p, reg)
    if PROTECT_MANUAL and not bot: return dict(ok=False, instId=inst, why='manual position: break-even not applied (protect_manual_trades)')
    tick = _instrument(inst)['tickSize']; long_ = is_long(p)
    avg = float(p['averagePrice']); mark = float(p.get('markPrice') or 0)
    old_sl = _stop_of(pending_tpsl(inst)); be = breakeven_price(p, tick)
    if old_sl is not None and ((long_ and old_sl >= be) or (not long_ and old_sl <= be)):
        return dict(ok=True, instId=inst, note=f'stop {old_sl:g} already at or past break-even {be:g}')
    if check_trigger:
        dist = (rec or {}).get('stop_dist') or (abs(avg - old_sl) if old_sl else None)
        if not dist: return dict(ok=False, instId=inst, why='initial stop distance unknown; pass --no-check to force')
        trig = avg + M['breakeven_at_r'] * dist if long_ else avg - M['breakeven_at_r'] * dist
        if (long_ and mark < trig) or (not long_ and mark > trig):
            return dict(ok=True, instId=inst, note=f'not yet: mark {mark:g}, trigger {trig:g}')
    if _past(long_, be, mark): return dict(ok=False, instId=inst, why=f'mark {mark:g} is already through break-even {be:g}')
    out = replace_stop(p, be, keep_tp=True)
    if not out['ok']: return out
    try:
        a1 = atr(candles(inst, '1H'))
        ct = cancel_trail(inst); out['trail_cancel'] = ct
        if ct['ok']:
            out['trail_place'] = place_trail(p, float(EX.q(M['breakeven_trail_atr'] * a1, tick)))
            if not out['trail_place']['ok']: out['warning'] = f'trailing stop not replaced: {out["trail_place"]["why"]}'
        else: out['warning'] = f'old trailing stop not cancelled, new one not placed: {ct["errors"]}'
    except Exception as e: out['warning'] = f'trail step failed: {str(e)[:200]}'
    if rec:
        rg = registry.load()
        if inst in rg: rg[inst]['be_done'] = True; registry.save(rg)
    return out

def tighten_stop(inst, price, reg=None):
    """Move a bot position's stop to `price` only if that is tighter than the current stop."""
    p = position(inst)
    if not p: return dict(ok=False, instId=inst, why='no open position')
    if PROTECT_MANUAL and not is_bot_position(p, reg): return dict(ok=False, instId=inst, why='manual position (protect_manual_trades)')
    long_ = is_long(p); mark = float(p.get('markPrice') or 0)
    tick = _instrument(inst)['tickSize']; new = float(EX.q(price, tick, ROUND_UP if long_ else ROUND_DOWN))
    if _past(long_, new, mark): return dict(ok=False, instId=inst, why=f'stop {new:g} is on the wrong side of mark {mark:g}')
    old = _stop_of(pending_tpsl(inst))
    if old is not None and ((long_ and new <= old) or (not long_ and new >= old)):
        return dict(ok=False, instId=inst, why=f'refused: {new:g} would widen or not change the stop {old:g}')
    return replace_stop(p, new, keep_tp=True)

# ---- hard cut -----------------------------------------------------------------------------------
def wallet_balance():
    d = [x for x in (bf.private('GET', '/api/v1/account/balance').get('data') or {}).get('details', []) if x.get('currency') == 'USDT']
    if not d: raise RuntimeError('balance unreadable')
    return float(d[0].get('balance') or d[0]['equity'])   # cash balance excludes unrealized PnL

def hard_cut_distance(avg, coin, long_, equity, hc=HARD_CUT, fee=FEE):
    """Price distance d from avg so that loss at the stop incl. entry + exit taker fees = hc x equity.
    long: coin*d + fee*coin*(avg + avg - d) = L  ->  d = (L/coin - 2*fee*avg) / (1 - fee)
    short: coin*d + fee*coin*(avg + avg + d) = L ->  d = (L/coin - 2*fee*avg) / (1 + fee)"""
    L = hc * equity
    return (L / coin - 2 * fee * avg) / ((1 - fee) if long_ else (1 + fee))

def loss_at(avg, stop, coin, fee=FEE):
    return coin * abs(avg - stop) + fee * coin * (avg + stop)

def _cut_basis(rec, wallet):
    return float(rec['equity_at_entry']) if rec and rec.get('equity_at_entry') else wallet

def hard_cut(reg=None):
    reg = registry.load() if reg is None else reg
    wallet = wallet_balance(); out = dict(wallet_balance=wallet, tightened=[], closed=[], skipped_manual=[], errors=[])
    for p in positions():
        inst = p['instId']; bot, rec = owner(p, reg)
        if PROTECT_MANUAL and not bot: out['skipped_manual'].append(inst); continue
        ins = _instrument(inst); tick = ins['tickSize']; long_ = is_long(p)
        coin = abs(float(p['positions'])) * float(ins['contractValue'])
        avg = float(p['averagePrice']); mark = float(p.get('markPrice') or avg)
        basis = _cut_basis(rec, wallet); limit = HARD_CUT * basis
        open_loss = loss_at(avg, mark, coin) if _past(long_, avg, mark) else 0.0   # loss incl. fees if closed now
        d = hard_cut_distance(avg, coin, long_, basis)
        if open_loss >= limit or d <= 0:
            r = close_position(inst, reg=reg)
            (out['closed'] if r['ok'] else out['errors']).append(dict(instId=inst, loss_now=round(open_loss, 4), limit=round(limit, 4), why=r.get('why') or 'HARD CUT BREACHED: closed'))
            continue
        hc = float(EX.q(avg - d if long_ else avg + d, tick, ROUND_UP if long_ else ROUND_DOWN))
        if _past(long_, hc, mark):
            r = close_position(inst, reg=reg)
            (out['closed'] if r['ok'] else out['errors']).append(dict(instId=inst, hard_cut_stop=hc, mark=mark, why=r.get('why') or 'mark already past the hard-cut stop: closed'))
            continue
        sl = _stop_of(pending_tpsl(inst))
        if sl is None or (long_ and sl < hc) or (not long_ and sl > hc):   # tighten only, never widen
            r = replace_stop(p, hc, keep_tp=True)
            (out['tightened'] if r['ok'] else out['errors']).append(dict(instId=inst, old=sl, new=hc, basis=basis, why=r.get('why')))
    return _ok(out)

def ensure_stops(reg=None):
    reg = registry.load() if reg is None else reg
    out = dict(placed=[], closed=[], alerts=[], errors=[]); wallet = None
    for p in positions():
        inst = p['instId']
        if _stop_of(pending_tpsl(inst)) is not None: continue
        bot, rec = owner(p, reg)
        if PROTECT_MANUAL and not bot: out['alerts'].append(f'{inst}: manual position has NO STOP'); continue
        if wallet is None: wallet = wallet_balance()
        ins = _instrument(inst); tick = ins['tickSize']; long_ = is_long(p)
        coin = abs(float(p['positions'])) * float(ins['contractValue']); avg = float(p['averagePrice'])
        mark = float(p.get('markPrice') or avg)
        d = hard_cut_distance(avg, coin, long_, _cut_basis(rec, wallet))
        hc = avg - d if long_ else avg + d
        init = rec.get('initial_stop') if rec else None          # only from a matching record
        cand = [x for x in (init, hc) if x is not None]
        sl = max(cand) if long_ else min(cand)                     # the tighter of initial stop and hard cut
        sl = float(EX.q(sl, tick, ROUND_UP if long_ else ROUND_DOWN))
        if d <= 0 or _past(long_, sl, mark):
            r = close_position(inst, reg=reg)
            entry = dict(instId=inst, stop=sl, mark=mark, why=f'NO STOP and mark already past the stop {sl:g}: CLOSED AT MARKET' if r['ok'] else r.get('why'))
            (out['closed'] if r['ok'] else out['errors']).append(entry); continue
        r = place_tpsl(p, sl=sl, tp=rec.get('target') if rec else None)
        (out['placed'] if r['ok'] else out['errors']).append(dict(instId=inst, stop=sl, why=r.get('why')))
    return _ok(out)

def status(reg=None):
    reg = registry.load() if reg is None else reg
    wallet = wallet_balance(); out = dict(ok=True, wallet_balance=wallet, hard_cut_pct=C['risk']['hard_cut_pct'], positions=[])
    for p in positions():
        inst = p['instId']; tp = pending_tpsl(inst); sl = _stop_of(tp); bot, rec = owner(p, reg)
        avg = float(p['averagePrice']); mark = float(p.get('markPrice') or 0); upnl = float(p.get('unrealizedPnl') or 0)
        long_ = is_long(p); flags = []
        if sl is None: flags.append('MISSING_STOP')
        try:
            ins = _instrument(inst); coin = abs(float(p['positions'])) * float(ins['contractValue'])
            if _past(long_, avg, mark) and loss_at(avg, mark, coin) >= HARD_CUT * _cut_basis(rec, wallet): flags.append('HARD_CUT_BREACHED')
        except Exception: pass
        dist = rec.get('stop_dist') if rec else None
        if bot and dist and not rec.get('be_done'):
            trig = avg + M['breakeven_at_r'] * dist if long_ else avg - M['breakeven_at_r'] * dist
            if (long_ and mark >= trig) or (not long_ and mark <= trig): flags.append('BREAKEVEN_DUE')
        try: trails = len(pending_trails(inst))
        except Exception: trails = None
        out['positions'].append(dict(instId=inst, side='long' if long_ else 'short', owner='bot' if bot else 'manual',
                                     record=bool(rec), marginMode=p['marginMode'], avg=avg, mark=mark, upnl=upnl, stop=sl,
                                     target=_tp_of(tp), trailing_stops=trails, flags=flags))
    return out

def exit_code(res):
    return 0 if isinstance(res, dict) and res.get('ok') else 1

LG = C["logging"]

def bookkeeping(removed, reg):
    """Closed-trade lines + funding + nothing else. Never raises; returns a dict for the output."""
    out = dict(closes=[], funding_errors=[], errors=[])
    try:
        lines, errs = closes.record(removed, _get, funding_get=_get if LG['funding'] else None)
        out['closes'] = [dict(instId=x['instId'], positionId=x.get('positionId'), realized_pnl=x.get('realized_pnl')) for x in lines]
        out['errors'] += errs
    except Exception as e: out['errors'].append(f'close log failed: {str(e)[:160]}')
    if LG['funding'] and reg:
        n, errs = funding.update(reg, _get); out['funding_errors'] = errs
        if n:
            try:
                cur = registry.load()
                for k, v in reg.items():
                    if k in cur: cur[k].update({f: v[f] for f in ('funding_paid', 'funding_n', 'funding_bills', 'funding_ts') if f in v})
                registry.save(cur)
            except Exception as e: out['funding_errors'].append(f'funding save failed: {str(e)[:160]}')
    return out

def log_run(cmd, res, book):
    if not LG['run_records']: return None
    rid = runlog.new_run_id()
    try:
        runlog.append(dict(ts_utc=runlog.now_z(), run_id=rid, source=runlog.SOURCE, cmd=cmd, ok=bool(res.get('ok')),
                           pruned_records=res.get('pruned_records'), closes=book.get('closes'),
                           funding_errors=book.get('funding_errors'), errors=book.get('errors')))
        return rid
    except Exception as e:
        res.setdefault('warnings', []).append(f'run record not written: {str(e)[:160]}'); return None

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('cmd', choices=['status', 'ensure-stops', 'hard-cut', 'breakeven', 'tighten', 'close', 'cancel-tpsl', 'cancel-trail'])
    ap.add_argument('inst', nargs='?')
    ap.add_argument('price', nargs='?', type=float, help='tighten: new stop price')
    ap.add_argument('--force', action='store_true', help='close: allow closing a manual position')
    ap.add_argument('--no-check', action='store_true', help='breakeven: skip the trigger-price check')
    a = ap.parse_args()
    bf.require_keys('manage.py')
    if a.cmd in ('breakeven', 'tighten', 'close', 'cancel-tpsl', 'cancel-trail') and not a.inst:
        print(f'{a.cmd} needs an instId, e.g. BTC-USDT', file=sys.stderr); sys.exit(2)
    if a.cmd == 'tighten' and a.price is None:
        print('tighten needs a price: manage.py tighten BTC-USDT 80000', file=sys.stderr); sys.exit(2)
    book = {}
    try:
        if a.cmd == 'status':
            try:
                with lock.exclusive():
                    reg, removed = registry.prune_full(positions()); pruned = sorted(removed); book = bookkeeping(removed, reg)
            except lock.LockBusy: reg, pruned = registry.load(), []
            res = status(reg); res['pruned_records'] = pruned
        else:
            with lock.exclusive():
                reg, removed = registry.prune_full(positions()); pruned = sorted(removed); book = bookkeeping(removed, reg)
                actions = {
                    'ensure-stops': lambda: ensure_stops(reg),
                    'hard-cut': lambda: hard_cut(reg),
                    'breakeven': lambda: move_stop_to_breakeven(a.inst, check_trigger=not a.no_check, reg=reg),
                    'tighten': lambda: tighten_stop(a.inst, a.price, reg=reg),
                    'close': lambda: close_position(a.inst, force=a.force, reg=reg),
                    'cancel-tpsl': lambda: cancel_tpsl(a.inst),
                    'cancel-trail': lambda: cancel_trail(a.inst),
                }
                res = actions[a.cmd](); res['pruned_records'] = pruned
    except lock.LockBusy as e: res = dict(ok=False, why=str(e))
    except Exception as e: res = dict(ok=False, why=f'error: {str(e)[:300]}')
    if book: res['bookkeeping'] = book
    rid = log_run(a.cmd, res, book)
    if rid: res['run_id'] = rid
    print(json.dumps(res, indent=1, default=str))
    sys.exit(exit_code(res))

if __name__ == '__main__':
    main()
