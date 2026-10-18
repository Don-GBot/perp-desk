"""Minimal Blofin REST client (public + signed private calls). Python stdlib only.

Credentials come ONLY from environment variables:
  BLOFIN_API_KEY, BLOFIN_API_SECRET, BLOFIN_PASSPHRASE
Create the key with READ + TRADE permission only. Never give it TRANSFER or withdrawal rights.
Keys are sent in HTTP headers by urllib; they never appear in a process argument list.

Public calls need no keys. Private calls raise MissingKeys when the keys are not set.
"""
import os, sys, time, hmac, hashlib, base64, json, uuid, socket, secrets
import urllib.request, urllib.error, urllib.parse

BASE = os.environ.get("BLOFIN_BASE_URL", "https://openapi.blofin.com")
KEY_VARS = ("BLOFIN_API_KEY", "BLOFIN_API_SECRET", "BLOFIN_PASSPHRASE")

class MissingKeys(RuntimeError):
    pass

class RequestUncertain(RuntimeError):
    """The request may have reached the exchange but no usable response came back
    (timeout, dropped connection, unreadable body). For order calls, check the order state
    before assuming it failed."""
    pass

def client_order_id(prefix, kind=""):
    """Unique alphanumeric client order id, max 32 chars: prefix + kind + ms timestamp + 8 hex."""
    cid = f"{prefix}{kind}{int(time.time() * 1000)}{secrets.token_hex(4)}"
    if not cid.isalnum() or len(cid) > 32: raise ValueError(f"bad client order id {cid!r}")
    return cid

def has_keys():
    return all(os.environ.get(k) for k in KEY_VARS)

def require_keys(script):
    """Exit with a clear message if the account keys are missing."""
    if not has_keys():
        missing = [k for k in KEY_VARS if not os.environ.get(k)]
        sys.exit(f"{script}: needs Blofin API keys. Missing: {', '.join(missing)}. Put them in .env (see .env.example).")

def _http(method, url, headers=None, body=None, timeout=20):
    h = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (perp-desk)"}
    h.update(headers or {})
    data = body.encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try: return json.loads(raw)
        except Exception: raise RuntimeError(f"HTTP {e.code}: {raw[:300]}")
    except (socket.timeout, TimeoutError) as e:
        raise RequestUncertain(f"timeout after {timeout}s: {e}")
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)): raise RequestUncertain(f"timeout after {timeout}s: {e.reason}")
        raise RuntimeError(f"network error: {e.reason}")   # could not connect: request not sent
    except (ConnectionError, OSError) as e:
        raise RequestUncertain(f"connection dropped: {e}")
    try: return json.loads(raw)
    except Exception: raise RequestUncertain(f"unreadable response: {raw[:300]}")

def _qs(params):
    return ("?" + urllib.parse.urlencode(params)) if params else ""

def public(path, params=None):
    return _http("GET", BASE + path + _qs(params))

def private(method, path, params=None, body=None):
    if not has_keys():
        raise MissingKeys("missing BLOFIN_API_KEY / BLOFIN_API_SECRET / BLOFIN_PASSPHRASE in the environment")
    rp = path + _qs(params)
    b = "" if body is None else json.dumps(body, separators=(",", ":"))
    ts = str(int(time.time() * 1000)); nonce = str(uuid.uuid4())
    msg = rp + method + ts + nonce + b
    sig = base64.b64encode(hmac.new(os.environ["BLOFIN_API_SECRET"].encode(), msg.encode(), hashlib.sha256).hexdigest().encode()).decode()
    h = {"ACCESS-KEY": os.environ["BLOFIN_API_KEY"], "ACCESS-SIGN": sig, "ACCESS-TIMESTAMP": ts,
         "ACCESS-NONCE": nonce, "ACCESS-PASSPHRASE": os.environ["BLOFIN_PASSPHRASE"]}
    return _http(method, BASE + rp, h, b if body is not None else None)

def resp_ok(r):
    """True only if the top-level code AND every per-item data code are "0".
    Blofin can return code=0 at the top level with the real failure in data.code."""
    if not isinstance(r, dict) or str(r.get("code")) != "0": return False
    d = r.get("data")
    items = d if isinstance(d, list) else [d] if isinstance(d, dict) else []
    return all(str(x.get("code", "0")) in ("0", "None") for x in items if isinstance(x, dict))

def resp_err(r):
    if not isinstance(r, dict): return str(r)[:200]
    d = r.get("data")
    items = d if isinstance(d, list) else [d] if isinstance(d, dict) else []
    sub = [f"{x.get('code')} {x.get('msg')}" for x in items if isinstance(x, dict) and str(x.get("code", "0")) not in ("0", "None")]
    return f"code {r.get('code')} {r.get('msg')}" + (f"; {'; '.join(sub)}" if sub else "")
