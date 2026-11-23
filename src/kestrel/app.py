"""FastAPI application factory."""

from __future__ import annotations

import logging
import math
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from kestrel import __version__
from kestrel.core.clock import iso
from kestrel.core.money import currency_for, from_micro, to_micro
from kestrel.datasources.base import guess_market
from kestrel.datasources.chain import QuoteChain, default_chain
from kestrel.db.engine import connect, transaction
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


def _chart_geometry(
    bars: list[dict],
    width: int = 760,
    height: int = 240,
    overlays: dict | None = None,
) -> dict:
    """将 OHLC 历史 K 线、MA20/MA60 均线及建议/持仓价位线映射到 SVG 坐标系。"""
    if not bars:
        return {"line": "", "area": "", "ma20": "", "ma60": "", "min": 0, "max": 0, "bars": [], "lines": []}
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    closes = [float(b["close"]) for b in bars]
    if overlays:
        for k in ("entry_low", "entry_high", "stop", "target1"):
            val = overlays.get(k)
            if val and val > 0:
                highs.append(float(val))
                lows.append(float(val))
    top, bottom = max(highs), min(lows)
    span = (top - bottom) or 1.0
    pad = 14
    step = (width - pad * 2) / max(1, len(bars) - 1) if len(bars) > 1 else width - pad * 2
    candle_w = round(max(2.5, min(12.0, step * 0.58)), 1)

    def y_of(value: float) -> float:
        return pad + (top - value) / span * (height - pad * 2)

    points = []
    ma20_pts = []
    ma60_pts = []
    candles = []
    for index, bar in enumerate(bars):
        x = pad + index * step
        o_val = float(bar["open"])
        c_val = float(bar["close"])
        close_y = y_of(c_val)
        open_y = y_of(o_val)
        points.append(f"{x:.1f},{close_y:.1f}")
        if index >= 19:
            m20 = sum(closes[index - 19 : index + 1]) / 20
            ma20_pts.append(f"{x:.1f},{y_of(m20):.1f}")
        if index >= 59:
            m60 = sum(closes[index - 59 : index + 1]) / 60
            ma60_pts.append(f"{x:.1f},{y_of(m60):.1f}")
        candles.append(
            {
                "x": round(x, 1),
                "date": str(bar.get("date") or ""),
                "open": round(open_y, 1),
                "close": round(close_y, 1),
                "high": round(y_of(float(bar["high"])), 1),
                "low": round(y_of(float(bar["low"])), 1),
                "body_y": round(min(open_y, close_y), 1),
                "body_h": round(max(1.5, abs(close_y - open_y)), 1),
                "up": c_val >= o_val,
            }
        )
    line = " ".join(points)
    area = f"{pad},{height - pad} {line} {pad + (len(bars) - 1) * step:.1f},{height - pad}"
    lines: list[dict] = []
    if overlays:
        for key, style in (
            ("target1", "target"),
            ("entry_high", "entry"),
            ("entry_low", "entry"),
            ("stop", "stop"),
        ):
            val = overlays.get(key)
            if val and val > 0:
                lines.append({"key": key, "value": float(val), "y": round(y_of(float(val)), 1), "kind": style})
    return {
        "line": line,
        "area": area,
        "ma20": " ".join(ma20_pts),
        "ma60": " ".join(ma60_pts),
        "min": bottom,
        "max": top,
        "width": width,
        "height": height,
        "candle_w": candle_w,
        "bars": candles,
        "lines": lines,
    }


