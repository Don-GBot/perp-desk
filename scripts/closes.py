"""Closed-trade log: when a bot position is gone, find its row in the exchange positions history and append one line to
DATA_DIR/closed_trades.jsonl (logging only; never sends an order).

Matching a positions-history row to a bot_positions.json record (close_fit):
  - same instId, not a partial close, and side not known-opposite. History rows are positionSide "net" with unsigned
    sizes, so the side is inferred from sign(close - open) vs sign(gross pnl); an undeterminable side (close exactly at
    entry) is not treated as a mismatch.
  - positionId on both: must be equal (best match). Different ids never match.
  - else createTime within registry.MATCH_WINDOW_MS of the record's position_createTime (else opened_at); the closest wins.
    A tolerance, not equality: the exchange can report a createTime 1 ms off the one seen at entry.
  - one history row never closes two records in a run (consumed), and a row that fits another unresolved record on
    the same pair more closely is left for it (rivals).
Pruned records wait in closed_pending.json (registry.py) until their row is found (kept up to PENDING_MAX_H hours).

Close kind: the record's close_by when manage.py closed it ('cut' = hard cut, 'stop' = mark already past the stop,
'manual' = close command). Otherwise the exchange closed it, and the positions-history row is not relied on to say why
(no close-type field is used), so a LOSING close of unknown cause counts as 'stop' (it starts a cooldown: errs toward
blocking) and a winning one as 'tp'. A manual close done outside manage.py therefore also counts as a stop if it lost.
Re-entry cooldown (cooldown_check): a close starts one when realized pnl < 0, kind is stop / trail / cut, and it is not
a break-even scratch (exit at/beyond the recorded be_stop minus a slippage tolerance, or a loss under 0.5R; see
is_scratch). The 0.5R uses the FEE-INCLUSIVE planned risk (risk_allin: coins x execute.planned_risk_per_coin(entry,
initial_stop), the sizing number incl. entry slip); if that can't be computed, price-only risk (coins x |entry - stop|),
then the old equity_at_entry x risk_per_trade_pct basis, each fallback logged (scratch_basis, SCRATCH_FALLBACKS).
Manual closes don't.
R per closed trade (r_multiples, report): R = realized_pnl / fee-inclusive planned risk; R_price = realized_pnl / price-only
planned risk (kept for history). Coins = size x ctVal, else price-only risk_usdt / |entry - initial_stop| when the
contract value is missing. Old lines are recomputed the same way by report(); a line with no entry/stop is unknown_R.
  python3 scripts/closes.py report     # read-only summary of closed_trades.jsonl
Closes not logged yet are covered too: a pending record (closed_pending.json) on the same pair+side blocks unless it is
a manual close, for cooldown_hours from pending_since (fail closed until it is resolved).
A logged line without close_ms uses logged_utc as the close time.
"""
import os, json, time, fcntl, datetime as dt
import config, registry

PATH = '/api/v1/account/positions-history'
FILE = 'closed_trades.jsonl'
PENDING_MAX_H = 24

def fnum(x, d=None):
    try: return float(x)
    except (TypeError, ValueError): return d

def norm_side(s):
    s = str(s or '').strip().lower()
    return 'long' if s in ('buy', 'long') else 'short' if s in ('sell', 'short') else None

def hist_side(h):
    for k in ('side', 'direction', 'posSide', 'positionSide'):
        v = norm_side(h.get(k))
        if v: return v
    op, cp, rp = fnum(h.get('openAveragePrice')), fnum(h.get('closeAveragePrice')), fnum(h.get('realizedPnl'))
    if op is None or cp is None or rp is None or cp == op: return None
    gross = rp + abs(fnum(h.get('fee'), 0.0) or 0.0)
    if gross == 0: return None
    return 'long' if (cp > op) == (gross > 0) else 'short'

def hist_key(h):
    return str(h.get('positionId') or h.get('historyId') or f"{h.get('createTime')}|{h.get('updateTime')}")

def close_fit(inst, rec, h, window_ms=None):
    """None if row h can't be rec's close, else a sort key (lower = better)."""
    window_ms = registry.MATCH_WINDOW_MS if window_ms is None else window_ms
    if str(h.get('instId') or '') != str(inst): return None
    if str(h.get('state') or '').startswith('partially'): return None
    side, hs = norm_side(rec.get('side')), hist_side(h)
    if hs and side and hs != side: return None
    pid, hp = str(rec.get('positionId') or ''), str(h.get('positionId') or '')
    if pid and hp: return (0, 0) if pid == hp else None
    ct = int(fnum(h.get('createTime'), 0) or 0)
    ref = int(fnum(rec.get('position_createTime'), 0) or 0) or registry.iso_ms(rec.get('opened_at'))
    if not ct or not ref or abs(ct - ref) > window_ms: return None
    return (1, abs(ct - ref))

