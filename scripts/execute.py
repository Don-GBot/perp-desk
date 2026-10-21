#!/usr/bin/env python3
"""Executor order script: checks setups from scan.json and opens AT MOST ONE trade. Sends real orders.

  python3 scripts/execute.py                 # try setups in scan.json, best composite score first
  python3 scripts/execute.py --inst SOL-USDT # only this pair's setup
  python3 scripts/execute.py --dry           # run every check, print the plan, place nothing

Needs BLOFIN_API_KEY / BLOFIN_API_SECRET / BLOFIN_PASSPHRASE. Holds DATA_DIR/execute.lock while running.

Per run:
  - Reject the whole scan if scan.json is older than entry.max_scan_age_min.
  - Account checks once: equity readable, max positions (risk_caps.max_positions), account in net (one-way) mode.
  - Every setup goes through the checks below (no order); a failing one is skipped with a reason:
    side valid; no position on the pair; not a stock perp (entry.stock_perp_block, stockperp.py; fails closed);
    universe tier: an 'ext' setup (volume rank beyond scan.universe_size) needs a fresh news-analyst rating, else
    no_fundamentals (universe.py; fails closed); re-entry cooldown; caps (risk caps, or the slot caps as fallback);
    no unresolved bot record on the pair (entry.unresolved_record_block); stop/target rounded to tick and
    stop < entry < target (long) or target < entry < stop (short) at the live price;
    R:R (price-only: (target - entry) / (entry - stop)) >= min_rr; stop >= ATR floor;
    rr_cap: R:R <= entry.rr_cap_max (8); cost_heavy: fee_share < entry.cost_heavy_max_share (0.12); 4H trend re-checked now;
    1H trend aligned with the side (entry.trend_1h, trend1h.py; flat blocks both sides, a data error skips);
    sizing (fit_size): scaled down to the binding limit, skip fee_heavy / below_min_size / liq / cap_risk.
  - Ranking (entry.composite, on): the passing setups are scored by composite_score and tried best score first;
    each one is re-checked at the live price just before its order. composite off: scan order (R:R).
  - First setup that passes: set leverage, market order with attached TP/SL, then a trailing stop. A failed re-check or a
    refused leverage falls through to the next candidate; at most one entry per run.
Sizing: risk_per_trade_pct is a MAXIMUM. A clean stop-out of a planned entry loses about risk_per_trade_pct of equity
INCLUDING entry slippage, entry and exit taker fees and an exit slippage buffer: loss per coin = |entry - stop| +
entry_slip_frac x entry + taker_rate x (entry + stop) + slip_frac x entry. With risk.scale_down the size is then cut
(never raised) to fit leverage.max / the instrument max, the margin cap (25% equity, 95% free margin), the highest
leverage whose estimated liquidation is > 2x the stop distance, and the risk-cap headroom (fit_size).
Caps (risk.risk_caps, riskcap.py): total open risk + new <= total_pct of equity, same-direction bot open risk + new <=
direction_pct, max_positions open positions. If any position's risk can't be computed, the slot caps apply
(max_open overall, max_per_direction bot positions per side). Re-entry cooldown: no entry on a pair+side within
risk.cooldown_hours of a losing bot stop / trail / hard-cut close (closes.cooldown_check). Any check failing to read is
a skip, never a pass.

Once an order is accepted the result is ok:true, written to DATA_DIR/exec_last.json right away, and any
later problem (position not visible yet, trailing stop rejected) is reported under "warnings".
New entries are recorded in DATA_DIR/bot_positions.json so manage.py can tell bot trades from manual ones.

The news gate (news_gate.check) and, when enabled, the macro window (news_gate.macro_check) are
applied inside evaluate() to every candidate, so falling through to the next setup can never open a
news-blocked trade.

Every per-setup skip is logged to DATA_DIR/blocked_setups.jsonl (blocklog.py, source "execute"; with logging.log_all_skips,
otherwise only the news / 1h / stock-perp / universe / sizing gates); --dry lines go to
DATA_DIR/logs/blocked_setups_dry.jsonl, except the scheduled routine's dry decision run (mode "scheduled_dry", live
file; blocklog.scheduled_dry). A log failure only adds a warning. Run-level exits (stale scan, equity unreadable, max
positions, not net mode) end the run before any setup is walked: they return a gate_reason and write no block line.

If the order request times out or the connection drops, the script looks the order up by its
clientOrderId (and checks positions) before deciding it failed.
"""
import os, sys, json, math, time, argparse, datetime as dt
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blofin_client as bf
import config, lock, registry, news_gate
import trend1h as T1, stockperp as SP, blocklog as BL, closes
import universe as UN, riskcap as RC