def _src_url(source: str, market: str, code: str) -> str:
    """Where the number came from, so the user can go verify it."""
    market = (market or "").upper()
    code = str(code or "")
    source = (source or "eastmoney").lower()

    if market == "CN":
        if code.startswith(("6", "9", "5")):
            prefix = "sh"
        elif code.startswith(("8", "4", "92")):
            prefix = "bj"
        else:
            prefix = "sz"
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
    from kestrel.services import rules as rule_service

    glossary_service.seed()
    rule_service.ensure_seeded()
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

    def _normalize_locale(raw: str | None) -> str | None:
        """将任意语言标识（如 zh / zh-CN / en）规范化为受支持的语言代码。"""
        if not raw:
            return None
        val = str(raw).strip()
        if not val:
            return None
        lower = val.lower().replace("_", "-")
        if lower in ("zh", "zh-cn", "zh-hans"):
            return "zh-CN"
        if lower in ("en", "en-us", "en-gb"):
            return "en"
        if val in available_locales():
            return val
        return None

    def _set_locale_cookies(response, locale_code: str) -> None:
        """在响应上写入 kestrel_locale（并同步旧 locale Cookie 以保持兼容）。"""
        response.set_cookie(
            "kestrel_locale",
            locale_code,
            max_age=31536000,
            path="/",
            samesite="lax",
        )
        response.set_cookie(
            "locale",
            locale_code,
            max_age=31536000,
            path="/",
            samesite="lax",
        )

    def _locale(request: Request) -> str:
        """当前语言。查询参数优先于 cookie——分享链接、截图脚本、以及
        「临时看另一种语言」都需要一个不用改 cookie 的入口。"""
        query_locale = _normalize_locale(request.query_params.get("locale"))
        if query_locale:
            return query_locale
        cookie_locale = _normalize_locale(
            request.cookies.get("kestrel_locale") or request.cookies.get("locale")
        )
        return cookie_locale or "zh-CN"

    @app.middleware("http")
    async def _persist_locale_query_cookie(request: Request, call_next):
        response = await call_next(request)
        query_locale = _normalize_locale(request.query_params.get("locale"))
        if query_locale:
            _set_locale_cookies(response, query_locale)
        return response

    def _scheduler_running(request: Request | None = None) -> bool:
        """检查后台 APScheduler 盯盘调度器是否处于运行状态。"""
        if request is not None:
            sched = getattr(request.app.state, "scheduler", None)
            if sched is not None and bool(getattr(sched, "running", False)):
                return True
        try:
            from kestrel.scheduler import service as scheduler_service

            sched2 = getattr(scheduler_service, "_scheduler", None)
            return bool(sched2 is not None and getattr(sched2, "running", False))
        except Exception:  # noqa: BLE001
            return False

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
        from kestrel.desktop import tray as tray_service
        from kestrel.services import config as config_service
        from kestrel.services import copilot as copilot_service
        from kestrel.services import portfolio as portfolio_service

        locale = _locale(request)
        query_theme = request.query_params.get("theme")
        theme = query_theme or request.cookies.get("theme") or "auto"
        if theme not in ("auto", "light", "dark"):
            theme = "auto"

        conn = connect()
        try:
            ui_learning = config_service.learning_enabled(conn)
            ui_glossary = bool(config_service.get(conn, "ui.glossary", True))
            ui_privacy = bool(config_service.get(conn, "ui.privacy", False))
            close_to_tray = bool(config_service.get(conn, "desktop.close_to_tray", True))
            autostart_enabled = bool(config_service.get(conn, "desktop.autostart", False))
            portfolio_bar = portfolio_service.snapshot(conn=conn, include_quotes=True, limit_ledger=1)
        except Exception:  # noqa: BLE001
            ui_learning = False
            ui_glossary = True
            ui_privacy = False
            close_to_tray = True
            autostart_enabled = False
            portfolio_bar = {
                "initial_capital_cny": 50000.0,
                "available_cash_cny": 50000.0,
                "market_value_cny": 0.0,
                "total_equity_cny": 50000.0,
                "total_return_cny": 0.0,
                "total_return_pct": 0.0,
                "exposure_pct": 0.0,
                "open_count": 0,
            }
        finally:
            conn.close()

        desktop_state = {
            "close_to_tray": close_to_tray,
            "autostart": autostart_enabled,
            "scheduler_running": _scheduler_running(request),
            "tray_supported": tray_service.tray_supported(),
        }

        symbols_known = extra.pop("symbols_known", None)
        if symbols_known is None:
            symbols_known = _known_symbols(150)

        return {
            "t": lambda key, **kw: translate(locale, key, **kw),
            "locale": locale,
            "locales": available_locales(),
            "locale_label": locale_label,
            "theme": theme,
            "version": __version__,
            "catalog": load_catalog(locale),
            "ui_learning": ui_learning,
            "ui_glossary": ui_glossary,
            "ui_privacy": ui_privacy,
            "desktop_state": desktop_state,
            "portfolio_bar": portfolio_bar,
            "copilot_state": copilot_service.status(),
            "copilot_presets": copilot_service.PRESETS,
            "symbols_known": symbols_known,
            **extra,
        }

    @app.get("/healthz", response_class=JSONResponse)
    async def healthz(request: Request):
        chain: QuoteChain = request.app.state.chain
        return {"ok": True, "version": __version__, "sources": chain.health}

    @app.get("/welcome", response_class=HTMLResponse)
    async def welcome_page(request: Request):
        """首次启动的四步智能引导。每一项都对应真实配置并实时给出推荐理由。"""
        from kestrel.services import advisor as advisor_service
        from kestrel.services import config as config_service
        from kestrel.services import copilot as copilot_service
        from kestrel.services import timeframe

        conn = connect()
        try:
            values = config_service.all_values(conn)
            values["scan.markets"] = config_service.active_markets(conn)
            values["ui.learning"] = config_service.learning_enabled(conn)
        finally:
            conn.close()
        cap = float(values.get("account.capital") or 50000.0)
        rec = advisor_service.recommend_markets(capital_cny=cap)
        return templates.TemplateResponse(
            request=request,
            name="welcome.html",
            context=_ctx(
                request,
                values=values,
                market_rec=rec,
                horizon_options=timeframe.options(_locale(request)),
                copilot_presets=copilot_service.PRESETS,
            ),
        )

    @app.post("/api/onboarding", response_class=JSONResponse)
    async def api_onboarding(request: Request):
        from kestrel.services import config as config_service
        from kestrel.services import timeframe

        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "Invalid onboarding request"}, status_code=400)
        skip = config_service._coerce_bool(body.get("skip"))
        try:
            capital = float(body["capital"]) if "capital" in body else None
            risk = float(body["risk_pct"]) if "risk_pct" in body else None
            markets = config_service.market_list(body.get("markets"))
            horizon = str(body.get("horizon") or timeframe.AUTO).strip().lower()
            if capital is not None and (not math.isfinite(capital) or not 0 < capital < 1e12):
                raise ValueError
            if risk is not None and not 0 < risk <= 100:
                raise ValueError
            if "markets" in body and not markets or horizon not in (timeframe.AUTO, *timeframe.ORDER):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return JSONResponse({"ok": False, "error": translate(_locale(request), "welcome.settings_invalid")}, status_code=400)

        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not skip:
                if capital is not None:
                    config_service.put(conn, "account.capital", capital)
                if risk is not None:
                    config_service.put(conn, "risk.per_trade_pct", risk)
                if markets:
                    config_service.put(conn, "scan.markets", markets)
                    config_service.put(conn, "ui.markets_configured", True)
                config_service.put(conn, "trade.horizon", horizon)
                if "learning" in body:
                    config_service.put(conn, "ui.learning", body["learning"])
                brokers = body.get("brokers") or {}
                if isinstance(brokers, dict):
                    for mkt in ("CN", "HK", "US"):
                        if brokers.get(mkt):
                            config_service.put(conn, f"broker.{mkt}", str(brokers[mkt]))
                agent = body.get("agent") or {}
                if isinstance(agent, dict) and agent.get("base_url") and agent.get("model"):
                    config_service.put(conn, "agent.base_url", str(agent["base_url"]).strip().rstrip("/"))
                    config_service.put(conn, "agent.model", str(agent["model"]).strip())
                    if agent.get("api_key"):
                        config_service.put(conn, "agent.api_key", str(agent["api_key"]).strip())
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
            enabled = config_service.active_markets(conn)
        finally:
            conn.close()
        if not onboarded:
            from fastapi.responses import RedirectResponse

            query_locale = _normalize_locale(request.query_params.get("locale"))
            target_url = f"/welcome?locale={query_locale}" if query_locale else "/welcome"
            resp = RedirectResponse(target_url, status_code=303)
            if query_locale:
                _set_locale_cookies(resp, query_locale)
            return resp

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
            enabled = config_service.active_markets(conn)
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
        display_mode = _display_currency()
        return {
            "ok": True,
            "quotes": [
                {
                    "market": q.market,
                    "code": q.code,
                    "name": q.name,
                    "price": str(q.price),
                    "display_price": _quote(to_micro(q.price), q.market, display_mode),
                    "display_cny": _cny(to_micro(q.price), currency_for(q.market)),
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
        from kestrel.services import advisor as advisor_service
        from kestrel.services import analyst as analyst_service
        from kestrel.services import config as config_service
        from kestrel.services import engine as engine_service
        from kestrel.services import portfolio as portfolio_service
        from kestrel.services import scan as scan_service
        from kestrel.services import timeframe

        scan_service.expire_stale()
        try:
            analyst_service.ingest()
        except Exception as exc:  # noqa: BLE001
            log.warning("analyst ingest failed: %s", exc)

        market_filter = str(request.query_params.get("market") or "").upper().strip()
        conn = connect()
        try:
            last = config_service.get(conn, "scan.last", None)
            auto = bool(config_service.get(conn, "scan.auto_enabled", False))
            interval = int(config_service.get(conn, "scan.interval_minutes", 60) or 60)
            markets = config_service.active_markets(conn)
            markets_configured = config_service.markets_configured(conn)
            universe = str(config_service.get(conn, "scan.universe", "both"))
            requested = str(config_service.get(conn, "trade.horizon", "auto") or "auto")
            ui = {
                "learning": config_service.learning_enabled(conn),
                "glossary": bool(config_service.get(conn, "ui.glossary", True)),
            }
            snap = portfolio_service.snapshot(conn=conn, include_quotes=True, limit_ledger=1)
        finally:
            conn.close()

        market_filter = market_filter if market_filter in markets else "ALL"
        filter_markets = [market_filter] if market_filter != "ALL" else markets
        recs = scan_service.active_recommendations(50, markets=filter_markets)
        try:
            advice = advisor_service.evaluate()
        except Exception as exc:  # noqa: BLE001
            log.warning("advisor failed on page load: %s", exc)
            advice = {}
        market_rec = advisor_service.recommend_markets(
            capital_cny=snap["total_equity_cny"] if markets_configured else snap["initial_capital_cny"],
            configured=markets_configured,
            enabled=markets,
            horizon_report=advice,
        )

        pinned = timeframe.is_valid(requested)
        in_force = requested if pinned else (advice or {}).get("horizon") or timeframe.DEFAULT
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
                market_filter=market_filter or "ALL",
                markets_configured=markets_configured,
                market_rec=market_rec,
                universe=universe,
                history=scan_service.history(20, markets=filter_markets),
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

    def _confirmed_fill(conn, body: dict, *, market: str, code: str, name: str,
                        rec_id: int | None = None, source: str = "manual") -> dict:
        from kestrel.services import config as config_service
        from kestrel.services import execution
        from kestrel.services import fills as fill_service

        side = str(body.get("side") or "buy").strip().lower()
        try:
            price = Decimal(str(body.get("price") or ""))
            qty = Decimal(str(body.get("qty") or ""))
            fees = body.get("fees")
            if fees in (None, "") and all(
                v.is_finite() and 0 < v < Decimal("1e12") for v in (price, qty)
            ):
                broker = config_service.get(conn, f"broker.{market}")
                fees = fill_service.estimate_fees(market, price, qty, broker, side)
            return execution.record(execution.Fill(
                market=market, code=code, name=name, side=side, price=price, qty=qty,
                fees=fees or 0, rec_id=rec_id, source=source,
                filled_at=str(body.get("filled_at") or "") or None,
                trade_id=int(body["trade_id"]) if body.get("trade_id") else None,
                request_id=str(body.get("request_id") or "") or None,
                note=str(body.get("note") or ""), stop=body.get("stop"), target=body.get("target"),
                exit_reason=str(body.get("exit_reason") or ""),
                rule_break=config_service._coerce_bool(body.get("rule_break")),
                broker=config_service.get(conn, f"broker.{market}"),
                confirmed=rec_id is not None or config_service._coerce_bool(body.get("confirmed")),
            ), conn=conn)
        except (TypeError, ValueError, ArithmeticError):
            return {"ok": False, "error": "请输入券商显示的实际成交价格和数量，并检查费用、日期及持仓编号。"}

    @app.post("/advice/{rec_id}/decide", response_class=JSONResponse)
    async def advice_decide(rec_id: int, request: Request):
        from kestrel.services import scan as scan_service

        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)
        decision = body.get("decision")
        if decision != "executed":
            return {"ok": scan_service.decide(rec_id, decision, str(body.get("note") or ""))}

        conn = connect()
        try:
            with transaction(conn, immediate=True):
                rec = conn.execute("SELECT market, code, name FROM recommendation WHERE id=?", (rec_id,)).fetchone()
                if rec is None:
                    return {"ok": False, "error": "这条建议不存在"}
                result = _confirmed_fill(
                    conn, {**body, "price": body.get("fill_price"), "side": "buy"},
                    market=rec["market"], code=rec["code"], name=rec["name"],
                    rec_id=rec_id, source="advice",
                )
                return {"ok": True, "trade": result} if result["ok"] else result
        finally:
            conn.close()

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

    @app.post("/api/advice/scan", response_class=JSONResponse)
    async def api_advice_scan(request: Request):
        """托盘与快捷入口触发的市场扫描接口（委托统一建议引擎）。"""
        from kestrel.services import engine as engine_service

        try:
            body = await request.json()
        except Exception:
            body = {}
        markets = body.get("markets") if isinstance(body, dict) else None
        return await engine_service.run(markets=markets)

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
                         expired=expired, receipt_request_id=uuid.uuid4().hex),
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
        form = await request.form()
        action = str(form.get("action") or "")
        note = str(form.get("note") or "").strip()
        request_id = str(form.get("request_id") or "").strip()
        error = None
        result = None
        done = None
        expired = False
        conn = connect()
        try:
            with transaction(conn, immediate=True):
                row = conn.execute(
                    """
                    SELECT r.*, t.code AS token_code, t.expires_at AS token_expires,
                           t.uses, t.max_uses
                    FROM receipt_token t JOIN recommendation r ON r.id = t.rec_id
                    WHERE t.code = ?
                    """,
                    (code,),
                ).fetchone()
                rec = dict(row) if row else None
                if rec:
                    expired = _token_expired(rec)
                receipt_request = f"receipt:{code}:{request_id}" if request_id else None
                existing = conn.execute(
                    "SELECT action FROM receipt WHERE request_id=?",
                    (receipt_request,),
                ).fetchone() if receipt_request and rec else None
                if existing:
                    done = existing["action"]
                elif rec and not expired:
                    if action in ("executed", "partial"):
                        result = _confirmed_fill(
                            conn, {
                                "price": form.get("fill_price"), "qty": form.get("fill_qty"),
                                "fees": form.get("fees"), "filled_at": form.get("filled_at"),
                                "request_id": receipt_request, "note": note,
                            },
                            market=rec["market"], code=rec["code"], name=rec["name"],
                            rec_id=rec["id"], source="link",
                        )
                        error = result.get("error") if not result["ok"] else None
                    elif action == "skipped" and rec["status"] == "active":
                        conn.execute(
                            "UPDATE recommendation SET status='skipped', decided_at=?, decided_note=? WHERE id=?",
                            (datetime.now(UTC).isoformat(timespec="seconds"), note, rec["id"]),
                        )
                    else:
                        error = translate(_locale(request), "receipt.action_invalid")

                    if not error:
                        actual = bool(result and result["ok"])
                        conn.execute(
                            """
                            INSERT INTO receipt
                                (rec_id, token_code, action, fill_price, fill_qty, filled_at,
                                 deviation_pct, deviation_r, note, source, request_id)
                            VALUES (?,?,?,?,?,?,?,?,?, 'link', ?)
                            """,
                            (
                                rec["id"], code, action,
                                to_micro(form["fill_price"]) if actual else None,
                                result["qty"] if actual else None,
                                result["filled_at"] if actual else datetime.now(UTC).isoformat(timespec="seconds"),
                                result.get("deviation_pct") if actual else None,
                                result.get("deviation_r") if actual else None,
                                note or None, receipt_request,
                            ),
                        )
                        conn.execute("UPDATE receipt_token SET uses=uses+1 WHERE code=?", (code,))
                        done = action
        finally:
            conn.close()

        return templates.TemplateResponse(
            request=request, name="receipt.html",
            context=_ctx(request, rec=rec, code=code, existing=None, done=done,
                         result=result, expired=expired, error=error,
                         receipt_form=form, receipt_request_id=request_id or uuid.uuid4().hex),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        from kestrel.datasources import presets as preset_service
        from kestrel.datasources import registry as source_registry
        from kestrel.fees.calculator import PLANS
        from kestrel.services import channels as channel_service
        from kestrel.services import config as config_service
        from kestrel.services import glossary as glossary_service
        from kestrel.services import prompts as prompt_service
        from kestrel.services import timeframe
        from kestrel.services import universe as universe_service
        from kestrel.services import updates as updates_service

        locale = _locale(request)
        conn = connect()
        try:
            values = config_service.all_values(conn)
            values["scan.markets"] = config_service.active_markets(conn)
            values["ui.learning"] = config_service.learning_enabled(conn)
        finally:
            conn.close()
        broker_options = {
            mkt: [
                {"key": k, "name": p.name, "name_en": getattr(p, "name_en", p.name) or p.name}
                for k, p in PLANS.items()
                if p.market == mkt
            ]
            for mkt in ("CN", "HK", "US")
        }
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context=_ctx(
                request,
                values=values,
                glossary=glossary_service.list_all(locale),
                glossary_categories=glossary_service.CATEGORIES,
                sources=source_registry.list_all(locale),
                presets=preset_service.PRESETS,
                channels=channel_service.all_channels(locale),
                broker_options=broker_options,
                pool=universe_service.pool_summary(),
                horizon_options=timeframe.options(locale),
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

        from kestrel.desktop import autostart as autostart_service
        from kestrel.services import config as config_service
        from kestrel.services import settings_schema

        def _form_bool(form_data, *names: str) -> bool:
            for name in names:
                if name in form_data:
                    return config_service._coerce_bool(form_data.get(name), True)
            return False

        form = await request.form()
        if section == "desktop":
            close_val = _form_bool(form, "desktop.close_to_tray", "close_to_tray")
            auto_val = _form_bool(form, "desktop.autostart", "autostart")
            priv_val = _form_bool(form, "ui.privacy", "ui_privacy", "privacy")
            conn = connect()
            try:
                config_service.put(conn, "desktop.close_to_tray", close_val)
                config_service.put(conn, "desktop.autostart", auto_val)
                config_service.put(conn, "ui.privacy", priv_val)
                conn.execute(
                    "INSERT INTO audit_log (action, target) VALUES ('settings.save', ?)",
                    ("desktop",),
                )
                conn.commit()
            finally:
                conn.close()
            autostart_service.sync_autostart(auto_val)
            return RedirectResponse("/settings?saved=desktop#desktop", status_code=303)

        target = settings_schema.SECTION_MAP.get(section)
        if target is not None:
            conn = connect()
            sync_auto: bool | None = None
            try:
                for field_def in target.fields:
                    value = (
                        field_def.key in form
                        if field_def.kind == "switch"
                        else settings_schema.coerce(field_def, str(form.get(field_def.key, "")))
                    )
                    config_service.put(conn, field_def.key, value)
                # 兼容在通用设置表单中同时携带桌面/隐私字段
                for extra_key, aliases in (
                    ("desktop.close_to_tray", ("desktop.close_to_tray", "close_to_tray")),
                    ("desktop.autostart", ("desktop.autostart", "autostart")),
                    ("ui.privacy", ("ui.privacy", "ui_privacy", "privacy")),
                ):
                    if any(a in form for a in aliases):
                        bval = _form_bool(form, *aliases)
                        config_service.put(conn, extra_key, bval)
                        if extra_key == "desktop.autostart":
                            sync_auto = bval
                conn.execute(
                    "INSERT INTO audit_log (action, target) VALUES ('settings.save', ?)",
                    (section,),
                )
                conn.commit()
            finally:
                conn.close()
            if sync_auto is not None:
                autostart_service.sync_autostart(sync_auto)
            if section in ("scan", "trading", "strategy"):
                try:
                    from kestrel.scheduler import service as scheduler_service

                    scheduler_service.reschedule()
                except Exception:  # noqa: BLE001
                    pass
        return RedirectResponse(f"/settings?saved={section}", status_code=303)

    @app.get("/journal", response_class=HTMLResponse)
    async def journal_page(request: Request):
        from kestrel.services import execution, journal
        from kestrel.services import portfolio as portfolio_service
        from kestrel.services import positions as position_service

        open_positions = position_service.open_positions()
        quality = execution.quality_report()
        known = _known_symbols()
        snap = portfolio_service.snapshot(include_quotes=True, limit_ledger=15)
        curve = journal.equity_curve()
        return templates.TemplateResponse(
            request=request, name="journal.html",
            context=_ctx(request, open_trades=journal.list_trades("open"),
                         closed=journal.list_trades("closed"), stats=journal.stats(),
                         positions=open_positions,
                         symbols_known=known,
                         portfolio_snap=snap,
                         equity_curve=curve,
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
            qty=str(form.get("qty") or 0),
            price=str(form.get("entry_price") or 0),
            fees=str(form.get("fees") or 0),
            filled_at=str(form.get("filled_at") or "") or None,
            note=str(form.get("note") or ""),
            name=str(form.get("name") or "").strip(),
            source="manual",
            confirmed=True,
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
        fees_raw = str(form.get("fees") or "").strip()
        result = journal.close_trade(
            trade_id,
            str(form.get("exit_price") or 0),
            str(form.get("review") or ""),
            bool(form.get("rule_break")),
            qty=qty_raw or None,
            exit_reason=str(form.get("exit_reason") or ""),
            fees_override=fees_raw if fees_raw != "" else None,
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
            "permutation": None,
            "plateau": None,
            "profit_factor": None,
            "bar_sharpe": None,
            "error": None,
            "form": {
                "symbol": (symbols[0]["market"] + "/" + symbols[0]["code"]) if symbols else "",
                "strategy": "trend_pullback",
                "horizon": horizon_default,
                "fee_bps": 20.0,
                "fee_pct": 0.2,
                "slippage_bps": 10.0,
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

        def _parse_float(raw_val, default_val: float, lo: float = 0.0, hi: float = 500.0) -> float:
            if raw_val in (None, ""):
                return default_val
            try:
                return max(lo, min(hi, float(raw_val)))
            except (TypeError, ValueError):
                return default_val

        # 同时兼容 fee_bps（基点，默认 20 bps = 0.20%）、fee_pct（百分比 0.2）与 fee_drag_pct（小数 0.002）
        if form.get("fee_drag_pct") not in (None, ""):
            fee_drag_pct = _parse_float(form.get("fee_drag_pct"), 0.002, 0.0, 0.05)
            fee_bps = round(fee_drag_pct * 10_000.0, 2)
        elif form.get("fee_pct") not in (None, "") and form.get("fee_bps") in (None, ""):
            fee_pct_val = _parse_float(form.get("fee_pct"), 0.2, 0.0, 5.0)
            fee_drag_pct = fee_pct_val / 100.0
            fee_bps = round(fee_drag_pct * 10_000.0, 2)
        else:
            fee_bps = _parse_float(form.get("fee_bps"), 20.0, 0.0, 500.0)
            fee_drag_pct = fee_bps / 10_000.0

        slippage_bps = _parse_float(
            form.get("slippage_bps") if form.get("slippage_bps") not in (None, "") else form.get("slippage"),
            10.0,
            0.0,
            500.0,
        )
        form_echo = {
            "symbol": raw,
            "strategy": strategy or "trend_pullback",
            "horizon": horizon_code,
            "fee_bps": fee_bps,
            "fee_pct": round(fee_bps / 100.0, 3),
            "slippage_bps": slippage_bps,
        }

        if not raw:
            return templates.TemplateResponse(
                request=request,
                name="sim.html",
                context=_sim_context(request, error="先填一个股票代码。", form=form_echo),
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
                request=request,
                name="sim.html",
                context=_sim_context(
                    request,
                    error=f"找不到「{raw}」。检查代码是否正确，或者直接写成 市场/代码，比如 US/AAPL。",
                    form=form_echo,
                ),
            )
        market, code = found.market, found.code

        rules = {r["code"]: r for r in rule_service.all_rules()}
        if strategy not in rules:
            strategy = "trend_pullback"
        form_echo["symbol"] = f"{market}/{code}"
        form_echo["strategy"] = strategy

        error = None
        result = None
        perm_report = None
        plateau_report = None
        profit_factor = None
        bar_sharpe = None
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
                all_reg = rule_service.registry(enabled_only=False)
                result = backtest.run(
                    bars,
                    strategy,
                    max_hold=cfg.max_hold,
                    stop_band=cfg.stop_band,
                    fee_drag_pct=fee_drag_pct,
                    next_open=True,
                    slippage_bps=slippage_bps,
                    registry_override=all_reg,
                )
                perm_report = backtest.permutation_test(
                    bars,
                    strategy,
                    n_perms=500,
                    seed=42,
                    max_hold=cfg.max_hold,
                    stop_band=cfg.stop_band,
                    fee_drag_pct=fee_drag_pct,
                    slippage_bps=slippage_bps,
                    registry_override=all_reg,
                    precomputed_result=result,
                )
                plateau_report = backtest.parameter_plateau(
                    bars,
                    strategy,
                    base_hold=cfg.max_hold,
                    fee_drag_pct=fee_drag_pct,
                    slippage_bps=slippage_bps,
                    stop_band=cfg.stop_band,
                    registry_override=all_reg,
                    next_open=True,
                )
                profit_factor = result["stats"]["profit_factor"]
                bar_sharpe = result["stats"]["bar_sharpe"]
                result["timeframe"] = cfg.code
                result["bar"] = cfg.bar
                result["note"] = note
                result["fee_bps"] = fee_bps
                result["slippage_bps"] = slippage_bps
                result["permutation"] = perm_report
                result["plateau"] = plateau_report
                result["profit_factor"] = profit_factor
                result["bar_sharpe"] = bar_sharpe
                backtest.save(
                    market,
                    code,
                    strategy,
                    result,
                    str(bars[0]["date"]),
                    str(bars[-1]["date"]),
                    timeframe=cfg.code,
                )
        finally:
            conn.close()

        return templates.TemplateResponse(
            request=request,
            name="sim.html",
            context=_sim_context(
                request,
                result=result,
                permutation=perm_report,
                plateau=plateau_report,
                profit_factor=profit_factor,
                bar_sharpe=bar_sharpe,
                error=error,
                form=form_echo,
            ),
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
    async def api_glossary(request: Request, force: bool = False):
        from kestrel.services import glossary as glossary_service

        return glossary_service.payload_for_ui(_locale(request), force=force)

    @app.post("/glossary/{key}/toggle")
    async def glossary_toggle(key: str, request: Request):
        from fastapi.responses import RedirectResponse

        conn = connect()
        try:
            conn.execute("UPDATE glossary SET suppressed = 1 - suppressed WHERE key=?", (key,))
            conn.commit()
        finally:
            conn.close()
        accept = request.headers.get("accept", "")
        if "application/json" in accept or request.query_params.get("ajax"):
            return JSONResponse({"ok": True, "key": key})
        return RedirectResponse("/settings#glossary", status_code=303)

    # ---------------------------------------------------------------- stock

    async def _render_stock_page(request: Request, market: str, code: str) -> HTMLResponse:
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
        overlays = None
        if rec:
            overlays = {
                "entry_low": float(from_micro(rec["entry_low"])),
                "entry_high": float(from_micro(rec["entry_high"] or rec["entry_low"])),
                "stop": float(from_micro(rec["stop"])),
                "target1": float(from_micro(rec["target1"])),
            }
        elif trade:
            overlays = {
                "entry_low": float(from_micro(trade["entry_price"])),
                "stop": float(from_micro(trade["stop_price"])) if trade["stop_price"] else None,
                "target1": float(from_micro(trade["target_price"])) if trade["target_price"] else None,
            }
        chart = _chart_geometry(bars[-60:], overlays=overlays)
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

    @app.get("/stock/{code}", response_class=HTMLResponse)
    async def stock_quick_page(request: Request, code: str):
        """支持顶栏快速代码搜索直达 /stock/<code>（自动匹配已存标的或推断市场）。"""
        raw = code.strip()
        if ":" in raw:
            head, tail = raw.split(":", 1)
            if head.upper() in ("CN", "HK", "US") and tail.strip():
                return await _render_stock_page(request, head.upper(), tail.strip())
        conn = connect()
        try:
            row = conn.execute(
                "SELECT market, code FROM symbol WHERE UPPER(code)=UPPER(?) ORDER BY is_active DESC, id ASC LIMIT 1",
                (raw,),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return await _render_stock_page(request, row["market"], row["code"])
        return await _render_stock_page(request, guess_market(raw), raw.upper() if raw.isalpha() else raw)

    @app.get("/stock/{market}/{code}", response_class=HTMLResponse)
    async def stock_page(request: Request, market: str, code: str):
        return await _render_stock_page(request, market, code)

    @app.get("/api/market-status", response_class=JSONResponse)
    async def api_market_status(request: Request):
        from kestrel.services import calendar as calendar_service
        from kestrel.services import config as config_service

        conn = connect()
        try:
            enabled = config_service.active_markets(conn)
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
            markets = config_service.market_list((body or {}).get("markets")) or config_service.active_markets(conn)
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

        translated = _translated_rules(request, rule_service.all_rules())
        drift_map = rule_service.live_drift_report()
        return templates.TemplateResponse(
            request=request,
            name="rules.html",
            context=_ctx(
                request,
                rules=translated,
                drift_map=drift_map,
                falsification_gates=rule_service.GATE_DEFINITIONS,
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

    @app.get("/api/rules/drift", response_class=JSONResponse)
    async def api_rules_drift():
        """返回各策略实盘已平仓与推荐纸面样本相对回测期望的偏离监控报告。"""
        from kestrel.services import rules as rule_service

        return {"ok": True, "drift": rule_service.live_drift_report()}

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
        from starlette.background import BackgroundTask

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
            snapshot,
            filename=f"kestrel-backup-{stamp}.db",
            media_type="application/octet-stream",
            background=BackgroundTask(lambda p: os.path.exists(p) and os.unlink(p), snapshot),
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
        result = statement.parse_table(text, locale=_locale(request))
        result["ok"] = True
        return result

    @app.get("/api/journal/fill/suggest", response_class=JSONResponse)
    async def api_fill_suggest(
        request: Request,
        code: str = "", market: str = "", price: str = "", qty: str = "",
        side: str = "buy", name: str = "", horizon: str = "",
    ):
        """记账弹窗用：把市场、名称、手续费、持仓可卖股数、止损目标一次算齐。"""
        from kestrel.services import config as config_service
        from kestrel.services import execution
        from kestrel.services import fills as fill_service

        locale = _locale(request)
        en = locale.lower().startswith("en")
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

        levels = {"ok": False, "reason": "Enter ticker and price first." if en else "先填代码和成交价"}
        if target.code and price:
            try:
                levels = fill_service.suggest_levels(
                    target.market, target.code, price, horizon or None, locale=locale
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("level suggestion failed: %s", exc)
                levels = {"ok": False, "reason": "No cached bars; enter your own stop." if en else "行情数据不可用，先自己填一个止损"}

        held = float(execution.held_qty(target.market, target.code)) if target.code else 0.0
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
            "held_qty": held,
            "levels": levels,
        }

    @app.post("/api/journal/import", response_class=JSONResponse)
    async def api_journal_import(request: Request):
        from kestrel.services import statement

        body = await request.json()
        rows = (body or {}).get("rows") or []
        result = statement.save(rows, locale=_locale(request), request_id=str((body or {}).get("request_id") or ""))
        return {"ok": True, **result}

    @app.post("/api/journal/fill", response_class=JSONResponse)
    async def api_journal_fill(request: Request):
        """登记一笔真实成交。价格与数量允许和建议不同。

        市场、名称、手续费都可以不填：这里是最后一个能补上它们的地方，
        否则用户会被迫先去行情页查一遍再回来记账。
        """
        from kestrel.services import fills as fill_service

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "请求格式不对"}, status_code=400)

        raw_code = str(body.get("code") or "").strip()
        if not raw_code:
            return JSONResponse({"ok": False, "error": "填一个代码或名称"}, status_code=400)

        target = fill_service.resolve_target(
            raw_code, str(body.get("market") or "").strip().upper() or None,
            name=str(body.get("name") or ""),
        )
        asked_market = str(body.get("market") or "").strip().upper()
        if not target.code and not asked_market and "/" not in raw_code:
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

        try:
            rec_id = int(body["rec_id"]) if body.get("rec_id") else None
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "建议编号无效"}, status_code=400)
        conn = connect()
        try:
            with transaction(conn, immediate=True):
                return _confirmed_fill(
                    conn, body, market=target.market, code=target.code, name=target.name,
                    rec_id=rec_id,
                )
        finally:
            conn.close()

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
        from kestrel.desktop import autostart as autostart_service
        from kestrel.services import config as config_service

        body = await request.json()
        items = (body or {}).get("items") or {}
        if not isinstance(items, dict):
            return JSONResponse({"ok": False, "error": "items must be an object"}, status_code=400)
        from kestrel.services import timeframe

        if "trade.horizon" in items and items["trade.horizon"] not in (timeframe.AUTO, *timeframe.ORDER):
            return JSONResponse({"ok": False, "error": translate(_locale(request), "welcome.settings_invalid")}, status_code=400)
        if "account.capital" in items:
            try:
                capital = float(items["account.capital"])
                if not math.isfinite(capital) or not 0 < capital < 1e12:
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                return JSONResponse({"ok": False, "error": translate(_locale(request), "welcome.settings_invalid")}, status_code=400)
            items["account.capital"] = capital
        if "scan.markets" in items:
            mkts = config_service.market_list(items["scan.markets"])
            if not mkts:
                return JSONResponse(
                    {"ok": False, "error": "至少需要保留一个交易市场"},
                    status_code=400,
                )
            items["scan.markets"] = mkts
            items["ui.markets_configured"] = True
        conn = connect()
        try:
            with transaction(conn, immediate=True):
                for key, value in items.items():
                    config_service.put(conn, str(key), value)
        finally:
            conn.close()
        if "desktop.autostart" in items:
            autostart_service.sync_autostart(config_service._coerce_bool(items["desktop.autostart"]))
        if any(str(k).startswith("scan.") for k in items):
            try:
                from kestrel.scheduler import service as scheduler_service

                scheduler_service.reschedule()
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "saved": len(items)}

    # ---------------------------------------------------------------- v0.4.0 portfolio, learning, copilot

    @app.get("/api/portfolio", response_class=JSONResponse)
    async def api_portfolio():
        from kestrel.services import portfolio as portfolio_service

        return {"ok": True, **portfolio_service.snapshot(include_quotes=True, limit_ledger=20)}

    @app.post("/api/portfolio/calibrate-cash", response_class=JSONResponse)
    async def api_portfolio_calibrate_cash(request: Request):
        from kestrel.services import portfolio as portfolio_service

        body = await request.json()
        res = portfolio_service.calibrate_cash(
            target_available_cny=(body or {}).get("target_available_cny"),
            target_equity_cny=(body or {}).get("target_equity_cny"),
            delta_cny=(body or {}).get("delta_cny"),
            initial_capital_cny=(body or {}).get("initial_capital_cny"),
            kind=str((body or {}).get("kind") or "calibration"),
            note=str((body or {}).get("note") or ""),
        )
        if not res.get("ok"):
            return JSONResponse(res, status_code=400)
        return res

    @app.post("/api/portfolio/calibrate-position", response_class=JSONResponse)
    async def api_portfolio_calibrate_position(request: Request):
        from kestrel.services import portfolio as portfolio_service

        body = await request.json()
        res = portfolio_service.calibrate_position(
            trade_id=int(body["trade_id"]) if (body or {}).get("trade_id") else None,
            market=str((body or {}).get("market") or ""),
            code=str((body or {}).get("code") or ""),
            name=str((body or {}).get("name") or ""),
            qty=(body or {}).get("qty") or 0,
            entry_price=(body or {}).get("entry_price") or 0,
            stop_price=(body or {}).get("stop_price"),
            target_price=(body or {}).get("target_price"),
            affect_cash=bool((body or {}).get("affect_cash")),
            note=str((body or {}).get("note") or ""),
        )
        if not res.get("ok"):
            return JSONResponse(res, status_code=400)
        return res

    @app.post("/api/ui/learning", response_class=JSONResponse)
    async def api_ui_learning(request: Request):
        from kestrel.services import config as config_service

        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = config_service._coerce_bool((body or {}).get("enabled", True))
        conn = connect()
        try:
            config_service.put(conn, "ui.learning", enabled)
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "enabled": enabled}

    @app.post("/api/locale", response_class=JSONResponse)
    async def api_locale_switch(request: Request):
        """切换界面语言并持久化到 Cookie 与配置库（供顶栏双语胶囊调用）。"""
        from kestrel.services import config as config_service

        try:
            body = await request.json()
        except Exception:
            body = {}
        raw = (body or {}).get("locale") if isinstance(body, dict) else None
        chosen = _normalize_locale(raw) if raw else None
        if chosen is None:
            current = _locale(request)
            chosen = "en" if current != "en" else "zh-CN"

        conn = connect()
        try:
            config_service.put(conn, "ui.locale", chosen)
            conn.commit()
        finally:
            conn.close()

        resp = JSONResponse({"ok": True, "locale": chosen})
        _set_locale_cookies(resp, chosen)
        return resp

    @app.post("/api/ui/privacy", response_class=JSONResponse)
    async def api_ui_privacy(request: Request):
        """开启/关闭具体金额隐私打码（若未传 enabled 则对当前状态取反）。"""
        from kestrel.services import config as config_service

        try:
            body = await request.json()
        except Exception:
            body = {}

        conn = connect()
        try:
            current = bool(config_service.get(conn, "ui.privacy", False))
            if isinstance(body, dict) and "enabled" in body and body["enabled"] is not None:
                enabled = config_service._coerce_bool(body["enabled"], current)
            else:
                enabled = not current
            config_service.put(conn, "ui.privacy", enabled)
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "enabled": enabled}

    def _build_desktop_status(request: Request) -> dict:
        """汇总当前桌面端后台常驻、自启与调度器运行状态。"""
        from kestrel.desktop import autostart as autostart_service
        from kestrel.desktop import tray as tray_service
        from kestrel.services import config as config_service

        conn = connect()
        try:
            close_to_tray = bool(config_service.get(conn, "desktop.close_to_tray", True))
            autostart_enabled = bool(config_service.get(conn, "desktop.autostart", False))
            privacy_enabled = bool(config_service.get(conn, "ui.privacy", False))
        finally:
            conn.close()

        return {
            "ok": True,
            "close_to_tray": close_to_tray,
            "autostart": autostart_enabled,
            "privacy": privacy_enabled,
            "scheduler_running": _scheduler_running(request),
            "tray_supported": tray_service.tray_supported(),
            "autostart_installed": autostart_service.is_installed(),
        }

    @app.get("/api/desktop/status", response_class=JSONResponse)
    async def api_desktop_status(request: Request):
        """读取桌面常驻、托盘支持与开机自启实时状态。"""
        status_payload = _build_desktop_status(request)
        status_payload.pop("privacy", None)
        return status_payload

    @app.post("/api/desktop/settings", response_class=JSONResponse)
    async def api_desktop_settings(request: Request):
        """部分或全量更新桌面常驻与隐私配置，并在修改 autostart 时同步系统启动项。"""
        from kestrel.desktop import autostart as autostart_service
        from kestrel.services import config as config_service

        data: dict = {}
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            form = await request.form()
            data = dict(form)
        else:
            try:
                raw_body = await request.json()
                if isinstance(raw_body, dict):
                    data = raw_body
            except Exception:
                data = {}

        has_close = any(k in data for k in ("close_to_tray", "desktop.close_to_tray"))
        has_auto = any(k in data for k in ("autostart", "desktop.autostart"))
        has_priv = any(k in data for k in ("privacy", "ui_privacy", "ui.privacy"))

        sync_auto_val: bool | None = None
        conn = connect()
        try:
            if has_close:
                raw_close = (
                    data["close_to_tray"] if "close_to_tray" in data else data["desktop.close_to_tray"]
                )
                config_service.put(
                    conn, "desktop.close_to_tray", config_service._coerce_bool(raw_close, True)
                )
            if has_auto:
                raw_auto = data["autostart"] if "autostart" in data else data["desktop.autostart"]
                sync_auto_val = config_service._coerce_bool(raw_auto, False)
                config_service.put(conn, "desktop.autostart", sync_auto_val)
            if has_priv:
                raw_priv = (
                    data["privacy"]
                    if "privacy" in data
                    else data.get("ui_privacy", data.get("ui.privacy"))
                )
                config_service.put(
                    conn, "ui.privacy", config_service._coerce_bool(raw_priv, False)
                )
            conn.commit()
        finally:
            conn.close()

        if sync_auto_val is not None:
            autostart_service.sync_autostart(sync_auto_val)

        return _build_desktop_status(request)

    @app.get("/api/advisor/recommend", response_class=JSONResponse)
    async def api_advisor_recommend(request: Request, capital: float | None = None, markets: str = ""):
        from kestrel.services import advisor as advisor_service
        from kestrel.services import config as config_service

        if capital is not None and (not math.isfinite(capital) or not 0 < capital < 1e12):
            return JSONResponse({"ok": False, "error": translate(_locale(request), "welcome.settings_invalid")}, status_code=400)
        enabled = config_service.market_list(markets.split(","))
        rec = advisor_service.recommend_markets(
            capital_cny=capital, enabled=enabled or None, configured=True if enabled else None,
        )
        return {"ok": True, "recommendation": rec, "advisor": advisor_service.cached() or {}}

    @app.post("/api/journal/fill/preview", response_class=JSONResponse)
    async def api_journal_fill_preview(request: Request):
        from kestrel.services import execution

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
        from kestrel.services.fills import resolve_target

        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
        target = resolve_target(str(body.get("code") or ""), str(body.get("market") or ""))
        if not target.code:
            return {"ok": False}
        try:
            return execution.preview_fill_vs_plan(
                market=target.market,
                code=target.code,
                side=str(body.get("side") or "buy"),
                qty=body.get("qty") or 0,
                price=body.get("price") or 0,
                fees=body.get("fees"),
                stop=body.get("stop"),
                target=body.get("target"),
                rec_id=int(body["rec_id"]) if body.get("rec_id") else None,
                trade_id=int(body["trade_id"]) if body.get("trade_id") else None,
                confirmed=body.get("confirmed") is True,
                locale=_locale(request),
            )
        except (ArithmeticError, TypeError, ValueError):
            return {"ok": False}

    @app.get("/api/copilot/status", response_class=JSONResponse)
    async def api_copilot_status():
        from kestrel.services import copilot as copilot_service

        return {"ok": True, **copilot_service.status()}

    @app.post("/api/copilot/config", response_class=JSONResponse)
    async def api_copilot_config(request: Request):
        from kestrel.services import copilot as copilot_service

        body = await request.json()
        data = body or {}
        res = copilot_service.quick_config(
            base_url=str(data.get("base_url") or ""),
            model=str(data.get("model") or ""),
            api_key=data.get("api_key"),
            set_engine_llm=bool(data.get("set_engine_llm")),
        )
        if not res.get("ok"):
            return JSONResponse(res, status_code=400)
        return res

    @app.get("/api/copilot/history", response_class=JSONResponse)
    async def api_copilot_history(session_id: str = "default"):
        from kestrel.services import copilot as copilot_service

        return {"ok": True, "messages": copilot_service.history(session_id=session_id)}

    @app.post("/api/copilot/clear", response_class=JSONResponse)
    async def api_copilot_clear(request: Request):
        from kestrel.services import copilot as copilot_service

        try:
            body = await request.json()
        except Exception:
            body = {}
        copilot_service.clear_history(session_id=str((body or {}).get("session_id") or "default"))
        return {"ok": True}

    @app.post("/api/copilot/chat", response_class=JSONResponse)
    async def api_copilot_chat(request: Request):
        from kestrel.services import copilot as copilot_service

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
        data = body or {}
        locale = _locale(request)
        res = await anyio.to_thread.run_sync(
            lambda: copilot_service.chat(
                str(data.get("message") or ""),
                page_path=str(data.get("page_path") or ""),
                symbol_hint=str(data.get("symbol_hint") or ""),
                session_id=str(data.get("session_id") or "default"),
                locale=locale,
            )
        )
        if not res.get("ok") and res.get("code") == "not_configured":
            return JSONResponse(res, status_code=400)
        return res

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
