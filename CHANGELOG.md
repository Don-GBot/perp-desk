# Changelog

## 2026-10-20

Entry rules (both on in `config.example.yaml`; turn off in `config.yaml` if you don't want them):
- **1H trend gate** (`entry.trend_1h`, `scripts/trend1h.py`): longs only in a 1H uptrend, shorts only in a 1H downtrend
  (EMA20/EMA50 and close vs EMA50 on 200 closed 1H candles). Flat blocks both. A data error skips the setup.
  Applied in `execute.py` and mirrored in `shadow_swap.py`.
- **Stock-perp block** (`entry.stock_perp_block`, `scripts/stockperp.py`): no new entries on stock perps (maintained list
  of exchange Stocks/Indices pairs, or assetClass Stocks/Indices for new listings). Checked first, fails closed.
  Open positions are never touched. `stockperp.asset_class()` tags pairs as stock / index / commodity / forex / crypto.

Logging (no rule change):
- `blocked_setups.jsonl`: one line per news-gate, 1h-trend or stock-perp block (`scripts/blocklog.py`, also a CLI for
  the agent). New `execute.py --dry`; dry-run lines go to `logs/blocked_setups_dry.jsonl`, and a dry run never
  overwrites `exec_last.json`.
- Run records: `manage.py` appends one record per invocation to `runs.json` and prints its `run_id`;
  `scripts/runlog.py annotate` lets the agent attach fields instead of hand-writing records.
- Closed trades: `manage.py` logs each closed bot position to `closed_trades.jsonl` (`scripts/closes.py`).
- Funding: `manage.py` records funding paid per bot position from the exchange funding-fee bills (`scripts/funding.py`).
- Transfers: `scripts/transfers.py` logs owner deposits/withdrawals (`add`, `list`, `detect --since` from the exchange
  bills, read-only) so equity changes are not mistaken for PnL.

Fixes:
- Position / close matching: a recorded position createTime now matches within 120 s instead of by exact string, so a
  createTime reported 1 ms off no longer makes a bot position look manual or leaves its close unmatched. Closes match
  by positionId first, a known opposite side is rejected, and one history row never closes two records.
- `scanner.py`: one pass at a time (`scanner.lock`), skip if a full pass ran in the last 5 min, and if every flag check
  errors the previous `flags_latest.json` is kept and `last_run` is not advanced (the next run retries at once).
  A dedup-only empty pass also keeps the previous flags. State writes are fsync'd and atomic.

Shadow:
- New `scripts/shadow_variants.py` (read-only): virtual books BASELINE, BASELINE_NO1H, V_1H, V_ATR15, V_RR10,
  STOCKS_ONLY, V_SLOWTREND, V_CROWD walk every new scan with their own filters and track exits; `--stats` grades them.
  Pure helpers for the momentum z-score and funding crowding in `scripts/slowcrowd.py`.

Tests: new `tests/test_gates_and_logs.py` (all mocked, no network).

## 2026-10-17

- First public release.
