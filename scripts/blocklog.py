#!/usr/bin/env python3
"""Blocked-setups log (logging only; never changes a gate, stop or order).

One JSON line per blocked setup (news gate, 1h trend, stock perp) in DATA_DIR/blocked_setups.jsonl, appended under
blocked_setups.jsonl.lock with fsync. execute.py writes lines with source="execute"; an agent can log its own blocks
with this CLI (source="agent"):

  python3 scripts/blocklog.py add --pair P --side long|short --entry E --sl S --tp T --rr R
          [--rank N] [--mode live|dry] [--run-id ID] [--reason TEXT]

Dry runs (execute.py --dry, --mode dry) go to DATA_DIR/logs/blocked_setups_dry.jsonl so they never mix with live lines;
readers use load_live(), which also ignores any mode=dry line found in the live file.
rating, confidence, rating_ts_utc, sources and flagged_news come from DATA_DIR/fundamentals.json. --run-id defaults to
env RUN_ID. Dedupe (CLI): same run_id + pair + side + entry already in the log -> not written again (exit 0).
Exit codes: 0 ok/dup, 1 write failed, 2 bad arguments.
"""
import os, sys, json, time, fcntl, argparse, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import stockperp as SP

ENABLED = bool(config.CFG["logging"]["blocked_setups"])
LOCK_WAIT_S = 2.0

def log_path(mode=None):
    if mode == 'dry':
        d = config.data_path('logs'); os.makedirs(d, exist_ok=True)
        return os.path.join(d, 'blocked_setups_dry.jsonl')
    return config.data_path('blocked_setups.jsonl')

def load_live(path=None):
    """Lines of the live log, excluding mode=dry."""
    out = []
    try: fh = open(path or log_path())
    except FileNotFoundError: return out
    with fh:
        for x in fh:
            try: o = json.loads(x)
            except Exception: continue
            if o.get('mode') != 'dry': out.append(o)
    return out

def _ts(x):
    try:
        t = dt.datetime.fromisoformat(str(x).replace('Z', '+00:00')); return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception: return None

def fund_fields(inst, path=None):
    """fundamentals.json pairs[inst] fields for the log line (never raises; a read error goes to fund_read_error)."""
    try:
        with open(path or config.data_path('fundamentals.json')) as fh: r = ((json.load(fh).get('pairs') or {}).get(str(inst).strip().upper())) or {}
    except Exception as e: return dict(fund_read_error=str(e)[:200])
    t = _ts(r.get('ts_utc'))
    return dict(rating=(str(r.get('rating') or '').strip().lower() or None), confidence=(str(r.get('confidence') or '').strip().lower() or None),
                rating_ts_utc=r.get('ts_utc'), rating_age_h=round((dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600, 2) if t else None,
                sources=r.get('sources'), flagged_news=r.get('flagged_news'), summary=r.get('summary'), fund_read_error=None)

def build_line(source, mode, pair, side, entry, sl, tp, rr, rank, gate_reason, run_id=None, fund=None, scan_ts=None, reason=None, **extra):
    f = fund if fund is not None else fund_fields(pair)
    line = dict(ts_utc=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), run_id=run_id, mode=mode, source=source,
                pair=pair, side=side, entry=entry, sl=sl, tp=tp, rr=rr, rank=rank, gate_reason=gate_reason, reason=reason,
                rating=f.get('rating'), confidence=f.get('confidence'), rating_ts_utc=f.get('rating_ts_utc'), rating_age_h=f.get('rating_age_h'),
                sources=f.get('sources'), flagged_news=f.get('flagged_news'), summary=f.get('summary'),
                fund_read_error=f.get('fund_read_error'), scan_ts=scan_ts)
    extra.setdefault('asset_class', SP.asset_class(pair))
    line.update(extra); return line

def append(line, path=None, wait=None, dedupe_run=False):
    """Append one line under the lock. Returns True if written, False if deduped. Raises on failure."""
    path = path or log_path(line.get('mode')); wait = LOCK_WAIT_S if wait is None else wait
    data = (json.dumps(line, default=str) + '\n').encode()
    lfd = os.open(path + '.lock', os.O_CREAT | os.O_RDWR, 0o644)
    try:
        t0 = time.time()
        while True:
            try: fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.time() - t0 > wait: raise TimeoutError('blocked_setups lock busy')
                time.sleep(0.05)
        if dedupe_run and line.get('run_id') and os.path.exists(path):
            k = (str(line['run_id']), line['pair'], line['side'], float(line['entry']))
            with open(path) as fh:
                for x in fh:
                    try:
                        o = json.loads(x)
                        if (str(o.get('run_id')), o.get('pair'), o.get('side'), float(o.get('entry'))) == k: return False
                    except (ValueError, TypeError): continue
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
        try: os.write(fd, data); os.fsync(fd)
        finally: os.close(fd)
        return True
    finally: os.close(lfd)

def main(argv=None):
    ap = argparse.ArgumentParser(prog='blocklog.py', description='Append a blocked setup to blocked_setups.jsonl')
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('add')
    a.add_argument('--pair', required=True); a.add_argument('--side', required=True, choices=['long', 'short'])
    for k in ('entry', 'sl', 'tp', 'rr'): a.add_argument('--' + k, required=True, type=float)
    a.add_argument('--rank', type=int); a.add_argument('--mode', choices=['live', 'dry'], default='live')
    a.add_argument('--run-id', default=None); a.add_argument('--reason', default=None)
    try: o = ap.parse_args(argv)
    except SystemExit as e: return 0 if e.code == 0 else 2
    pair = o.pair.strip().upper()
    if not pair.endswith('-USDT') or len(pair) < 6: print(f'blocklog.py: --pair must look like XXX-USDT, got {o.pair!r}', file=sys.stderr); return 2
    for k in ('entry', 'sl', 'tp'):
        if not getattr(o, k) > 0: print(f'blocklog.py: --{k} must be > 0', file=sys.stderr); return 2
    run_id = o.run_id or os.environ.get('RUN_ID') or None
    line = build_line('agent', o.mode, pair, o.side, o.entry, o.sl, o.tp, o.rr, o.rank, 'news_gate', run_id=run_id, reason=o.reason or 'agent news gate block')
    try: wrote = append(line, dedupe_run=True)
    except Exception as e: print(f'blocklog.py: write failed: {str(e)[:200]}', file=sys.stderr); return 1
    print(('OK logged ' if wrote else 'DUP already logged (run_id+pair+side+entry) ') + f'{pair} {o.side} entry {o.entry} run_id={run_id}')
    return 0

if __name__ == '__main__': sys.exit(main())
