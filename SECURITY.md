# Security Policy

## Scope

Kestrel runs entirely on your machine and stores everything in `data/`. The things worth
protecting are:

- `data/secret.key` — signs receipt links (HMAC). It does not encrypt anything; API keys
  and webhook URLs are stored in plain text in the local SQLite file, protected only by
  your OS account permissions.
- `data/kestrel.db` — holdings, trades and receipts
- notification channel credentials (bot tokens, SMTP passwords)

None of these are ever sent anywhere except to the service they belong to.

## Reporting a vulnerability

Open a private security advisory on GitHub rather than a public issue. Please include
reproduction steps and the version (`kestrel --version`).

## Hardening notes

- The server binds to `127.0.0.1` by default. Exposing it on `0.0.0.0` requires an access
  token; do not put it on the public internet without a reverse proxy and TLS.
- Receipt links are HMAC-signed, expire after 72 hours and are limited to 5 uses.
- Never commit `.env`, `secret.key` or a populated database — `.gitignore` covers them, but
  check `git status` before pushing.