def _load_json(path):
    with open(path) as fh:
        return json.load(fh)
from scan import candles, atr, trend, ema, rr as price_rr, net_rr

C = config.CFG; R_ = C["risk"]; E = C["entry"]; LV = C["leverage"]; M = C["management"]
RISK = R_["risk_per_trade_pct"] / 100.0
FEE = float(C["fees"]["taker_rate"]); SLIP = float(C["fees"]["slip_frac"]); ENTRY_SLIP = float(C["fees"]["entry_slip_frac"])
SCALE_DOWN = bool(R_["scale_down"]); FEE_HEAVY_MAX = float(R_["fee_heavy_max_share"]); MIN_RISK_PCT = float(R_["min_risk_pct"])
UNRES_BLOCK = bool(E["unresolved_record_block"]); LOG_ALL_SKIPS = bool(C["logging"]["log_all_skips"])
COOLDOWN_H = float(R_["cooldown_hours"])
PREFIX = C["bot"]["client_order_prefix"]
MAXDIR = int(R_["max_per_direction"]); MAXALL = int(R_["max_open"]); MINRR = float(E["min_rr"])
ATR_FLOOR = float(E["min_stop_atr"]) * float(E["atr_tolerance"])
MAX_AGE_MIN = float(E["max_scan_age_min"])
NEW_MM = LV["margin_mode"]
LEVMIN = int(LV["min"]); LEVMAX = int(LV["max"]); MARGIN_CAP = LV["margin_cap_pct"] / 100.0
FREE_BUF = float(LV["free_margin_buffer"]); MMR_DEFAULT = float(LV["mmr_default"]); MAX_STOP_TO_LIQ = float(LV["max_stop_to_liq"])
TREND1H = T1.ENABLED; STOCKPERP = bool(E["stock_perp_block"]["enabled"])
RR_CAP = float(E["rr_cap_max"]); COST_HEAVY_MAX = float(E["cost_heavy_max_share"])   # 0 = off
CP = E["composite"]; COMPOSITE = bool(CP["enabled"])
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

def risk_per_coin(entry, stop, fee=FEE, slip=SLIP, entry_slip=0.0):
    """Loss of one coin at the stop: price distance + taker fee on entry and exit + exit slippage buffer
    (+ entry slippage for a planned entry, see planned_risk_per_coin)."""
    return abs(entry - stop) + entry_slip * entry + fee * (entry + stop) + slip * entry

def planned_risk_per_coin(entry, stop):
    """Loss of one coin of a PLANNED market entry at the quote, stopped out: adds fees.entry_slip_frac x entry."""
    return risk_per_coin(entry, stop, FEE, SLIP, ENTRY_SLIP)

def loss_per_contract(entry, stop, cv, fee=FEE, slip=SLIP, entry_slip=None):
    """Loss of one contract of a planned entry at the stop, incl. entry slippage, taker fees and the exit slippage buffer."""
    return cv * risk_per_coin(entry, stop, fee, slip, ENTRY_SLIP if entry_slip is None else entry_slip)

def size_for(equity, entry, stop, cv, lot, fee=FEE, slip=SLIP):
    """The full-risk (risk_per_trade_pct) size in contracts, lot-rounded DOWN."""
    per = loss_per_contract(entry, stop, cv, fee, slip)
    return float(q((equity * RISK) / per, lot, ROUND_DOWN)) if per > 0 else 0.0

def fee_share(entry, stop):
    """Round-trip taker fees / stop-out loss, per coin (size cancels). The denominator deliberately leaves out the
    entry-slip term (|entry - stop| + taker x (entry + stop) + slip_frac x entry). Deliberate: the threshold keeps its
    meaning from before entry slippage was added to sizing."""
    return FEE * (entry + stop) / risk_per_coin(entry, stop)

def filter_gate(entry, stop, rr):
    """(gate_reason, why) for the rr_cap / cost_heavy hard filters, else (None, None).
    rr_cap: planned price R:R above entry.rr_cap_max. cost_heavy: fee_share (round-trip taker fees / stop-out loss, the
    same share as fee_heavy) >= entry.cost_heavy_max_share. Applies to every trade, scaled or not."""
    if RR_CAP > 0 and rr > RR_CAP: return 'rr_cap', f'rr_cap: planned R:R {rr:.2f} > {RR_CAP:g}'
    if COST_HEAVY_MAX > 0:
        c = fee_share(entry, stop)
        if c >= COST_HEAVY_MAX: return 'cost_heavy', f'cost_heavy: round-trip fees {c*100:.1f}% of stop-out loss >= {COST_HEAVY_MAX*100:g}%'
    return None, None

