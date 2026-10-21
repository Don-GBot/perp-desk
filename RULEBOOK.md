# Rulebook

The single list of rules for perp-desk. Values live in `config.yaml`; this file explains them. The bots read this file. They do not change it.

**Who can change rules:** only the human owner, or the Manager acting on the owner's explicit approval. A bot that wants a rule changed asks. Bots never change each other's rules, code, schedules or state.

---

## Team-wide

| # | Rule |
|---|---|
| T1 | Blofin USDT-margined perpetual futures only. Funds in other accounts are ignored. |
| T2 | API keys come only from environment variables (`BLOFIN_API_KEY`, `BLOFIN_API_SECRET`, `BLOFIN_PASSPHRASE`). Trade permission only, withdrawals disabled. Never use keys pasted in chat. |
| T3 | Bots never transfer or convert funds. |
| T4 | Only the Executor places orders. `scan.py` / `scan.json` is the only setup source. |
| T5 | Pipeline: the alerts scanner writes `flags_latest.json`; the news analyst writes `fundamentals.json`; the Executor reads both plus `scan.json` and applies its own risk rules. Agents do not message each other. Shared files are the interface. |
| T6 | Scanner flags are info only. They are recorded, never used to enter, size, skip or exit. |
| T7 | No swaps. No bot closes a trade to swap into another without the owner's approval. |
| T8 | Schedules: see `docs/scheduling.md`. Small start-time drift is acceptable. |
| T9 | Quiet by default. The Executor reports opens, closes, adjustments and errors. The others report only new, urgent items or failures. |
| T10 | Orders are real. Only the Executor's environment holds trading keys. All order actions go through `execute.py` and `manage.py`, which hold a shared lock so two actions never overlap. |

---

## 1. Alerts scanner (optional; `scripts/scanner.py`)

Never trades. Never produces setups. Sends nothing to the Executor.

| Rule | Value |
|---|---|
| Universe | Identical to the Executor's: top `universe_ext_size` (40; `universe_size` 25 if `universe_ext_enabled` is off) USDT perps by 24h quote volume, then drop spread > `max_spread_pct`. |
| Big move | abs(change from 24h open) ≥ `move_alert_pct` (5%). |
| Re-alert | Only on a direction flip, or a further same-direction move ≥ max(`realert_min_pts`, `realert_frac` × last reported move). Memory `memory_hours` (24h). |
| New in universe | Pair not seen in the universe in the last 24h. |
| Coils | 12×1H range / median prior range < `coil_ratio` (0.5). Report the `coil_top` (3) tightest, new once per 24h. Coils only ride along with another alert. |
| Flags | Last 2 **closed** 15m candles. unusual_volume (≥ 2× avg of prior 20), volume_spike (≥ 3×), breakout/breakdown, strong_move, behind a strong-close and no-chase gate. Deduped on (pair, type, candle_close_time). Info only. |

---

## 2. Executor

### Run order
1. Run the alerts scanner (step 0) and read its summary.
2. Manage open positions.
3. Refresh the scan if older than `max_scan_age_min` (30) and a slot is free.
4. Filter setups.
5. Open at most one trade.
6. Log the run.

