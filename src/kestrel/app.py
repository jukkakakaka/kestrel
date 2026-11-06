"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC
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

    tpl = Jinja2Templates(directory=str(TEMPLATES))
    tpl.env.filters["money"] = _money
    return tpl


def _money(value, places: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value) / 1_000_000:,.{places}f}"
    except (TypeError, ValueError):
        return str(value)


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
    from kestrel.scheduler import service as scheduler_service

    app.state.scheduler = scheduler_service.start()
    log.info("kestrel %s ready, data dir %s", __version__, settings.home)
    yield
    scheduler_service.stop()


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
                 100 if market in ("HK", "CN") else 1),
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

    @app.get("/scan", response_class=HTMLResponse)
    async def scan_page(request: Request):
        from kestrel.services import config as config_service
        from kestrel.services import scan as scan_service

        scan_service.expire_stale()
        recs = scan_service.active_recommendations(50)
        conn = connect()
        try:
            last = config_service.get(conn, "scan.last", None)
            ui = {
                "learning": bool(config_service.get(conn, "ui.learning_mode", True)),
                "glossary": bool(config_service.get(conn, "ui.glossary", True)),
            }
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request, name="scan.html",
            context=_ctx(request, recs=recs, last=last, ui=ui),
        )

    @app.post("/api/scan", response_class=JSONResponse)
    async def api_scan(request: Request):
        from kestrel.notify import dispatcher
        from kestrel.services import scan as scan_service

        try:
            body = await request.json()
        except Exception:
            body = {}
        markets = body.get("markets") if isinstance(body, dict) else None
        result = await scan_service.run_scan(trigger="manual", markets=markets)
        if result.get("ok"):
            queued = 0
            for rec in scan_service.active_recommendations(10):
                queued += dispatcher.queue(rec["id"])
            result["queued"] = queued
            result["notify"] = await dispatcher.dispatch()
        return result

    @app.get("/api/recommendations", response_class=JSONResponse)
    async def api_recommendations():
        from kestrel.services import scan as scan_service

        return {"ok": True, "items": scan_service.active_recommendations(50)}

    @app.get("/r/{code}", response_class=HTMLResponse)
    async def receipt_page(request: Request, code: str):
        conn = connect()
        try:
            row = conn.execute(
                """
                SELECT r.*, t.code AS token_code, t.expires_at, t.uses, t.max_uses
                FROM receipt_token t JOIN recommendation r ON r.id = t.rec_id
                WHERE t.code = ?
                """,
                (code,),
            ).fetchone()
            existing = None
            if row:
                existing = conn.execute(
                    "SELECT * FROM receipt WHERE rec_id=? ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request,
            name="receipt.html",
            context=_ctx(request, rec=dict(row) if row else None, code=code,
                         existing=dict(existing) if existing else None),
        )

    @app.post("/r/{code}", response_class=HTMLResponse)
    async def receipt_submit(request: Request, code: str):
        from datetime import datetime

        from kestrel.core.money import to_micro

        form = await request.form()
        action = str(form.get("action") or "skipped")
        fill_raw = str(form.get("fill_price") or "").strip()
        note = str(form.get("note") or "").strip()

        conn = connect()
        try:
            row = conn.execute(
                """
                SELECT r.*, t.code AS token_code, t.expires_at, t.uses, t.max_uses
                FROM receipt_token t JOIN recommendation r ON r.id = t.rec_id
                WHERE t.code = ?
                """,
                (code,),
            ).fetchone()
            if row is None:
                return templates.TemplateResponse(
                    request=request, name="receipt.html",
                    context=_ctx(request, rec=None, code=code, existing=None),
                )

            fill_price = None
            deviation_pct = deviation_r = None
            if action in ("executed", "partial") and fill_raw:
                try:
                    fill_price = to_micro(fill_raw)
                    entry = row["entry_low"]
                    stop = row["stop"]
                    if entry:
                        deviation_pct = (fill_price - entry) / entry * 100
                    if entry and stop and entry > stop:
                        deviation_r = (fill_price - entry) / (entry - stop)
                except Exception:
                    fill_price = None

            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO receipt
                    (rec_id, token_code, action, fill_price, fill_qty, filled_at,
                     deviation_pct, deviation_r, note, source)
                VALUES (?,?,?,?,?,?,?,?,?, 'link')
                """,
                (
                    row["id"], code, action, fill_price, row["qty"],
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    deviation_pct, deviation_r, note or None,
                ),
            )
            conn.execute(
                "UPDATE receipt_token SET uses = uses + 1 WHERE code = ?", (code,)
            )
            status = {"executed": "executed", "partial": "executed", "skipped": "skipped"}[action]
            conn.execute("UPDATE recommendation SET status=? WHERE id=?", (status, row["id"]))
            conn.execute("COMMIT")
        finally:
            conn.close()

        rec = dict(row) if row else None
        return templates.TemplateResponse(
            request=request, name="receipt.html",
            context=_ctx(request, rec=rec, code=code, existing=None, done=action),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        from kestrel.services import settings_schema

        conn = connect()
        try:
            values = settings_schema.read_all(conn)
            glossary = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM glossary ORDER BY key"
                ).fetchall()
            ]
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request, name="settings.html",
            context=_ctx(request, sections=settings_schema.SECTIONS, values=values,
                         glossary=glossary, saved=request.query_params.get("saved")),
        )

    @app.post("/settings/{section}")
    async def settings_save(request: Request, section: str):
        from fastapi.responses import RedirectResponse

        from kestrel.services import config as config_service
        from kestrel.services import settings_schema

        target = settings_schema.SECTION_MAP.get(section)
        if target is not None:
            form = await request.form()
            conn = connect()
            try:
                for field_def in target.fields:
                    value = (
                        field_def.key in form
                        if field_def.kind == "switch"
                        else settings_schema.coerce(field_def, str(form.get(field_def.key, "")))
                    )
                    config_service.put(conn, field_def.key, value)
                conn.execute(
                    "INSERT INTO audit_log (action, target) VALUES ('settings.save', ?)",
                    (section,),
                )
                conn.commit()
            finally:
                conn.close()
        return RedirectResponse(f"/settings?saved={section}", status_code=303)

    @app.get("/journal", response_class=HTMLResponse)
    async def journal_page(request: Request):
        from kestrel.services import journal

        return templates.TemplateResponse(
            request=request, name="journal.html",
            context=_ctx(request, open_trades=journal.list_trades("open"),
                         closed=journal.list_trades("closed"), stats=journal.stats(),
                         positions=journal.positions(), pnl=journal.pnl),
        )

    @app.post("/journal/add")
    async def journal_add(request: Request):
        from fastapi.responses import RedirectResponse

        from kestrel.services import journal

        form = await request.form()
        journal.add_trade(
            market=str(form.get("market", "HK")),
            code=str(form.get("code", "")).strip(),
            name=str(form.get("name") or "").strip(),
            qty=int(float(form.get("qty") or 0)),
            entry_price=str(form.get("entry_price") or 0),
            stop_price=str(form.get("stop_price") or "") or None,
            target_price=str(form.get("target_price") or "") or None,
            strategy=str(form.get("strategy") or "") or None,
        )
        return RedirectResponse("/journal", status_code=303)

    @app.post("/journal/{trade_id}/close")
    async def journal_close(request: Request, trade_id: int):
        from fastapi.responses import RedirectResponse

        from kestrel.services import journal

        form = await request.form()
        journal.close_trade(
            trade_id,
            str(form.get("exit_price") or 0),
            str(form.get("review") or ""),
            bool(form.get("rule_break")),
        )
        return RedirectResponse("/journal", status_code=303)

    @app.post("/journal/{trade_id}/delete")
    async def journal_delete(trade_id: int):
        from fastapi.responses import RedirectResponse

        from kestrel.services import journal

        journal.delete_trade(trade_id)
        return RedirectResponse("/journal", status_code=303)

    @app.get("/ipo", response_class=HTMLResponse)
    async def ipo_page(request: Request):
        from kestrel.services import journal

        return templates.TemplateResponse(
            request=request, name="ipo.html",
            context=_ctx(request, rows=journal.list_ipos(), stats=journal.ipo_stats(),
                         pnl=journal.ipo_pnl),
        )

    @app.post("/ipo/add")
    async def ipo_add(request: Request):
        from fastapi.responses import RedirectResponse

        from kestrel.services import journal

        form = await request.form()
        journal.add_ipo(
            name=str(form.get("name", "")).strip(),
            code=str(form.get("code") or "").strip() or None,
            lot_size=int(float(form.get("lot_size") or 1)),
            offer_price=str(form.get("offer_price") or 0),
            lots=int(float(form.get("lots") or 1)),
            fee=str(form.get("fee") or 0),
            allot_qty=int(float(form.get("allot_qty") or 0)),
            sell_price=str(form.get("sell_price") or "") or None,
            listed_on=str(form.get("listed_on") or "") or None,
        )
        return RedirectResponse("/ipo", status_code=303)

    @app.post("/ipo/{ipo_id}/delete")
    async def ipo_delete(ipo_id: int):
        from fastapi.responses import RedirectResponse

        from kestrel.services import journal

        journal.delete_ipo(ipo_id)
        return RedirectResponse("/ipo", status_code=303)

    @app.get("/fees", response_class=HTMLResponse)
    async def fees_page(request: Request):
        from decimal import Decimal

        from kestrel.fees.calculator import PLANS, quote_fees

        market = request.query_params.get("market", "HK")
        amount = request.query_params.get("amount", "10000")
        shares = request.query_params.get("shares", "100")
        broker = request.query_params.get("broker") or None
        result = None
        try:
            buy = quote_fees(market, Decimal(amount), Decimal(shares), False, broker)
            sell = quote_fees(market, Decimal(amount), Decimal(shares), True, broker)
            total = buy.total + sell.total
            result = {
                "buy": buy.as_dict(),
                "sell": sell.as_dict(),
                "total": f"{total:,.2f}",
                "breakeven_pct": f"{(total / Decimal(amount) * 100):.3f}"
                if Decimal(amount)
                else "0",
            }
        except Exception:
            result = None
        return templates.TemplateResponse(
            request=request, name="fees.html",
            context=_ctx(request, plans=list(PLANS.values()), result=result, market=market,
                         amount=amount, shares=shares, broker=broker or ""),
        )

    @app.get("/backtest", response_class=HTMLResponse)
    async def backtest_page(request: Request):
        from kestrel.services import backtest

        return templates.TemplateResponse(
            request=request, name="backtest.html",
            context=_ctx(request, runs=backtest.recent(20), result=None, error=None,
                         form={"market": "HK", "code": "00700", "strategy": "trend_pullback"}),
        )

    @app.post("/backtest", response_class=HTMLResponse)
    async def backtest_run(request: Request):
        from kestrel.services import backtest
        from kestrel.services import bars as bars_service

        form = await request.form()
        market = str(form.get("market", "HK")).upper()
        code = str(form.get("code", "")).strip()
        strategy = str(form.get("strategy", "trend_pullback"))
        error = None
        result = None
        conn = connect()
        try:
            row = conn.execute(
                "SELECT id FROM symbol WHERE market=? AND code=?", (market, code)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT OR IGNORE INTO symbol (market, code, name, currency, lot_size)"
                    " VALUES (?,?,?,?,?)",
                    (market, code, code,
                     {"CN": "CNY", "HK": "HKD", "US": "USD"}.get(market, "USD"),
                     100 if market in ("HK", "CN") else 1),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT id FROM symbol WHERE market=? AND code=?", (market, code)
                ).fetchone()
            bars = await bars_service.get_bars(conn, row["id"], market, code, limit=600)
            if len(bars) < 80:
                error = "历史数据不足（少于 80 根日线）"
            else:
                result = backtest.run(bars, strategy)
                backtest.save(market, code, strategy, result, str(bars[0]["date"]),
                              str(bars[-1]["date"]))
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request, name="backtest.html",
            context=_ctx(request, runs=backtest.recent(20), result=result, error=error,
                         form={"market": market, "code": code, "strategy": strategy}),
        )

    @app.get("/api/glossary", response_class=JSONResponse)
    async def api_glossary(request: Request):
        locale = request.cookies.get("locale") or "zh-CN"
        zh = locale.startswith("zh")
        conn = connect()
        try:
            rows = conn.execute("SELECT * FROM glossary WHERE suppressed = 0").fetchall()
        finally:
            conn.close()
        return {
            "ok": True,
            "items": {
                r["key"]: {
                    "term": r["term_zh"] if zh else r["term_en"],
                    "body": r["body_zh"] if zh else r["body_en"],
                }
                for r in rows
            },
        }

    @app.post("/glossary/{key}/toggle")
    async def glossary_toggle(key: str):
        from fastapi.responses import RedirectResponse

        conn = connect()
        try:
            conn.execute("UPDATE glossary SET suppressed = 1 - suppressed WHERE key=?", (key,))
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse("/settings#glossary", status_code=303)

    return app


app = create_app()
