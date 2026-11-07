"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from kestrel import __version__
from kestrel.core.clock import iso
from kestrel.core.money import to_micro
from kestrel.datasources.base import guess_market
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
    tpl.env.filters["cur"] = _cur
    tpl.env.filters["cny"] = _cny
    tpl.env.filters["when"] = _when
    tpl.env.filters["src_url"] = _src_url
    tpl.env.filters["pct"] = _pct
    tpl.env.filters["px"] = _px
    return tpl


_PX_DIGITS = {"CN": 2, "HK": 3, "US": 2}


def _px(value, market: str = "US") -> str:
    """Price with the decimal count that market actually quotes in."""
    if value is None:
        return "—"
    digits = _PX_DIGITS.get(str(market).upper(), 2)
    try:
        return f"{int(value) / 1_000_000:,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value, places: int = 2) -> str:
    """Signed percentage, used for both quote changes and returns."""
    try:
        number = float(0 if value is None else value)
    except (TypeError, ValueError):
        return "—"
    return f"{'+' if number >= 0 else ''}{number:.{places}f}%"


def _chart_geometry(bars: list[dict], width: int = 760, height: int = 240) -> dict:
    """Scale a bar series into SVG coordinates for the detail page."""
    if not bars:
        return {"line": "", "area": "", "min": 0, "max": 0, "bars": []}
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    top, bottom = max(highs), min(lows)
    span = (top - bottom) or 1.0
    pad = 12
    step = (width - pad * 2) / max(1, len(bars) - 1)

    def y_of(value: float) -> float:
        return pad + (top - value) / span * (height - pad * 2)

    points = []
    candles = []
    for index, bar in enumerate(bars):
        x = pad + index * step
        close_y = y_of(float(bar["close"]))
        points.append(f"{x:.1f},{close_y:.1f}")
        candles.append(
            {
                "x": round(x, 1),
                "open": round(y_of(float(bar["open"])), 1),
                "close": round(close_y, 1),
                "high": round(y_of(float(bar["high"])), 1),
                "low": round(y_of(float(bar["low"])), 1),
                "up": float(bar["close"]) >= float(bar["open"]),
            }
        )
    line = " ".join(points)
    area = f"{pad},{height - pad} {line} {pad + (len(bars) - 1) * step:.1f},{height - pad}"
    return {
        "line": line,
        "area": area,
        "min": bottom,
        "max": top,
        "width": width,
        "height": height,
        "bars": candles,
    }


def _src_url(source: str, market: str, code: str) -> str:
    """Where the number came from, so the user can go verify it."""
    market = (market or "").upper()
    code = str(code or "")
    source = (source or "eastmoney").lower()

    if market == "CN":
        prefix = "sh" if code.startswith(("6", "9", "5")) else "sz"
        if source == "tencent":
            return f"https://gu.qq.com/{prefix}{code}"
        if source == "sina":
            return f"https://finance.sina.com.cn/realstock/company/{prefix}{code}/nc.shtml"
        return f"https://quote.eastmoney.com/{prefix}{code}.html"

    if market == "HK":
        if source == "tencent":
            return f"https://gu.qq.com/hk{code}"
        if source == "sina":
            return f"https://stock.finance.sina.com.cn/hkstock/quotes/{code}.html"
        return f"https://quote.eastmoney.com/hk/{code}.html"

    if source == "tencent":
        return f"https://gu.qq.com/us{code}"
    if source == "sina":
        return f"https://stock.finance.sina.com.cn/usstock/quotes/{code}.html"
    return f"https://quote.eastmoney.com/us/{code}.html"


def _money(value, places: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value) / 1_000_000:,.{places}f}"
    except (TypeError, ValueError):
        return str(value)


_CUR_SYMBOL = {"CNY": "¥", "HKD": "HK$", "USD": "US$"}
_FX_CNY = {"CNY": 1.0, "HKD": 0.92, "USD": 7.1}


