"""Risk-based position caps (config.yaml -> risk.risk_caps). Used by execute.py.
Fail closed: if any open position's risk can't be computed, callers fall back to the slot caps (risk.max_open /
risk.max_per_direction).

Open risk of a position (USDT) = loss if its CURRENT stop is hit, from the live mark, incl. estimated exit costs:
    coins x max(0, (mark - stop) long | (stop - mark) short) + coins x stop x taker_rate + coins x mark x slip_frac
  = 0 only if a stop-out nets >= 0 after the entry taker fee + exit taker fee + slippage (be_net).
  Missing mark -> not computable; stop orders covering only part of the size -> not computable.
  Manual position with no stop -> manual_no_stop_pct of equity; BOT position with no stop -> not computable.
  Several stop orders on a pair -> the LOOSEST one counts (conservative).
  A bot record on the pair that doesn't match the live position (ambiguous ownership) -> not computable.
Caps: all open risk + new <= total_pct of equity; same-direction BOT open risk + new <= direction_pct of equity;
hard max max_positions open positions (checked by the caller).
"""
import config

RC = config.CFG["risk"]["risk_caps"]
ENABLED = bool(RC["enabled"])
TOTAL_CAP = RC["total_pct"] / 100.0; DIR_RISK_CAP = RC["direction_pct"] / 100.0
MAX_POS = int(RC["max_positions"]); MANUAL_NOSTOP = RC["manual_no_stop_pct"] / 100.0

def be_net(long_, avg, stop, taker, slip):
    """True if a stop-out at `stop` nets >= 0 after the entry taker fee and the exit taker fee + slip:
    long stop*(1-taker-slip) >= avg*(1+taker); short stop*(1+taker+slip) <= avg*(1-taker)."""
    return stop * (1 - taker - slip) >= avg * (1 + taker) if long_ else stop * (1 + taker + slip) <= avg * (1 - taker)

def pos_risk(long_, coins, avg, mark, stop, equity, manual, taker, slip):
    if stop is None:
        if manual: return MANUAL_NOSTOP * equity
        raise ValueError('bot position without a stop')
    if coins <= 0 or not avg or not mark: raise ValueError('size/price unknown')
    if be_net(long_, avg, stop, taker, slip): return 0.0
    dist = (mark - stop) if long_ else (stop - mark)
    return max(0.0, coins * dist) + coins * stop * taker + coins * mark * slip

def sl_coverage(rows, size):
    """True if the stop rows cover the whole position: any size '-1'/missing/0 = full; else sum(size) >= position size."""
    tot = 0.0
    for o in rows or []:
        try: v = float(o.get('size') if o.get('size') not in (None, '') else -1)
        except Exception: v = -1
        if v <= 0: return True
        tot += v
    return tot >= size - 1e-12

def loosest_stop(rows, long_):
    s = []
    for o in rows or []:
        try:
            v = float(o.get('slTriggerPrice') or 0)
            if v > 0: s.append(v)
        except Exception: pass
    return (min(s) if long_ else max(s)) if s else None

def assess(side, positions, tpsl_rows, cv_of, equity, is_bot, taker, slip, bot_record=None):
    """side 'buy'|'sell'. positions = live non-zero positions; tpsl_rows = pending TP/SL orders (all pairs); cv_of(inst) -> float;
    is_bot(p) -> bool; bot_record(inst) -> True if a bot record exists for the pair. Returns dict(mode='risk'|'slots', n_pos,
    total_open, dir_open, headroom, head_total, head_dir, rows, error). Never raises."""
    want_long = side == 'buy'; rows = []
    out = dict(mode='risk', n_pos=len(positions), max_pos=MAX_POS, error=None)
    try:
        if not ENABLED: raise ValueError('risk_caps disabled')
        if not equity or equity <= 0: raise ValueError('equity unknown')
        if tpsl_rows is None: raise ValueError('tpsl orders unknown')
        tot = 0.0; dr = 0.0
        for p in positions:
            q = float(p['positions']); lg = q > 0; inst = p['instId']; bot = bool(is_bot(p))
            if not bot and bot_record is not None and bot_record(inst):
                raise ValueError(f'{inst}: bot record does not match the live position (ambiguous ownership)')
            srows = [o for o in tpsl_rows if o.get('instId') == inst and o.get('slTriggerPrice') not in (None, '', '0')]
            stop = loosest_stop(srows, lg)
            if stop is not None and not sl_coverage(srows, abs(q)):
                raise ValueError(f'{inst}: stop orders cover only part of the position')
            avg = float(p.get('averagePrice') or 0); mark = float(p.get('markPrice') or 0)
            if not mark: raise ValueError(f'{inst}: mark price missing')
            r = pos_risk(lg, abs(q) * float(cv_of(inst)), avg, mark, stop, equity, not bot, taker, slip)
            tot += r
            if bot and lg == want_long: dr += r
            rows.append(dict(instId=inst, side='long' if lg else 'short', bot=bot, stop=stop, mark=mark, risk=round(r, 4)))
        ht = TOTAL_CAP * equity - tot; hd = DIR_RISK_CAP * equity - dr
        out.update(total_open=round(tot, 4), dir_open=round(dr, 4), head_total=round(ht, 4), head_dir=round(hd, 4),
                   headroom=max(0.0, min(ht, hd)), rows=rows,
                   total_pct=round(tot / equity * 100, 3), dir_pct=round(dr / equity * 100, 3))
    except Exception as e:
        out.update(mode='slots', error=f'{type(e).__name__}: {str(e)[:160]}', rows=rows)
    return out

def summary(a):
    if a.get('mode') != 'risk': return f"slot-cap fallback ({a.get('error')})"
    return (f"open risk total {a['total_open']:.2f} ({a['total_pct']:.2f}%) / cap {TOTAL_CAP*100:g}%, same-dir bot {a['dir_open']:.2f} "
            f"({a['dir_pct']:.2f}%) / cap {DIR_RISK_CAP*100:g}%, headroom {a['headroom']:.2f} USDT, positions {a['n_pos']}/{MAX_POS}")
