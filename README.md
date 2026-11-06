<p align="center">
  <img src="assets/brand/logo.svg" alt="Kestrel 隼" width="420">
</p>

# Kestrel 隼

**Watch. Then strike.** — a self-hosted, multi-market stock scanner and trading journal that keeps you honest.

[中文说明](README.zh-CN.md) · [Architecture](docs/architecture.md) · [Fee model](docs/fee-model.md) · [Notifications](docs/notifications.md) · [Contributing](CONTRIBUTING.md)

---

## Why

Most tools help you *find* trades. Kestrel also asks the harder question afterwards:
**did you actually follow your plan?**

Every recommendation carries an entry zone, a stop, a target, a position size, the
fees, the break-even price and an expiry. It is pushed to your phone with a one-tap
receipt page. Tap "executed", type the real fill, and the deviation between the plan and
your behaviour becomes a number you can improve.

## What it does

- **Multi-market** — A-shares, Hong Kong and US, each with its own switch.
- **Plans, not tips** — entry zone, ATR stop, 2R/3R targets, lot-aware sizing, fee-aware
  break-even, same-day expiry, and a plan is refused when one lot already breaks the
  risk budget.
- **Three rule strategies** — trend pullback, 20-day breakout, mean reversion. All
  explainable, all backtestable with the same code path.
- **Receipt loop** — signed short links over ntfy, webhook, WeClawBot or SMTP; the fill
  and its deviation are stored next to the plan.
- **Journal** — trades, positions, IPO ledger, discipline stats that compare the trades
  where you followed the rules with the ones where you did not.
- **Cost engine** — exchange rules plus ten broker plans (Fosun, uSmart, ZA Bank, Futu,
  Tiger, IBKR, generic A-share), editable in the UI.
- **Learning mode** — plain-language explanations under every plan, plus a glossary with
  per-term "don't show again".
- **Bilingual, dark mode, local-first** — zh-CN/en, auto/light/dark, SQLite on your disk,
  no account, no telemetry.

## Quick start

```bash
# requires Python 3.11+
uv sync --extra desktop
uv run kestrel init
uv run kestrel desktop      # native window + tray
# or
uv run kestrel serve        # http://127.0.0.1:8000
```

Container:

```bash
docker compose up           # http://127.0.0.1:8000, data in a named volume
```

## Screens

| Page | What it answers |
|---|---|
| Dashboard | What am I watching, and what is it doing right now? |
| Scan | What is worth doing today, at what price, with what risk? |
| Journal | What did I actually do, and what did it cost me? |
| Backtest | Does this rule survive history, or only today? |
| Fees | How much must the price rise before I break even? |
| Settings | Capital, risk, broker, notifications, agents, glossary |

## How a scan works

```
watchlist → bars (cached) → indicators → strategies → best signal
          → plan (size from risk budget, never from conviction)
          → recommendation + receipt token → notification outbox → phone
```

If nothing qualifies, the scan stores a diagnostic (`near`, `blocked_lot`) and the UI
says why. It will not invent a trade.

## Data sources

Eastmoney (primary, JSON), Tencent and Sina (GBK fallbacks) behind a circuit-breaker
chain; Yahoo for daily bars when the others fail. All are public, unofficial endpoints —
expect breakage, which is why every parser is isolated and fixture-tested.

## Status

Feature-complete for the v1 scope: scanning, planning, delivery, receipts, journal,
fees, backtest, desktop shell, container. Not in scope: order execution, social features,
a native mobile app, intraday strategies.

## Disclaimer

Kestrel is a research and learning tool. It does **not** provide investment advice, does
not predict prices, and does not place orders. Nothing it produces is a recommendation to
buy or sell any security. Markets carry risk; you are responsible for your own decisions.

## License

Apache-2.0 © Kestrel contributors