def _cur(value, currency: str = "CNY", places: int = 2) -> str:
    """Format a micro-unit amount with its currency, e.g. US$341.34."""
    if value is None:
        return "—"
    try:
        amount = int(value) / 1_000_000
    except (TypeError, ValueError):
        return str(value)
    return f"{_CUR_SYMBOL.get(currency, '¥')}{amount:,.{places}f}"


def _cny(value, currency: str = "CNY") -> str:
    """Same amount converted to CNY, for the 'what does this cost me' line."""
    if value is None:
        return "—"
    try:
        amount = int(value) / 1_000_000
    except (TypeError, ValueError):
        return "—"
    rate = _FX_CNY.get(currency, 1.0)
    return f"¥{amount * rate:,.0f}"


def _when(iso_text: str) -> str:
    """Render an ISO timestamp in Beijing time, which is what the user reads."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not iso_text:
        return "—"
    try:
        moment = datetime.fromisoformat(str(iso_text))
    except ValueError:
        return str(iso_text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    return moment.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")


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
    from kestrel.datasources import resolve as resolver

    seeded = resolver.load_from_db()
    if seeded:
        log.info("resolved %d cached security ids", seeded)
    from kestrel.fees import plans as plan_service

    plan_service.hydrate()
    from kestrel.services import glossary as glossary_service

    glossary_service.seed()
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
        from kestrel.services import calendar as calendar_service

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
            request=request,
            name="market.html",
            context=_ctx(
                request,
                items=[dict(r) for r in rows],
                markets=calendar_service.all_status(),
            ),
        )

    @app.post("/api/watchlist", response_class=JSONResponse)
    async def add_symbol(request: Request):
        from kestrel.datasources import resolve as resolver

        body = await request.json()
        raw = str(body.get("code", "")).strip()
        if not raw:
            return JSONResponse({"ok": False, "error": "empty code"}, status_code=400)
        if any(ch.isspace() for ch in raw):
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"「{raw}」里有空格。股票代码不能带空格——如果你是想输入名称，请用中文名，比如「腾讯」。",
                },
                status_code=400,
            )
        asked = (body.get("market") or "").upper() or None
        if not asked:
            # let the resolver pick the market when the user did not say
            asked = None

        found = await resolver.resolve(raw, asked)
        if found is None:
            hint_market = asked or guess_market(raw)
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"找不到「{raw}」这只标的。检查代码是否正确，或换一个市场再试。",
                    "hint": f"已尝试市场：{hint_market}",
                },
                status_code=404,
            )

        market, code = found.market, found.code
        conn = connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO symbol (market, code, name, currency, lot_size, exchange, secid,
                                    resolve_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, datetime('now'))
                ON CONFLICT(market, code) DO UPDATE SET
                    name        = excluded.name,
                    exchange    = excluded.exchange,
                    secid       = excluded.secid,
                    resolve_error = NULL,
                    is_active   = 1,
                    updated_at  = datetime('now')
                """,
                (
                    market,
                    code,
                    found.name,
                    found.currency,
                    found.lot_size,
                    found.exchange,
                    found.secid,
                ),
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
        return {
            "ok": True,
            "market": market,
            "code": code,
            "name": found.name,
            "secid": found.secid,
        }

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

    @app.get("/advice", response_class=HTMLResponse)
    async def advice_page(request: Request):
        from kestrel.services import config as config_service
        from kestrel.services import scan as scan_service

        scan_service.expire_stale()
        recs = scan_service.active_recommendations(50)
        conn = connect()
        try:
            last = config_service.get(conn, "scan.last", None)
            auto = bool(config_service.get(conn, "scan.auto_enabled", False))
            interval = int(config_service.get(conn, "scan.interval_minutes", 60) or 60)
            markets = config_service.get(conn, "scan.markets", ["CN", "HK", "US"])
            universe = str(config_service.get(conn, "scan.universe", "both"))
            ui = {
                "learning": bool(config_service.get(conn, "ui.learning_mode", True)),
                "glossary": bool(config_service.get(conn, "ui.glossary", True)),
            }
        finally:
            conn.close()

        next_run = None
        if auto and last and last.get("finished_at"):
            from datetime import datetime, timedelta

            from kestrel.core.clock import utcnow

            try:
                base = datetime.fromisoformat(str(last["finished_at"]).replace("Z", "+00:00"))
            except ValueError:
                base = utcnow()
            next_run = base + timedelta(minutes=interval)

        return templates.TemplateResponse(
            request=request,
            name="advice.html",
            context=_ctx(
                request,
                recs=recs,
                last=last,
                ui=ui,
                auto=auto,
                interval=interval,
                next_run=next_run,
                markets=markets,
                universe=universe,
                history=scan_service.history(20),
                scans=scan_service.recent_scans(6),
            ),
        )

    @app.get("/scan")
    async def scan_redirect():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/advice", status_code=301)

    @app.post("/advice/{rec_id}/decide", response_class=JSONResponse)
    async def advice_decide(rec_id: int, request: Request):
        from kestrel.services import scan as scan_service

        try:
            body = await request.json()
        except Exception:
            body = {}
        decision = str((body or {}).get("decision", "")).strip()
        note = str((body or {}).get("note", "")).strip()
        fill = (body or {}).get("fill_price")
        try:
            fill_price = float(fill) if fill not in (None, "") else None
        except (TypeError, ValueError):
            fill_price = None
        ok = scan_service.decide(rec_id, decision, note, fill_price)
        return {"ok": ok}

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
        from kestrel.datasources import registry as source_registry
        from kestrel.services import channels as channel_service
        from kestrel.services import config as config_service
        from kestrel.services import universe as universe_service

        conn = connect()
        try:
            values = config_service.all_values(conn)
            glossary = [
                dict(r) for r in conn.execute("SELECT * FROM glossary ORDER BY key").fetchall()
            ]
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context=_ctx(
                request,
                values=values,
                glossary=glossary,
                sources=source_registry.list_sources(),
                channels=channel_service.all_channels(),
                pool=universe_service.pool_summary(),
                saved=request.query_params.get("saved"),
            ),
        )

    @app.post("/api/datasources", response_class=JSONResponse)
    async def api_datasources(request: Request):
        from kestrel.datasources import registry as source_registry
        from kestrel.services import config as config_service

        body = await request.json()
        items = (body or {}).get("items") or {}
        conn = connect()
        try:
            for key, enabled in items.items():
                config_service.put(conn, f"datasource.{key}.enabled", bool(enabled))
            conn.commit()
        finally:
            conn.close()
        request.app.state.chain = source_registry.build()
        return {"ok": True, "sources": source_registry.list_sources()}

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

    @app.get("/cost", response_class=HTMLResponse)
    async def cost_page(request: Request):
        from decimal import Decimal

        from kestrel.fees import plans as plan_service
        from kestrel.fees.calculator import quote_fees

        market = request.query_params.get("market", "HK").upper()
        amount = request.query_params.get("amount", "10000")
        shares = request.query_params.get("shares", "100")
        broker = request.query_params.get("broker") or None
        result = None
        try:
            buy = quote_fees(market, Decimal(amount), Decimal(shares), False, broker)
            sell = quote_fees(market, Decimal(amount), Decimal(shares), True, broker)
            total = buy.total + sell.total
            cost_amount = Decimal(amount) or Decimal("1")
            result = {
                "buy": buy.as_dict(),
                "sell": sell.as_dict(),
                "total": f"{total:,.2f}",
                "breakeven_pct": f"{(total / cost_amount * 100):.3f}",
                "breakeven_price": f"{(Decimal(amount) + total) / (Decimal(shares) or Decimal('1')):.3f}",
            }
        except Exception:
            result = None

        plan_list = plan_service.list_plans()
        return templates.TemplateResponse(
            request=request,
            name="cost.html",
            context=_ctx(
                request,
                plans=plan_list,
                plan_market={},
                result=result,
                market=market,
                amount=amount,
                shares=shares,
                broker=broker or "",
            ),
        )

    @app.get("/fees")
    async def fees_redirect():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/cost", status_code=301)

    @app.get("/sim", response_class=HTMLResponse)
    async def sim_page(request: Request):
        from kestrel.services import backtest

        return templates.TemplateResponse(
            request=request, name="sim.html",
            context=_ctx(request, runs=backtest.recent(20), result=None, error=None,
                         form={"market": "HK", "code": "00700", "strategy": "trend_pullback"}),
        )

    @app.get("/backtest")
    async def backtest_redirect():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/sim", status_code=301)

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
            request=request, name="sim.html",
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

    # ---------------------------------------------------------------- stock

    @app.get("/stock/{market}/{code}", response_class=HTMLResponse)
    async def stock_page(request: Request, market: str, code: str):
        from kestrel.indicators.engine import Series, compute
        from kestrel.services import bars as bars_service
        from kestrel.services import calendar as calendar_service

        market = market.upper()
        conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM symbol WHERE market=? AND code=?", (market, code)
            ).fetchone()
            if row is None:
                return templates.TemplateResponse(
                    request=request,
                    name="stock.html",
                    context=_ctx(request, sym=None, code=code, market=market),
                    status_code=404,
                )
            quote = conn.execute(
                "SELECT * FROM quote_latest WHERE symbol_id=?", (row["id"],)
            ).fetchone()
            rec = conn.execute(
                "SELECT * FROM recommendation WHERE symbol_id=? AND status='active'"
                " ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            trade = conn.execute(
                "SELECT * FROM trade WHERE market=? AND code=? AND status='open'"
                " ORDER BY id DESC LIMIT 1",
                (market, code),
            ).fetchone()
            bars = bars_service.cached_bars(conn, row["id"], 120)
        finally:
            conn.close()

        ind = compute(Series.from_bars(bars)) if len(bars) >= 40 else None
        chart = _chart_geometry(bars[-60:])
        return templates.TemplateResponse(
            request=request,
            name="stock.html",
            context=_ctx(
                request,
                sym=dict(row),
                quote=dict(quote) if quote else None,
                rec=dict(rec) if rec else None,
                trade=dict(trade) if trade else None,
                bars=bars[-60:],
                chart=chart,
                ind=ind,
                status=calendar_service.status(market),
            ),
        )

    @app.get("/api/market-status", response_class=JSONResponse)
    async def api_market_status():
        from kestrel.services import calendar as calendar_service

        return {
            "ok": True,
            "markets": [
                {
                    "market": s.market,
                    "name": s.name,
                    "state": s.state,
                    "label": s.label,
                    "local_time": s.local_time,
                    "seconds_to_open": s.seconds_to_open,
                }
                for s in calendar_service.all_status()
            ],
        }

    # ---------------------------------------------------------------- pool

    @app.post("/api/pool/refresh", response_class=JSONResponse)
    async def api_pool_refresh(request: Request):
        from kestrel.services import config as config_service
        from kestrel.services import universe as universe_service

        try:
            body = await request.json()
        except Exception:
            body = {}
        conn = connect()
        try:
            markets = (body or {}).get("markets") or config_service.get(
                conn, "scan.markets", ["CN", "HK", "US"]
            )
            size = int((body or {}).get("size") or config_service.get(conn, "scan.pool_size", 30))
        finally:
            conn.close()
        counts = await universe_service.refresh(markets, size)
        return {"ok": True, "counts": counts, "total": universe_service.pool_summary()}

    @app.get("/api/pool", response_class=JSONResponse)
    async def api_pool(market: str = "", limit: int = 60):
        from kestrel.services import universe as universe_service

        markets = [market.upper()] if market else ["CN", "HK", "US"]
        return {"ok": True, "items": universe_service.pool_symbols(markets, limit)}

    # ---------------------------------------------------------------- backup

    @app.get("/api/backup/export")
    async def api_backup_export():
        """Download a consistent snapshot of the whole database."""
        import sqlite3
        import tempfile

        from fastapi.responses import FileResponse

        from kestrel.settings import settings

        with tempfile.NamedTemporaryFile(
            prefix="kestrel-", suffix=".db", delete=False
        ) as handle:
            snapshot = handle.name

        src = sqlite3.connect(str(settings.db_path))
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        return FileResponse(
            snapshot, filename=f"kestrel-backup-{stamp}.db", media_type="application/octet-stream"
        )

    @app.get("/api/backup/json", response_class=JSONResponse)
    async def api_backup_json():
        conn = connect()
        try:
            data = {}
            for table in ("symbol", "watchlist", "trade", "ipo_ledger", "config_kv", "recommendation"):
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                data[table] = [dict(r) for r in rows]
        finally:
            conn.close()
        return {"ok": True, "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "tables": data}

    @app.post("/api/backup/import", response_class=JSONResponse)
    async def api_backup_import(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "不是有效的 JSON"}, status_code=400)
        tables = (body or {}).get("tables")
        if not isinstance(tables, dict):
            return JSONResponse({"ok": False, "error": "缺少 tables 字段"}, status_code=400)

        allowed = {"symbol", "watchlist", "trade", "ipo_ledger", "config_kv", "recommendation"}
        conn = connect()
        added = 0
        try:
            conn.execute("BEGIN")
            for table, rows in tables.items():
                if table not in allowed or not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict) or not row:
                        continue
                    cols = ",".join(row.keys())
                    marks = ",".join("?" * len(row))
                    try:
                        conn.execute(
                            f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({marks})",
                            tuple(row.values()),
                        )
                        added += 1
                    except Exception:  # noqa: BLE001, PERF203
                        continue
            conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001
            conn.execute("ROLLBACK")
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        finally:
            conn.close()
        return {"ok": True, "rows": added}

    # ---------------------------------------------------------------- broker plans

    @app.get("/api/brokers", response_class=JSONResponse)
    async def api_brokers(market: str = ""):
        from kestrel.fees import plans as plan_service

        return {"ok": True, "items": plan_service.list_plans(market or None)}

    @app.post("/api/brokers", response_class=JSONResponse)
    async def api_broker_save(request: Request):
        from kestrel.fees import plans as plan_service

        body = await request.json()
        try:
            key = plan_service.upsert(body or {})
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "key": key}

    @app.post("/api/brokers/{key}/delete", response_class=JSONResponse)
    async def api_broker_delete(key: str):
        from kestrel.fees import plans as plan_service

        return {"ok": plan_service.delete(key)}

    # ---------------------------------------------------------------- notify test

    @app.post("/api/notify/test", response_class=JSONResponse)
    async def api_notify_test(request: Request):
        from kestrel.notify import dispatcher

        try:
            body = await request.json()
        except Exception:
            body = {}
        channel = str((body or {}).get("channel", "")).strip() or None
        result = await dispatcher.send_test(channel)
        return result

    @app.post("/api/notify/config", response_class=JSONResponse)
    async def api_notify_config(request: Request):
        from kestrel.services import channels as channel_service

        body = await request.json()
        key = str((body or {}).get("key", "")).strip()
        if not key:
            return JSONResponse({"ok": False, "error": "missing key"}, status_code=400)
        channel_service.save(key, body or {})
        return {"ok": True}

    # ---------------------------------------------------------------- trade import

    @app.post("/api/journal/parse", response_class=JSONResponse)
    async def api_journal_parse(request: Request):
        from kestrel.services import statement

        body = await request.json()
        text = str((body or {}).get("text", ""))
        parsed = statement.parse(text)
        return {"ok": True, "rows": parsed}

    @app.post("/api/journal/import", response_class=JSONResponse)
    async def api_journal_import(request: Request):
        from kestrel.services import statement

        body = await request.json()
        rows = (body or {}).get("rows") or []
        saved = statement.save(rows)
        return {"ok": True, "saved": saved}

    # ---------------------------------------------------------------- settings extras

    @app.post("/api/settings", response_class=JSONResponse)
    async def api_settings_save(request: Request):
        from kestrel.services import config as config_service

        body = await request.json()
        items = (body or {}).get("items") or {}
        if not isinstance(items, dict):
            return JSONResponse({"ok": False, "error": "items must be an object"}, status_code=400)
        conn = connect()
        try:
            for key, value in items.items():
                config_service.put(conn, str(key), value)
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "saved": len(items)}

    return app


app = create_app()