### Entry filters
| Rule | Value |
|---|---|
| Minimum R:R | ≥ `min_rr` (3) at the live price (ask for long, bid for short), price-only: (target − entry) / (entry − stop), mirrored for shorts, stop and target rounded to tick. Fees are not deducted from R:R; sizing covers them. |
| Side geometry | After rounding: stop < entry < target for longs, target < entry < stop for shorts. |
| Fresh scan | `scan.json` no older than `max_scan_age_min` (35). |
| Stop vs ATR | Stop distance ≥ `min_stop_atr` (1) × ATR14 on 1H. The order script allows `atr_tolerance` (0.95) for drift. |
| Trend | With the 4H trend: EMA20 > EMA50 and close > EMA50 for longs, mirror for shorts. Flat pairs are skipped. Re-checked at order time. |
| 1H trend gate | `entry.trend_1h` (on): longs only in a 1H uptrend, shorts only in a 1H downtrend (EMA20/EMA50 + close vs EMA50 on 200 closed 1H candles, `scripts/trend1h.py`). Flat blocks both. A data error skips the setup (fails closed). Logged as `1h_trend`. |
| Stock perps | `entry.stock_perp_block` (on): no new entries on stock perps (equities, ETFs, index perps, tokenized stocks): the maintained list in `scripts/stockperp.py` OR exchange assetClass Stocks/Indices. Checked first; unreadable metadata for an unlisted pair skips (fails closed). Open positions are never touched. Logged as `stock_perp`. |
| Equity | Skip if equity can't be read. |
| Universe tier | Volume ranks 1–`universe_size` (25) are `core`. Ranks beyond that (26–40, `ext`) also need a fresh news-analyst rating for the pair (`fundamentals.json`, younger than `news_gate.fresh_hours`), else skip `no_fundamentals` (fails closed). The rank is fixed per scan (`scan.json` `vol_ranks`). `scripts/universe.py`. |
| Caps | Risk caps (`risk.risk_caps`, `scripts/riskcap.py`): open risk of a position = loss if its CURRENT stop is hit from the live mark, incl. exit taker fee and slippage; 0 only if a stop-out nets ≥ 0 after entry + exit fees and slippage; a manual position with no stop counts 5% of equity. Total open risk + the new trade ≤ `total_pct` (20%) of equity; same-direction BOT open risk + new ≤ `direction_pct` (15%). The new trade is scaled down to the headroom (skip `cap_risk` if it then falls below the minimum). Hard backstop `max_positions` (8). Fail closed: if any position's risk can't be computed (no mark, stop orders covering only part of the size, bot position without a stop, ambiguous ownership, orders unreadable), the slot caps apply: `max_open` (4) overall incl. manual, `max_per_direction` (3) bot positions per side. Entry-only, never closes anything. |
| Unresolved record | `entry.unresolved_record_block` (on): no entry, either side, on a pair holding a bot record with no matching live position, or a closed bot record whose close line was not found yet. `bot_positions.json` never overwrites another trade's record. |
| Re-entry cooldown | After a bot position closes at a loss by its stop-loss, trailing stop or hard cut: no new entry on that pair in the same direction for `cooldown_hours` (4h) from the close. Opposite direction allowed. Wins, break-even scratches (BE must have fired; exit at/beyond the BE stop minus a slippage tolerance, or a loss under 0.5R fee-inclusive), manual positions and manual closes don't trigger it. A read error skips the entry. |
| No duplicates | No new trade on a pair with any position. |
| One per run | At most one new open per run. If the best setup fails a check, the next one is tried. |
| Position mode | Account must be in net (one-way) mode. |
| News gate | See below. |
| Macro window | Off by default (`macro_window.enabled: false`) because nothing feeds it yet. When on: no new entries from `hours_before` (2h) before to `hours_after` (1h) after a dated macro event. |

### Setup definition (`scan.py`)
- Long: entry = ask. Stop = the lower of (lowest low of the last 12 1H candles − 0.25 ATR) and (entry − 1 ATR). Target = highest high of the last 60 4H candles.
- Short: the mirror.
- Keep setups with R:R ≥ 3, best R:R first.

### Sizing, leverage, margin
| Rule | Value |
|---|---|
| Risk per trade | A MAXIMUM of `risk_per_trade_pct` (5%) of live equity lost at the technical stop, including entry slippage (`entry_slip_frac` 0.18%), entry and exit taker fees and the exit slippage buffer (`slip_frac` 0.18% of entry). Total open risk is capped by the risk caps (20%). |
| Size | Full size = floor(equity × risk / (contract value × (stop distance + entry_slip_frac × entry + fee × (entry + stop) + slip_frac × entry)), lot size). Skip if below the instrument minimum. With `risk.scale_down` (on) the size is then cut, never raised, lot-rounded, to the binding limit: the risk-cap headroom, then leverage ≤ min(`max` 20, instrument max, highest L whose estimated liquidation is > 2× the stop distance), then margin ≤ min(25% equity, 95% free margin). |
| Fee-heavy skip | Only a scaled-down trade is skipped (`fee_heavy`) if round-trip taker fees are more than `fee_heavy_max_share` (1/3) of its stop-out loss; a full-size (unscaled) trade is never skipped for its fee share. The share's denominator is stop distance + taker fees + exit slip and deliberately excludes the entry slip used in sizing, so the threshold keeps its meaning. A scaled trade below the instrument minimum is skipped `below_min_size`. |
| Margin mode | `margin_mode` (isolated) for new bot trades. |
| Leverage | Lowest integer L in [`min`, allowed max] whose margin = notional / L ≤ min(`margin_cap_pct` (25%) of equity, 95% of free margin). With `scale_down` off: skip if none fits. |
| Liquidation buffer | Skip if stop distance ≥ `max_stop_to_liq` (0.5) × estimated liquidation distance, i.e. liquidation must be more than 2× further than the stop (`liq`). |
| Existing pairs | Never change leverage on a pair with any existing position. |

