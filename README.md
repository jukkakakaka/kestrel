<div align="center">

<img src="assets/brand/png/logo-mark-1024.png" width="84" alt="Kestrel">

<h1>Kestrel</h1>

**Watch first. Then act.**

A self-hosted trading companion: tells you what to do today, sizes the position and
places the stop for you, then records what you *actually* paid — and shows you the gap.

<br/>

[![CI](https://github.com/jukkakakaka/kestrel/actions/workflows/ci.yml/badge.svg)](https://github.com/jukkakakaka/kestrel/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-informational.svg)](pyproject.toml) [![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux%20%7C%20Docker-lightgrey.svg)](#install)

[English](README.md) · [简体中文](README.zh-CN.md) · [Docs](docs/) · [Engines](docs/engines.md) · [Architecture](docs/architecture.md)

<img src="docs/screenshots/today.png" width="900" alt="Today: position risk, pending advice and watchlist quotes in one place">

</div>

---

## What it is for

Open a trading app for the first time and you get three hundred flashing numbers and a
search box. The questions that actually matter are only three:

1. **Do I need to do anything with what I already hold?**
2. **Is there anything worth buying — how much, at what price, and where do I admit I was wrong?**
3. **What did I actually do last time, and how far was it from the plan?**

Kestrel is organised around those three. The home page is called *Today* and lists only
things to do. Every plan carries a stop, a target, a share count and the money you lose
if it goes wrong. After you fill, you come back and record the real fill — **the price and
quantity are allowed to differ from the suggestion**. The journal then shows you how much
you overpaid, and by how many risk units you drifted.

The point is not to make you feel an AI is picking stocks. It is that after a few dozen
trades you understand *why* the stop was there and *why* the size was that size.

## Three-minute start

**Install the app** (macOS, Apple Silicon and Intel)

Grab the latest `Kestrel-*.dmg` from [releases](https://github.com/jukkakakaka/kestrel/releases)
and drag it into Applications. The bundle is ad-hoc signed, so clear the quarantine flag once:

```bash
xattr -dr com.apple.quarantine /Applications/Kestrel.app
open /Applications/Kestrel.app
```

**The first run asks three questions**: how much capital, how much you are willing to lose
per trade, and which markets you trade. Those answers drive every size and stop calculation
from then on.

**Then**:

1. Go to *Advice* → *Start scan*. It pulls a market-cap-ranked candidate pool per market and
   computes indicators for each name.
2. Qualified setups appear as cards: entry band, stop, target, share count, reward-to-risk,
   and the worst-case loss in money.
3. Place the order in your broker app, then come back and press *I bought it* —
   **enter the real fill price and quantity**.
4. *Today* keeps telling you each day which position is near its stop, which advice is about
   to expire, and when it is time to sell.

> A rejected setup is information too. The page names the reason — *"one lot already exceeds
> your risk budget"*, *"stop distance of 22% is too wide"* — instead of pretending nothing came up.

<img src="docs/screenshots/advice.png" width="900" alt="Advice: entry band, stop, target, size, reward-to-risk, worst-case loss, and a plain-language explanation">

## Plan → fill → journal, closed end to end

```
scan → advice (entry / stop / target / qty / fees / expiry)
     → you fill at your own price and quantity
     → record the fill (receipt page or journal)
     → position monitor (live price, open P&L, distance to stop, R multiple)
     → push alert on stop hit / near stop / target reached
     → close (partial closes allowed) → execution quality: follow rate, average drift in R
```

This is what separates it from a screener: **it records facts, and does not prettify them.**

- Advice said buy 100 at 400–404. You got 60 filled at 402.5 — record 60 at 402.5. The journal
  marks it a partial fill, and execution quality counts it as *planned 100 / actual 60*.
- Sells can be partial. Each sell closes the oldest position first (FIFO); the original entry
  row is never rewritten.
- Paid 0.5R above plan and execution quality says so: *"that ate half your stop distance."*
- Not following the plan is recorded too. A low follow rate is a signal, not a sin: the advice
  does not suit you, or you are not ready to act on it.

<img src="docs/screenshots/journal.png" width="900" alt="Journal: live price, open P&L, distance to stop and R multiple per position, with a one-click exit">

## Markets and frequencies

| Market | What is different | Fee model |
| :--- | :--- | :--- |
| **A-shares** | 100-share lots, T+1, price limits | commission (0.025%, min ¥5) + 0.05% stamp on sells + transfer fee |
| **Hong Kong** | lot size varies per stock (100/500/1000), no price limits | commission + platform fee + **0.1% stamp each side** + four levies |
| **US** | single shares, no stamp duty | per-share platform fee + SEC/TAF on sells |

**Frequency** is the *horizon*, and the three tiers move everything together:

| Horizon | Bars | Typical hold | Plan lifetime | Stop band |
| :--- | :--- | :--- | :--- | :--- |
| Swing | daily | days to weeks | next session close | 1.5%–15% |
| Position | weekly | weeks to two months | 10 days | 3%–18% |
| Long (Beta) | monthly | months | 45 days | 5%–30% |

Leave it on *auto* and it weighs pool breadth, median daily range, your capital and your actual
fee schedule, picks one, and **explains why** (*"capital of 50k is small — fees dominate, so trade
less rather than faster"*). Pin a horizon at any time and it stops arguing.

## Three engines

Who produces advice is one global switch. The advice page has one button and the label follows it.

| Engine | Decides | Latency | Cost |
| :--- | :--- | :--- | :--- |
| **Rules** | fixed conditions over indicators; rules are JSON you can edit in the UI | instant | free |
| **Model** (OpenAI-compatible) | reads indicators, your account, and each rule's measured record | ~30 s | per token |
| **WorkBuddy** | same, but over a file queue serviced by a scheduled agent | minutes | free |

In both AI modes **risk and sizing stay in local code**: the model's share count is discarded,
the stop must sit inside the current horizon's band, reward must clear 1.5R, the ticker must be
in the candidate list, one sector may not dominate, and total exposure is capped. Rejected picks
are logged with a reason and shown in the UI.

Model calls have a daily budget. When it runs out the app **says so and stops** — it never
quietly falls back to rules, because then you would think you were using a model when you were not.

## Rules must prove themselves

Every built-in rule is walk-forward tested against a fair benchmark: the same entry, the same
holding period, no stop. Beat holding or do not get published.

Measured over ~100 names and 25 years of daily bars (refolded to weekly and monthly):

| Horizon | Result |
| :--- | :--- |
| Daily | 5 rules "no edge", 1 "unstable"; expectancy comes mostly from the 2:1 payoff, not the entry |
| Weekly | 2 "unstable", 3 "no edge" |
| Monthly | only *Trend pullback* (96 trades) and *Washout bounce* (42) have enough trades to judge; the rest read "insufficient sample" |

One rule set gives three different answers on three horizons, so verdicts are stored per horizon:
a rule that loses on daily is disabled on daily only, not removed. **The value here is discipline
and cost, not prediction** — and the rules page says exactly that, including the verdicts that
read "insufficient sample".

## Data sources

| Source | Cost | Markets | Notes |
| :--- | :--- | :--- | :--- |
| Eastmoney | free | CN / HK / US | primary, JSON |
| Tencent · Sina | free | CN / HK / US | GBK fallbacks |
| Yahoo | free, no key | CN / HK / US | used to cross-check the others |
| Finnhub · Alpha Vantage · Twelve Data · Polygon · Tiingo | free tier or paid | per provider | paste a key |

Built-in sources can be toggled individually; disabling one falls through to the next. You can
also define your own source: URL template, JSON field paths, a scale divisor, and a **test** button
that shows the parsed number before you save it.

## Install

**macOS app** (the dmg above, or build it yourself)

```bash
make package        # dist/Kestrel.app
make package-dmg    # plus dist/Kestrel-*.dmg
```

**From source** (Python 3.11+, [uv](https://docs.astral.sh/uv/) required)

```bash
uv sync --extra desktop
uv run kestrel init
uv run kestrel desktop     # native window
uv run kestrel serve       # or the browser: http://127.0.0.1:8000
```

**Windows**

GitHub Actions builds a `Kestrel-windows` zip (Windows 10/11, no Python needed) — download it
from the Artifacts of the *Build desktop app* workflow and run `Kestrel.exe`.

**Docker**

```bash
docker compose up          # http://127.0.0.1:8000, data in a named volume
```

**Everything stays on your machine**: SQLite lives in `~/Library/Application Support/Kestrel`
(macOS), `%APPDATA%\Kestrel` (Windows) or `./data` (source). No account, no telemetry, nothing uploaded.

## Configuration

Everything else lives in the database and is edited in the UI, effective immediately.
Bootstrap values are environment variables:

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `KESTREL_HOME` | `./data`, or the OS app-data dir when bundled | database, cache, logs, bridge queue |
| `KESTREL_HOST` | `127.0.0.1` | bind address |
| `KESTREL_PORT` | `8000` | port |
| `KESTREL_DEBUG` | `false` | verbose logging |

## Honest limits

The part most READMEs leave out.

- **Daily bars only.** No order book, no fund flows, no earnings expectations. On that information
  set the selection edge is close to zero, and the rules page will show you exactly that:
  expectancy is positive, but almost all of it comes from the 2:1 payoff, not from the entry.
- **The stop and the size do the work.** Measured against holding the same entry for the same number
  of days with no stop, the excess return of every shipped rule is near zero. Treat this as a
  discipline and cost tool, not an oracle.
- **The horizon recommendation is a heuristic**, not a model: breadth, volatility, capital and fee
  drag, weighted and explained. It can be wrong. Pin the horizon you want and it stops arguing.
- **Long-term is Beta.** Monthly bars see price and volume and nothing else, and the sample is
  thinnest — only two of the seven rules produce enough trades to judge.
- **No broker API, no automatic orders.** That is deliberate: placing an order is irreversible.
  Every fill goes through one `BrokerAdapter` interface, so wiring a real broker later means
  replacing one class — the journal, alerts and statistics stay as they are.
- **WorkBuddy mode is asynchronous** and its finest schedule is hourly.
- **OCR is not implemented.** Pasting text is more accurate, and less likely to misread a digit.

## Project layout

```
src/kestrel/
  app.py            FastAPI app, routes, template filters
  services/         scan / analyst / engine / rules / universe / calendar /
                    execution (fills) / positions (monitor) / today (briefing) /
                    advisor (horizon) / timeframe (bar folding)
  strategies/       rule compiler, built-in rule library, AI-facing JSON Schema
  datasources/      provider chain, Eastmoney/Tencent/Sina, resolver, presets, custom
  planning/         position sizing, stop/target placement, fee-aware plans
  fees/             exchange levy model, broker plans
  web/              Jinja templates + static assets
  db/               schema and versioned migrations
packaging/          PyInstaller spec, macOS build script, icons
```

## Documentation

| Document | Contents |
| :--- | :--- |
| [docs/engines.md](docs/engines.md) | The three engines, shared risk checks, what each page does in AI mode |
| [docs/architecture.md](docs/architecture.md) | Module map, data flow, storage |
| [docs/fee-model.md](docs/fee-model.md) | Exchange levies and broker plans |
| [docs/notifications.md](docs/notifications.md) | Channels, receipts, signature scheme |
| [docs/ui-map.md](docs/ui-map.md) | Route table and naming rationale |

## Development

```bash
uv sync --extra desktop --extra dev
uv run kestrel init
uv run pytest            # 133 tests
uv run ruff check src tests
uv run uvicorn kestrel.app:app --reload --app-dir src
```

The suite must pass against an **empty data directory**: CI has no pre-seeded database, and an
earlier version was green locally and red in CI. `tests/conftest.py` now guarantees a migrated
database for every session.

## Disclaimer

Kestrel is a research and learning tool. It does **not** provide investment advice, forecast
prices, or place orders. Nothing it produces is an offer to buy or sell any security.
Markets carry risk; the decisions are yours. Its most useful feature may be telling you that
today is not the day.

## License

Apache-2.0 © Kestrel contributors
