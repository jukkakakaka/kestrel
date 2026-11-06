<p align="center">
  <img src="assets/brand/logo.svg" alt="Kestrel 隼" width="420">
</p>

# Kestrel 隼

**Watch. Then strike.** — a self-hosted, multi-market stock scanner and trading journal that keeps you honest.

[中文说明](README.zh-CN.md) · [Docs](docs/) · [Contributing](CONTRIBUTING.md)

---

## Why

Most trading tools help you *find* trades. Kestrel also asks the harder question afterwards: **did you actually follow your plan?**

Every recommendation produced by Kestrel carries an entry zone, a stop, a target, a position size and an expiry — and is pushed to your phone with a one-tap receipt page. When you tap "executed", the real fill price is recorded next to the plan, so the deviation between what was suggested and what you did becomes a number you can improve.

## Features

- **Multi-market** — A-shares, Hong Kong and US, each with an independent on/off switch.
- **Layered data sources** — Eastmoney, Tencent, Sina and Yahoo behind one adapter with a circuit-breaker fallback chain.
- **Plans, not tips** — every signal becomes a trade plan with sizing, fees, break-even and R multiple.
- **Receipt loop** — signed short links work over WeChat, ntfy, WeCom, Telegram, SMTP or a generic webhook.
- **Cost engine** — exchange rules plus broker plans (fee tiers, platform fees, minimums), editable in the UI.
- **Learning mode** — every plan explains itself in plain language, with a glossary you can mute term by term.
- **Local first** — SQLite on your machine, keys encrypted at rest, no account, no telemetry.
- **Desktop or server** — one codebase runs as a macOS/Windows app, or in a container behind a reverse proxy.

## Quick start

```bash
# requires Python 3.11+
uv sync --extra desktop
uv run kestrel init
uv run kestrel desktop      # native window + tray
# or
uv run kestrel serve        # http://127.0.0.1:8000
```

## Status

Early alpha. P0 (skeleton, data layer, desktop shell) is in place. Scanning,
journal, fees and notifications land in the following milestones — see the
roadmap in `docs/`.

## Disclaimer

Kestrel is a research and learning tool. It does **not** provide investment
advice, does not predict prices, and does not place orders. Nothing it produces
is a recommendation to buy or sell any security. Markets carry risk; you are
responsible for your own decisions.

## License

Apache-2.0 © Kestrel contributors
