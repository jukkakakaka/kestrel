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
from kestrel.core.quantity import db_value, to_qty
from kestrel.datasources.base import guess_market
from kestrel.datasources.chain import QuoteChain, default_chain
from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.i18n import available_locales, load_catalog, locale_label, translate
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
    tpl.env.filters["quote"] = _quote
    tpl.env.filters["num"] = _num
    return tpl


def _num(value, places: int = 2) -> str:
    """Render a config number without a trailing ``.0`` noise.

    设置页里 ``10000.0`` 读起来像精度错误；用户填的是「一万」，不是浮点数。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value if value is not None else "")
    if number == int(number):
        return str(int(number))
    return f"{number:.{places}f}".rstrip("0").rstrip(".")


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
_MARKET_CURRENCY = {"CN": "CNY", "HK": "HKD", "US": "USD"}


def _display_currency() -> str:
    """用户选择的显示货币；``local`` 表示各市场用自己的本币。"""
    try:
        from kestrel.services import config as config_service

        conn = connect()
        try:
            value = str(config_service.get(conn, "ui.display_currency", "local") or "local")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        value = "local"
    value = value.strip()
    return value.upper() if value.lower() != "local" else "local"


def _quote(value, market: str = "US", display: str = "local") -> str:
    """带货币符号的价格。

    行情页以前只给数字——「409.600」既可能是港币也可能是美元，三个市场同屏时
    这是实实在在的误读来源。``display`` 不是 ``local`` 时按固定汇率折算。
    """
    if value is None or value == "":
        return "—"
    currency = _MARKET_CURRENCY.get(str(market).upper(), "USD")
    try:
        amount = int(value) / 1_000_000
    except (TypeError, ValueError):
        return str(value)

    target = currency if display == "local" else display
    if target not in _FX_CNY:
        target = currency
    if target != currency:
        # 先折成人民币，再折到目标货币，保持一个统一的中转口径。
        amount = amount * _FX_CNY.get(currency, 1.0) / _FX_CNY[target]

    digits = _PX_DIGITS.get(str(market).upper(), 2)
    return f"{_CUR_SYMBOL.get(target, '')}{amount:,.{digits}f}"


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

    def _locale(request: Request) -> str:
        """当前语言。查询参数优先于 cookie——分享链接、截图脚本、以及
        「临时看另一种语言」都需要一个不用改 cookie 的入口。"""
        query_locale = request.query_params.get("locale")
        if query_locale:
            return query_locale if query_locale in available_locales() else "zh-CN"
        return request.cookies.get("locale") or "zh-CN"

    def _known_symbols(limit: int = 300) -> list[dict]:
        """记账弹窗的代码联想：自选优先，其次候选池。"""
        conn = connect()
        try:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT s.market, s.code, s.name FROM symbol s"
                    " LEFT JOIN watchlist w ON w.symbol_id = s.id"
                    " WHERE w.id IS NOT NULL OR s.is_pool=1"
                    " ORDER BY (w.id IS NULL), s.market, s.code LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
        finally:
            conn.close()

    def _ctx(request: Request, **extra):
        locale = _locale(request)
        query_theme = request.query_params.get("theme")
        theme = query_theme or request.cookies.get("theme") or "auto"
        # 校验的是**最终**取值，不是查询参数。早先这里校验 query_theme，
        # 而它平时是 None，于是 cookie 里的深色每次都被改回 auto——下拉框
        # 写着「深色」，页面仍是浅色。
        if theme not in ("auto", "light", "dark"):
            theme = "auto"
        return {
            "t": lambda key, **kw: translate(locale, key, **kw),
            "locale": locale,
            "locales": available_locales(),
            "locale_label": locale_label,
            "theme": theme,
            "version": __version__,
            "catalog": load_catalog(locale),
            **extra,
        }

    @app.get("/healthz", response_class=JSONResponse)
    async def healthz(request: Request):
        chain: QuoteChain = request.app.state.chain
        return {"ok": True, "version": __version__, "sources": chain.health}

    @app.get("/welcome", response_class=HTMLResponse)
    async def welcome_page(request: Request):
        """首次启动的三步引导。每一项都对应一个真实设置，不是装饰。"""
        return templates.TemplateResponse(
            request=request,
            name="welcome.html",
            context=_ctx(request),
        )

    @app.post("/api/onboarding", response_class=JSONResponse)
    async def api_onboarding(request: Request):
        from kestrel.services import config as config_service
        from kestrel.services import timeframe

        try:
            body = await request.json()
        except Exception:
            body = {}
        conn = connect()
        try:
            if not (body or {}).get("skip"):
                capital = body.get("capital")
                if capital:
                    config_service.put(conn, "account.capital", float(capital))
                risk = body.get("risk_pct")
                if risk:
                    config_service.put(conn, "risk.per_trade_pct", float(risk))
                markets = [
                    str(m).upper() for m in (body.get("markets") or [])
                    if str(m).upper() in ("CN", "HK", "US")
                ]
                if markets:
                    config_service.put(conn, "scan.markets", markets)
                horizon = str(body.get("horizon") or "auto")
                if horizon == timeframe.AUTO or timeframe.is_valid(horizon):
                    config_service.put(conn, "trade.horizon", horizon)
            config_service.put(conn, "ui.onboarded", True)
            conn.commit()
        finally:
            conn.close()
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        """首页回答「今天该做什么」，而不是再放一张行情表。

        新手打开工具最想问的是「我现在要干嘛」；行情表在 /market 有完整一版，
        首页把它降级成一张小卡片，把待办放上去。
        """
        from kestrel.services import calendar as calendar_service
        from kestrel.services import config as config_service
        from kestrel.services import today as today_service

        conn = connect()
        try:
            onboarded = bool(config_service.get(conn, "ui.onboarded", False))
            enabled = [str(m).upper() for m in config_service.get(
                conn, "scan.markets", ["CN", "HK", "US"]
            ) or ["CN", "HK", "US"]]
        finally:
            conn.close()
        if not onboarded:
            from fastapi.responses import RedirectResponse

            return RedirectResponse("/welcome", status_code=303)

        try:
            brief = today_service.briefing(_locale(request))
        except Exception as exc:  # noqa: BLE001
            log.warning("today briefing failed: %s", exc)
            brief = {"todos": [], "urgent": 0, "headline": "暂时算不出来",
                     "quiet": True, "book": {}, "positions": [], "active_advice": 0}

        marks = ",".join("?" * len(enabled)) or "''"
        conn = connect()
        try:
            rows = conn.execute(
                f"""
                SELECT s.market, s.code, s.name, s.currency, w.note,
                       q.price, q.prev_close, q.change_pct, q.source, q.fetched_at
                FROM watchlist w
                JOIN symbol s ON s.id = w.symbol_id
                LEFT JOIN quote_latest q ON q.symbol_id = s.id
                WHERE s.market IN ({marks})
                ORDER BY s.market, s.code
                LIMIT 8
                """,
                tuple(enabled),
            ).fetchall()
            watch_count = conn.execute(
                f"SELECT COUNT(*) AS n FROM watchlist WHERE symbol_id IN"
                f" (SELECT id FROM symbol WHERE market IN ({marks}))",
                tuple(enabled),
            ).fetchone()["n"]
        finally:
            conn.close()

        return templates.TemplateResponse(
            request=request,
            name="today.html",
            context=_ctx(
                request,
                brief=brief,
                items=[dict(r) for r in rows],
                watch_count=watch_count,
                markets=calendar_service.all_status(enabled, _locale(request)),
                display_currency=_display_currency(),
            ),
        )

    @app.get("/market", response_class=HTMLResponse)
    async def market_page(request: Request):
        from kestrel.services import calendar as calendar_service
        from kestrel.services import config as config_service

        conn = connect()
        try:
            enabled = [str(m).upper() for m in config_service.get(
                conn, "scan.markets", ["CN", "HK", "US"]
            ) or ["CN", "HK", "US"]]
            marks = ",".join("?" * len(enabled)) or "''"
            rows = conn.execute(
                f"""
                SELECT s.market, s.code, s.name, s.currency, w.note,
                       q.price, q.prev_close, q.change_pct, q.source, q.fetched_at
                FROM watchlist w
                JOIN symbol s ON s.id = w.symbol_id
                LEFT JOIN quote_latest q ON q.symbol_id = s.id
                WHERE s.market IN ({marks})
                ORDER BY s.market, s.code
                """,
                tuple(enabled),
            ).fetchall()
        finally:
            conn.close()
        return templates.TemplateResponse(
            request=request,
            name="market.html",
            context=_ctx(
                request,
                items=[dict(r) for r in rows],
                markets=calendar_service.all_status(enabled, _locale(request)),
                enabled_markets=enabled,
                display_currency=_display_currency(),
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
        from kestrel.services import analyst as analyst_service
        from kestrel.services import config as config_service
        from kestrel.services import scan as scan_service

        scan_service.expire_stale()
        # an answer may have landed since the last visit
        try:
            analyst_service.ingest()
        except Exception as exc:  # noqa: BLE001
            log.warning("analyst ingest failed: %s", exc)
        recs = scan_service.active_recommendations(50)
        conn = connect()
        try:
            last = config_service.get(conn, "scan.last", None)
            auto = bool(config_service.get(conn, "scan.auto_enabled", False))
            interval = int(config_service.get(conn, "scan.interval_minutes", 60) or 60)
            markets = config_service.get(conn, "scan.markets", ["CN", "HK", "US"])
            universe = str(config_service.get(conn, "scan.universe", "both"))
            requested = str(config_service.get(conn, "trade.horizon", "auto") or "auto")
            ui = {
                "learning": bool(config_service.get(conn, "ui.learning_mode", True)),
                "glossary": bool(config_service.get(conn, "ui.glossary", True)),
            }
        finally:
            conn.close()

        from kestrel.services import advisor as advisor_service
        from kestrel.services import timeframe

        advice = advisor_service.cached()
        if advice is None:
            # First visit after an upgrade: the page should not be blank about
            # which bar size is in force.
            try:
                advice = advisor_service.evaluate()
            except Exception as exc:  # noqa: BLE001
                log.warning("advisor failed on page load: %s", exc)
                advice = {}

        pinned = timeframe.is_valid(requested)
        in_force = requested if pinned else (advice or {}).get("horizon") or timeframe.DEFAULT
        from kestrel.services import engine as engine_service

        engine_info = engine_service.status().localised(_locale(request))

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
                analyst_runs=analyst_service.recent_runs(3),
                engine=engine_info,
                symbols_known=_known_symbols(),
                horizon={
                    "requested": requested,
                    "pinned": pinned,
                    "in_force": in_force,
                    "label": timeframe.label(in_force, _locale(request)),
                    "beta": timeframe.get(in_force).beta,
                    "advice": advice or {},
                    "options": timeframe.options(_locale(request)),
                    "labels": {
                        item["code"]: item["label"]
                        for item in timeframe.options(_locale(request))
                    },
                },
            ),
        )

    @app.post("/api/horizon", response_class=JSONResponse)
    async def api_horizon(request: Request):
        """Pin a bar size, or hand the decision back to the advisor."""
        from kestrel.services import advisor as advisor_service
        from kestrel.services import config as config_service
        from kestrel.services import timeframe

        try:
            body = await request.json()
        except Exception:
            body = {}
        value = str((body or {}).get("horizon", "")).strip().lower()
        if not value and (body or {}).get("force"):
            # Recompute without touching the pin: the user asked what the
            # numbers say now, not to change their setting.
            return {"ok": True, "advisor": advisor_service.evaluate(force=True)}
        if value != timeframe.AUTO and not timeframe.is_valid(value):
            return {"ok": False, "error": "unknown horizon"}

        conn = connect()
        try:
            config_service.put(conn, "trade.horizon", value)
            conn.commit()
        finally:
            conn.close()

        if value == timeframe.AUTO:
            report = advisor_service.evaluate(force=True)
        else:
            report = advisor_service.cached() or {}
        return {
            "ok": True,
            "requested": value,
            "horizon": value if value != timeframe.AUTO else report.get("horizon"),
            "advisor": report,
        }

    @app.get("/scan")
    async def scan_redirect():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/advice", status_code=301)

    @app.post("/advice/{rec_id}/decide", response_class=JSONResponse)
    async def advice_decide(rec_id: int, request: Request):
        from kestrel.services import execution
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

        if decision == "executed":
            # 点“已买入”就是一笔真实成交，必须进台账；只改状态会让持仓页空着。
            conn = connect()
            try:
                rec = conn.execute(
                    "SELECT * FROM recommendation WHERE id=?", (rec_id,)
                ).fetchone()
            finally:
                conn.close()
            if rec is None:
                return {"ok": False, "error": "这条建议不存在"}
            qty_raw = (body or {}).get("qty")
            qty = to_qty(qty_raw) if qty_raw not in (None, "") else to_qty(rec["qty"])
            if qty <= 0:
                qty = to_qty(rec["qty"])
            price = fill_price or rec["entry_low"] / 1_000_000
            result = execution.record(
                execution.Fill(
                    market=rec["market"],
                    code=rec["code"],
                    side="buy",
                    qty=qty,
                    price=price,
                    rec_id=rec_id,
                    source="advice",
                    note=note,
                    name=rec["name"],
                )
            )
            if not result.get("ok"):
                return {"ok": False, "error": result.get("error")}
            import contextlib

            with contextlib.suppress(Exception):
                scan_service.decide(rec_id, "executed", note, fill_price)
            return {"ok": True, "trade": result}

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
                SELECT r.*, t.code AS token_code, t.expires_at AS token_expires,
                       t.uses, t.max_uses
                FROM receipt_token t JOIN recommendation r ON r.id = t.rec_id
                WHERE t.code = ?
                """,
                (code,),
            ).fetchone()
            existing = None
            expired = False
            if row:
                expired = _token_expired(row)
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
                         existing=dict(existing) if existing else None,
                         expired=expired),
        )

    def _token_expired(row) -> bool:
        """链接是否失效：超时或次数用尽。

        过期后仍然显示建议内容（用户可能只是想回顾），但不再接受回执，
        否则一个三周前的链接会把今天的持仓记成昨天的事。
        """
        try:
            deadline = datetime.fromisoformat(
                str(row["token_expires"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            return False
        if deadline < datetime.now(UTC):
            return True
        return int(row["uses"] or 0) >= int(row["max_uses"] or 5)

    @app.post("/r/{code}", response_class=HTMLResponse)
    async def receipt_submit(request: Request, code: str):
        from decimal import Decimal

        from kestrel.core.money import to_micro
        from kestrel.services import execution

        form = await request.form()
        action = str(form.get("action") or "skipped")
        fill_raw = str(form.get("fill_price") or "").strip()
        qty_raw = str(form.get("fill_qty") or "").strip()
        note = str(form.get("note") or "").strip()

        conn = connect()
        try:
            row = conn.execute(
                """
                SELECT r.*, t.code AS token_code, t.expires_at AS token_expires,
                       t.uses, t.max_uses
                FROM receipt_token t JOIN recommendation r ON r.id = t.rec_id
                WHERE t.code = ?
                """,
                (code,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return templates.TemplateResponse(
                request=request, name="receipt.html",
                context=_ctx(request, rec=None, code=code, existing=None, expired=False),
            )
        if _token_expired(row):
            return templates.TemplateResponse(
                request=request, name="receipt.html",
                context=_ctx(request, rec=dict(row), code=code, existing=None, expired=True),
            )

        rec = dict(row)
        result = None
        if action in ("executed", "partial"):
            try:
                fill_price = Decimal(fill_raw) if fill_raw else Decimal(rec["entry_low"]) / Decimal(
                    1_000_000
                )
            except Exception:  # noqa: BLE001
                fill_price = None
            qty = to_qty(qty_raw) if qty_raw else to_qty(rec["qty"])
            if qty <= 0:
                qty = to_qty(rec["qty"])
            if fill_price is None or fill_price <= 0:
                return templates.TemplateResponse(
                    request=request, name="receipt.html",
                    context=_ctx(request, rec=rec, code=code, existing=None,
                                 expired=False, error="成交价填一个数字，比如 402.5"),
                )
            result = execution.record(
                execution.Fill(
                    market=rec["market"],
                    code=rec["code"],
                    side="buy",
                    qty=qty,
                    price=fill_price,
                    rec_id=rec["id"],
                    source="link",
                    note=note,
                    name=rec["name"],
                )
            )
            if not result.get("ok"):
                return templates.TemplateResponse(
                    request=request, name="receipt.html",
                    context=_ctx(request, rec=rec, code=code, existing=None,
                                 expired=False, error=result.get("error")),
                )
            pct = result.get("deviation_pct")
            r_dev = result.get("deviation_r")
        else:
            pct = r_dev = None

        conn = connect()
        try:
            conn.execute(
                """
                INSERT INTO receipt
                    (rec_id, token_code, action, fill_price, fill_qty, filled_at,
                     deviation_pct, deviation_r, note, source)
                VALUES (?,?,?,?,?,?,?,?,?, 'link')
                """,
                (
                    rec["id"], code, action,
                    to_micro(fill_price) if action in ("executed", "partial") and fill_raw else None,
                    db_value(qty) if qty_raw else rec["qty"],
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    pct, r_dev, note or None,
                ),
            )
            conn.execute("UPDATE receipt_token SET uses = uses + 1 WHERE code = ?", (code,))
            if action == "skipped":
                conn.execute(
                    "UPDATE recommendation SET status='skipped', decided_at=?"
                    " WHERE id=?",
                    (datetime.now(UTC).isoformat(timespec="seconds"), rec["id"]),
                )
            conn.commit()
        finally:
            conn.close()

        return templates.TemplateResponse(
            request=request, name="receipt.html",
            context=_ctx(request, rec=rec, code=code, existing=None, done=action,
                         result=result, expired=False),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        from kestrel.datasources import presets as preset_service
        from kestrel.datasources import registry as source_registry
        from kestrel.services import channels as channel_service
        from kestrel.services import config as config_service
        from kestrel.services import prompts as prompt_service
        from kestrel.services import timeframe
        from kestrel.services import universe as universe_service
        from kestrel.services import updates as updates_service

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
                sources=source_registry.list_all(),
                presets=preset_service.PRESETS,
                channels=channel_service.all_channels(),
                pool=universe_service.pool_summary(),
                horizon_options=timeframe.options(_locale(request)),
                prompts=prompt_service.prompts(),
                about=updates_service.info(),
                update=updates_service.cached() or {},
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
        return {"ok": True, "sources": source_registry.list_all()}

    @app.post("/api/datasources/custom", response_class=JSONResponse)
    async def api_datasource_custom(request: Request):
        from kestrel.datasources import custom as custom_service
        from kestrel.datasources import registry as source_registry

        body = await request.json()
        try:
            key = custom_service.save(body or {})
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        request.app.state.chain = source_registry.build()
        return {"ok": True, "key": key}

    @app.post("/api/datasources/custom/{key}/delete", response_class=JSONResponse)
    async def api_datasource_custom_delete(key: str, request: Request):
        from kestrel.datasources import custom as custom_service
        from kestrel.datasources import registry as source_registry

        ok = custom_service.delete(key)
        request.app.state.chain = source_registry.build()
        return {"ok": ok}

    @app.post("/api/datasources/test", response_class=JSONResponse)
    async def api_datasource_test(request: Request):
        from kestrel.datasources import custom as custom_service

        body = await request.json()
        market = str((body or {}).get("market") or "US").upper()
        code = str((body or {}).get("code") or "").strip()
        if not code:
            return {"ok": False, "error": "填一个代码再试，比如 AAPL"}
        return await custom_service.try_source(body or {}, market, code)

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
        from kestrel.services import execution, journal
        from kestrel.services import positions as position_service

        open_positions = position_service.open_positions()
        quality = execution.quality_report()
        known = _known_symbols()
        return templates.TemplateResponse(
            request=request, name="journal.html",
            context=_ctx(request, open_trades=journal.list_trades("open"),
                         closed=journal.list_trades("closed"), stats=journal.stats(),
                         positions=open_positions,
                         symbols_known=known,
                         book=position_service.summary(open_positions),
                         quality=quality,
                         lesson=execution.lesson(
                             quality, _locale(request)
                         ),
                         pnl=journal.pnl),
        )

    @app.post("/journal/add")
    async def journal_add(request: Request):
        from fastapi.responses import RedirectResponse

        from kestrel.services import execution

        form = await request.form()
        market = str(form.get("market", "HK")).upper()
        code = str(form.get("code", "")).strip()
        payload = execution.Fill(
            market=market,
            code=code,
            side=str(form.get("side") or "buy"),
            qty=to_qty(form.get("qty") or 0),
            price=str(form.get("entry_price") or 0),
            fees=str(form.get("fees") or 0),
            filled_at=str(form.get("filled_at") or "") or None,
            note=str(form.get("note") or ""),
            name=str(form.get("name") or "").strip(),
            source="manual",
            stop=str(form.get("stop_price") or "") or None,
            target=str(form.get("target_price") or "") or None,
        )
        result = execution.record(payload)
        target = "/journal"
        if not result.get("ok"):
            from urllib.parse import quote

            target = "/journal?error=" + quote(str(result.get("error"))[:160])
        return RedirectResponse(target, status_code=303)

    @app.post("/journal/{trade_id}/close")
    async def journal_close(request: Request, trade_id: int):
        from urllib.parse import quote

        from fastapi.responses import RedirectResponse

        from kestrel.services import journal

        form = await request.form()
        qty_raw = str(form.get("qty") or "").strip()
        result = journal.close_trade(
            trade_id,
            str(form.get("exit_price") or 0),
            str(form.get("review") or ""),
            bool(form.get("rule_break")),
            qty=to_qty(qty_raw) if qty_raw else None,
        )
        if isinstance(result, dict) and result.get("ok") is False:
            return RedirectResponse(
                "/journal?error=" + quote(str(result.get("error"))[:160]), status_code=303
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

    def _sim_context(request: Request, **extra):
        from kestrel.services import backtest, timeframe
        from kestrel.services import config as config_service
        from kestrel.services import engine as engine_service
        from kestrel.services import rules as rule_service

        report = {
            "ok": True,
            "rules": _translated_rules(request, rule_service.all_rules()),
            "engine": engine_service.status().localised(_locale(request)),
        }
        report["symbols"] = len({r["code"] for r in report["rules"]})
        validated = [r for r in report["rules"] if r.get("validation")]
        report["validated"] = len(validated)
        conn = connect()
        try:
            symbols = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT DISTINCT s.market, s.code, s.name FROM symbol s
                    LEFT JOIN watchlist w ON w.symbol_id = s.id
                    WHERE s.is_pool = 1 OR w.id IS NOT NULL
                    ORDER BY (w.id IS NULL), s.market, s.code
                    LIMIT 200
                    """
                ).fetchall()
            ]
            horizon_default = str(config_service.get(conn, "trade.horizon", "auto") or "auto")
        finally:
            conn.close()
        if not timeframe.is_valid(horizon_default):
            horizon_default = timeframe.DEFAULT

        # 试跑必须能跑任意标的，而且策略下拉要列出**全部**规则——
        # 早先这里写死了三条策略的标签，用户自建或后来新增的规则根本选不到。
        rules = report["rules"]
        locale = _locale(request)

        def rule_name(item: dict) -> str:
            # 规则名是数据，但同时有 i18n 词条；有词条就按语言显示，
            # 用户自建的规则没有词条，退回它自己的名字。
            key = f"rule.{item['code']}"
            translated = translate(locale, key)
            return item["name"] if translated == key else translated

        disabled_suffix = " (disabled)" if str(locale).lower().startswith("en") else "（已停用）"
        options = [(r["code"], rule_name(r)) for r in rules if r.get("enabled", 1)]
        for item in rules:
            if not item.get("enabled", 1):
                options.append((item["code"], rule_name(item) + disabled_suffix))
        context = {
            "report": report,
            "symbols": symbols,
            "strategy_options": options,
            "horizon_options": timeframe.options(locale),
            "horizon_labels": {
                item["code"]: item["label"] for item in timeframe.options(locale)
            },
            "horizon_default": horizon_default,
            "runs": backtest.recent(20),
            "result": None,
            "error": None,
            "form": {
                "symbol": (symbols[0]["market"] + "/" + symbols[0]["code"]) if symbols else "",
                "strategy": "trend_pullback",
                "horizon": horizon_default,
            },
            "engine": report.get("engine"),
        }
        # The POST handler supplies result/error/form; merging keeps the defaults
        # from colliding with them.
        context.update(extra)
        return _ctx(request=request, **context)

    @app.get("/sim", response_class=HTMLResponse)
    async def sim_page(request: Request):
        return templates.TemplateResponse(request=request, name="sim.html", context=_sim_context(request))

    @app.get("/backtest")
    async def backtest_redirect():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/sim", status_code=301)

    @app.post("/sim", response_class=HTMLResponse)
    async def sim_run(request: Request):
        from kestrel.datasources import resolve as resolver
        from kestrel.services import backtest
        from kestrel.services import bars as bars_service
        from kestrel.services import rules as rule_service
        from kestrel.services import timeframe as timeframe_service

        form = await request.form()
        raw = str(form.get("symbol", "")).strip()
        strategy = str(form.get("strategy", "")).strip()
        horizon_code = str(form.get("horizon", "short")).strip().lower()
        if not timeframe_service.is_valid(horizon_code):
            horizon_code = timeframe_service.DEFAULT
        cfg = timeframe_service.get(horizon_code)

        if not raw:
            return templates.TemplateResponse(
                request=request, name="sim.html",
                context=_sim_context(request, error="先填一个股票代码。"),
            )

        # 接受 00700 / HK/00700 / 腾讯 三种写法，任意标的都能试跑，
        # 不再要求先加自选。
        market = ""
        code = raw
        if "/" in raw:
            head, tail = raw.split("/", 1)
            if head.upper() in ("CN", "HK", "US"):
                market, code = head.upper(), tail.strip()
        found = await resolver.resolve(code, market or None)
        if found is None:
            return templates.TemplateResponse(
                request=request, name="sim.html",
                context=_sim_context(
                    request,
                    error=f"找不到「{raw}」。检查代码是否正确，或者直接写成 市场/代码，比如 US/AAPL。",
                    form={"symbol": raw, "strategy": strategy, "horizon": horizon_code},
                ),
            )
        market, code = found.market, found.code

        rules = {r["code"]: r for r in rule_service.all_rules()}
        if strategy not in rules:
            strategy = "trend_pullback"

        error = None
        result = None
        note = ""
        conn = connect()
        try:
            conn.execute(
                """
                INSERT INTO symbol (market, code, name, currency, lot_size, updated_at)
                VALUES (?,?,?,?,?,datetime('now'))
                ON CONFLICT(market, code) DO UPDATE SET
                    name=excluded.name, updated_at=datetime('now')
                """,
                (
                    market, code, found.name, found.currency, found.lot_size,
                ),
            )
            conn.commit()
            symbol_id = conn.execute(
                "SELECT id FROM symbol WHERE market=? AND code=?", (market, code)
            ).fetchone()["id"]

            daily = await bars_service.get_bars(
                conn, symbol_id, market, code, limit=cfg.lookback
            )
            # Backtest on exactly the bars the scanner would see for this
            # horizon, or the simulated result says nothing about the plan.
            bars = timeframe_service.prepare(daily, cfg.code)
            if len(bars) < cfg.min_bars:
                # 数据不足就先**去取**，而不是让用户自己猜要换哪个周期。
                deeper = await bars_service.fetch_only(market, code, limit=max(cfg.lookback, 4000))
                if deeper:
                    bars_service.save_bars(conn, symbol_id, deeper, depth=len(deeper))
                    conn.commit()
                    daily = deeper
                    bars = timeframe_service.prepare(daily, cfg.code)
                if len(bars) < cfg.min_bars:
                    error = (
                        f"这只标的只有 {len(bars)} 根 {cfg.bar} 线，至少需要 {cfg.min_bars} 根"
                        f"（约 {cfg.min_bars if cfg.bar == 'D' else cfg.min_bars} 根周期）。"
                        "已经尝试补取更长的历史，但仍然不够——通常是上市时间太短。"
                        "换短线（日线）再试，或者换一只上市更久的股票。"
                    )
                else:
                    note = "原有缓存不足，已自动补取更长历史后再试跑。"
            if error is None:
                # 用完整规则表回测：策略下拉列出的是全部规则（含用户自建的），
                # 只用内置三条会让 pullback_quiet 等规则报 "unknown strategy"。
                result = backtest.run(
                    bars, strategy, max_hold=cfg.max_hold, stop_band=cfg.stop_band,
                    registry_override=rule_service.registry(enabled_only=False),
                )
                result["timeframe"] = cfg.code
                result["bar"] = cfg.bar
                result["note"] = note
                backtest.save(market, code, strategy, result,
                              str(bars[0]["date"]), str(bars[-1]["date"]),
                              timeframe=cfg.code)
        finally:
            conn.close()

        return templates.TemplateResponse(
            request=request, name="sim.html",
            context=_sim_context(request, result=result, error=error,
                                 form={"symbol": f"{market}/{code}", "strategy": strategy,
                                       "horizon": horizon_code}),
        )

    @app.post("/api/edge", response_class=JSONResponse)
    async def api_edge(request: Request):
        from kestrel.services import advisor, edge, timeframe
        from kestrel.services import config as config_service

        try:
            body = await request.json()
        except Exception:
            body = {}
        limit = int((body or {}).get("symbols") or 120)

        # Measure the horizon actually in force, unless the caller names one.
        conn = connect()
        try:
            requested = config_service.get(conn, "trade.horizon", timeframe.AUTO)
        finally:
            conn.close()
        horizon, _advice = advisor.resolve(requested)
        horizon = str((body or {}).get("horizon") or horizon).strip().lower()
        if not timeframe.is_valid(horizon):
            horizon = timeframe.DEFAULT
        return edge.evaluate(max_symbols=limit, horizon=horizon)

    @app.get("/api/edge", response_class=JSONResponse)
    async def api_edge_get():
        from kestrel.services import edge

        return edge.cached() or {"ok": False, "error": "还没算过"}

    @app.get("/api/glossary", response_class=JSONResponse)
    async def api_glossary(request: Request):
        locale = _locale(request)
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
                status=calendar_service.status(market, _locale(request)),
            ),
        )

    @app.get("/api/market-status", response_class=JSONResponse)
    async def api_market_status(request: Request):
        from kestrel.services import calendar as calendar_service
        from kestrel.services import config as config_service

        conn = connect()
        try:
            enabled = [str(m).upper() for m in config_service.get(
                conn, "scan.markets", ["CN", "HK", "US"]
            ) or ["CN", "HK", "US"]]
        finally:
            conn.close()
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
                for s in calendar_service.all_status(enabled, _locale(request))
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

    # ---------------------------------------------------------------- analyst

    @app.post("/api/run", response_class=JSONResponse)
    async def api_run(request: Request):
        """The only way advice is produced. The engine comes from settings."""
        from kestrel.services import engine as engine_service

        try:
            body = await request.json()
        except Exception:
            body = {}
        markets = (body or {}).get("markets")
        return await engine_service.run(markets=markets)

    @app.post("/api/analyse", response_class=JSONResponse)
    async def api_analyse(request: Request):
        """Deprecated alias kept so old shortcuts keep working."""
        from kestrel.services import engine as engine_service

        try:
            body = await request.json()
        except Exception:
            body = {}
        return await engine_service.run(markets=(body or {}).get("markets"))

    @app.get("/api/engine", response_class=JSONResponse)
    async def api_engine(request: Request):
        from kestrel.services import engine as engine_service

        return {"ok": True, "engine": engine_service.status().localised(_locale(request)).as_dict()}

    @app.post("/api/analyst/ingest", response_class=JSONResponse)
    async def api_analyst_ingest():
        from kestrel.services import analyst

        return analyst.ingest()

    @app.get("/api/analyst/pending", response_class=JSONResponse)
    async def api_analyst_pending():
        from kestrel.services import bridge

        items = bridge.pending_requests("analyse")
        return {
            "ok": True,
            "count": len(items),
            "bridge": bridge.bridge_info(),
            "items": [
                {
                    "id": item.get("id"),
                    "created_at": item.get("created_at"),
                    "reply_to": item.get("reply_to"),
                    "prompt": bridge.task_prompt(item),
                    "brief": (item.get("input") or {}).get("brief"),
                }
                for item in items
            ],
        }

    @app.get("/api/analyst/runs", response_class=JSONResponse)
    async def api_analyst_runs():
        from kestrel.services import analyst

        return {"ok": True, "runs": analyst.recent_runs(10)}

    # ---------------------------------------------------------------- rules

    def _translated_rules(request: Request, rules: list[dict]) -> list[dict]:
        locale = _locale(request)
        out = []
        for item in rules:
            row = dict(item)
            key = f"rule.{row['code']}"
            translated = translate(locale, key)
            if translated != key:
                row["name"] = translated
            out.append(row)
        return out

    @app.get("/rules", response_class=HTMLResponse)
    async def rules_page(request: Request):
        from kestrel.services import engine as engine_service
        from kestrel.services import rules as rule_service
        from kestrel.services import timeframe
        from kestrel.strategies.schema import prompt_for_ai
        from kestrel.strategies.spec import FIELDS

        return templates.TemplateResponse(
            request=request,
            name="rules.html",
            context=_ctx(
                request,
                rules=_translated_rules(request, rule_service.all_rules()),
                fields=sorted(FIELDS),
                ai_prompt=prompt_for_ai(),
                engine=engine_service.status(),
                states=rule_service.timeframe_states(),
                horizon_options=timeframe.options(_locale(request)),
                horizon_labels={
                    item["code"]: item["label"]
                    for item in timeframe.options(_locale(request))
                },
            ),
        )

    @app.get("/api/rules/schema", response_class=JSONResponse)
    async def api_rules_schema():
        from kestrel.strategies.schema import schema_bundle

        return schema_bundle()

    @app.post("/api/rules/save", response_class=JSONResponse)
    async def api_rule_save(request: Request):
        from kestrel.services import rules as rule_service
        from kestrel.strategies.spec import RuleError

        body = await request.json()
        try:
            code = rule_service.save(body or {})
        except RuleError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "code": code}

    @app.post("/api/rules/{code}/toggle", response_class=JSONResponse)
    async def api_rule_toggle(code: str, request: Request):
        from kestrel.services import rules as rule_service

        try:
            body = await request.json()
        except Exception:
            body = {}
        return {"ok": rule_service.set_enabled(code, bool((body or {}).get("enabled")))}

    @app.post("/api/rules/{code}/delete", response_class=JSONResponse)
    async def api_rule_delete(code: str):
        from kestrel.services import rules as rule_service

        return {"ok": rule_service.delete(code)}

    @app.post("/api/rules/validate", response_class=JSONResponse)
    async def api_rules_validate(request: Request):
        from kestrel.services import rules as rule_service
        from kestrel.services import timeframe
        from kestrel.strategies.spec import RuleError

        try:
            body = await request.json()
        except Exception:
            body = {}
        code = str((body or {}).get("code") or "").strip()
        horizon = str((body or {}).get("horizon") or timeframe.DEFAULT).strip().lower()
        if not timeframe.is_valid(horizon):
            horizon = timeframe.DEFAULT
        try:
            if code:
                return rule_service.validate(code, horizon=horizon)
            results = rule_service.validate_all(horizon=horizon)
            saved = sum(1 for item in results if item.get("persisted"))
            skipped = len(results) - saved
            if skipped and saved:
                note = f"{skipped} 条规则样本不足，未覆盖原判定。"
            elif skipped:
                note = "全部规则样本不足，未覆盖已保存的判定。"
            else:
                note = ""
            return {
                "ok": True,
                "timeframe": horizon,
                "results": results,
                "persisted": saved,
                "skipped": skipped,
                "note": note,
            }
        except RuleError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

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
        skipped = 0
        try:
            conn.execute("BEGIN")
            for table, rows in tables.items():
                if table not in allowed or not isinstance(rows, list):
                    continue
                # 列名来自用户上传的文件，不能直接拼进 SQL。先按真实表结构过滤，
                # 否则一个带引号的键就能改写整条语句。
                known = {
                    r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for row in rows:
                    if not isinstance(row, dict) or not row:
                        skipped += 1
                        continue
                    cols = [c for c in row if c in known]
                    if not cols:
                        skipped += 1
                        continue
                    marks = ",".join("?" * len(cols))
                    try:
                        cur = conn.execute(
                            f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({marks})",
                            tuple(row[c] for c in cols),
                        )
                        # INSERT OR IGNORE 跳过重复行时 rowcount 是 0；只数真正写入的，
                        # 否则「导入 40 行」里可能一行都没进去。
                        if cur.rowcount:
                            added += cur.rowcount
                        else:
                            skipped += 1
                    except Exception:  # noqa: BLE001, PERF203
                        skipped += 1
                        continue
            conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001
            conn.execute("ROLLBACK")
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        finally:
            conn.close()
        return {"ok": True, "rows": added, "skipped": skipped}

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
        result = statement.parse_table(text)
        # note 告诉用户「认出了哪些列、跳过了几行」，这比一个行数有用得多。
        result["ok"] = True
        return result

    @app.get("/api/journal/fill/suggest", response_class=JSONResponse)
    async def api_fill_suggest(
        code: str = "", market: str = "", price: str = "", qty: str = "",
        side: str = "buy", name: str = "", horizon: str = "",
    ):
        """记账弹窗用：把用户没填的（市场、名称、手续费、止损目标）算出来。

        一次请求返回所有能自动填的东西，页面就不必让用户先知道市场再去查
        名称、再自己算手续费。算不出来的字段返回 ``null`` 和原因，而不是 0。
        """
        from kestrel.services import config as config_service
        from kestrel.services import fills as fill_service

        target = fill_service.resolve_target(code, market or None, name=name)

        conn = connect()
        try:
            broker = config_service.get(conn, f"broker.{target.market}")
        finally:
            conn.close()

        fees = None
        try:
            if price and qty:
                fees = float(fill_service.estimate_fees(
                    target.market, price, qty, broker, side
                ))
        except Exception:  # noqa: BLE001
            fees = None

        levels = {"ok": False, "reason": "先填代码和成交价"}
        if target.code and price:
            try:
                levels = fill_service.suggest_levels(
                    target.market, target.code, price, horizon or None
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("level suggestion failed: %s", exc)
                levels = {"ok": False, "reason": "行情数据不可用，先自己填一个止损"}

        return {
            "ok": True,
            "market": target.market,
            "code": target.code,
            "name": target.name,
            "currency": target.currency,
            "lot_size": target.lot_size,
            "known": target.known,
            "broker": broker,
            "fees": fees,
            "levels": levels,
        }

    @app.post("/api/journal/import", response_class=JSONResponse)
    async def api_journal_import(request: Request):
        from kestrel.services import statement

        body = await request.json()
        rows = (body or {}).get("rows") or []
        result = statement.save(rows)
        return {"ok": True, **result}

    @app.post("/api/journal/fill", response_class=JSONResponse)
    async def api_journal_fill(request: Request):
        """登记一笔真实成交。价格与数量允许和建议不同。

        市场、名称、手续费都可以不填：这里是最后一个能补上它们的地方，
        否则用户会被迫先去行情页查一遍再回来记账。
        """
        from kestrel.services import config as config_service
        from kestrel.services import execution
        from kestrel.services import fills as fill_service

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)

        raw_code = str(body.get("code") or "").strip()
        if not raw_code:
            return JSONResponse({"ok": False, "error": "填一个代码或名称"}, status_code=400)

        target = fill_service.resolve_target(
            raw_code, str(body.get("market") or "").strip().upper() or None,
            name=str(body.get("name") or ""),
        )
        if not target.code and raw_code:
            # 库里没有（还没加自选）：去解析一次。记账不该要求用户先去加自选，
            # 但解析失败时也不要瞎猜市场——那会把港股写成美股。
            from kestrel.datasources import resolve as resolver

            found = await resolver.resolve(raw_code)
            if found is not None:
                target = fill_service.ResolvedFillTarget(
                    market=found.market, code=found.code, name=found.name,
                    currency=found.currency, lot_size=found.lot_size, known=False,
                )
        if not target.code:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        f"找不到「{raw_code}」。先在行情页加自选，"
                        "或者直接填代码（例如 00700、AAPL）。"
                    ),
                },
                status_code=400,
            )

        price = body.get("price") or 0
        qty = to_qty(body.get("qty") or 0)
        fees = body.get("fees")
        if fees in (None, ""):
            conn = connect()
            try:
                broker = config_service.get(conn, f"broker.{target.market}")
            finally:
                conn.close()
            fees = fill_service.estimate_fees(
                target.market, price, qty, broker, str(body.get("side") or "buy")
            )

        try:
            fill = execution.Fill(
                market=target.market,
                code=target.code,
                side=str(body.get("side") or "buy"),
                qty=qty,
                price=str(price),
                fees=str(fees or 0),
                filled_at=str(body.get("filled_at") or "") or None,
                rec_id=int(body["rec_id"]) if body.get("rec_id") else None,
                note=str(body.get("note") or ""),
                name=target.name,
                source="manual",
                stop=body.get("stop"),
                target=body.get("target"),
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": f"数字填错了：{exc}"}, status_code=400)
        result = execution.record(fill)
        return result

    @app.post("/api/journal/{trade_id}/stop", response_class=JSONResponse)
    async def api_move_stop(trade_id: int, request: Request):
        """采纳一条移动止损建议。只能往有利方向移动。"""
        from decimal import Decimal

        from kestrel.services import exits

        try:
            body = await request.json()
        except Exception:
            body = {}
        raw = (body or {}).get("stop")
        try:
            value = Decimal(str(raw))
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "止损价填一个数字"}, status_code=400)
        result = exits.apply_stop(trade_id, value, str((body or {}).get("reason") or ""))
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return result

    @app.get("/api/journal/{trade_id}/stops", response_class=JSONResponse)
    async def api_stop_history(trade_id: int):
        from kestrel.services import exits

        return {"ok": True, "items": exits.stop_history(trade_id)}

    @app.get("/api/positions", response_class=JSONResponse)
    async def api_positions():
        from kestrel.services import positions as position_service

        items = position_service.open_positions()
        book = position_service.summary(items)
        # 先算汇总再转 float：summary 用 Decimal 做金额运算，转完就乘不动了。
        for item in items:
            for key in ("entry", "price", "cost", "fees", "stop", "target", "pnl"):
                if item.get(key) is not None:
                    item[key] = float(item[key])
        return {
            "ok": True,
            "items": items,
            "summary": {
                "count": book["count"],
                "priced": book["priced"],
                "cost_cny": float(book["cost_cny"]),
                "pnl_cny": float(book["pnl_cny"]),
                "pnl_pct": book["pnl_pct"],
                "unpriced": book["unpriced"],
            },
        }

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

    # ---------------------------------------------------------------- updates

    @app.get("/api/update", response_class=JSONResponse)
    async def api_update_status(force: bool = False):
        from kestrel.services import updates

        return await updates.check(force=force)

    @app.get("/api/about", response_class=JSONResponse)
    async def api_about():
        from kestrel.services import updates

        return {"ok": True, **updates.info()}

    @app.post("/api/update/install", response_class=JSONResponse)
    async def api_update_install():
        from kestrel.services import updates

        return await updates.install()

    return app


app = create_app()
