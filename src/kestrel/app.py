"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from kestrel import __version__
from kestrel.core.clock import iso
from kestrel.core.money import to_micro
from kestrel.datasources.base import guess_market, normalize_code
from kestrel.datasources.chain import QuoteChain, default_chain
from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.i18n import available_locales, load_catalog, translate
from kestrel.settings import settings

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
TEMPLATES = WEB_DIR / "templates"
STATIC = WEB_DIR / "static"


def _templates():
    from fastapi.templating import Jinja2Templates

    return Jinja2Templates(directory=str(TEMPLATES))


def _persist_quotes(quotes) -> None:
    """Cache the latest quote and fill in the real display name when unknown."""
    conn = connect()
    try:
        conn.execute("BEGIN")
        for quote in quotes.values():
            row = conn.execute(
                "SELECT id, name FROM symbol WHERE market=? AND code=?",
                (quote.market, quote.code),
            ).fetchone()
            if not row:
                continue
            if quote.name and (row["name"] == row["id"] or row["name"] == quote.code):
                conn.execute("UPDATE symbol SET name=? WHERE id=?", (quote.name, row["id"]))
            conn.execute(
                """
                INSERT INTO quote_latest
                    (symbol_id, price, prev_close, open, high, low, volume, turnover,
                     change_pct, source, quoted_at, fetched_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(symbol_id) DO UPDATE SET
                    price=excluded.price, prev_close=excluded.prev_close, open=excluded.open,
                    high=excluded.high, low=excluded.low, volume=excluded.volume,
                    turnover=excluded.turnover, change_pct=excluded.change_pct,
                    source=excluded.source, quoted_at=excluded.quoted_at,
                    fetched_at=excluded.fetched_at
                """,
                (
                    row["id"],
                    to_micro(quote.price),
                    to_micro(quote.prev_close) if quote.prev_close else None,
                    to_micro(quote.open) if quote.open else None,
                    to_micro(quote.high) if quote.high else None,
                    to_micro(quote.low) if quote.low else None,
                    quote.volume,
                    to_micro(quote.turnover) if quote.turnover else None,
                    quote.change_pct,
                    quote.source,
                    iso(quote.quoted_at),
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    init_db()
    app.state.chain = default_chain()
    log.info("kestrel %s ready, data dir %s", __version__, settings.home)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Kestrel",
        description="Multi-market stock scanner and trading journal.",
        version=__version__,
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    templates = _templates()

    def _ctx(request: Request, **extra):
        locale = request.cookies.get("locale") or "zh-CN"
        theme = request.cookies.get("theme") or "auto"
        return {
            "t": lambda key, **kw: translate(locale, key, **kw),
            "locale": locale,
            "locales": available_locales(),
            "theme": theme,
            "version": __version__,
            "catalog": load_catalog(locale),
            **extra,
        }

    @app.get("/healthz", response_class=JSONResponse)
    async def healthz(request: Request):
        chain: QuoteChain = request.app.state.chain
        return {"ok": True, "version": __version__, "sources": chain.health}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT s.market, s.code, s.name, s.currency, w.note,
                       q.price, q.prev_close, q.change_pct, q.source, q.fetched_at
                FROM watchlist w
                JOIN symbol s ON s.id = w.symbol_id
                LEFT JOIN quote_latest q ON q.symbol_id = s.id
                ORDER BY s.market, s.code
                """
            ).fetchall()
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request, name="dashboard.html", context=_ctx(request, items=[dict(r) for r in rows])
        )

    @app.post("/api/watchlist", response_class=JSONResponse)
    async def add_symbol(request: Request):
        body = await request.json()
        raw = str(body.get("code", "")).strip()
        if not raw:
            return JSONResponse({"ok": False, "error": "empty code"}, status_code=400)
        market = (body.get("market") or guess_market(raw)).upper()
        code = normalize_code(market, raw)
        conn = connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "INSERT OR IGNORE INTO symbol (market, code, name, currency, lot_size)"
                " VALUES (?, ?, ?, ?, ?)",
                (market, code, code, {"CN": "CNY", "HK": "HKD", "US": "USD"}[market],
                 1000 if market == "HK" else 100),
            )
            sid = conn.execute(
                "SELECT id FROM symbol WHERE market=? AND code=?", (market, code)
            ).fetchone()["id"]
            conn.execute("INSERT OR IGNORE INTO watchlist (symbol_id) VALUES (?)", (sid,))
            conn.execute(
                "INSERT INTO audit_log (action, target, detail) VALUES (?,?,?)",
                ("watchlist.add", f"{market}:{code}", raw),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return {"ok": True, "market": market, "code": code}

    @app.delete("/api/watchlist/{market}/{code}", response_class=JSONResponse)
    async def remove_symbol(market: str, code: str):
        conn = connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM watchlist WHERE symbol_id IN"
                " (SELECT id FROM symbol WHERE market=? AND code=?)",
                (market.upper(), code),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
        return {"ok": True}

    @app.get("/api/quotes", response_class=JSONResponse)
    async def quotes(request: Request):
        chain: QuoteChain = request.app.state.chain
        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT s.market, s.code FROM watchlist w
                JOIN symbol s ON s.id = w.symbol_id
                """
            ).fetchall()
        finally:
            conn.close()
        symbols = [(r["market"], r["code"]) for r in rows]
        if not symbols:
            return {"ok": True, "quotes": []}
        data = await chain.fetch(symbols)
        if data:
            _persist_quotes(data)
        return {
            "ok": True,
            "quotes": [
                {
                    "market": q.market,
                    "code": q.code,
                    "name": q.name,
                    "price": str(q.price),
                    "prev_close": str(q.prev_close) if q.prev_close else None,
                    "change_pct": q.change_pct,
                    "source": q.source,
                }
                for q in data.values()
            ],
            "stale": len(data) < len(symbols),
        }

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return templates.TemplateResponse(
            request=request, name="settings.html", context=_ctx(request)
        )

    return app


app = create_app()
