#!/usr/bin/env python3
"""Ops helper: swap files (a new script version, a config) or restart a logger only when the bot is idle.
Never kills a process and never trades. config.yaml -> ops.

  python3 scripts/swapguard.py check [--timeout S]                  # is it safe right now? (waits up to S)
  python3 scripts/swapguard.py swap SRC DST [SRC DST ...] [--timeout S]

guard(): refuses inside the freeze windows (ops.freeze_windows, minutes past the hour, box local time, around the executor
schedule) and waits until (1) no execute.py / manage.py process is running, dry or live (/proc/*/cmdline), and (2)
DATA_DIR/execute.lock (execute.py + manage.py) and DATA_DIR/scanner.lock are free (non-blocking flock probe). Polls every
ops.swap_poll_s (2 s) up to ops.swap_timeout_s (180 s); then SKIP.
swap(): validates every pair first (realpath; src must be staged in the same directory as dst), guard, then HOLDS execute.lock
and re-checks the freeze window, the scanner lock and execute/manage processes before os.replace(src, dst) per pair.
Each file is replaced atomically; the set is NOT atomic: a failure mid-set leaves earlier files swapped and is reported
as partial (exit 3). Freeze windows are minutes past the hour in the box's LOCAL time.
restart(fn): guard, then calls fn().
Exit codes: 0 ok, 3 skipped (busy, freeze window, or a staged file problem), 2 bad arguments.
Linux only (/proc).
"""
import os, sys, time, json, fcntl, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

O = config.CFG["ops"]
WATCH = ('execute.py', 'manage.py'); POLL_S = float(O["swap_poll_s"]); DEFAULT_TIMEOUT_S = float(O["swap_timeout_s"])
FREEZE = tuple(tuple(int(x) for x in w.split('-')) for w in O["freeze_windows"])
PROC = '/proc'

def default_locks():
    return (config.data_path('execute.lock'), config.data_path('scanner.lock'))

def running(proc=None, me=None):
    """[(pid, cmdline)] for processes whose argv runs execute.py or manage.py anywhere in argv (any path, interpreter or wrapper, dry or live)."""
    proc = proc or PROC; me = os.getpid() if me is None else me; out = []
    for d in os.listdir(proc):
        if not d.isdigit() or int(d) == me: continue
        try:
            with open(os.path.join(proc, d, 'cmdline'), 'rb') as fh: argv = fh.read().split(b'\0')
        except OSError: continue
        argv = [a.decode('utf-8', 'replace') for a in argv if a]
        if any(os.path.basename(a) in WATCH for a in argv): out.append((int(d), ' '.join(argv)[:200]))
    return out

def lock_free(path):
    """True if nobody holds an flock on path (probe with LOCK_NB, released at once). Missing file = free."""
    try: fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError: return True
    try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(fd, fcntl.LOCK_UN); return True
    except BlockingIOError: return False
    finally: os.close(fd)

def in_freeze(now=None):
    m = (now or dt.datetime.now()).minute
    return any(a <= m < b for a, b in FREEZE)

def busy(proc=None, locks=None):
    b = [f'pid {p}: {c}' for p, c in running(proc)]
    for l in (locks if locks is not None else default_locks()):
        if not lock_free(l): b.append(f'lock held: {os.path.basename(l)}')
    return b

def guard(timeout_s=None, proc=None, locks=None, now=None, sleep=time.sleep, clock=time.monotonic):
    timeout_s = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s; t0 = clock()
    while True:
        if in_freeze(now() if callable(now) else now): b = ['freeze window (' + ', '.join(f':{a:02d}-:{z:02d}' for a, z in FREEZE) + ')']
        else: b = busy(proc, locks)
        w = round(clock() - t0, 1)
        if not b: return dict(ok=True, waited_s=w, busy=[], reason='clear')
        if w >= timeout_s: return dict(ok=False, waited_s=w, busy=b, reason=f'still busy after {timeout_s:g}s: SKIP (nothing killed)')
        sleep(POLL_S)

def swap(pairs, timeout_s=None, proc=None, locks=None, now=None, sleep=time.sleep, clock=time.monotonic, hold_lock=None):
    """Each os.replace is atomic per file; the SET is not: if one replace fails mid-set the earlier ones stay done and the
    result reports ok=False, partial=True with swapped / failed (exit 3). Everything is validated before the first replace."""
    g = guard(timeout_s, proc, locks, now, sleep, clock)
    if not g['ok']: return dict(g, swapped=[])
    rp = [(os.path.realpath(s), os.path.realpath(d)) for s, d in pairs]
    for (src, dst), (a, b) in zip(pairs, rp):
        if not os.path.isfile(a): return dict(g, ok=False, swapped=[], reason=f'staged file missing: {src}')
        if os.path.dirname(a) != os.path.dirname(b):
            return dict(g, ok=False, swapped=[], reason=f'{src} not in the same directory as {dst} (os.replace must be atomic)')
        if a == b: return dict(g, ok=False, swapped=[], reason=f'{src} and {dst} are the same file')
        if os.path.isdir(b): return dict(g, ok=False, swapped=[], reason=f'{dst} is a directory')
        if not os.access(os.path.dirname(b), os.W_OK): return dict(g, ok=False, swapped=[], reason=f'directory of {dst} not writable')
    if len({b for _, b in rp}) != len(rp): return dict(g, ok=False, swapped=[], reason='same destination twice')
    fd = os.open(hold_lock or config.data_path('execute.lock'), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return dict(g, ok=False, swapped=[], reason='execute.lock taken between check and swap: SKIP')
        # recheck everything under the lock: freeze window, scanner lock (and other locks except the one we hold), processes
        if in_freeze(now() if callable(now) else now): return dict(g, ok=False, swapped=[], reason='entered a freeze window between check and swap: SKIP')
        held = os.path.realpath(hold_lock or config.data_path('execute.lock'))
        other = [l for l in (locks if locks is not None else default_locks()) if os.path.realpath(l) != held]
        bad = [os.path.basename(l) for l in other if not lock_free(l)]
        if bad: return dict(g, ok=False, swapped=[], busy=[f'lock held: {b}' for b in bad], reason='lock taken between check and swap: SKIP')
        r = running(proc)
        if r: return dict(g, ok=False, swapped=[], busy=[f'pid {p}: {c}' for p, c in r], reason='execute/manage started between check and swap: SKIP')
        done = []
        for a, b in rp:
            try: os.replace(a, b); done.append(b)
            except OSError as x:
                return dict(g, ok=False, partial=bool(done), swapped=done, failed=b, error=str(x)[:200],
                            reason=f'replace failed for {b} after {len(done)} of {len(rp)} files: set is PARTIAL, fix by hand')
        return dict(g, swapped=done, at=dt.datetime.now().strftime('%H:%M:%S'))
    finally: os.close(fd)

def restart(fn, timeout_s=None, **kw):
    g = guard(timeout_s, **kw)
    if not g['ok']: return dict(g, restarted=False)
    return dict(g, restarted=True, result=fn())

def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv); to = None
    if '--timeout' in a:
        i = a.index('--timeout')
        try: to = float(a[i + 1])
        except (IndexError, ValueError): print(__doc__); return 2
        del a[i:i + 2]
    if a[:1] == ['check']: r = guard(to)
    elif a[:1] == ['swap'] and len(a) >= 3 and len(a) % 2 == 1: r = swap(list(zip(a[1::2], a[2::2])), to)
    else: print(__doc__); return 2
    print(json.dumps(r)); return 0 if r['ok'] else 3

if __name__ == '__main__': sys.exit(main())
