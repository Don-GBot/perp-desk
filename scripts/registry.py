"""Registry of positions the bot opened: DATA_DIR/bot_positions.json, keyed by instId.

A record only counts for a position if it MATCHES that position: same instId and side, then the
positionId when both the record and the position carry one (equal = match, different = no match). Only if
either side lacks a positionId: the stored position createTime within CT_TOL_MS (1 s) of the position's
createTime (the exchange can move a createTime by a few ms on the same position), else the position was
created within MATCH_WINDOW_MS (120 s) of the record's opened_at. Pair + side alone is never enough, so a
stale record can't make a later manual trade on the same pair look like the bot's. prune() drops records that match no
open position; execute.py and manage.py call it at the start of every run.
"""
import os, json, datetime as dt
import config

FILE = "bot_positions.json"
MATCH_WINDOW_MS = 120_000
CT_TOL_MS = 1000          # position_createTime tolerance, used only when either side lacks a positionId

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
    rpid, ppid = str(rec.get("positionId") or ""), str(p.get("positionId") or "")
    if rpid and ppid: return rpid == ppid          # both have a positionId: it decides (createTime is not consulted)
    try: ct = int(p.get("createTime") or 0)
    except Exception: ct = 0
    if not ct: return False
    pct = rec.get("position_createTime")
    try: pct = int(float(pct)) if pct else None
    except (TypeError, ValueError): pct = None
    if pct: return abs(ct - pct) <= CT_TOL_MS
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
        since = dt.datetime.now(dt.timezone.utc).isoformat()
        pend += [dict(v, pending_since=since) for v in removed.values() if (v.get("instId"), v.get("clientOrderId"), v.get("opened_at")) not in have]
        save_pending(pend); save(keep)
    return keep, removed

def prune(positions, reg=None):
    """Drop records with no matching open position. Returns (registry, removed_instIds)."""
    keep, removed = prune_full(positions, reg)
    return keep, sorted(removed)

class UnresolvedRecord(RuntimeError):
    """put() refused: another (unresolved) bot record holds the pair."""

def same_record(old, rec):
    if old.get("clientOrderId") and old.get("clientOrderId") == rec.get("clientOrderId"): return True
    a, b = old.get("position_createTime"), rec.get("position_createTime")
    return bool(a) and str(a) == str(b)

def put(rec):
    """Write rec for its pair. Never overwrites a DIFFERENT record on the same pair (same clientOrderId or same
    position_createTime = the same trade, an update): raises UnresolvedRecord instead."""
    reg = load(); old = reg.get(rec["instId"])
    if isinstance(old, dict) and not same_record(old, rec):
        raise UnresolvedRecord(f'{rec["instId"]}: unresolved bot record (opened {old.get("opened_at")}) present: not overwritten')
    reg[rec["instId"]] = rec; save(reg)

def unresolved(inst, positions, reg=None, pending=None):
    """Reason string if the pair holds an unresolved bot record (either side): a record matching no live position on
    the pair, or a pruned record (closed_pending.json) whose close line was not found yet (however it was closed); else None. Read errors raise (callers fail closed)."""
    reg = load_strict() if reg is None else reg
    rec = reg.get(inst)
    if isinstance(rec, dict) and not any(p.get("instId") == inst and matches(p, rec) for p in positions):
        return f'unresolved_record ({inst} {rec.get("side")} bot record opened {rec.get("opened_at")} has no live position and no close record yet)'
    for r in (load_pending() if pending is None else pending):
        if r.get("instId") == inst:
            return f'unresolved_record ({inst} {r.get("side")} bot record closed, close line not found yet, pending since {r.get("pending_since")})'
    return None
