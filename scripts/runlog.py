#!/usr/bin/env python3
"""Script-written run records in DATA_DIR/runs.json (a list).

manage.py appends ONE entry per invocation (source "manage.py", a run_id, also printed in its JSON output as run_id).
The agent then attaches its own fields to THAT entry instead of hand-writing a run record:

  python3 scripts/runlog.py annotate --run-id <run_id> --json '<JSON object>'   merge fields into that entry
  python3 scripts/runlog.py annotate --run-id <run_id> --json-file <path>       same, JSON from a file
  python3 scripts/runlog.py show --run-id <run_id|latest>                       print the entry (read-only)

Writes hold DATA_DIR/runs.lock (waits up to 20 s) and replace the file atomically. annotate never creates an entry
(unknown run_id -> exit 3). Top-level keys are merged (nested objects one level deep). Keys written by manage.py are
protected (exit 2). Exit codes: 0 ok | 1 write failed | 2 bad arguments/JSON/protected key | 3 run_id not found | 4 lock busy
"""
import os, sys, json, time, fcntl, secrets, argparse, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

FILE = 'runs.json'; LOCK = 'runs.lock'; LOCK_WAIT_S = 20.0; SOURCE = 'manage.py'; KEEP = 2000
PROTECTED = ('ts_utc', 'run_id', 'source', 'cmd', 'ok', 'pruned_records', 'closes', 'funding_errors', 'errors', 'annotated_utc')

class LockTimeout(Exception): pass

def now_z(): return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
def new_run_id(): return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + f'-{os.getpid()}-{secrets.token_hex(2)}'

class locked:
    def __init__(s, wait=None): s.path = config.data_path(LOCK); s.wait = LOCK_WAIT_S if wait is None else wait; s.fd = None
    def __enter__(s):
        s.fd = os.open(s.path, os.O_CREAT | os.O_RDWR, 0o644); end = time.monotonic() + s.wait
        while True:
            try: fcntl.flock(s.fd, fcntl.LOCK_EX | fcntl.LOCK_NB); return s
            except BlockingIOError:
                if time.monotonic() >= end: os.close(s.fd); s.fd = None; raise LockTimeout(f'{LOCK} busy for {s.wait:.0f}s')
                time.sleep(0.05)
    def __exit__(s, *e):
        if s.fd is not None:
            try: fcntl.flock(s.fd, fcntl.LOCK_UN)
            finally: os.close(s.fd)

def _load():
    try:
        with open(config.data_path(FILE)) as fh: runs = json.load(fh)
    except FileNotFoundError: return []
    if not isinstance(runs, list): raise ValueError(f'{FILE} is not a list')
    return runs

def _write(runs):
    p = config.data_path(FILE); tmp = p + '.tmp'
    with open(tmp, 'w') as fh: json.dump(runs[-KEEP:], fh, indent=1, default=str); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, p)

def append(entry, wait=None):
    with locked(wait):
        runs = _load()
        if any(isinstance(r, dict) and r.get('run_id') == entry.get('run_id') for r in runs): return False
        runs.append(entry); _write(runs)
    return True

def find(runs, run_id):
    idx = [i for i, r in enumerate(runs) if isinstance(r, dict) and r.get('source') == SOURCE and (run_id == 'latest' or r.get('run_id') == run_id)]
    return idx[-1] if idx else None

def annotate(run_id, fields, wait=None):
    if not isinstance(fields, dict) or not fields: raise ValueError('fields must be a non-empty JSON object')
    bad = [k for k in fields if k in PROTECTED]
    if bad: raise ValueError(f'protected key(s) written by manage.py: {bad}')
    with locked(wait):
        runs = _load(); i = find(runs, run_id)
        if i is None: raise KeyError(f'no manage.py run entry with run_id {run_id!r}')
        e = runs[i]
        for k, v in fields.items(): e[k] = dict(e[k], **v) if isinstance(v, dict) and isinstance(e.get(k), dict) else v
        e['annotated_utc'] = now_z(); _write(runs)
    return e

def main(argv=None):
    ap = argparse.ArgumentParser(prog='runlog.py'); ap.add_argument('cmd', choices=['annotate', 'show'])
    ap.add_argument('--run-id', required=True); g = ap.add_mutually_exclusive_group()
    g.add_argument('--json'); g.add_argument('--json-file')
    try: o = ap.parse_args(argv)
    except SystemExit as e: return 0 if e.code == 0 else 2
    if o.cmd == 'show':
        runs = _load(); i = find(runs, o.run_id)
        if i is None: print(f'runlog.py: no manage.py run entry with run_id {o.run_id!r}', file=sys.stderr); return 3
        print(json.dumps(runs[i], indent=1, default=str)); return 0
    if o.run_id == 'latest' or (o.json is None and o.json_file is None):
        print('runlog.py: annotate needs an exact --run-id and one of --json / --json-file', file=sys.stderr); return 2
    try: fields = json.loads(o.json) if o.json is not None else json.load(open(o.json_file))
    except Exception as e: print(f'runlog.py: bad JSON: {e}', file=sys.stderr); return 2
    try: annotate(o.run_id, fields)
    except LockTimeout as x: print(f'runlog.py: LOCKED: {x}; nothing written', file=sys.stderr); return 4
    except KeyError as x: print(f'runlog.py: {x.args[0]}; nothing written (never creates an entry)', file=sys.stderr); return 3
    except ValueError as x: print(f'runlog.py: {x}; nothing written', file=sys.stderr); return 2
    except Exception as x: print(f'runlog.py: write failed: {type(x).__name__}: {x}', file=sys.stderr); return 1
    print(f'RUNLOG annotated run_id={o.run_id} keys={sorted(fields)}'); return 0

if __name__ == '__main__': sys.exit(main())