def _pos_finite(x):
    try: return math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError): return False

def composite_score(side, entry, stop, target, atr1h, ema_fast4h, ema_slow4h, btc_trend4h):
    """Composite rank score (higher = better) and its components. Pure; weights in entry.composite.
      target: -target_atr_pen per 1H ATR of |target - entry| beyond target_atr_free (no valid 1H ATR: unscorable, score None)
      gap:    4H EMA fast-slow gap % in the trade direction: + min(gap, gap_full_pct) / gap_full_pct, and above
              gap_full_pct -gap_over_pen per % beyond it (a gap against the trade scores negative)
      cost:   -fee_share_pen per percentage point of fee_share above fee_share_free
      btc:    +btc_bonus if the BTC 4H trend agrees with the side"""
    long_ = side in ('buy', 'long')
    if not _pos_finite(atr1h): return dict(score=None, error='1H ATR unreadable')   # unscorable (ranked last), like missing 4H EMA
    tgt_atr = abs(target - entry) / atr1h
    s_tgt = -CP["target_atr_pen"] * max(0.0, tgt_atr - CP["target_atr_free"])
    gap = (ema_fast4h - ema_slow4h) / ema_slow4h * 100 * (1 if long_ else -1) if ema_fast4h and ema_slow4h else None
    full = float(CP["gap_full_pct"])
    if gap is None: s_gap = 0.0
    elif gap <= full: s_gap = gap / full
    else: s_gap = 1.0 - CP["gap_over_pen"] * (gap - full)
    fsh = fee_share(entry, stop)
    s_cost = -CP["fee_share_pen"] * max(0.0, (fsh - CP["fee_share_free"]) * 100)
    al = (btc_trend4h == 'up' and long_) or (btc_trend4h == 'down' and not long_)
    s_btc = float(CP["btc_bonus"]) if al else 0.0
    sc = s_tgt + s_gap + s_cost + s_btc
    return dict(score=round(sc, 4), target_atr=None if tgt_atr is None else round(tgt_atr, 3), gap4h_pct=None if gap is None else round(gap, 3),
                fee_share=round(fsh, 4), btc_trend4h=btc_trend4h, btc_aligned=bool(al),
                s_target=round(s_tgt, 4), s_gap=round(s_gap, 4), s_cost=round(s_cost, 4), s_btc=round(s_btc, 4))

def ema4h(inst, closes4h=None):
    """(ema_fast, ema_slow) of 4H closes, lengths from entry.trend_filter."""
    tf = E['trend_filter']; c = closes4h if closes4h is not None else [x[3] for x in candles(inst, '4H')]
    return ema(c, tf['ema_fast'])[-1], ema(c, tf['ema_slow'])[-1]

def closes_cached(inst, bar, ctx=None):
    """Closes of inst/bar, fetched at most once per first pass when ctx is given (cache in ctx['_closes']);
    ctx['_fresh'] (the pre-order re-check) bypasses the cache."""
    if ctx is None or ctx.get('_fresh'): return [x[3] for x in candles(inst, bar)]   # pre-order re-check: always fresh
    cache = ctx.setdefault('_closes', {})
    if (inst, bar) not in cache: cache[(inst, bar)] = [x[3] for x in candles(inst, bar)]
    return cache[(inst, bar)]

def btc_trend4h(ctx=None):
    try: return trend(closes_cached('BTC-USDT', '4H', ctx))[0]
    except Exception: return None

def lev_liq_max(entry, stop, mmr=MMR_DEFAULT, maxlev=LEVMAX):
    """Highest L in [min, min(max, maxlev)] whose estimated liq distance entry x (1/L - mmr) is beyond the stop distance
    by 1 / max_stop_to_liq (2x by default); None if none."""
    d = abs(entry - stop)
    ok = [L for L in range(LEVMIN, int(min(LEVMAX, maxlev)) + 1) if d < MAX_STOP_TO_LIQ * entry * (1.0 / L - mmr)]
    return max(ok) if ok else None

