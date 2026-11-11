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
| Timeframes | `services/timeframe.py` | Daily / weekly / monthly horizons: bar folding plus the lookback, warmup, time-stop and stop band each one needs |
| Regime | `services/advisor.py` | Which horizon the current tape, capital and positions actually suit |
| Indicators | `indicators/engine.py` | SMA / EMA / RSI / ATR / slope / volume ratio, plain Python |
| Strategies | `strategies/base.py` | Pure functions of the indicator snapshot, return a scored signal |
| Planning | `planning/builder.py` | Signal → executable plan: entry zone, ATR stop, 2R/3R targets, lot-aware size, fees, break-even, expiry |
| Fees | `fees/calculator.py` | Exchange rules + broker plans, all `Decimal` |
| Execution | `services/execution.py` | Fills → trades: lot checks, FIFO sells, plan snapshots, deviation in % and R, the `BrokerAdapter` seam |
| Monitor | `services/positions.py` | Open positions with live P&L, distance to stop, R multiple, de-duplicated alerts |
| Briefing | `services/today.py` | Turns positions and pending advice into a ranked to-do list |
| Delivery | `notify/` | Outbox pattern (advice and free-standing messages), retry, per-channel adapters |
| Receipts | `receipts/tokens.py` | HMAC short codes, 72h expiry, mobile page |
| Journal | `services/journal.py` | Trades, partial closes, IPO ledger, discipline stats |
| Backtest | `services/backtest.py` | Same strategies over history, event driven |
| Scheduling | `scheduler/service.py` | Interval scans, expiry sweep, dispatch |

## Non-negotiables

1. **Money is integer micro-units.** SQLite never stores a float price. Conversions
   happen only at the presentation boundary via `core/money.py`.
2. **The planner owns position size.** No agent, LLM or external service may change
   `qty`, `stop` or `target` after the plan is built.
3. **Agents never block a scan.** Every agent call has a fallback to the rules text.
4. **A plan without a stop does not exist.** `build()` returns `None` when the stop
   distance is outside the band for its horizon (1.5–15 % daily, 3–18 % weekly,
   5–30 % monthly) or when one lot exceeds the risk budget.
5. **Every recommendation is stamped with its market, its horizon and an expiry.**
   No open-ended advice. A weekly plan is not allowed to die at tonight's close.

## Lookahead safety

The backtest recomputes indicators on the prefix ending at the signal bar. It never
reads the full series, so a signal at bar *i* cannot see bar *i+1*. `LookaheadError`
exists to make this explicit; any future optimisation that breaks it should raise.

## Data flow for a scan

1. `run_scan()` resolves the horizon: `trade.horizon` if pinned, otherwise
   `services/advisor.py` scores daily / weekly / monthly against pool breadth,
   volatility, capital and fee drag.
2. `run_scan()` loads the watchlist for the enabled markets and asks for
   `horizon.lookback` daily bars — a fresh cache that is too shallow still gets
   re-fetched, otherwise monthly bars would never appear.
3. Bars are served from `bar_daily`, refreshed from Eastmoney when stale, and a
   trailing bar for a session that has not closed is dropped so a half-built
   candle cannot drive indicators. Daily bars are then folded to the horizon's
   bar size.
4. Each strategy scores the snapshot; the best score above `scan.min_score` wins,
   and rules measured as losing money *on this bar size* are skipped.
5. `build()` converts the signal into a plan (or refuses).
6. The recommendation is stored with its horizon, a receipt token minted, and a
   notification queued. The exposure ceiling is applied across the whole batch,
   so three plans risking 2 % each are not counted as independent.
7. `dispatch()` sends it; the phone link (or the advice card) records a real
   `trade` row through `services/execution.py`, with the deviation between plan
   and fill in both percent and risk units.
8. `services/positions.py` watches what is open — on every scheduler tick, and on
   every page load — and raises a stop / target alert once per position and kind.
9. `services/today.py` folds the whole picture into the home page: what is urgent,
   what is pending, and what needs no action at all.

## Failure modes

- Provider outage → circuit breaker opens for 5 minutes, next provider serves the
  request, cached bars keep the scanner alive.
- No setup today → the scan stores a diagnostic summary (`near`, `blocked_lot`) so
  the UI can explain why nothing fired instead of looking broken.
- Notification channel down → the row stays `pending` with the error, other channels
  still deliver.