Note: with a small account, low-volatility pairs (1H ATR under ~1%) often fail the leverage fit and never trade. That is expected.

- **Minimum risk** (`risk.min_risk_pct`, 1%): if the fee-inclusive risk after all scaling is below 1% of equity, skip as `below_min_risk` (block line). Not worth the fees.
- **R** is reported fee-inclusive (`pnl_net / planned risk incl. fees + entry slip`); `R_price` is the price-only number. The 0.5R break-even scratch test uses fee-inclusive R.

### Orders
| Rule | Value |
|---|---|
| Entry | Market order with attached TP (target) and SL (technical stop), both market on trigger. |
| Trailing stop | Right after the fill: full size, reduce-only, activates at +`trail_activate_r` (1R) from the average fill, callback `trail_callback_atr` (1.5) × 1H ATR. |
| Verify | Confirm TP/SL and the trailing stop are live, and liquidation is well beyond the stop. |
| One trail | Cancel the old trailing stop before placing a new one. |

### Managing open trades
| Rule | Value |
|---|---|
| Missing stop | Add one. |
| Break-even | When price reaches +`breakeven_at_r` (0.75) × initial stop distance: stop to entry ± round-trip taker fees (`breakeven_fee_legs` 2 × 0.06% = 0.12%), rounded to the tick in the trade's favour, so a BE exit nets about 0. A recorded BE stop keeps priority. Keep the target, replace the trail with `breakeven_trail_atr` (1) × ATR. Once per position. |
| Hard cut | Each bot position may lose at most `hard_cut_pct` (5.5%) of equity at entry at its stop, entry and exit fees and the slippage buffer (`fees.slip_frac` 0.18% of entry) included, the same terms as sizing. Manual positions (only when `protect_manual_trades` is false) use the price-only distance. Known limit: the cut cannot catch slippage inside the exchange's own stop fill. Equity at entry comes from `bot_positions.json`; if unknown, the wallet balance without unrealized PnL. Tighten looser or missing stops, never widen. Close at market a position already down that much, or one with no stop whose mark is already past where the stop would go (`manage.py hard-cut`, `manage.py ensure-stops`). |
| Margin mode | Always use the position's own margin mode. Never switch it. |
| Never resize | Do not resize open trades. |
| Flagged news | Medium or high confidence may tighten a stop, never widen it. Low confidence is logged only. |

### Manual trades (opened by the owner)
Controlled by `bot.protect_manual_trades` (default `true`). A position is the bot's if a `bot_positions.json` record matches it, or the exchange order history shows a filled, non-reduce-only opening order with the bot's client order id prefix (`bot.client_order_prefix`) created within 120 s of the position. A record matches only if pair and side agree and the position was created within 120 s of the record's `opened_at` (or has the recorded position create time). Pair and side alone never count, so an old record cannot claim a later manual trade. Records that match no open position are pruned at the start of every `execute.py` and `manage.py` run. With `bot.adopt_from_history` (on), `manage.py` rebuilds the missing record of a position owned through the order history (entry = average price, stop / target = the live TP/SL), so it gets full bot treatment; it never overwrites an existing record.
- `true`: the hard cut, break-even and scripted closes apply only to bot positions. A manual position without a stop gets an alert to the owner; the bot places nothing on it. The bot never closes, trims, adds to or re-levers a manual position.
- `false`: manual positions are managed exactly like bot positions.
Either way, manual positions block bot entries on that pair and count toward the caps.