def pick(inst, rec, hist, consumed=None, rivals=()):
    consumed = set() if consumed is None else consumed
    cands = []
    for h in hist or []:
        if hist_key(h) in consumed: continue
        sc = close_fit(inst, rec, h)
        if sc is None: continue
        if any(r is not rec and (rs := close_fit(inst, r, h)) is not None and rs < sc for r in rivals): continue
        cands.append((sc, -int(fnum(h.get('updateTime'), 0) or 0), h))
    if not cands: return None
    h = min(cands, key=lambda x: (x[0], x[1]))[2]
    consumed.add(hist_key(h))
    return h

def close_kind(rec, pnl):
    if rec.get('close_by'): return rec['close_by']
    return 'stop' if pnl is not None and pnl < 0 else 'tp'

SCRATCH_R = 0.5   # a loss under 0.5R is a scratch
SCRATCH_FALLBACKS = []   # lines whose scratch test could not use the fee-inclusive risk (also logged as a warning)

def _entry_stop(c):
    e = fnum(c.get('entry')); e = e if e else fnum(c.get('open_avg'))
    st = fnum(c.get('initial_stop')); st = st if st is not None else fnum(c.get('stop'))
    return e, st

def coins_of(c):
    """size x ctVal; if the contract value is missing, price-only risk_usdt / |entry - initial_stop|; else None."""
    n = fnum(c.get('size') if c.get('size') is not None else c.get('contracts')); cv = fnum(c.get('ctVal'))
    if n is not None and cv: return n * cv
    e, st = _entry_stop(c); r = fnum(c.get('risk_usdt'))
    if r and e is not None and st is not None and e != st: return r / abs(e - st)
    return None

def risk_allin(c):
    """Fee-inclusive planned risk (USDT): recorded risk_usdt_allin, else coins x execute.planned_risk_per_coin(entry, stop)."""
    r = fnum(c.get('risk_usdt_allin'))
    if r and r > 0: return r
    e, st = _entry_stop(c); k = coins_of(c)
    if not k or e is None or st is None or e == st: return None
    import execute
    return k * execute.planned_risk_per_coin(e, st)

def risk_price(c):
    """Price-only planned risk (USDT): risk_usdt, else coins x |entry - stop|."""
    r = fnum(c.get('risk_usdt'))
    if r and r > 0: return r
    e, st = _entry_stop(c); k = coins_of(c)
    return k * abs(e - st) if k and e is not None and st is not None and e != st else None

def r_multiples(c):
    """(R, R_price) of a closed-trade line: realized_pnl over the fee-inclusive / price-only planned risk (None if unknown)."""
    pnl = fnum(c.get('realized_pnl')); ra, rp = risk_allin(c), risk_price(c)
    if pnl is None: return None, None
    return (round(pnl / ra, 4) if ra else None), (round(pnl / rp, 4) if rp else None)

def scratch_risk(c):
    """(risk_usdt, basis) for the 0.5R test: 'allin', else 'price_fallback', else 'equity_fallback', else (None, None)."""
    ra = risk_allin(c)
    if ra: return ra, 'allin'
    rp = risk_price(c); basis = 'price_fallback'
    if not rp:
        eq = fnum(c.get('equity_at_entry'))
        rp = eq * config.CFG['risk']['risk_per_trade_pct'] / 100.0 if eq else None; basis = 'equity_fallback'
    if rp:
        SCRATCH_FALLBACKS.append(dict(instId=c.get('instId'), side=c.get('side'), basis=basis, risk=rp))
        import logging; logging.getLogger('closes').warning(f"be-scratch: fee-inclusive risk not computable for {c.get('instId')} {c.get('side')}; {basis}")
        return rp, basis
    return None, None

