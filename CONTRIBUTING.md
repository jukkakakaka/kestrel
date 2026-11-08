# Contributing to Kestrel

Thanks for taking a look. Kestrel is small on purpose — a few rules keep it that way.

## Ground rules

- **No pandas, no SQLAlchemy, no Node build chain.** The stack is FastAPI + SQLite + Jinja2 + vanilla JS, and that is a feature.
- **Money is integer micro-units.** Never store or compare floats for prices, fees or P&L. Use `kestrel.core.money`.
- **Money paths need tests.** Any change to `fees/`, `ledger/` or `planning/sizing.py` must come with a test that pins the expected numbers.
- **Every new config option goes through Pydantic.** The settings UI is generated from the schema, so a new field appears automatically.
- **i18n parity is enforced.** `tests/unit/test_i18n_parity.py` fails if a key exists in one locale and not the other.

## Development

```bash
uv sync --extra desktop --extra dev
uv run kestrel init
uv run uvicorn kestrel.app:app --reload --app-dir src
uv run pytest -q
uv run ruff check src tests
```

## Data source adapters

New providers implement `QuoteProvider` (`datasources/base.py`) and get registered in
`default_chain()`. Keep parsing pure and side-effect free so it can be tested against
recorded fixtures — note that Tencent and Sina return **GBK**, so fixtures must store
raw bytes, not decoded strings.

## Packaging the desktop app

```bash
make package        # dist/Kestrel.app, ad-hoc signed, smoke-tested
make package-dmg    # also dist/Kestrel-0.1.0.dmg
```

`packaging/macos/build.sh` generates `packaging/icon.icns` from `icon.png`, runs
PyInstaller with `packaging/pyinstaller/kestrel.spec`, signs the bundle and then **starts a
frozen copy and checks that `/advice` renders**. Keep that check: a bundle can launch
successfully and still serve a blank page when a data directory was left out of the spec.

When adding a new non-Python asset (templates, locales, migrations, images), add its
directory to `DATAS` in the spec as well, or it will be missing only in the packaged app.

## Commit messages

Plain and specific: `fix(fees): round HK stamp duty up to the nearest dollar`.

## Reporting bugs

Include the Kestrel version, OS, the exact symbol and timestamp that misbehaved, and the
relevant lines from `data/logs/`. Please do **not** paste API keys, tokens or account numbers.