def fit_size(equity, avail, entry, stop, cv, lot, mins, maxlev=LEVMAX, mmr=MMR_DEFAULT, risk_cap=None):
    """Sizing. Returns dict(size, size_full, lev, cap, notional, scaled_down,
    scaled_by, risk_pct_actual, fee_share, gate, why). gate None = may proceed, else 'below_min_size' | 'no_leverage_fit' |
    'fee_heavy' | 'liq' | 'cap_risk' | 'below_min_risk'. Start: the full-risk size (size_for). Scale DOWN only, lot-rounded:
      1. risk_cap (USDT, risk-cap headroom): size <= risk_cap / (planned loss per contract); below min -> cap_risk
      2. leverage: L <= min(leverage.max, maxlev, lev_liq_max); none -> liq
      3. margin: lowest L whose margin fits min(margin_cap, free margin buffer) (pick_lev); if none, the size that fits at
         the highest allowed L
    A scaled size is skipped if fee_share > fee_heavy_max_share (fee_heavy) or < minSize (below_min_size). fee_heavy applies
    ONLY to scaled-down trades (deliberate, as live): an unscaled full-risk trade is never skipped for its fee share.
    Finally, ANY trade (scaled or not) whose fee-inclusive planned risk is below risk.min_risk_pct of equity is skipped
    (below_min_risk).
    risk.scale_down false -> no scaling: no_leverage_fit skip (the liq check then runs in evaluate)."""
    size = size_for(equity, entry, stop, cv, lot); fsh = fee_share(entry, stop)
    out = dict(size=size, size_full=size, lev=None, cap=None, notional=size * cv * entry, scaled_down=False, scaled_by=[],
               fee_share=round(fsh, 4), risk_pct_actual=None, gate=None, why=None)
    if size < mins: out.update(gate='below_min_size', why=f'size {size:g} below min {mins:g}'); return out
    if risk_cap is not None:
        per = loss_per_contract(entry, stop, cv)
        rc = float(q(max(0.0, risk_cap) / per, lot, ROUND_DOWN)) if per > 0 else 0.0
        if rc < size:
            out.update(size=rc, notional=rc * cv * entry, scaled_down=True); out['scaled_by'].append('cap_risk')
            if rc < mins or rc <= 0:
                out.update(gate='cap_risk', why=f'cap_risk: risk headroom {max(0.0, risk_cap):.2f} USDT fits {rc:g} < min {mins:g} (full size {size:g})'); return out
    Lmax = int(min(LEVMAX, maxlev))
    if SCALE_DOWN:
        Lliq = lev_liq_max(entry, stop, mmr, maxlev)
        if Lliq is None: out.update(gate='liq', why=f'liq too close at every leverage >= {LEVMIN}x: stop dist {abs(entry-stop):.8g}'); return out
        Lmax = min(Lmax, Lliq)
    L, cap = pick_lev(out['notional'], equity, avail, Lmax); out['cap'] = cap
    if L is None:
        if not SCALE_DOWN or cap <= 0:
            out.update(gate='no_leverage_fit', why=f"no leverage <= {Lmax} fits: notional {out['notional']:.2f}, margin cap {cap:.2f}"); return out
        s2 = min(out['size'], float(q(cap * Lmax / (cv * entry), lot, ROUND_DOWN)))
        out.update(size=s2, notional=s2 * cv * entry, scaled_down=True); out['scaled_by'].append('margin'); L = Lmax
    if out['scaled_down']:
        if fsh > FEE_HEAVY_MAX:
            out.update(gate='fee_heavy', why=f"fee_heavy: round-trip fees {fsh*100:.1f}% of stop-out loss > {FEE_HEAVY_MAX*100:.1f}% (scaled size {out['size']:g} at {L}x)"); return out
        if out['size'] < mins or out['size'] <= 0:
            out.update(gate='below_min_size', why=f"below_min_size: scaled size {out['size']:g} < min {mins:g} (full size {size:g})"); return out
    out['lev'] = L
    rp = out['size'] * loss_per_contract(entry, stop, cv) / equity * 100 if equity else None   # unrounded: the gate compares this
    out['risk_pct_actual'] = round(rp, 3) if rp is not None else None                       # rounded for display / logs only
    if MIN_RISK_PCT > 0 and (rp is None or rp < MIN_RISK_PCT):
        out.update(gate='below_min_risk', why=f"below_min_risk: fee-inclusive risk {out['risk_pct_actual']}% of equity < {MIN_RISK_PCT:g}% "
                                             f"(size {out['size']:g} at {L}x, full size {size:g}, scaled_by {'+'.join(out['scaled_by']) or '-'})")
    return out

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

