# perp-desk

An AI-agent trading desk for crypto perps: scripts find the setups, agents make the calls, strict risk rules keep losses bounded.

A small set of AI agents and Python scripts. Currently supports Blofin USDT perpetuals. It works in any agent harness, or in plain cron.

> **Risk disclaimer.** Not financial advice. Leveraged futures can lose money, fast. This code places real orders with your account. Read it before you give it your keys.

## What it is

- **Scripts** do the deterministic work: find setups, size positions, pick leverage, check filters, log shadow decisions.
- **Agents** do the judgment work: run the trade check and manage open positions, rate the news, audit the others.
- **Shared files** connect them. Agents never message each other.

| Role | Prompt | Schedule | Trades? |
|---|---|---|---|
| Executor | `prompts/executor.md` | every 30 min (:07, :37) | yes, the only one |
| News analyst | `prompts/news-analyst.md` | every 4h | no |
| Manager | `prompts/manager.md` | daily + weekly | no |
| Alerts scanner (optional) | `prompts/scanner.md` | step 0 of the Executor, or :00/:30 | no |
| Shadow swap logger | none, plain script | every 15 min from cron | never |
| Shadow filter variants | none, plain script | every 5 min from cron | never |

## How the pieces fit

1. `scripts/scan.py` ranks the top 40 USDT perps by volume (crypto, commodities, forex; stock perps are blocked later), drops wide spreads, keeps pairs in a 4H trend, and builds setups with price-only R:R ≥ 3. Ranks 1–25 are the core universe; ranks 26–40 (`ext`) only trade with a fresh news-analyst rating (`scripts/universe.py`). Output: `data/scan.json`.
2. `scripts/scanner.py` (optional) writes alerts and info-only flags to `data/flags_latest.json`.
3. The news analyst rates each pair and writes `data/fundamentals.json`.
4. The Executor manages open positions with `scripts/manage.py`, filters setups (news gate, 1H trend gate, stock-perp block, universe tier, risk caps), sizes the trade (5% risk is a maximum, scaled down to the binding limit), and opens at most one trade with `scripts/execute.py`. Every skip is logged to `data/blocked_setups.jsonl`; `manage.py` writes run records, closed trades and funding paid.
5. `scripts/shadow_swap.py` logs whether swapping an open trade for a new setup would have paid. `scripts/shadow_variants.py` runs the same setups through alternative filter sets in virtual books. Neither ever trades.
6. The Manager audits everything against `RULEBOOK.md` and the logs.

See `docs/architecture.md` for the diagram.

## Quickstart

Needs Python 3.9+. No pip packages (PyYAML is used if installed).

1. Create a Blofin API key with READ + TRADE permission only (no transfer, no withdrawal). Put it in `.env`:
   ```sh
   cp .env.example .env        # then uncomment and fill in the three BLOFIN_ lines
   set -a; . ./.env; set +a
   ```
2. Run the scan (public data, no keys needed):
   ```sh
   python3 scripts/scan.py     # writes data/scan.json
   ```
3. Try a dry run first (every check, nothing placed), then the real executor, which places a real order if a setup passes every check:
   ```sh
   python3 scripts/execute.py --dry
   python3 scripts/execute.py
   ```

Defaults live in `config.example.yaml`. To change them, copy it to `config.yaml` in the repo root (or point `BOT_CONFIG` at a file) and edit. Bad values stop the scripts with a clear error.

**How much is at risk.** Each trade risks at most `risk_per_trade_pct` (default 5%) of equity at the stop, fees, a 0.18% entry slippage and a 0.18% exit slippage buffer included; it is scaled down when leverage, margin, liquidation distance or the risk caps don't allow the full size. The risk caps (`risk.risk_caps`, `scripts/riskcap.py`) keep total open risk at the current stops ≤ 20% of equity and same-direction bot risk ≤ 15%, with at most 8 positions; if the open risk can't be computed, the old slot caps (4 open, 3 bot per side) apply. Gaps and slippage can make a loss bigger than planned. Lower risk_per_trade_pct (and hard_cut_pct proportionally) for less risk.

## Adapting it to your harness

The prompts are plain markdown. Nothing is tied to one platform.

1. Create one agent per role and paste the matching `prompts/*.md` as its instructions.
2. Give it shell access to this repo and the env vars from `.env.example`. Only the Executor needs trading keys; give the others none, or a READ-only key.
3. Schedule it (see `docs/scheduling.md`): the harness's routine/cron feature, or system cron calling your harness CLI.
4. Run `shadow_swap.py` from plain cron, not from an agent.
5. Read `docs/usage-costs.md` before adding wakes or agent-to-agent messages.

## Repo layout

```
config.example.yaml   every tunable value, with comments
RULEBOOK.md           the rules, in plain words
prompts/              one role prompt per agent
scripts/              Python scripts (stdlib only): scan, scanner, execute, manage, news_gate, shadow_swap,
                      shadow_variants, transfers, runlog, blocklog, plus helpers: trend1h, stockperp,
                      universe, riskcap, slowcrowd, closes, funding, registry, blofin_client, config, lock
docs/                 architecture, scheduling, usage costs
tests/                offline unit tests (no network, no orders)
CHANGELOG.md          what changed, by date
examples/             fundamentals.json format
run.sh                wrapper for cron/systemd (loads .env)
```

## License

MIT. See `LICENSE`. Use at your own risk. The software comes with no warranty.
