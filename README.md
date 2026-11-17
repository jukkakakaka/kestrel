<div align="center">

<img src="assets/brand/png/logo-mark-1024.png" width="96" alt="Kestrel">

<h1>Kestrel</h1>

**What to buy today, how much, and when to sell.**

A local-first stock trading and risk workbench that runs on your own machine: it screens China A-shares, Hong Kong, and US stocks, puts strategies through a 4-gate statistical falsification pipeline, builds fee-aware executable plans, logs your *actual* fills, and stays quietly in your system tray to watch stops and targets until positions are closed.

<br/>

[![CI](https://github.com/jukkakakaka/kestrel/actions/workflows/ci.yml/badge.svg)](https://github.com/jukkakakaka/kestrel/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-informational.svg)](pyproject.toml) [![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux%20%7C%20Docker-lightgrey.svg)](#install)

[English](README.md) · [简体中文](README.zh-CN.md) · [Docs](docs/) · [Engines & Falsification](docs/engines.md) · [Architecture](docs/architecture.md) · [UI Map](docs/ui-map.md)

<img src="docs/screenshots/today.en.png" width="900" alt="Today: 3-column adaptive workbench, position risk, pending advice, and watchlist">

</div>

---

## Eight Core Capabilities (v0.4.0)

1. **Adaptive 3-Column Financial Workbench & Tri-State Theme** — Collapsible 3-group linear SVG rail (`220px ↔ 64px` icon rail / slide-over drawer under `960px`) + `56px` frosted top workspace bar (with `/` quick ticker search) + toggleable `380px` right-side Trading Agent drawer. Powered by CSS Container Queries (`@container main`) for zero-overflow reflow at any window size, paired with **Sun / Moon / System tri-state icon switching** and instant `zh-CN / EN` localization (1,098 parity-checked keys).
2. **One-Click Monetary Privacy Masking (Eye Button / `Alt+P`)** — Share screens or screenshots safely: toggle `#toggle-privacy` or press `Alt+P` for zero-FOUC CSS masking that turns personal capital, equity, cash, market value, P&L amounts, and share counts into tabular `••••••` / `••••` (and blurs unfocused capital inputs) while keeping public stock prices, `%` changes, and R-multiples legible.
3. **Native System Tray & Close-to-Tray Background Watch** — Main-thread Cocoa `NSStatusItem` on macOS + `pystray` daemon on Windows/Linux with a 7-item bilingual tray menu. Closing the main window hides it to the tray by default while keeping `APScheduler` stop/target monitoring alive; supports Dock/tray restore, `kestrel desktop --minimized` silent startup, and cross-platform login autostart.
4. **Dynamic Cash Ledger & Manual Calibration** — Your configured `account.capital` acts as initial capital: confirmed buys debit cash + fees automatically, and FIFO partial/full sells release proceeds while splitting entry fees proportionally. Click the top-bar equity pill anytime to reconcile **Available Cash, Total Equity, Deposits/Withdrawals**, or add/edit/clear **existing stock holdings**.
5. **Quasi-Live Execution Contract & What-if Attribution** — Enforces real exchange mechanics (`100`-share CN lots, `200+1` STAR/BSE rules, CN `T+1` sell locks, HK board/odd lots, US `0.0001` fractional shares, and tick rounding). Displays round-trip fees, **fee-inclusive break-even**, beginner limit-order instructions, live fill-vs-plan drift previews, and **What-if P&L attribution**.
6. **4-Gate Quantitative Falsification & Parameter Plateau Heatmap** — Lookahead-safe **next-open execution** (`next_open=True`) with configurable round-trip `fee_bps` and one-way `slippage_bps`, **500-run fixed-seed (`seed=42`) permutation test p-values**, **`3×3` parameter plateau heatmaps** (hold period × ATR stop multiplier), and the **G1 Fee/Slippage Filter → G2 Walk-Forward → G3 Permutation Downgrade Guard → G4 Live Drift Alert** pipeline.
7. **Multi-Market (CN/HK/US) & Multi-Horizon Advisor** — Trade CN A-shares, HK, and US equities across five horizons (`fast`, `short`, `medium`, `long`, or `auto`). During onboarding or in `auto` mode, Kestrel recommends optimal market combinations and cadence based on capital size, minimum fee/lot drag, and market breadth regimes.
8. **75+ Hover-to-Learn Terms & Scoped Trading Copilot** — Enable **Learn Mode** to subtly underline 75+ trading concepts naturally across pages, modals, and AI replies for instant hover/click plain-language definitions and AI follow-ups; toggle off for zero underlines and zero banners. The right-side Copilot requires your own OpenAI-compatible API, injects live portfolio + strategy falsification stats (never fabricating unmeasured metrics), and strictly refuses off-topic prompts.

## Never bought a stock before? Start here

You do not need the theory first. Walk through these four steps once to complete an end-to-end disciplined trade.

**Step 1 — Tell it your starting capital** (1 minute)

First-run onboarding asks for your initial capital, markets, and cadence (with live capital-aware market recommendations).

- **Initial Capital** seeds your dynamic cash ledger and drives risk-based position sizing. Enter an amount you can genuinely afford to put at risk.
- **Max Risk per Trade** defaults to 2% (e.g., 1,000 max loss at the stop on 50,000 capital; 1%–2% recommended).
- **Markets**: disabled markets disappear cleanly across the entire app.

> Why start with risk? Share count is never a guess — it is derived from `risk budget ÷ per-share stop distance`. Deciding share count first is the classic beginner mistake.

**Step 2 — Run a scan** (10 seconds)

Open *Advice* → *Start scan*. Qualifying setups appear as 8-metric executable cards:

| On the card | What it means |
| :--- | :--- |
| Entry band | Placing a limit order inside this zone counts as following the plan |
| Stop | Pre-committed exit price if wrong — sell on a break, never average down |
| First target | Planned level to lock in partial profits |
| Shares | Exact tradable quantity respecting your cash, risk cap, and exchange lot rules |
| Est. fees & Break-even | Round-trip broker/exchange fees and true fee-inclusive break-even price |
| Net R:R & Risk amount | Reward-to-risk ratio (plans below `1.5` are rejected) and worst-case loss |

When nothing qualifies, Kestrel **explicitly tells you to sit on your hands today** and explains why (score below threshold / one board lot exceeds risk budget / stop band too wide).

**Step 3 — Place the limit order in your broker app, then log the real fill**

Click *Log actual fill* (or `+ Log Fill` in the top bar) and enter the **exact price and quantity filled**. If the plan said 100 shares at 400–404 and you got 60 at 402.5, record 60 at 402.5 — the modal previews slippage impact in real time and updates your cash ledger.

**Step 4 — Glance at Today (or let the system tray watch in the background)**

The `/` home page lists prioritized actions only:
- Position below or near stop → **exit now** (with explicit `T+1` lock badges on same-day CN buys)
- Position at `+1R` / `+2R` → **ratchet stop up to break-even or +1R**
- Advice expiring soon → **act or skip**
- Nothing urgent → **nothing to do today**

## Selling is the main event

Every open position receives **one** clear next action with an exact price attached:

| Trigger | Action | Why |
|---|---|---|
| Price below stop | Exit immediately | A stop is a pre-agreed exit, not a suggestion |
| Reaches `+1R` | Move stop to **break-even** | Worst case becomes a scratch net of fees |
| Reaches `+2R` | Move stop to **`+1R`** | Lock in one risk unit while letting winners run |
| Hits Target 1 | **Sell half (lot-aligned)** | Trail the remainder instead of guessing tops |
| Exceeds horizon max hold | **Time stop** | Dead capital and attention carry opportunity cost |

Stops move **up only** — `exits.apply_stop` rejects downward edits, and every adjustment is audited in `stop_change`.

<img src="docs/screenshots/journal.en.png" width="900" alt="Journal: dynamic equity summary, open R-progress, closed equity curve, and execution attribution">

## Plan → Fill → Journal → Falsification, Closed End-to-End

```
scan → fee-aware plan (entry band / stop / target / shares / fees / break-even / expiry)
     → you execute in your broker app & log actual price/qty (with live drift preview)
     → dynamic cash ledger debits/credits cash; monitor tracks live P&L & R-multiples
     → system tray stays resident in background for stop / near-stop / target alerts
     → partial or full FIFO close → execution quality + What-if P&L attribution + Gate 4 live drift
```

<img src="docs/screenshots/advice.en.png" width="900" alt="Advice: market regime badges, horizon advisor, and 8-metric executable setup cards">

## 4-Gate Quantitative Falsification Pipeline

Most retail tools show cherry-picked backtests. Kestrel treats statistical falsification as a first-class workflow across `/sim` and `/rules`:

1. **G1 Fee & Slippage Filter (`next_open=True`)** — Indicators compute strictly on historical prefixes (`bars[:i+1]`); signals at bar `i` close fill at bar `i+1` **open plus one-way `slippage_bps`** (default `10 bps`) and round-trip `fee_bps` (default `20 bps`), requiring `≥ 12` trades and positive net expectancy.
2. **G2 Walk-Forward vs. Hold** — Multi-fold out-of-sample replay benchmarked against equal-duration unfiltered buy-and-hold.
3. **G3 500-Run Permutation Test & `3×3` Parameter Plateau** — Fixed-seed (`seed=42`, `n_perms=500`, `<150ms`) Monte Carlo permutation testing computes `p_value`; rules with positive walk-forward return but `p ≥ 0.10` are **automatically downgraded from Edge to Unstable**. In `/sim`, a `3×3` grid across hold periods × ATR stop multipliers highlights **Robust Plateaus** vs. **Isolated Overfit Spikes**.
4. **G4 Live / Paper Drift Monitor** — Continuously compares realized average R from historical recommendations and closed journal trades against backtest expectancy, raising a drift warning banner when live results lag significantly (`n ≥ 3`).

<img src="docs/screenshots/rules.en.png" width="900" alt="Rules & 4-Gate Falsification: G1–G4 gate badges, walk-forward folds, and permutation p-values">

## Supported Markets & Five Horizons

| Market | Exchange Mechanics Enforced | Fee Model |
| :--- | :--- | :--- |
| **A-shares (CN)** | `100`-share board lots, STAR/BSE `200+1` sizing, `T+1` sell lock, daily price limits | Commission (0.025%, min ¥5) + 0.05% sell stamp duty + transfer fee |
| **Hong Kong (HK)** | Variable board lots (100/500/1000), no price limits; optional broker odd-lot support | Commission + platform fee + **0.1% stamp duty each side** + 4 levies |
| **US Equities (US)** | Single shares + optional `0.0001` fractional shares & min-dollar checks; no stamp duty | Per-share platform fee + sell-side SEC / TAF fees |

<img src="docs/screenshots/market.en.png" width="900" alt="Markets: session badges, native currency, or unified CNY/HKD/USD conversion">

| Horizon | Bar Size | Typical Hold | Plan Expiry | Valid Stop Band |
| :--- | :--- | :--- | :--- | :--- |
| Fast (`fast`) | 2-day folded | 1–5 sessions | Next session close | 1.5%–12% |
| Swing (`short`) | Daily | Days to 2 weeks | Next session close | 1.5%–15% |
| Position (`medium`) | Weekly | Weeks to 2 months | 10 days | 3%–18% |
| Long (`long` · Beta) | Monthly | Months | 45 days | 5%–30% |

In `auto` mode, the advisor scores pool breadth, volatility, capital, and broker fee drag to pick the optimal horizon and explain why.

## Three Engines, Shared Local Risk Guardrails

| Engine | Decision Maker | Latency | Cost |
| :--- | :--- | :--- | :--- |
| **Rules** | Deterministic JSON indicator specs editable and falsifiable in `/rules` | Instant | Free |
| **Model** (OpenAI-compatible) | Evaluates candidate indicators, account constraints, and rule falsification records | ~30 s | Per token |
| **WorkBuddy** | Asynchronous file-queue workflow consumed by external scheduled agents | Minutes | Free |

In both AI modes, **position sizing and hard risk checks stay 100% in local Python code**: model share counts are discarded, stops must sit inside the horizon band, net reward-to-risk must clear `1.5`, tickers must come from the candidate whitelist, and daily call budgets fail loudly rather than silently falling back to rules.

## Install & Desktop Background Mode

**macOS Desktop App** (macOS 11+, Apple Silicon & Intel)

Download `Kestrel-*.dmg` from [Releases](https://github.com/jukkakakaka/kestrel/releases) and drag **Kestrel** into Applications. Clear the ad-hoc quarantine attribute once before first launch:

```bash
xattr -dr com.apple.quarantine /Applications/Kestrel.app
open /Applications/Kestrel.app
```

- **Menu Bar Tray & Resident Watch**: Kestrel places a native icon in the macOS menu bar with 7 actions (*Open Kestrel Window*, *Open in Browser*, *Background Watch Active*, *Scan Markets Now*, *Mask Monetary Amounts*, *Keep Running on Window Close*, *Quit Kestrel*). Closing the window hides it to the tray by default; click the menu bar or Dock icon anytime to reopen.
- **Build from source on macOS**:
  ```bash
  make package        # builds dist/Kestrel.app in an isolated .build-venv
  make package-dmg    # builds dist/Kestrel.app + dist/Kestrel-0.4.0.dmg with built-in smoke test
  ```

**From Source** (Python 3.11+, [uv](https://docs.astral.sh/uv/))

```bash
uv sync --extra desktop
uv run kestrel init
uv run kestrel desktop               # 1360×860 native window + system tray
uv run kestrel desktop --minimized   # start hidden in system tray for silent background watch
uv run kestrel serve                 # local browser mode: http://127.0.0.1:8000
```

**Windows & Docker**

- **Windows**: Download `Kestrel-windows.zip` from the *Build desktop app* GitHub Actions workflow artifacts and run `Kestrel.exe` (includes Windows system tray & Startup autostart support).
- **Docker**: `docker compose up` (`http://127.0.0.1:8000`, persistent named volume).

**100% Local Data Ownership**: Integer micro-unit SQLite data stays in `~/Library/Application Support/Kestrel` (macOS), `%APPDATA%\Kestrel` (Windows), or `./data` (source). Zero accounts, zero telemetry.

## Environment Variables

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `KESTREL_HOME` | `./data` (or OS app-data dir when bundled) | SQLite database, bar cache, logs, and WorkBuddy queue |
| `KESTREL_HOST` | `127.0.0.1` | Local server bind address |
| `KESTREL_PORT` | `8000` | Local server port |
| `KESTREL_TRAY` | Auto-detected (`0` disables) | Enable/disable system tray (`KESTREL_TRAY=0` for headless CI/smoke tests) |
| `KESTREL_AUTOSTART_DIR` | OS user autostart directory | Override login-item directory (used for isolated testing) |
| `KESTREL_DEBUG` | `false` | Verbose logging |

## Keyboard Shortcuts & Hover-to-Learn

- **Inline Vocabulary**: Toggle **Learn Mode** in the top bar to underline 75+ trading terms naturally across pages, modals, and Copilot answers; turn it off for a pure institutional terminal view.
- **Shortcuts**: `/` focus ticker search · `Alt+P` toggle monetary privacy masking · `⌘K` (`Ctrl+K`) toggle right-side Trading Copilot · `Esc` dismiss modals or compact nav drawer.

<img src="docs/screenshots/welcome.en.png" width="900" alt="Four-step onboarding: initial capital, smart market recommendations, horizon, and optional Copilot API">

## Project Layout

```
src/kestrel/
  app.py            FastAPI routes, SSR privacy/locale/desktop context, OHLC chart geometry
  cli.py            CLI entrypoints (init / serve / desktop [--minimized] / scan / update / version)
  desktop/          app.py (native window & close-to-tray), tray.py (macOS NSStatusItem + pystray),
                    autostart.py (macOS LaunchAgents / Windows Startup / Linux XDG autostart)
  services/         portfolio (dynamic cash & calibration) / execution (OrderIntent, T+1, FIFO, What-if) /
                    backtest (next-open, fee/slippage bps, 500-run permutation, 3×3 plateau grid) /
                    rules (G1–G4 falsification gates & live drift monitor) / copilot (scoped AI Agent) /
                    advisor / timeframe / scan / analyst / exits / positions / journal / glossary / config
  core/             money.py (integer micro-units & tick rounding), quantity.py (100-lot / 200+1 / fractionals)
  indicators/       Wilder RSI / ATR, SMA slopes, session-normalized volume ratio, VCP ratio
  strategies/       declarative JSON rule compiler, 7 built-in strategies, AI JSON Schema
  datasources/      Eastmoney / Tencent / Sina / Yahoo / custom provider fallback chain
  planning/         cash-, fee-, and broker-lot-aware position builder
  fees/             exchange levy calculators & editable broker fee plans
  web/              base.html 3-column workbench, _page_icons.html linear SVGs, 12 views & vanilla JS
  db/               SQLite WAL transaction guard & 0001–0013 migrations
packaging/          PyInstaller kestrel.spec, macOS .app / .dmg builder & light/dark Asset Catalog
scripts/            check.sh gatekeeper, check_locales.py parity check, 1440×900 screenshot generator
```

## Documentation

| Document | Contents |
| :--- | :--- |
| [docs/architecture.md](docs/architecture.md) | Layered architecture, native tray lifecycle, privacy contract, and lookahead safety |
| [docs/ui-map.md](docs/ui-map.md) | 3-column workbench layout, complete route map, and `data-privacy` masking rules |
| [docs/engines.md](docs/engines.md) | Rules / LLM / WorkBuddy engines, shared risk checks, and G1–G4 falsification pipeline |
| [docs/fee-model.md](docs/fee-model.md) | Exchange levies and customizable broker fee schedules |
| [docs/notifications.md](docs/notifications.md) | Alert channels, mobile signed receipts, and WorkBuddy cron jobs |

## Development

```bash
uv sync --extra desktop --extra dev
uv run kestrel init
bash scripts/check.sh    # ruff lint (src/tests/scripts) + 1098-key locale parity + full pytest suite
uv run uvicorn kestrel.app:app --reload --app-dir src
```

## Disclaimer

Kestrel gives no investment advice, forecasts no prices, and places no automatic orders. Nothing it produces is an offer or solicitation to buy or sell any security. Markets carry risk; all trading decisions are yours. Its most valuable feature may be telling you that today is not the day to trade.

## License

GPL-3.0 © Kestrel contributors
