"""Offline tests: transfers.py detect is idempotent (no double-logging in DATA_DIR/logs/transfers_detect.jsonl).
Dedupe by exchange transfer_id, falling back to type + amount + ts_utc. All mocked, fake IDs, no network.
Run: python3 -m unittest discover -s tests -v
"""
import os, json, unittest
import test_offline as T          # sets DATA_DIR / BOT_CONFIG to temp before the scripts are imported
from test_offline import NoNetwork
import transfers

SINCE = "2026-01-01T00:00:00Z"
BILLS = [dict(ts="1767225600000", amount="148.69657458", currency="USDT", fromAccount="spot", toAccount="futures", transferId="fake-0001"),
         dict(ts="1767225660000", amount="4.0", currency="USDT", fromAccount="funding", toAccount="futures", transferId="fake-0002")]

def get(p): return dict(code="0", data=BILLS if p.get("toAccount") == "futures" else [])

def lines():
    out = []
    try:
        with open(transfers.detect_log()) as fh:
            for l in fh:
                try: r = json.loads(l)
                except Exception: continue
                if isinstance(r, dict): out.append(r)
    except FileNotFoundError: pass
    return out

def write_log(*rows):
    with open(transfers.detect_log(), "w") as fh:
        for r in rows: fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")

class TestDetectDedupe(NoNetwork):
    def setUp(self):
        super().setUp()
        for p in (transfers.state_path(), transfers.state_path() + ".prev", transfers.detect_log()):
            if os.path.exists(p): os.remove(p)

    def test_first_detect_logs_each_once(self):
        f, n, a = transfers.detect(SINCE, False, get)
        self.assertEqual(len(n), 2); self.assertEqual(len(lines()), 2); self.assertEqual(a, [])

    def test_repeat_detect_reports_but_does_not_relog(self):
        transfers.detect(SINCE, False, get)
        f, n, a = transfers.detect(SINCE, False, get)
        self.assertEqual(len(n), 2); self.assertEqual(len(lines()), 2)

    def test_record_after_detect_adds_once_log_unchanged(self):
        transfers.detect(SINCE, False, get)
        f, n, a = transfers.detect(SINCE, True, get)
        self.assertEqual(len(a), 2); self.assertEqual(len(transfers.load_list()), 2); self.assertEqual(len(lines()), 2)

    def test_after_record_nothing_new(self):
        transfers.detect(SINCE, True, get)
        f, n, a = transfers.detect(SINCE, True, get)
        self.assertEqual(n, []); self.assertEqual(len(transfers.load_list()), 2); self.assertEqual(len(lines()), 2)

    def test_fallback_type_amount_time_when_logged_row_has_no_id(self):
        write_log(dict(type="deposit", amount=148.69657458, ts_utc="2026-01-01T00:00:00Z"))
        transfers.detect(SINCE, False, get)
        self.assertEqual(len(lines()), 2)
        self.assertEqual(sum(1 for r in lines() if r["amount"] == 148.69657458), 1)

    def test_dedupe_on_transfer_id_and_bad_lines_ignored(self):
        write_log(dict(type="deposit", amount=1.0, ts_utc="2000-01-01T00:00:00Z", transfer_id="fake-0002"), "not json", "[1, 2]")
        transfers.detect(SINCE, False, get)
        self.assertEqual([r.get("transfer_id") for r in lines()], ["fake-0002", "fake-0001"])

    def test_no_duplicate_ids_after_many_runs(self):
        for _ in range(3): transfers.detect(SINCE, False, get)
        transfers.detect(SINCE, True, get); transfers.detect(SINCE, False, get)
        ids = [r.get("transfer_id") for r in lines()]
        self.assertEqual(len(ids), len(set(ids))); self.assertEqual(len(ids), 2)

    def test_log_keys_empty_when_missing(self):
        lk = transfers.log_keys(); self.assertEqual((lk.ids, lk.keys), (set(), set()))

    def test_distinct_ids_same_type_amount_time_both_kept(self):
        global BILLS
        old = BILLS; BILLS = [dict(old[1]), dict(old[1], transferId="fake-0003")]
        try: f, n, a = transfers.detect(SINCE, True, get)
        finally: BILLS = old
        self.assertEqual(len(n), 2); self.assertEqual(len(a), 2); self.assertEqual(len(lines()), 2)

    def test_int_id_in_log_matches_str_id(self):
        write_log(dict(type="deposit", amount=1.0, ts_utc="2000-01-01T00:00:00Z", transfer_id=7))
        global BILLS
        old = BILLS; BILLS = [dict(old[0], transferId="7")]
        try: transfers.detect(SINCE, False, get)
        finally: BILLS = old
        self.assertEqual(len(lines()), 1)

    def test_float_amount_noise_still_dedupes(self):
        write_log(dict(type="deposit", amount=148.696574580001, ts_utc="2026-01-01T00:00:00Z"))
        transfers.detect(SINCE, False, get)
        self.assertEqual(len(lines()), 2)

    def test_state_add_stays_deduped(self):
        r = lambda: transfers.make("deposit", 5, "2026-01-02T00:00:00Z")
        self.assertTrue(transfers.append([r()])[0]); self.assertFalse(transfers.append([r()])[0])
        self.assertEqual(len(transfers.load_list()), 1)

if __name__ == "__main__": unittest.main()
