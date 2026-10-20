"""Registry of positions the bot opened: DATA_DIR/bot_positions.json, keyed by instId.

A record only counts for a position if it MATCHES that position: same instId and side, and either
the stored position createTime equals the position's createTime, or the position was created within
MATCH_WINDOW_MS of the record's opened_at. The createTime comparison is a tolerance (within MATCH_WINDOW_MS), not
string equality: the exchange can report a createTime a millisecond off the one seen right after the fill. Pair + side alone is never enough, so a stale record can't
make a later manual trade on the same pair look like the bot's. prune() drops records that match no
open position; execute.py and manage.py call it at the start of every run.
"""
import os, json, datetime as dt
import config

FILE = "bot_positions.json"
MATCH_WINDOW_MS = 120_000

def load():
    try:
        with open(config.data_path(FILE)) as fh: reg = json.load(fh)
        return reg if isinstance(reg, dict) else {}
    except Exception:
        return {}

def save(reg):
    p = config.data_path(FILE); tmp = p + ".tmp"
    with open(tmp, "w") as fh: json.dump(reg, fh, indent=1, default=str)
    os.replace(tmp, p)

def iso_ms(x):
    try:
        t = dt.datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        if t.tzinfo is None: t = t.replace(tzinfo=dt.timezone.utc)
        return int(t.timestamp() * 1000)
    except Exception:
        return None

def matches(p, rec, window_ms=MATCH_WINDOW_MS):
    if not rec or rec.get("instId") != p.get("instId"): return False
    if (rec.get("side") == "buy") != (float(p.get("positions") or 0) > 0): return False
    try: ct = int(p.get("createTime") or 0)
    except Exception: ct = 0
    if not ct: return False
    pct = rec.get("position_createTime")
    try: pct = int(float(pct)) if pct else None
    except (TypeError, ValueError): pct = None
    if pct and abs(ct - pct) <= window_ms: return True
    oa = iso_ms(rec.get("opened_at"))
    return bool(oa and abs(ct - oa) <= window_ms)

def match(p, reg=None):
    reg = load() if reg is None else reg
    rec = reg.get(p.get("instId"))
    return rec if matches(p, rec) else None

PENDING = "closed_pending.json"   # pruned records waiting for their close line (closes.py), so no close is lost

def load_pending():
    try:
        with open(config.data_path(PENDING)) as fh: d = json.load(fh)
        return d if isinstance(d, list) else []
    except FileNotFoundError: return []

def save_pending(items):
    p = config.data_path(PENDING); tmp = p + ".tmp"
    with open(tmp, "w") as fh: json.dump(items, fh, indent=1, default=str)
    os.replace(tmp, p)

def load_strict():
    """Like load() but raises if the file exists and can't be read (callers fail closed)."""
    p = config.data_path(FILE)
    if not os.path.exists(p): return {}
    with open(p) as fh: reg = json.load(fh)
    if not isinstance(reg, dict): raise ValueError(f"{FILE} is not a mapping")
    return reg

def prune_full(positions, reg=None):
    """Drop records with no matching open position. Returns (registry, {instId: removed record}).
    Removed records are queued in closed_pending.json for closes.py, whoever prunes (execute.py or manage.py)."""
    reg = load() if reg is None else reg
    keep = {k: v for k, v in reg.items() if any(matches(p, v) for p in positions)}
    removed = {k: v for k, v in reg.items() if k not in keep}
    if removed:
        pend = load_pending(); have = {(x.get("instId"), x.get("clientOrderId"), x.get("opened_at")) for x in pend}
        pend += [v for v in removed.values() if (v.get("instId"), v.get("clientOrderId"), v.get("opened_at")) not in have]
        save_pending(pend); save(keep)
    return keep, removed

def prune(positions, reg=None):
    """Drop records with no matching open position. Returns (registry, removed_instIds)."""
    keep, removed = prune_full(positions, reg)
    return keep, sorted(removed)

def put(rec):
    reg = load(); reg[rec["instId"]] = rec; save(reg)
