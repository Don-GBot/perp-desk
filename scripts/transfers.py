#!/usr/bin/env python3
"""Deposits/withdrawals log, so equity changes are not mistaken for trading PnL. DATA_DIR/transfers.json (a list).

  python3 scripts/transfers.py add --type deposit|withdrawal --amount X --ts-utc ISO [--source TEXT] [--note TEXT] [--transfer-id ID]
  python3 scripts/transfers.py list
  python3 scripts/transfers.py detect --since ISO [--record]
      read-only GET /api/v1/asset/bills (toAccount=futures -> deposit, fromAccount=futures -> withdrawal); prints the
      transfers not yet logged and appends them to DATA_DIR/logs/transfers_detect.jsonl. --record also adds them
      (source=blofin_bills, transfer_id kept). Needs a READ key for detect only.
Writes hold DATA_DIR/transfers.lock and replace the file atomically (.prev kept).
Dedupe: identical type + amount + ts_utc, or the same exchange transfer_id, in transfers.json AND in the detect log
(detect is idempotent: repeat runs never re-append a transfer already logged). Exit codes: 0 ok/dup, 2 bad args, 1 write/API fail, 4 lock busy.
"""
import os, sys, json, time, fcntl, shutil, argparse, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

FILE = 'transfers.json'; LOCK = 'transfers.lock'; LOCK_WAIT_S = 10.0
class LockBusy(RuntimeError): pass
class BadArgs(ValueError): pass

def state_path(): return config.data_path(FILE)
def detect_log():
    d = config.data_path('logs'); os.makedirs(d, exist_ok=True); return os.path.join(d, 'transfers_detect.jsonl')

def parse_ts(s):
    try: t = dt.datetime.fromisoformat(str(s).strip().replace('Z', '+00:00'))
    except Exception: raise BadArgs(f'bad --ts-utc {s!r}')
    if t.tzinfo is None: raise BadArgs('--ts-utc needs a zone (Z or +00:00)')
    return t.astimezone(dt.timezone.utc)
def iso(t): return t.strftime('%Y-%m-%dT%H:%M:%SZ')
def key(r): return (r.get('type'), round(float(r.get('amount', 0)), 8), r.get('ts_utc'))

def make(type_, amount, ts_utc, source=None, note=None, transfer_id=None):
    if type_ not in ('deposit', 'withdrawal'): raise BadArgs('--type must be deposit or withdrawal')
    try: a = float(amount)
    except Exception: raise BadArgs(f'bad --amount {amount!r}')
    if not (a > 0) or a == float('inf'): raise BadArgs('--amount must be a positive number (type gives the sign)')
    r = dict(type=type_, amount=round(a, 8), ts_utc=iso(parse_ts(ts_utc)), source=source or 'manual', note=note or '',
             logged_utc=iso(dt.datetime.now(dt.timezone.utc)))
    if transfer_id: r['transfer_id'] = str(transfer_id)
    return r

def load_list():
    try:
        with open(state_path()) as fh: tl = json.load(fh)
    except FileNotFoundError: return []
    return tl if isinstance(tl, list) else []