def is_scratch(c):
    """Break-even scratch ONLY if the exit is at or beyond be_stop in the trade's favour (minus a slippage tolerance of
    fees.slip_frac x be_stop), or the loss is under SCRATCH_R x the fee-inclusive planned risk (scratch_risk; logged
    fallbacks). be_done alone is not enough: a BE that was marked but not filled near the BE stop still counts as a loss."""
    slip = float(config.CFG['fees']['slip_frac']); s = norm_side(c.get('side'))
    ex, bes = fnum(c.get('close_avg')), fnum(c.get('be_stop'))
    if ex is not None and bes:
        if s == 'long' and ex >= bes * (1 - slip): return True
        if s == 'short' and ex <= bes * (1 + slip): return True
    pnl = fnum(c.get('realized_pnl'))
    if pnl is not None:
        R, _ = scratch_risk(c)
        if R and R > 0 and -pnl < SCRATCH_R * R: return True
    return False

def cooldown_trigger(c):
    """(True, kind) if closed-trade line c starts a re-entry cooldown, else (False, why)."""
    pnl = fnum(c.get('realized_pnl'))
    if pnl is None: return False, 'pnl unknown'
    if pnl >= 0: return False, 'not a loss'
    if c.get('kind') not in ('stop', 'trail', 'cut'): return False, f"close by {c.get('kind')}, not a stop/trail/cut"
    if is_scratch(c): return False, 'break-even scratch'
    return True, c['kind']

def load_closed(path=None):
    """All closed-trade lines. A corrupt line raises ValueError naming its line number (callers fail closed)."""
    out = []
    try: fh = open(path or config.data_path(FILE))
    except FileNotFoundError: return out
    with fh:
        for n, x in enumerate(fh, 1):
            x = x.strip()
            if not x: continue
            try: out.append(json.loads(x))
            except ValueError as e: raise ValueError(f'{FILE} line {n} is corrupt: {e}') from None
    return out

def append_line(line, path=None, wait=2.0):
    """Append one JSON line under <file>.lock with fsync (same pattern as blocklog.append). Raises on failure."""
    path = path or config.data_path(FILE)
    lfd = os.open(path + '.lock', os.O_CREAT | os.O_RDWR, 0o644)
    try:
        t0 = time.time()
        while True:
            try: fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.time() - t0 > wait: raise TimeoutError(f'{FILE} lock busy')
                time.sleep(0.05)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        try: os.write(fd, (json.dumps(line, default=str) + '\n').encode()); os.fsync(fd)
        finally: os.close(fd)
    finally: os.close(lfd)

def pending_block(inst, side, now_ms, hours):
    """Reason if a not-yet-logged close on inst+side must block (fail closed), else None."""
    for r in registry.load_pending():
        if r.get('instId') != inst or norm_side(r.get('side')) != side: continue
        if r.get('close_by') == 'manual': continue   # outcome unknown until logged: be_done alone does not exempt it
        since = registry.iso_ms(r.get('pending_since')) or now_ms
        if now_ms < since + int(hours * 3_600_000):
            why = f"close by {r['close_by']}" if r.get('close_by') else 'close not logged yet (outcome unknown)'
            return f"cooldown: pending {why}, blocked until {dt.datetime.fromtimestamp((since + hours * 3_600_000) / 1000, dt.timezone.utc):%H:%M}Z"
    return None

def cooldown_check(inst, side, now=None, hours=4, path=None):
    """dict(block, until, reason). No closed_trades.jsonl -> allowed. Unreadable -> raises."""
    s = norm_side(side)
    if s is None: raise ValueError(f'invalid side {side!r}')
    now_ms = int((now or dt.datetime.now(dt.timezone.utc)).timestamp() * 1000); best = None
    pb = pending_block(inst, s, now_ms, hours)
    if pb: return dict(block=True, until=None, reason=pb)
    for c in load_closed(path):
        if c.get('instId') != inst or norm_side(c.get('side')) != s: continue
        c = dict(c, close_ms=c.get('close_ms') or registry.iso_ms(c.get('logged_utc')))
        if not c['close_ms']: continue
        ok, kind = cooldown_trigger(c)
        if not ok: continue
        until = int(c['close_ms']) + int(hours * 3_600_000)
        if until > now_ms and (best is None or until > best[0]): best = (until, kind, c)
    if not best: return dict(block=False, until=None, reason='no cooldown')
    u = dt.datetime.fromtimestamp(best[0] / 1000, dt.timezone.utc)
    return dict(block=True, until=u.isoformat(), reason=f"cooldown until {u:%H:%M}Z (bot {best[1]} loss {best[2].get('realized_pnl')}; {hours:g}h same pair+side)")

