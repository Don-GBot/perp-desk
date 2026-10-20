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
"""
import os, json, datetime as dt
import config, registry

PATH = '/api/v1/account/positions-history'
FILE = 'closed_trades.jsonl'

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

def record(removed, get, now_ms=None, funding_get=None):
    """removed: {instId: record} pruned this run. get(path, params) -> data list. Appends one line per matched close.
    Returns (lines, errors). Never raises."""
    lines, errs, consumed = [], [], set()
    by_inst = {}
    for inst, rec in (removed or {}).items(): by_inst.setdefault(inst, []).append(rec)
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
                        initial_stop=rec.get('initial_stop'), target=rec.get('target'), be_done=rec.get('be_done'),
                        asset_class=rec.get('asset_class'), logged_utc=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
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