def append(recs):
    """Append recs (deduped) under the lock. Returns (added, dups)."""
    fd = os.open(config.data_path(LOCK), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        t0 = time.monotonic()
        while True:
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic() - t0 >= LOCK_WAIT_S: raise LockBusy(f'{LOCK} busy for {LOCK_WAIT_S:g}s')
                time.sleep(0.2)
        tl = load_list(); keys = {key(r) for r in tl}; ids = {r.get('transfer_id') for r in tl if r.get('transfer_id')}
        added, dups = [], []
        for r in recs:
            if key(r) in keys or (r.get('transfer_id') and r['transfer_id'] in ids): dups.append(r); continue
            tl.append(r); keys.add(key(r)); added.append(r)
            if r.get('transfer_id'): ids.add(r['transfer_id'])
        if added:
            p = state_path(); tmp = p + '.tmp'
            with open(tmp, 'w') as fh: json.dump(tl, fh, indent=1, default=str); fh.flush(); os.fsync(fh.fileno())
            if os.path.exists(p): shutil.copy2(p, p + '.prev')
            os.replace(tmp, p)
        return added, dups
    finally:
        try: fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception: pass
        os.close(fd)

def fetch_bills(since_ms, get=None):
    if get is None:
        import blofin_client as bf
        get = lambda params: bf.private('GET', '/api/v1/asset/bills', params)
    out = []
    for typ, side in (('deposit', 'toAccount'), ('withdrawal', 'fromAccount')):
        r = get({'currency': 'USDT', side: 'futures', 'before': str(int(since_ms)), 'limit': '100'})
        if str(r.get('code')) != '0': raise RuntimeError(f"bills {typ}: code {r.get('code')} {str(r.get('msg'))[:120]}")
        for b in r.get('data') or []:
            if int(b['ts']) < since_ms: continue
            if typ == 'deposit' and b.get('fromAccount') == 'futures': continue
            if typ == 'withdrawal' and b.get('toAccount') == 'futures': continue
            t = dt.datetime.fromtimestamp(int(b['ts']) / 1000, dt.timezone.utc)
            rec = make(typ, b['amount'], iso(t), 'blofin_bills', f"{b.get('fromAccount')}->{b.get('toAccount')} {b.get('currency')}", b.get('transferId'))
            rec['ts_ms'] = int(b['ts']); out.append(rec)
    return sorted(out, key=lambda r: r['ts_ms'])

def log_keys():
    """(keys, transfer_ids) already in the detect log; unreadable lines are ignored."""
    ks, ids = set(), set()
    try:
        with open(detect_log()) as fh:
            for l in fh:
                try: r = json.loads(l)
                except Exception: continue
                if not isinstance(r, dict): continue
                ks.add(key(r))
                if r.get('transfer_id'): ids.add(str(r['transfer_id']))
    except FileNotFoundError: pass
    return ks, ids

def detect(since, record=False, get=None):
    found = fetch_bills(int(parse_ts(since).timestamp() * 1000), get)
    have = load_list(); keys = {key(r) for r in have}; ids = {r.get('transfer_id') for r in have if r.get('transfer_id')}
    new = [r for r in found if key(r) not in keys and r.get('transfer_id') not in ids]
    lk, li = log_keys()   # idempotent detect log: a transfer already in the log is never appended again
    tolog = [r for r in new if key(r) not in lk and not (r.get('transfer_id') and r['transfer_id'] in li)]
    if tolog:
        with open(detect_log(), 'a') as fh:
            for r in tolog:
                fh.write(json.dumps(dict(r, detected_utc=iso(dt.datetime.now(dt.timezone.utc)))) + '\n')
                lk.add(key(r))
                if r.get('transfer_id'): li.add(r['transfer_id'])
    added = append(new)[0] if (record and new) else []
    return found, new, added

def main(argv=None):
    ap = argparse.ArgumentParser(prog='transfers.py'); sp = ap.add_subparsers(dest='cmd', required=True)
    a = sp.add_parser('add'); a.add_argument('--type', required=True); a.add_argument('--amount', required=True)
    a.add_argument('--ts-utc', required=True); a.add_argument('--source'); a.add_argument('--note'); a.add_argument('--transfer-id')
    sp.add_parser('list')
    d = sp.add_parser('detect'); d.add_argument('--since', required=True); d.add_argument('--record', action='store_true')
    try: ns = ap.parse_args(argv)
    except SystemExit as e: return 0 if e.code == 0 else 2
    try:
        if ns.cmd == 'add':
            added, dups = append([make(ns.type, ns.amount, ns.ts_utc, ns.source, ns.note, ns.transfer_id)])
            print(('ADDED ' if added else 'DUP (already logged) ') + json.dumps(added[0] if added else dups[0])); return 0
        if ns.cmd == 'list':
            tl = load_list()
            for r in tl: print(json.dumps(r))
            net = sum(r['amount'] * (1 if r['type'] == 'deposit' else -1) for r in tl)
            print(f'{len(tl)} transfer(s); net {net:+.4f} USDT'); return 0
        found, new, added = detect(ns.since, ns.record)
        print(f'{len(found)} futures transfer(s) since {ns.since}; {len(new)} not yet logged' + (f'; {len(added)} recorded' if ns.record else ''))
        for r in new: print('NEW ' + json.dumps(r))
        return 0
    except BadArgs as e: print(f'ERROR: {e}', file=sys.stderr); return 2
    except LockBusy as e: print(f'LOCKED: {e}', file=sys.stderr); return 4
    except Exception as e: print(f'FAIL: {type(e).__name__}: {e}', file=sys.stderr); return 1

if __name__ == '__main__': sys.exit(main())