# ---- gates -------------------------------------------------------------------------------------
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
    """Append one blocked_setups line (at most one per setup per run). Never raises: a failure becomes a warning in ctx."""
    if not BL.ENABLED: return
    key = (setup.get('instId'), setup.get('side'), setup.get('_rank'))
    logged = ctx.setdefault('_logged', set())
    if key in logged: return
    logged.add(key)
    try:
        side = norm_side(setup.get('side'))
        line = BL.build_line('execute', BL.mode_for(ctx.get('dry')), setup.get('instId'), 'long' if side == 'buy' else 'short',
                             setup.get('entry'), setup.get('stop'), setup.get('target'), setup.get('rr'), setup.get('_rank'),
                             gate_reason, run_id=os.environ.get('RUN_ID'), scan_ts=ctx.get('scan_ts'), reason=reason,
                             universe_tier=UN.tier_of(setup), vol_rank=setup.get('vol_rank'), **(extra or {}))
        BL.append(line)
    except Exception as e: ctx.setdefault('warnings', []).append(f'blocked_setups log failed: {str(e)[:160]}')

GATES = (('rr_cap', 'rr_cap'), ('cost_heavy', 'cost_heavy'), ('unresolved', 'unresolved_record'), ('cooldown', 'cooldown'), ('max positions', 'max_positions'),
         ('overall cap', 'overall_cap'), ('direction cap', 'dir_cap'), ('already has a position', 'duplicate'),
         ('existing position', 'duplicate'), ('r:r', 'rr'), ('atr', 'atr'), ('trend now', 'trend'), ('news gate', 'news_gate'),
         ('macro', 'macro_window'), ('instrument', 'instrument'), ('needs stop', 'geometry'), ('needs target', 'geometry'),
         ('bad setup', 'bad_setup'), ('leverage', 'leverage'), ('error', 'error'))

def gate_of(why):
    """gate_reason for a skip that carries no explicit one."""
    w = str(why or '').lower()
    for k, g in GATES:
        if k in w: return g
    return 'other'

def universe_block(setup, ctx, now=None):
    """None, or the SKIP reason 'no_fundamentals (...)' for an 'ext' tier setup without a fresh rating (logged)."""
    why = UN.block_reason(setup, now=now)
    if why: log_block(setup, 'no_fundamentals', why, ctx)
    return why

def unresolved_block(inst, pos):
    if not UNRES_BLOCK: return None
    try: return registry.unresolved(inst, pos)
    except Exception as e: return f'unresolved_record check error: {str(e)[:160]}'

# ---- risk caps ---------------------------------------------------------------------------------
def risk_assess(side, pos, equity, tpsl_rows=None, cvm=None, history=True, reg=None):
    """riskcap.assess on live data: pending TP/SL (one page of 100, a full page counts as incomplete), contract values,
    bot ownership as for the direction cap (registry match, else order history when history=True). Any read error ->
    mode 'slots' (fail closed)."""
    try:
        if not RC.ENABLED: raise ValueError('risk_caps disabled')
        if tpsl_rows is None:
            tp = bf.private('GET', '/api/v1/trade/orders-tpsl-pending', {'limit': 100})
            if not bf.resp_ok(tp): raise ValueError('tpsl-pending read failed')
            tpsl_rows = tp.get('data') or []
        rows = list(tpsl_rows)
        if len(rows) >= 100: raise ValueError('tpsl-pending page full (100), may be incomplete')
        if cvm is None: cvm = {k: float(v['contractValue']) for k, v in instruments().items()}
        reg = registry.load_strict() if reg is None else reg
        import manage   # lazy: manage imports this module
        isb = lambda p: bool(registry.match(p, reg)) or (history and manage.history_says_bot(p))
        brec = lambda i: isinstance(reg.get(i), dict)
        return RC.assess(side, pos, rows, lambda i: cvm[i], equity, isb, FEE, SLIP, brec)
    except Exception as e: return dict(mode='slots', error=f'{type(e).__name__}: {str(e)[:160]}')

def cap_check(side, ctx):
    """(reason or None, assessment). Risk caps when computable (the new trade is scaled to the headroom in fit_size),
    else the slot caps."""
    cache = ctx.setdefault('_risk', {})
    if side not in cache: cache[side] = risk_assess(side, ctx['pos'], ctx['equity']) if RC.ENABLED else dict(mode='slots', error='risk_caps disabled')
    rca = cache[side]
    if rca.get('mode') == 'risk': return None, rca
    if len(ctx['pos']) >= MAXALL: return 'overall cap', rca
    return dircap_block(side, ctx['pos']), rca

