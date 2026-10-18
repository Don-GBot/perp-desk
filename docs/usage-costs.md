# Usage costs: lessons learned

LLM agents cost money and quota every time they wake up. These lessons come from running perp-desk for real.

## Each wake re-reads its context
An agent that wakes up re-reads its instructions, its memory and often its whole chat history. A long chat that wakes every 15 minutes pays for that history 96 times a day. Cost grows with both wake count and context size.

- **Prefer fewer wakes.** The Executor went from every 15 minutes to every 30. The setups use 1H and 4H candles; checking every 15 minutes found almost nothing extra.
- **Merge agents that run back to back.** The alerts scanner was its own agent. Now the Executor runs `scanner.py` as step 0. One wake instead of two.

## Put deterministic work in scripts
If the answer is arithmetic, write a script. Scanning, sizing, leverage, filters, the news gate and position management (stops, break-even, hard cut, closes) are all scripts. The agent runs them and reads a short result.

The shadow swap logger has no LLM at all. It runs from cron and costs nothing. A human or the Manager reads its log weekly.

## Avoid agent-to-agent messaging
Messages between agents wake the receiver, and each message lands in its chat history forever. The news analyst used to message the Executor every 4 hours. Now it writes `fundamentals.json` and the Executor reads it when it runs anyway.

Use shared files. Write them atomically (temp file, then rename) so a reader never sees half a file.

## Use a silence rule
No message when nothing happened. A bot that reports "no trade, all quiet" every 30 minutes fills its own chat and the human's. Report opens, closes, adjustments, real alerts and errors only.

Same error twice in a row: report once, then stop retrying. A failing step retried every run burns tokens and floods the chat.

## Keep runs lean
- Never paste whole JSON files or logs into the chat. Read them with a script or a filter and summarize in one line.
- Have scripts print short summaries.
- Do not let the agent explore. The prompt lists the steps; the agent runs those steps and stops.

## Reset long chats with a handoff summary
Chat history only grows. Every so often, have the agent write a handoff summary: current rules, open positions, open issues, recent decisions. Start a fresh chat with that summary. Do the busiest agent (the Executor) first.

## Rough checklist
- [ ] How many LLM wakes per day, across all agents?
- [ ] Is anything an agent does each run actually deterministic?
- [ ] Do any agents message each other?
- [ ] Does every agent have a silence rule?
- [ ] When was each chat last reset?
