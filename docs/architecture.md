# Architecture

Kestrel is a single Python process with a pluggable shell. The same code runs as a
desktop window, a local browser app, or a container behind a reverse proxy.

```
                 ┌──────────────── daemon (FastAPI + APScheduler + SQLite) ───────────────┐
 market data  →  │ datasources → indicators → strategies → planning → recommendation     │
 (public APIs)   │                                      ↓                                │
                 │                     notification outbox → channels (ntfy/webhook/…)      │
                 │                                      ↓                                │
                 │                     signed receipt link → receipt page → deviation      │
                 │                                      ↓                                │
                 │                            journal → discipline stats                    │
                 └───────────────────────────────────────────────────────────────────────┘
                                              ↑
                                  browser / pywebview window / tray
```

## Layers

| Layer | Module | Responsibility |
|---|---|---|
| Data | `datasources/` | One adapter per provider, circuit breaker fallback chain, daily bar cache |
| Indicators | `indicators/engine.py` | SMA / EMA / RSI / ATR / slope / volume ratio, plain Python |
| Strategies | `strategies/base.py` | Pure functions of the indicator snapshot, return a scored signal |
| Planning | `planning/builder.py` | Signal → executable plan: entry zone, ATR stop, 2R/3R targets, lot-aware size, fees, break-even, expiry |
| Fees | `fees/calculator.py` | Exchange rules + broker plans, all `Decimal` |
| Agents | `services/agent.py` | Optional prose rewriting (rules / LLM / WorkBuddy bridge). Never touches sizes or prices |
| Delivery | `notify/` | Outbox pattern, retry, per-channel adapters |
| Receipts | `receipts/tokens.py` | HMAC short codes, 72h expiry, mobile page |
| Journal | `services/journal.py` | Trades, positions, IPO ledger, discipline stats |
| Backtest | `services/backtest.py` | Same strategies over history, event driven |
| Scheduling | `scheduler/service.py` | Interval scans, expiry sweep, dispatch |

## Non-negotiables

1. **Money is integer micro-units.** SQLite never stores a float price. Conversions
   happen only at the presentation boundary via `core/money.py`.
2. **The planner owns position size.** No agent, LLM or external service may change
   `qty`, `stop` or `target` after the plan is built.
3. **Agents never block a scan.** Every agent call has a fallback to the rules text.
4. **A plan without a stop does not exist.** `build()` returns `None` when the stop
   distance is outside 1.5–15 % or when one lot exceeds the risk budget.
5. **Every recommendation is stamped with its market and an expiry.** No open-ended
   advice.

## Lookahead safety

The backtest recomputes indicators on the prefix ending at the signal bar. It never
reads the full series, so a signal at bar *i* cannot see bar *i+1*. `LookaheadError`
exists to make this explicit; any future optimisation that breaks it should raise.

## Data flow for a scan

1. `run_scan()` loads the watchlist for the enabled markets.
2. Bars are served from `bar_daily`, refreshed from Eastmoney when stale.
3. Each strategy scores the snapshot; the best score above `scan.min_score` wins.
4. `build()` converts the signal into a plan (or refuses).
5. The recommendation is stored, a receipt token minted, and a notification queued.
6. `dispatch()` sends it; the phone link writes back a `receipt` row with the
   deviation between plan and fill.

## Failure modes

- Provider outage → circuit breaker opens for 5 minutes, next provider serves the
  request, cached bars keep the scanner alive.
- No setup today → the scan stores a diagnostic summary (`near`, `blocked_lot`) so
  the UI can explain why nothing fired instead of looking broken.
- Notification channel down → the row stays `pending` with the error, other channels
  still deliver.
