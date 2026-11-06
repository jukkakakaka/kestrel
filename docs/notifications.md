# Notifications and the receipt loop

## Why a receipt loop

Push-only alerts turn into noise. Kestrel asks one question after every plan: **did you
actually do it, and at what price?** The answer is stored next to the plan, so the gap
between advice and behaviour becomes a number you can shrink.

```
plan stored → notification queued → channel sends → phone opens /r/{code}
     → tap "executed" + real fill → receipt row with deviation_pct / deviation_r
```

## Channels

All optional, all configurable in Settings → Notifications. A channel is skipped when
it is not configured; a failing channel never blocks the scan.

| Code | Transport | Notes |
|---|---|---|
| `ntfy` | HTTP POST to ntfy.sh or self-hosted | Supports an **action button** that opens the receipt page in one tap |
| `webhook` | JSON POST | Works with a WeCom group bot URL unchanged |
| `weclawbot` | `POST /bots/{bot_id}/messages` | Personal WeChat through a WeClawBot-API gateway. Push only; the upstream project is in grey release, so treat it as best effort |
| `smtp` | STARTTLS + login | Fallback for people who live in email |

WeChat cannot receive replies through that gateway, which is exactly why the receipt
page is a link rather than a reply. Any channel that can carry a URL works.

## Making the link reachable

The receipt page is served by your own machine, so the phone has to reach it:

1. **Same Wi-Fi** — set `receipt.public_base` to `http://<your-lan-ip>:8000`. Fastest,
   works only at home.
2. **Tunnel (recommended)** — Tailscale, Cloudflare Tunnel or ngrok, then point
   `receipt.public_base` at the public hostname.
3. **Server deployment** — if Kestrel runs on a VPS, use that hostname.

If the link is unreachable, the plan still works; you can log the fill manually in the
journal.

## Guarantees

- Receipt codes are HMAC-derived, 8 characters, valid 72 hours, max 5 uses.
- The outbox row is unique per `(recommendation, channel, locale)` — re-running a scan
  cannot spam you twice with the same plan.
- Failed sends keep their error and stay `pending` for the next dispatch pass.
- Every message renders in the user's locale; the payload is stored as JSON so the
  same row can be re-sent to another channel later.