def cooldown_block(inst, side, now=None):
    """None if the pair+side may be entered, else the SKIP reason. A read error is a skip."""
    if not COOLDOWN_H: return None
    try: cd = closes.cooldown_check(inst, side, now, COOLDOWN_H)
    except Exception as e: return f'cooldown check error: {str(e)[:160]}'
    return cd['reason'] if cd['block'] else None

def bot_side_count(side, pos, reg=None):
    """Open BOT positions on side ('buy'/'sell'): a matching bot_positions.json record OR, as manage.py decides
    ownership, a bot-prefixed opening order in the exchange history (manage.history_says_bot)."""
    import manage   # lazy: manage imports this module
    reg = registry.load_strict() if reg is None else reg
    return sum(1 for p in pos if (float(p['positions']) > 0) == (side == 'buy') and (registry.match(p, reg) or manage.history_says_bot(p)))

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
    why = universe_block(setup, ctx)
    if why: return None, why
    why = cooldown_block(inst, side)
    if why: return None, why
    why = unresolved_block(inst, pos)   # cheap local check before the cap reads
    if why: return None, why
    why, rca = cap_check(side, ctx)
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
    rr = price_rr(side, entry, stop, target)
    if rr < MINRR: return None, f'R:R {rr:.2f} < {MINRR:g}'
    lim = int(config.CFG['scan']['candles_limit']); share1h = TREND1H and T1.BAR == '1H' and T1.BARS == lim
    # one 1H fetch shared by ATR and the 1H trend only when both asked for the same limit: candles() drops the unconfirmed
    # bar, so slicing a larger fetch would hand one extra closed bar to the smaller consumer and shift its value
    try: c1 = candles(inst, '1H', lim)
    except Exception as e: return None, f'atr unreadable: 1H candles error: {str(e)[:120]}'
    try: a1 = atr(c1)
    except Exception as e: return None, f'atr unreadable: {str(e)[:120]}'
    if not _pos_finite(a1): return None, f'atr unreadable: 1H ATR {a1!r} (missing, 0 or non-finite)'
    if risk < ATR_FLOOR * a1: return None, f'stop {risk/a1:.2f}x ATR < {E["min_stop_atr"]:g}'
    fg, fw = filter_gate(entry, stop, rr)
    if fg:
        log_block(setup, fg, fw, ctx); return None, fw
    tf = E['trend_filter']; c4 = None
    if tf['enabled']:
        c4 = closes_cached(inst, tf['timeframe'], ctx)   # once per symbol in the first pass (shared with BTC trend)
        tr = trend(c4)[0]
        want = 'up' if side == 'buy' else 'down'
        if tr != want: return None, f'{tf["timeframe"]} trend now {tr}, need {want}'
    tw, t1d = trend1h_check(inst, side, c1 if share1h else None)
    if tw:
        log_block(setup, '1h_trend (data error)' if 'data error' in tw else '1h_trend', tw, ctx, dict(detail=t1d))
        return None, tw
    equity, avail = ctx['equity'], ctx['avail']
    mmr = float(ins.get('maintenanceMarginRate') or MMR_DEFAULT)
    sz = fit_size(equity, avail, entry, stop, cv, lot, mins, float(ins.get('maxLeverage') or LEVMAX), mmr,
                  rca.get('headroom') if rca.get('mode') == 'risk' else None)
    if sz['gate']:
        why = sz['why'] + (' | ' + RC.summary(rca) if sz['gate'] == 'cap_risk' else '')
        log_block(setup, sz['gate'], why, ctx, dict(sizing={k: v for k, v in sz.items() if k not in ('gate', 'why')}))
        return None, why
    size, notional, lev, cap = sz['size'], sz['notional'], sz['lev'], sz['cap']
    liq = est_liq(entry, side, lev, mmr)
    if risk >= MAX_STOP_TO_LIQ * abs(liq - entry):
        why = f'liq {liq:.8g} too close: stop dist {risk:.8g} >= {MAX_STOP_TO_LIQ:g} x liq dist {abs(liq-entry):.8g} at {lev}x'
        log_block(setup, 'liq', why, ctx); return None, why
    live = pair_positions(inst)
    if live: return None, 'existing position on pair; leverage untouched'
    ema4 = None
    if COMPOSITE:
        try: ema4 = ema4h(inst, c4 if tf['enabled'] and tf['timeframe'] == '4H' else None)
        except Exception as e: ctx['warnings'].append(f'{inst} 4H EMA for score unreadable: {str(e)[:120]}')
    coid = bf.client_order_id(PREFIX)
    plan = dict(instId=inst, side=side, news=ng, trend1h=t1d, asset_class=SP.asset_class(inst, ins), macro=mc, equity=equity, avail=avail, entry=entry, stop=stop, target=target, rr=rr, rr_net=net_rr(side, entry, stop, target),
                atr1h=a1, size=size, notional=notional, lev=lev, margin=notional / lev, margin_cap=cap, liq_est=liq,
                stop_dist=risk, fee_rate=FEE, slip_frac=SLIP, entry_slip_frac=ENTRY_SLIP, est_loss_at_stop=size * loss_per_contract(entry, stop, cv), tick=tick,
                ctVal=cv, risk_usdt=size * cv * risk, size_full=sz['size_full'], scaled_down=sz['scaled_down'], scaled_by=sz['scaled_by'], risk_pct_actual=sz['risk_pct_actual'],
                fee_share=sz['fee_share'], ema4h=ema4, universe_tier=UN.tier_of(setup), vol_rank=setup.get('vol_rank'),
                risk_caps={k: rca.get(k) for k in ('mode', 'total_pct', 'dir_pct', 'headroom', 'n_pos', 'error')},
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
    # Provisional bot record BEFORE the order: if the outcome is lost, the next match/prune still adopts the position as
    # the bot's and it counts toward the direction cap. No record, no order.
    rec = dict(instId=inst, side=side, clientOrderId=plan['order_req']['clientOrderId'], opened_at=now_utc().isoformat(),
               entry_quote=plan['entry'], initial_stop=plan['stop'], target=plan['target'], stop_dist=plan['stop_dist'],
               atr1h=plan['atr1h'], equity_at_entry=plan.get('equity'), be_done=False, asset_class=plan.get('asset_class'),
               universe_tier=plan.get('universe_tier'), vol_rank=plan.get('vol_rank'), scaled_down=plan.get('scaled_down'),
               risk_pct_actual=plan.get('risk_pct_actual'), risk_caps=plan.get('risk_caps'),
               size=plan.get('size'), ctVal=plan.get('ctVal'), risk_usdt=plan.get('risk_usdt'), risk_usdt_allin=plan.get('est_loss_at_stop'),
               provisional=True)
    try: registry.put(rec)
    except Exception as e: return dict(ok=False, why=f'bot_positions.json not writable ({str(e)[:120]}); order NOT sent', plan=plan)
    def _drop():
        try:
            rg = registry.load()
            if rg.get(inst, {}).get('clientOrderId') == rec['clientOrderId']: rg.pop(inst); registry.save(rg)
        except Exception: pass
    try:
        r = bf.private('POST', '/api/v1/trade/order', body=plan['order_req'])
        if not bf.resp_ok(r): _drop(); return dict(ok=False, why=f'order rejected: {bf.resp_err(r)}', plan=plan, order_resp=r)
    except Exception as e:
        state, ev = resolve_uncertain_order(plan)
        if state == 'rejected': _drop()   # 'unknown' keeps the provisional record (fail toward "bot position")
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
    rec.pop('provisional', None)
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
            rec.update(avg=avg, stop_dist=abs(avg - plan['stop']), position_createTime=p[0].get('createTime'),
                       positionId=p[0].get('positionId')); registry.put(rec)   # positionId: closes.py matches on it first
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
    if why: return dict(ok=False, why=why, gate_reason='stale_scan')
    equity, avail, pos, mode = account()
    if not dry:   # a dry run never writes bot_positions.json / closed_pending.json
        try: registry.prune(pos)
        except Exception: pass
    # run-level exits: gate_reason only, no block line (no setup was walked)
    if not equity > 0: return dict(ok=False, why='equity unreadable', gate_reason='equity')
    if len(pos) >= RC.MAX_POS: return dict(ok=False, why=f'max positions {len(pos)}/{RC.MAX_POS}', gate_reason='max_positions')   # hard backstop
    if mode != 'net_mode': return dict(ok=False, why=f'account position mode {mode} != net_mode', gate_reason='position_mode')
    ctx = dict(equity=equity, avail=avail, pos=pos, ins=instruments(), dry=dry, scan_ts=scan_ts, warnings=[])
    skipped = []
    if COMPOSITE: return _run_composite(setups, ctx, skipped, dry)
    for i, s in enumerate(setups):
        s = dict(s, _rank=i + 1)
        try: plan, why = evaluate(s, ctx)
        except Exception as e: plan, why = None, f'error: {str(e)[:200]}'
        if plan is None:
            if LOG_ALL_SKIPS: log_block(s, gate_of(why), why, ctx)   # every per-setup skip is a block line (no-op if a gate already logged it)
            skipped.append(dict(instId=s.get('instId'), why=why)); continue
        if dry: return dict(ok=True, dry=True, plan=plan, skipped=skipped, warnings=ctx['warnings'])
        out = place(plan); out['skipped'] = skipped
        if ctx['warnings']: out.setdefault('warnings', []).extend(ctx['warnings'])
        if out.get('ok') or out.get('order_resp') is not None: return out   # accepted, rejected or unknown: stop here
        if LOG_ALL_SKIPS: log_block(s, 'leverage', out.get('why'), ctx)
        skipped.append(dict(instId=s.get('instId'), why=out.get('why'))); continue  # leverage refused: try next
    return dict(ok=False, why='no setup passed the checks', skipped=skipped, warnings=ctx['warnings'], **(dict(dry=True) if dry else {}))

def _skip(s, why, ctx, skipped):
    if LOG_ALL_SKIPS: log_block(s, gate_of(why), why, ctx)   # no-op if a gate already logged this setup
    skipped.append(dict(instId=s.get('instId'), why=why))

def score_plan(plan, btc):
    try:
        if not plan.get('ema4h'): return dict(score=None, error='4H EMA unreadable')   # unscorable: ranked last
        e1, e2 = plan['ema4h']
        return composite_score(plan['side'], plan['entry'], plan['stop'], plan['target'], plan['atr1h'], e1, e2, btc)
    except Exception as e: return dict(score=None, error=f'{type(e).__name__}: {str(e)[:120]}')

def _run_composite(setups, ctx, skipped, dry):
    """Composite ranking (entry.composite): every setup goes through evaluate() (all checks, no order); the passing
    plans are scored (composite_score) and tried best score first (unscorable last, ties in scan order). dry: the
    best plan is returned. live: each candidate is re-evaluated at the live price right before placing; a failed
    re-check or a refused leverage falls through to the next candidate. At most one entry per run."""
    btc = btc_trend4h(ctx); cands = []
    for i, s in enumerate(setups):
        s = dict(s, _rank=i + 1)
        try: plan, why = evaluate(s, ctx)
        except Exception as e: plan, why = None, f'error: {str(e)[:200]}'
        if plan is None: _skip(s, why, ctx, skipped); continue
        cands.append(dict(i=i, s=s, plan=plan, sc=score_plan(plan, btc)))
    order = sorted(cands, key=lambda c: (c['sc'].get('score') is None, -(c['sc'].get('score') or 0), c['i']))
    ranked = [dict(rank=c['i'] + 1, instId=c['plan']['instId'], side=c['plan']['side'], rr=round(c['plan']['rr'], 3), **c['sc']) for c in order]
    base = dict(skipped=skipped, warnings=ctx['warnings'], candidates=ranked, btc_trend4h=btc)
    if dry:
        if order: return dict(ok=True, dry=True, plan=order[0]['plan'], score=order[0]['sc'], **base)
        return dict(ok=False, dry=True, why='no setup passed the checks', **base)
    ctx['_fresh'] = True   # the re-check below refetches market data
    for c in order:
        s = c['s']
        try: plan, why = evaluate(s, ctx)   # re-check at the live price right before the order (the walk took time)
        except Exception as e: plan, why = None, f'error: {str(e)[:200]}'
        if plan is None: _skip(s, why, ctx, skipped); continue
        out = place(plan); out.update(skipped=skipped, score=c['sc'], candidates=ranked)
        if ctx['warnings']: out.setdefault('warnings', []).extend(ctx['warnings'])
        if out.get('ok') or out.get('order_resp') is not None: return out   # accepted, rejected or unknown: stop here
        if LOG_ALL_SKIPS: log_block(s, 'leverage', out.get('why'), ctx)
        skipped.append(dict(instId=s.get('instId'), why=out.get('why')))
    return dict(ok=False, why='no setup passed the checks', **base)

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
