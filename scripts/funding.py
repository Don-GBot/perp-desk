"""Funding-paid logging per bot position (logging only; never changes a stop, order or close).

manage.py calls update() for the open bot records in bot_positions.json on every run, and closes.record() calls apply()
with final=True for positions that closed. Source: GET /api/v1/account/funding-fees (read-only; begin/end windows of at
most 7 days, paged and deduped by billId), matched to the record's pair over the hold window [position open, close or now].
Fields: funding_paid (USDT, exchange sign: cost negative), funding_n, funding_bills, funding_ts, funding_final.
Any failure is returned as an error string per record; nothing here raises into manage.py.
"""
import time, datetime as dt

PATH = '/api/v1/account/funding-fees'; WIN_MS = 7 * 86_400_000 - 60_000

def _ms(x):
    if x is None or x == '': return None
    try: v = float(x); return int(v if v > 1e11 else v * 1000)
    except (TypeError, ValueError): pass
    try:
        t = dt.datetime.fromisoformat(str(x).replace('Z', '+00:00'))
        if t.tzinfo is None: t = t.replace(tzinfo=dt.timezone.utc)
        return int(t.timestamp() * 1000)
    except Exception: return None

def open_ms(rec): return _ms(rec.get('position_createTime')) or _ms(rec.get('opened_at'))

def fees(get, inst, t0, t1):
    out = {}; b = t0
    while b <= t1:
        e = min(t1, b + WIN_MS)
        for r in get(PATH, {'instId': inst, 'begin': str(b), 'end': str(e), 'limit': '1000'}) or []:
            if r.get('instId') == inst and t0 <= int(r['fundingTime']) <= t1: out[str(r.get('billId'))] = r
        b = e + 1
    return list(out.values())

def apply(rec, inst, get, t1, final=False, t0=None):
    t0 = t0 or open_ms(rec)
    if not t0: raise ValueError('no open time on record')
    rows = fees(get, inst, t0, t1)
    rec['funding_paid'] = round(sum(float(r['fundingFee']) for r in rows), 8)
    rec['funding_n'] = len(rows); rec['funding_bills'] = sorted(str(r.get('billId')) for r in rows)
    rec['funding_ts'] = dt.datetime.fromtimestamp(t1 / 1000, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    if final: rec['funding_final'] = True
    return rec['funding_paid']

def update(reg, get, now_ms=None):
    """Mutates the registry dict in memory (caller saves it). Returns (n_updated, errors). Never raises."""
    errs = []; n = 0
    try:
        now_ms = now_ms or int(time.time() * 1000)
        for inst, rec in list((reg or {}).items()):
            if not isinstance(rec, dict): continue
            try: apply(rec, inst, get, now_ms); n += 1
            except Exception as e: errs.append(f'funding {inst}: {str(e)[:160]}')
    except Exception as e: errs.append(f'funding update failed: {str(e)[:160]}')
    return n, errs