### News gate (`scripts/news_gate.py`)
| Rule | Value |
|---|---|
| Source | `fundamentals.json`, written by the news analyst. |
| Freshness | A rating is valid for `fresh_hours` (4.5h) from its `ts_utc`, the real rating time. |
| Block | Only a fresh opposing rating with medium or high confidence blocks: bearish blocks a long, bullish blocks a short. |
| Low confidence | Logged only. |
| Neutral | Never blocks. |
| Fail open | If no rating is younger than `fail_open_after_hours` (~5h), or the file is missing, the gate allows entries. |
| Enforced by | `execute.py`, on every candidate, together with the macro window when enabled. |

- Fundamentals freshness is one setting, `news_gate.fresh_hours` (4.5h), for the gate, the opposing-rating check, and `no_fundamentals`. The cooldown (4h) is separate.

### Logging
Every run, open position and closed trade goes in `state.json`. Each new entry records its news rating and `scanner_flag` (that pair's flags within the last 2 closed 15m candles, or `[]`).

Script-written logs (logging only; none of them changes a stop, order or close):
| Log | Written by | Content |
|---|---|---|
| `blocked_setups.jsonl` | `execute.py` (and `blocklog.py add` for the agent) | One line per skipped setup (`logging.log_all_skips`; run-level exits — stale scan, equity unreadable, max positions, not net mode — write no line and only return a gate_reason; gate_reason e.g. news_gate, 1h_trend, stock_perp, no_fundamentals, cooldown, dir_cap, overall_cap, unresolved_record, fee_heavy, cap_risk, liq, below_min_size): pair, side, levels, rating, gate_reason, asset_class, universe_tier, vol_rank. `--dry` runs go to `logs/blocked_setups_dry.jsonl` instead, except the scheduled routine's dry decision run (RUN_ID = a manage.py run record from the last 30 min), which logs to the live file with mode `scheduled_dry`. |
| `runs.json` | `manage.py` | One run record per invocation (run_id in the output). The agent adds its own fields with `runlog.py annotate`, never by hand. |
| `closed_trades.jsonl` | `manage.py` | One line per closed bot position, matched to the exchange positions history by positionId, else createTime within 120 s (one history row per record). |
| funding fields | `manage.py` | `funding_paid` / `funding_n` on open bot records and closed-trade lines (exchange funding-fee bills, read-only). |
| `transfers.json` | `transfers.py` | Owner deposits and withdrawals, so equity changes are not read as PnL. `detect --since` lists futures-account transfers from the exchange bills (read-only). |

---

## 4. News analyst
| Rule | Value |
|---|---|
| Role | Rate each pair bullish / bearish / neutral with confidence low / medium / high, sources and the real rating time. |
| Coverage | The Executor's universe plus any pair with an open position. |
| Cadence | Every 4h. |
| Output | `fundamentals.json`, written atomically. Never messages other agents. |
| Never trades | Advisory only. |

---

## 5. Manager
Audits the bots against logs and this rulebook. See `prompts/manager.md`.

## Operating principles
1. One front door: the owner talks to the Manager, which relays approved changes.
2. No bot grades its own work.
3. No "done" without proof: a change counts only when you can show what changed and where.
4. Humans own the irreversible: rule changes, code changes and anything with real money need the owner's go.

## Usage rules
- Silence rule for all bots: no message when nothing happened.
- Same error twice in a row: report once, stop retrying.
- Lean runs: no full JSON or log dumps in chat.
- Long chats get reset with a handoff summary.

## Known gaps (decide these for your own setup)
- **Macro events need a source.** The window ships disabled because no calendar ships with this repo. The news analyst can add events to `fundamentals.json` under `macro.events`, or you can wire a calendar.
- **Break-even uses the current mark price**, not the best price since entry. A move that reaches the trigger and reverses between runs is missed.
- **Rating validity (4.5h) is only 30 min longer than the news cadence (4h).** A news run more than 30 min late leaves a gap where ratings are not fresh but the gate has not failed open yet. Entries are allowed in that gap.
- **Funding is not in R:R or the risk cap** (taker fees and a slippage buffer are). It is logged per position only.
- **Setup choice:** `execute.py` takes the first (best R:R) setup unless the agent passes `--inst`.
- **Ownership needs timestamps.** A position the bot opened is recognized by create time (within 120 s of the record or of a bot-prefixed opening order). If the exchange reports a create time far from the order time, the bot treats the position as manual.