def record_pending(get, now_ms=None, funding_get=None):
    """Process registry's closed_pending.json: log the matched ones, keep the rest (dropped after PENDING_MAX_H)."""
    import time
    now_ms = now_ms or int(time.time() * 1000)
    pend = registry.load_pending()
    if not pend: return [], []
    by = {}
    for r in pend: by.setdefault(r.get('instId'), []).append(r)
    lines, errs = record(by, get, now_ms, funding_get)
    done = {(x['instId'], x.get('clientOrderId'), x.get('opened_at')) for x in lines}
    keep = []
    for r in pend:
        if (r.get('instId'), r.get('clientOrderId'), r.get('opened_at')) in done: continue
        since = registry.iso_ms(r.get('pending_since')) or now_ms
        if now_ms - since <= PENDING_MAX_H * 3_600_000:
            r.setdefault('pending_since', dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone.utc).isoformat()); keep.append(r)
        else: errs.append(f"close {r.get('instId')}: no history row after {PENDING_MAX_H}h, dropped")
    registry.save_pending(keep)
    return lines, errs

def record(removed, get, now_ms=None, funding_get=None):
    """removed: {instId: record or [records]}. get(path, params) -> data list. Appends one line per matched close.
    Returns (lines, errors). Never raises."""
    lines, errs, consumed = [], [], set()
    by_inst = {}
    for inst, rec in (removed or {}).items(): by_inst.setdefault(inst, []).extend(rec if isinstance(rec, list) else [rec])
    for inst, recs in by_inst.items():
        try: hist = get(PATH, {'instId': inst, 'limit': '50'})
        except Exception as e: errs.append(f'close {inst}: history unreadable: {str(e)[:160]}'); continue
        for rec in recs:
            h = pick(inst, rec, hist, consumed, [r for r in recs if r is not rec])
            if not h: errs.append(f'close {inst}: no matching history row'); continue
            line = dict(instId=inst, side=rec.get('side'), clientOrderId=rec.get('clientOrderId'), opened_at=rec.get('opened_at'),
                        position_createTime=rec.get('position_createTime'), positionId=h.get('positionId'),
                        open_avg=fnum(h.get('openAveragePrice')), close_avg=fnum(h.get('closeAveragePrice')),
                        realized_pnl=fnum(h.get('realizedPnl')), fee=fnum(h.get('fee')), close_ms=int(fnum(h.get('updateTime'), 0) or 0),
                        initial_stop=rec.get('initial_stop'), target=rec.get('target'), be_done=rec.get('be_done'), be_stop=rec.get('be_stop'),
                        equity_at_entry=rec.get('equity_at_entry'), entry=rec.get('entry_quote'), size=rec.get('size'),
                        ctVal=rec.get('ctVal'), risk_usdt=rec.get('risk_usdt'), risk_usdt_allin=rec.get('risk_usdt_allin'),
                        asset_class=rec.get('asset_class'), logged_utc=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
            line['kind'] = close_kind(rec, line['realized_pnl'])
            line['R'], line['R_price'] = r_multiples(line); line['R_basis'] = 'allin'
            if funding_get is not None:
                try:
                    import funding
                    funding.apply(line, inst, funding_get, line['close_ms'] or now_ms, final=True, t0=funding.open_ms(rec))
                except Exception as e: errs.append(f'funding {inst} (close): {str(e)[:160]}')
            try:
                append_line(line); lines.append(line)
            except Exception as e: errs.append(f'close {inst}: write failed: {str(e)[:160]}')
    return lines, errs

def report(path=None):
    """Read-only summary of closed_trades.jsonl. R is RECOMPUTED for every line (old lines included) with r_multiples."""
    out = dict(R_basis='allin', n=0, wins=0, net_usdt=0.0, netR=0.0, netR_price=0.0, unknown_R=0)
    for c in load_closed(path):
        pnl = fnum(c.get('realized_pnl'))
        if pnl is None: continue
        out['n'] += 1; out['wins'] += pnl > 0; out['net_usdt'] = round(out['net_usdt'] + pnl, 4)
        R, Rp = r_multiples(c)
        if R is None: out['unknown_R'] += 1
        else: out['netR'] = round(out['netR'] + R, 4)
        if Rp is not None: out['netR_price'] = round(out['netR_price'] + Rp, 4)
    return out

if __name__ == '__main__':
    import sys
    if sys.argv[1:] != ['report']: print('usage: closes.py report', file=sys.stderr); sys.exit(2)
    print(json.dumps(report(), indent=1))
