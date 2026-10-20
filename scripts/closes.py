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
'manual' = close command), else from the result: a loss = 'stop' (exchange stop-loss or trailing stop), else 'tp'.
Re-entry cooldown (cooldown_check): a close starts one when realized pnl < 0, kind is stop / trail / cut, and it is not
a break-even scratch (be_done, or the exit at/beyond the recorded be_stop in the trade's favour). Manual closes don't.
"""
import os, json, datetime as dt
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

def is_scratch(c):
    if c.get('be_done') is True or c.get('kind') == 'be': return True
    try: ex, bes = float(c.get('close_avg')), float(c.get('be_stop'))
    except (TypeError, ValueError): return False
    s = norm_side(c.get('side'))
    return (ex >= bes) if s == 'long' else (ex <= bes) if s == 'short' else False

def cooldown_trigger(c):
    """(True, kind) if closed-trade line c starts a re-entry cooldown, else (False, why)."""
    pnl = fnum(c.get('realized_pnl'))
    if pnl is None: return False, 'pnl unknown'
    if pnl >= 0: return False, 'not a loss'
    if c.get('kind') not in ('stop', 'trail', 'cut'): return False, f"close by {c.get('kind')}, not a stop/trail/cut"
    if is_scratch(c): return False, 'break-even scratch'
    return True, c['kind']

def load_closed(path=None):
    out = []
    try: fh = open(path or config.data_path(FILE))
    except FileNotFoundError: return out
    with fh:
        for x in fh:
            x = x.strip()
            if x: out.append(json.loads(x))   # a corrupt line raises: callers skip the entry (fail closed)
    return out

def cooldown_check(inst, side, now=None, hours=4, path=None):
    """dict(block, until, reason). No closed_trades.jsonl -> allowed. Unreadable -> raises."""
    s = norm_side(side)
    if s is None: raise ValueError(f'invalid side {side!r}')
    now_ms = int((now or dt.datetime.now(dt.timezone.utc)).timestamp() * 1000); best = None
    for c in load_closed(path):
        if c.get('instId') != inst or norm_side(c.get('side')) != s or not c.get('close_ms'): continue
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
                        asset_class=rec.get('asset_class'), logged_utc=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
            line['kind'] = close_kind(rec, line['realized_pnl'])
            if funding_get is not None:
                try:
                    import funding
                    funding.apply(line, inst, funding_get, line['close_ms'] or now_ms, final=True, t0=funding.open_ms(rec))
                except Exception as e: errs.append(f'funding {inst} (close): {str(e)[:160]}')
            try:
                with open(config.data_path(FILE), 'a') as fh: fh.write(json.dumps(line, default=str) + '\n')
                lines.append(line)
            except Exception as e: errs.append(f'close {inst}: write failed: {str(e)[:160]}')
    return lines, errs
