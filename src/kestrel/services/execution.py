"""成交与准实盘执行：把「一条建议」变成「一笔真实持仓」与可执行复盘。

这是整个产品最关键的闭环，也是将来接入券商交易 API 的唯一契约层。

设计原则：

* **建议和成交是两件事。** 建议说「在 400–404 之间买 100 股」；成交可能是
  402.5 买了 100 股，也可能是 403 只买到 60 股，甚至根本没买。系统必须同时
  记住两者，并算得出差多少 —— 这个差值比选股准确率更影响最终收益。
* **成交只记事实。** 价格、数量、费用、时间都来自用户或券商回执，本地不猜。
* **卖出按 FIFO 平仓。** 用户可能分批买、分批卖，台账要能对上，买入手续费
  也按平仓比例准确分摊，历史仓位与盈亏不被篡改。
* **严格遵循各市场真实制度。** A 股主板 100 股整手、科创板/北交所 200+1、
  A 股 T+1 锁定、港股每手与碎股规则、美股整数与小数股规则。

将来接券商 API 时，只需要实现 :class:`BrokerAdapter` 下的真实适配器，
通过 :func:`pre_trade_check` 与 :class:`OrderIntent` 下发并把回报转成 :class:`Fill`
调 :func:`record` —— 上面的台账、风控与复盘逻辑一行都不用改。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from kestrel.core.clock import iso, utcnow
from kestrel.core.money import currency_for, from_micro, round_tick, to_cny, to_micro
from kestrel.core.quantity import (
    db_value,
    display,
    to_qty,
    validate_order_qty,
)
from kestrel.datasources.base import default_lot_size, normalize_code
from kestrel.db.engine import connect, transaction
from kestrel.fees.calculator import quote_fees
from kestrel.services import config as config_service

log = logging.getLogger(__name__)

CURRENCY = {"CN": "CNY", "HK": "HKD", "US": "USD"}
SIDE_BUY = "buy"
SIDE_SELL = "sell"
SHANGHAI_TZ = timezone(timedelta(hours=8))


@dataclass(slots=True)
class Fill:
    """一笔成交事实。所有字段都由外部提供，本地只做校验。"""

    market: str
    code: str
    side: str
    qty: Decimal
    price: Decimal
    fees: Decimal = Decimal("0")
    filled_at: str | None = None
    rec_id: int | None = None
    source: str = "manual"
    note: str = ""
    name: str = ""
    # 手工建仓或修改计划时用户定的止损/目标。显式提供时优先级高于建议快照。
    stop: Decimal | None = None
    target: Decimal | None = None
    # 卖出复盘标签：按止盈 / 触发止损 / 移动止损 / 时间止损 / 主观卖出
    exit_reason: str = ""
    rule_break: bool = False
    broker: str | None = None

    def normalised(self) -> Fill:
        mkt = self.market.upper().strip()
        return Fill(
            market=mkt,
            code=normalize_code(mkt, self.code.strip()),
            side=SIDE_BUY if str(self.side).lower().startswith("b") else SIDE_SELL,
            qty=to_qty(self.qty),
            price=Decimal(str(self.price)),
            fees=Decimal(str(self.fees or 0)),
            filled_at=self.filled_at or iso(utcnow()),
            rec_id=self.rec_id,
            source=self.source,
            note=self.note or "",
            name=self.name or "",
            stop=Decimal(str(self.stop)) if self.stop not in (None, "") else None,
            target=Decimal(str(self.target)) if self.target not in (None, "") else None,
            exit_reason=self.exit_reason or "",
            rule_break=bool(self.rule_break),
            broker=self.broker or None,
        )


@dataclass(slots=True)
class OrderIntent:
    """准实盘委托意图对象，在真正发给券商 API 或手动下单前构造与校验。"""

    market: str
    code: str
    side: str
    qty: Decimal
    order_type: str = "LIMIT"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    rec_id: int | None = None
    trade_id: int | None = None
    broker: str | None = None
    client_order_id: str = ""

    def __post_init__(self) -> None:
        self.market = str(self.market or "US").upper().strip()
        self.code = normalize_code(self.market, str(self.code or "").strip())
        self.side = SIDE_BUY if str(self.side).lower().startswith("b") else SIDE_SELL
        self.qty = to_qty(self.qty)
        self.order_type = str(self.order_type or "LIMIT").upper()
        if not self.client_order_id:
            self.client_order_id = f"ksl_{uuid.uuid4().hex[:12]}"


@dataclass(slots=True)
class PreTradeCheck:
    """下单前风控与制度预检结果。"""

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    est_fees: Decimal = Decimal("0")
    cash_after_cny: Decimal | None = None
    exposure_after_pct: float | None = None


@dataclass(slots=True)
class OrderTicket:
    """委托受理回执，待接入实盘 API 后返回券商委托号与状态机进度。"""

    ok: bool
    client_order_id: str
    intent_id: int | None
    status: str
    message: str = ""
    check: PreTradeCheck | None = None


def _shanghai_date(ts: str | None) -> str:
    """把时间戳统一换算为北京时间日期，用于 A 股 T+1 判定。"""
    if not ts:
        return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")
    raw = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d")
    except ValueError:
        return raw[:10]


def _symbol(conn, market: str, code: str) -> dict | None:
    row = conn.execute(
        "SELECT id, market, code, name, currency, lot_size FROM symbol"
        " WHERE market=? AND code=?",
        (market, code),
    ).fetchone()
    return dict(row) if row else None


def _plan_snapshot(conn, rec_id: int | None) -> tuple[dict | None, dict | None]:
    if not rec_id:
        return None, None
    row = conn.execute("SELECT * FROM recommendation WHERE id=?", (rec_id,)).fetchone()
    if row is None:
        return None, None
    rec = dict(row)
    snapshot = {
        "rec_id": rec["id"],
        "entry_low": rec["entry_low"],
        "entry_high": rec["entry_high"],
        "stop": rec["stop"],
        "target": rec["target1"],
        "qty": rec["qty"],
        "est_fees": rec.get("est_fees"),
        "breakeven": rec.get("breakeven"),
        "risk_amount": rec.get("risk_amount"),
        "r_multiple": rec.get("r_multiple"),
        "horizon": rec.get("horizon") or "short",
        "strategy": rec.get("strategy"),
        "source": rec.get("source"),
        "valid_until": rec.get("valid_until"),
    }
    return rec, snapshot


def deviation(price: Decimal, rec: dict | None) -> tuple[float | None, float | None]:
    """成交价相对建议下沿入场价的偏差：百分比和 R 倍数。

    正数表示买贵了（对买入不利），负数表示买得更好。R 倍数用
    ``(成交价 - 建议入场价) / (建议入场价 - 建议止损)``，即偏离了多少个风险单位。
    """
    if not rec:
        return None, None
    entry = from_micro(rec["entry_low"])
    stop = from_micro(rec["stop"])
    if not entry:
        return None, None
    pct = (price - entry) / entry * 100
    r = None
    if entry > stop:
        r = float((price - entry) / (entry - stop))
    return float(pct), r


def band_deviation(price: Decimal, rec: dict | None) -> dict:
    """按建议的 [entry_low, entry_high] 入场区间判断实际是否追高或折价。"""
    if not rec:
        return {"in_zone": None, "band_pct": None, "band_r": None}
    low = from_micro(rec["entry_low"])
    high = from_micro(rec.get("entry_high") or rec["entry_low"])
    stop = from_micro(rec["stop"])
    if not low:
        return {"in_zone": None, "band_pct": None, "band_r": None}
    if high < low:
        high = low
    if low <= price <= high:
        return {"in_zone": True, "band_pct": 0.0, "band_r": 0.0}
    anchor = high if price > high else low
    pct = float((price - anchor) / anchor * 100)
    r_val = float((price - anchor) / (low - stop)) if low > stop else None
    return {"in_zone": False, "band_pct": round(pct, 3), "band_r": round(r_val, 3) if r_val is not None else None}


def _ensure_symbol(conn, fill: Fill) -> dict:
    """没有这只标的时补一行，避免成交因为「没加自选」而丢失。"""
    norm_code = normalize_code(fill.market, fill.code)
    found = _symbol(conn, fill.market, norm_code)
    if found:
        return found
    lot = default_lot_size(fill.market, norm_code)
    conn.execute(
        "INSERT OR IGNORE INTO symbol (market, code, name, currency, lot_size)"
        " VALUES (?,?,?,?,?)",
        (
            fill.market,
            norm_code,
            fill.name or norm_code,
            CURRENCY.get(fill.market, currency_for(fill.market)),
            lot,
        ),
    )
    return _symbol(conn, fill.market, norm_code)


def _cn_sellable_breakdown(conn, code: str, fill_ts: str | None) -> tuple[Decimal, Decimal, list]:
    """查询 A 股当前总持仓与受 T+1 限制后今日真正可卖的批次。"""
    rows = conn.execute(
        "SELECT * FROM trade WHERE market='CN' AND code=? AND status='open'"
        " ORDER BY COALESCE(filled_at, opened_at), id",
        (code,),
    ).fetchall()
    total = sum((Decimal(str(r["qty"])) for r in rows), Decimal("0"))
    sell_date = _shanghai_date(fill_ts)
    sellable_rows = [
        r
        for r in rows
        if (r["source"] == "calibrated" or _shanghai_date(r["filled_at"] or r["opened_at"]) != sell_date)
    ]
    sellable = sum((Decimal(str(r["qty"])) for r in sellable_rows), Decimal("0"))
    return total, sellable, sellable_rows


def pre_trade_check(intent: OrderIntent) -> PreTradeCheck:
    """在下单或记账前执行制度校验、费用测算、现金与组合敞口预警。"""
    from kestrel.services import portfolio

    errors: list[str] = []
    warnings: list[str] = []
    if intent.qty <= 0:
        errors.append("委托数量必须大于 0")
    if intent.limit_price is not None and intent.limit_price <= 0:
        errors.append("委托价格必须大于 0")

    conn = connect()
    try:
        sym = _symbol(conn, intent.market, intent.code)
        lot_size = int(sym["lot_size"]) if sym else default_lot_size(intent.market, intent.code)
        broker = intent.broker or config_service.get(conn, f"broker.{intent.market}", "") or None

        held: Decimal | None = None
        if intent.side == SIDE_SELL:
            if intent.market == "CN":
                total_held, sellable_qty, _ = _cn_sellable_breakdown(conn, intent.code, iso(utcnow()))
                held = total_held
                if intent.qty <= total_held and intent.qty > sellable_qty:
                    errors.append(
                        f"受 A 股 T+1 限制，{intent.code} 今日可卖 {display(sellable_qty)} 股"
                        f"（总持仓 {display(total_held)} 股中其余为今日买入）"
                    )
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(qty), 0) AS q FROM trade"
                    " WHERE market=? AND code=? AND status='open'",
                    (intent.market, intent.code),
                ).fetchone()
                held = Decimal(str(row["q"] or 0))

        qty_ok, qty_err = validate_order_qty(
            intent.market,
            intent.code,
            intent.qty,
            lot_size,
            side=intent.side,
            broker=broker,
            held_qty=held,
        )
        if not qty_ok and qty_err:
            errors.append(qty_err)

        price = intent.limit_price or Decimal("0")
        if price > 0:
            rounded = round_tick(price, intent.market, intent.code)
            if abs(rounded - price) > Decimal("0.0001"):
                warnings.append(f"委托价 {price} 未对齐最小价位，建议调整为 {rounded}")

        est_fees = Decimal("0")
        if price > 0 and intent.qty > 0:
            try:
                est_fees = quote_fees(
                    intent.market,
                    price * intent.qty,
                    intent.qty,
                    intent.side == SIDE_SELL,
                    broker,
                ).total
            except Exception:  # noqa: BLE001
                est_fees = Decimal("0")

        snap = portfolio.snapshot(conn=conn, include_quotes=True, limit_ledger=1)
        avail_cny = Decimal(str(snap["available_cash_cny"]))
        total_eq_cny = Decimal(str(snap["total_equity_cny"]))
        mv_cny = Decimal(str(snap["market_value_cny"]))
        cur = currency_for(intent.market)
        order_cny = to_cny(price * intent.qty + est_fees, cur) if price > 0 else Decimal("0")

        cash_after = avail_cny - order_cny if intent.side == SIDE_BUY else avail_cny + order_cny
        mv_after = mv_cny + (to_cny(price * intent.qty, cur) if intent.side == SIDE_BUY else -to_cny(price * intent.qty, cur))
        mv_after = max(Decimal("0"), mv_after)
        exposure_after = float(mv_after / total_eq_cny * 100) if total_eq_cny > 0 else None

        if intent.side == SIDE_BUY and price > 0:
            if cash_after < 0:
                warnings.append(
                    f"买入后可用现金将为 ¥{cash_after:,.2f}（不足 ¥{abs(cash_after):,.2f}），"
                    "请检查券商可用余额或降低股数"
                )
            cap_pct = float(config_service.get(conn, "risk.max_total_exposure_pct", 60.0) or 60.0)
            if exposure_after is not None and exposure_after > cap_pct:
                warnings.append(
                    f"成交后总仓位约 {exposure_after:.1f}%，高于设定上限 {cap_pct:.0f}%"
                )

            daily_limit_pct = float(config_service.get(conn, "risk.daily_loss_limit_pct", 5.0) or 5.0)
            if daily_limit_pct > 0 and total_eq_cny > 0:
                today_str = _shanghai_date(iso(utcnow()))
                closed_today = conn.execute(
                    "SELECT market, currency, qty, entry_price, exit_price, fees, closed_at"
                    " FROM trade WHERE status='closed' AND exit_price IS NOT NULL"
                ).fetchall()
                today_pnl_cny = Decimal("0")
                for row in closed_today:
                    if _shanghai_date(row["closed_at"]) == today_str:
                        net_l = (
                            from_micro(row["exit_price"]) - from_micro(row["entry_price"])
                        ) * Decimal(str(row["qty"])) - from_micro(row["fees"] or 0)
                        today_pnl_cny += to_cny(net_l, row["currency"] or currency_for(row["market"]))
                loss_pct = float(-today_pnl_cny / total_eq_cny * 100) if today_pnl_cny < 0 else 0.0
                if loss_pct >= daily_limit_pct:
                    warnings.append(
                        f"今日已实现亏损 {loss_pct:.1f}% 已触发单日止损护栏（{daily_limit_pct:.1f}%），"
                        "建议今日暂停新开仓"
                    )

        return PreTradeCheck(
            ok=not errors,
            errors=errors,
            warnings=warnings,
            est_fees=est_fees,
            cash_after_cny=cash_after if price > 0 else None,
            exposure_after_pct=round(exposure_after, 1) if exposure_after is not None else None,
        )
    finally:
        conn.close()


def record_intent(
    intent: OrderIntent,
    status: str = "FILLED_MANUAL",
    reject_reason: str = "",
    trade_id: int | None = None,
) -> int:
    """持久化准实盘委托意图，便于未来对接实盘券商回执状态机。"""
    conn = connect()
    try:
        with transaction(conn):
            cur = conn.execute(
                """
                INSERT INTO order_intent
                    (client_order_id, rec_id, trade_id, market, code, side, order_type,
                     qty, limit_price, stop_price, status, broker, reject_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    trade_id=COALESCE(excluded.trade_id, order_intent.trade_id),
                    status=excluded.status,
                    reject_reason=excluded.reject_reason,
                    updated_at=datetime('now')
                """,
                (
                    intent.client_order_id,
                    intent.rec_id,
                    trade_id if trade_id is not None else intent.trade_id,
                    intent.market,
                    intent.code,
                    intent.side,
                    intent.order_type,
                    db_value(intent.qty),
                    to_micro(intent.limit_price) if intent.limit_price is not None else None,
                    to_micro(intent.stop_price) if intent.stop_price is not None else None,
                    status,
                    intent.broker,
                    reject_reason or None,
                ),
            )
            return int(cur.lastrowid)
    finally:
        conn.close()


def record(fill: Fill) -> dict:
    """记一笔成交。买入开仓，卖出按 FIFO 平仓，整个过程原子提交。"""
    fill = fill.normalised()
    if fill.qty <= 0:
        return {"ok": False, "error": "成交数量必须大于 0"}
    if fill.price <= 0:
        return {"ok": False, "error": "成交价格必须大于 0"}

    conn = connect()
    try:
        with transaction(conn):
            symbol = _ensure_symbol(conn, fill)
            rec, snapshot = _plan_snapshot(conn, fill.rec_id)
            if fill.side == SIDE_BUY:
                result = _buy(conn, fill, symbol, snapshot)
            else:
                result = _sell(conn, fill, symbol)
            if result.get("ok") and fill.rec_id:
                _mark_recommendation(conn, fill, result)
            if result.get("ok"):
                conn.execute(
                    """
                    INSERT INTO order_intent
                        (client_order_id, rec_id, trade_id, market, code, side, order_type,
                         qty, limit_price, stop_price, status, broker)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"ksl_{uuid.uuid4().hex[:12]}",
                        fill.rec_id,
                        result.get("trade_id") or ((result.get("closed_trades") or [None])[0]),
                        symbol["market"],
                        symbol["code"],
                        fill.side,
                        "LIMIT",
                        db_value(fill.qty),
                        to_micro(fill.price),
                        to_micro(fill.stop) if fill.stop else None,
                        "FILLED_PAPER" if fill.source == "paper" else "FILLED_MANUAL",
                        fill.broker,
                    ),
                )

        if not result.get("ok"):
            return result

        result["symbol"] = {
            "market": symbol["market"],
            "code": symbol["code"],
            "name": symbol["name"],
        }
        if rec:
            pct, r = deviation(fill.price, rec)
            band = band_deviation(fill.price, rec)
            result["deviation_pct"] = pct
            result["deviation_r"] = r
            result["in_zone"] = band["in_zone"]
            result["band_slippage_pct"] = band["band_pct"]
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("record fill failed")
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def _buy(conn, fill: Fill, symbol: dict, snapshot: dict | None) -> dict:
    lots = max(1, int(symbol["lot_size"] or default_lot_size(fill.market, symbol["code"])))
    broker = fill.broker or config_service.get(conn, f"broker.{fill.market}", "") or None

    # 兼容既有对 A 股整手报错文字的断言：含「每手 {lots} 股」
    if fill.market == "CN":
        if to_qty(fill.qty) != to_qty(fill.qty).to_integral_value():
            return {"ok": False, "error": f"A 股只能按整股成交，你填了 {fill.qty} 股"}
        ok_qty, err_qty = validate_order_qty(
            fill.market, symbol["code"], fill.qty, lots, side="buy", broker=broker
        )
        if not ok_qty:
            if "每手" not in err_qty and "至少 200" not in err_qty:
                err_qty = f"{symbol['name']} 每手 {lots} 股：{err_qty}"
            elif "每手" not in err_qty:
                err_qty = f"{symbol['name']} {err_qty}"
            return {"ok": False, "error": err_qty}
    else:
        ok_qty, err_qty = validate_order_qty(
            fill.market, symbol["code"], fill.qty, lots, side="buy", broker=broker
        )
        if not ok_qty:
            return {"ok": False, "error": err_qty}

    if fill.stop is not None and fill.stop >= fill.price:
        return {"ok": False, "error": "止损价必须低于买入成交价"}

    fee_micro = to_micro(fill.fees)
    cur = conn.execute(
        """
        INSERT INTO trade (market, code, name, currency, side, qty, entry_price,
                           fees, entry_fees, opened_at, filled_at, status, rec_id,
                           review, source, plan_snapshot)
        VALUES (?,?,?,?,'long', ?,?,?,?,?,?, 'open', ?,?,?,?)
        """,
        (
            symbol["market"],
            symbol["code"],
            symbol["name"],
            symbol["currency"],
            db_value(fill.qty),
            to_micro(fill.price),
            fee_micro,
            fee_micro,
            fill.filled_at,
            fill.filled_at,
            fill.rec_id,
            fill.note or None,
            fill.source,
            json.dumps(snapshot, ensure_ascii=False) if snapshot else None,
        ),
    )
    trade_id = cur.lastrowid

    # 优先级：用户本次手工指定的 stop/target 优先，否则沿用计划快照。
    final_stop = fill.stop
    final_target = fill.target
    if final_stop is None and snapshot and snapshot.get("stop"):
        cand_stop = from_micro(snapshot["stop"])
        if cand_stop < fill.price:
            final_stop = cand_stop
    if final_target is None and snapshot and snapshot.get("target"):
        final_target = from_micro(snapshot["target"])

    if final_stop is not None or final_target is not None:
        conn.execute(
            "UPDATE trade SET stop_price=?, initial_stop=?, target_price=? WHERE id=?",
            (
                to_micro(final_stop) if final_stop is not None else None,
                to_micro(final_stop) if final_stop is not None else None,
                to_micro(final_target) if final_target is not None else None,
                trade_id,
            ),
        )

    return {
        "ok": True,
        "trade_id": trade_id,
        "side": SIDE_BUY,
        "qty": fill.qty,
        "message": f"已记录买入 {symbol['name']} {display(fill.qty)} 股 @ {fill.price}",
    }


def _sell(conn, fill: Fill, symbol: dict) -> dict:
    rows = conn.execute(
        "SELECT * FROM trade WHERE market=? AND code=? AND status='open'"
        " ORDER BY COALESCE(filled_at, opened_at), id",
        (symbol["market"], symbol["code"]),
    ).fetchall()
    if not rows:
        return {"ok": False, "error": f"没有 {symbol['name']} 的持仓可卖，先记一笔买入"}

    available = sum((Decimal(str(r["qty"])) for r in rows), Decimal("0"))
    if fill.qty > available:
        return {
            "ok": False,
            "error": f"卖出 {fill.qty} 股超过现有持仓 {available} 股。如果你确实做空，这个工具不支持。",
        }

    ok_qty, err_qty = validate_order_qty(
        symbol["market"], symbol["code"], fill.qty, symbol["lot_size"],
        side="sell", broker=fill.broker, held_qty=available,
    )
    if not ok_qty:
        return {"ok": False, "error": err_qty}

    # A 股 T+1：当日买入的非补录底仓不可在同日卖出。
    if symbol["market"] == "CN":
        sell_date = _shanghai_date(fill.filled_at)
        sellable_rows = [
            r
            for r in rows
            if (r["source"] == "calibrated" or _shanghai_date(r["filled_at"] or r["opened_at"]) != sell_date)
        ]
        sellable = sum((Decimal(str(r["qty"])) for r in sellable_rows), Decimal("0"))
        if fill.qty > sellable:
            return {
                "ok": False,
                "error": (
                    f"受 A 股 T+1 规则限制，{symbol['name']} 今日最多可卖出 {display(sellable)} 股"
                    f"（总持仓 {display(available)} 股中其余为今日买入，需下一交易日方可卖出）"
                ),
            }
        rows = sellable_rows

    remaining = fill.qty
    closed: list[int] = []
    proceeds = Decimal("0")
    cost = Decimal("0")
    for row in rows:
        if remaining <= 0:
            break
        row_qty = Decimal(str(row["qty"]))
        take = min(remaining, row_qty)
        entry = from_micro(row["entry_price"])
        # 卖出费用按数量分摊到被平的这几笔，避免第一笔承担全部费用。
        sell_fee_share = (fill.fees * take / fill.qty).quantize(Decimal("0.01"))
        cost += entry * take
        proceeds += fill.price * take

        if take == row_qty:
            _close_row(conn, row["id"], fill, sell_fee_share)
            closed.append(row["id"])
        else:
            # 部分平仓：买入手续费按卖出股数比例拆入已平记录，剩余部分留在原持仓行。
            orig_fees = int(row["fees"] or 0)
            orig_entry_fees = int(row["entry_fees"] if row["entry_fees"] is not None else orig_fees)
            buy_fee_share_micro = int((Decimal(orig_entry_fees) * take / row_qty).to_integral_value())
            rem_fees_micro = max(0, orig_fees - buy_fee_share_micro)
            rem_entry_fees_micro = max(0, orig_entry_fees - buy_fee_share_micro)
            closed_fees_micro = buy_fee_share_micro + to_micro(sell_fee_share)

            conn.execute(
                "UPDATE trade SET qty=?, fees=?, entry_fees=? WHERE id=?",
                (db_value(row_qty - take), rem_fees_micro, rem_entry_fees_micro, row["id"]),
            )
            cur = conn.execute(
                """
                INSERT INTO trade (market, code, name, currency, side, qty, entry_price,
                                   exit_price, fees, entry_fees, opened_at, filled_at, closed_at,
                                   status, rec_id, review, rule_break, exit_reason, source,
                                   plan_snapshot, initial_stop, stop_price, target_price)
                SELECT market, code, name, currency, side, ?, entry_price, ?, ?, ?,
                       opened_at, filled_at, ?, 'closed', rec_id, ?, ?, ?, source,
                       plan_snapshot, initial_stop, stop_price, target_price
                FROM trade WHERE id=?
                """,
                (
                    db_value(take),
                    to_micro(fill.price),
                    closed_fees_micro,
                    buy_fee_share_micro,
                    fill.filled_at,
                    fill.note or None,
                    1 if fill.rule_break else 0,
                    fill.exit_reason or None,
                    row["id"],
                ),
            )
            closed.append(cur.lastrowid)
        remaining -= take

    gross = proceeds - cost
    return {
        "ok": True,
        "side": SIDE_SELL,
        "qty": fill.qty,
        "closed_trades": closed,
        "gross": float(gross),
        "message": f"已记录卖出 {symbol['name']} {display(fill.qty)} 股 @ {fill.price}，"
                   f"平掉 {len(closed)} 笔持仓",
    }


def _close_row(conn, trade_id: int, fill: Fill, fee_share: Decimal) -> None:
    conn.execute(
        """
        UPDATE trade SET exit_price=?, closed_at=?, status='closed',
                         fees=COALESCE(fees,0)+?,
                         rule_break=MAX(COALESCE(rule_break,0), ?),
                         exit_reason=COALESCE(?, exit_reason)
        WHERE id=?
        """,
        (
            to_micro(fill.price),
            fill.filled_at,
            to_micro(fee_share),
            1 if fill.rule_break else 0,
            fill.exit_reason or None,
            trade_id,
        ),
    )
    if fill.note:
        conn.execute(
            "UPDATE trade SET review=CASE WHEN COALESCE(review,'')='' THEN ? ELSE review || char(10) || ? END WHERE id=?",
            (fill.note, fill.note, trade_id),
        )


def _mark_recommendation(conn, fill: Fill, result: dict) -> None:
    if fill.side != SIDE_BUY:
        return
    agg = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) AS q,"
        " COALESCE(SUM(qty * entry_price), 0) AS amt"
        " FROM trade WHERE rec_id=? AND status IN ('open', 'closed')",
        (fill.rec_id,),
    ).fetchone()
    rec_row = conn.execute("SELECT qty FROM recommendation WHERE id=?", (fill.rec_id,)).fetchone()
    filled_total = Decimal(str(agg["q"] or fill.qty))
    avg_entry_micro = (
        int(round(agg["amt"] / float(filled_total))) if filled_total > 0 and agg["amt"] else to_micro(fill.price)
    )
    planned_qty = Decimal(str(rec_row["qty"] or 0)) if rec_row else Decimal("0")
    if planned_qty > 0 and filled_total < planned_qty:
        note = f"累计实际买入 {display(filled_total)}/{display(planned_qty)} 股 @ 均价 {from_micro(avg_entry_micro):.3f}"
    else:
        note = f"实际成交 {display(filled_total)} 股 @ {fill.price}"
    conn.execute(
        "UPDATE recommendation SET status='executed', fill_price=?, fill_qty=?, decided_at=?,"
        " decided_note=? WHERE id=?",
        (avg_entry_micro, float(filled_total), iso(utcnow()), note, fill.rec_id),
    )


def preview_fill_vs_plan(
    *,
    market: str,
    code: str,
    side: str,
    qty,
    price,
    fees=None,
    stop=None,
    target=None,
    rec_id: int | None = None,
    trade_id: int | None = None,
    broker: str | None = None,
    locale: str = "zh-CN",
) -> dict:
    """在成交弹窗输入实际价与数量时，实时对比计划风险、滑点与账户影响。"""
    from kestrel.services import portfolio

    en = str(locale).lower().startswith("en")
    mkt = str(market or "US").upper().strip()
    norm_code = normalize_code(mkt, str(code or "").strip())
    side_norm = SIDE_BUY if str(side).lower().startswith("b") else SIDE_SELL
    q = to_qty(qty or 0)
    p = Decimal(str(price or 0)) if price not in (None, "") else Decimal("0")
    s = Decimal(str(stop)) if stop not in (None, "") else None
    tgt = Decimal(str(target)) if target not in (None, "") else None
    cur = currency_for(mkt)

    conn = connect()
    try:
        brk = broker or config_service.get(conn, f"broker.{mkt}", "") or None
        sym = _symbol(conn, mkt, norm_code)
        lot = int(sym["lot_size"]) if sym else default_lot_size(mkt, norm_code)
        qty_ok, qty_err = (
            validate_order_qty(mkt, norm_code, q, lot, side=side_norm, broker=brk)
            if q > 0
            else (False, "")
        )

        # 计算手续费：用户未手填则估算当前侧，同时计算买卖双边预估方便新手看清成本。
        est_side_fees = Decimal("0")
        est_round_trip = Decimal("0")
        if p > 0 and q > 0:
            try:
                est_side_fees = quote_fees(mkt, p * q, q, side_norm == SIDE_SELL, brk).total
                sell_half = quote_fees(mkt, p * q, q, True, brk).total
                buy_half = quote_fees(mkt, p * q, q, False, brk).total
                est_round_trip = buy_half + sell_half
            except Exception:  # noqa: BLE001
                est_side_fees = Decimal("0")

        actual_fees = (
            Decimal(str(fees))
            if fees not in (None, "")
            else est_side_fees
        )
        breakeven = (
            float(p + (est_round_trip / q))
            if (q > 0 and p > 0)
            else None
        )

        rec, _ = _plan_snapshot(conn, rec_id)
        trade_row = None
        if trade_id:
            tr = conn.execute("SELECT * FROM trade WHERE id=?", (trade_id,)).fetchone()
            trade_row = dict(tr) if tr else None

        snap = portfolio.snapshot(conn=conn, include_quotes=True, limit_ledger=1)
        avail_cny = Decimal(str(snap["available_cash_cny"]))
        total_eq_cny = Decimal(str(snap["total_equity_cny"]))
        mv_cny = Decimal(str(snap["market_value_cny"]))

        out: dict = {
            "ok": True,
            "market": mkt,
            "code": norm_code,
            "currency": cur,
            "qty_valid": qty_ok,
            "qty_error": qty_err,
            "est_fees": float(est_side_fees),
            "used_fees": float(actual_fees),
            "breakeven": round(breakeven, 3) if breakeven is not None else None,
            "warnings": [],
            "summary": "",
        }

        if side_norm == SIDE_BUY and p > 0 and q > 0:
            cost_local = p * q + actual_fees
            cost_cny = to_cny(cost_local, cur)
            cash_after = avail_cny - cost_cny
            exp_after = float((mv_cny + to_cny(p * q, cur)) / total_eq_cny * 100) if total_eq_cny > 0 else 0.0
            out["order_amount"] = float(p * q)
            out["cash_after_cny"] = float(cash_after)
            out["exposure_after_pct"] = round(exp_after, 1)
            if cash_after < 0:
                out["warnings"].append(
                    f"Insufficient cash after buy (short by ¥{abs(cash_after):,.2f})"
                    if en
                    else f"买入后可用现金将不足 ¥{abs(cash_after):,.2f}，请核对余额或减仓"
                )

            eff_stop = s if s is not None else (from_micro(rec["stop"]) if rec and rec.get("stop") else None)
            eff_target = tgt if tgt is not None else (from_micro(rec["target1"]) if rec and rec.get("target1") else None)
            if eff_stop is not None:
                if eff_stop >= p:
                    out["warnings"].append(
                        "Stop price must be below buy price."
                        if en
                        else "实际止损价高于或等于买入价，止损已失效，请重新设定！"
                    )
                else:
                    actual_risk = (p - eff_stop) * q + est_round_trip
                    out["actual_risk"] = float(actual_risk)
                    out["actual_risk_cny"] = float(to_cny(actual_risk, cur))
                    out["actual_risk_pct"] = (
                        round(float(to_cny(actual_risk, cur) / total_eq_cny * 100), 2)
                        if total_eq_cny > 0
                        else None
                    )
                    if eff_target and eff_target > p:
                        out["actual_r"] = round(float((eff_target - p) / (p - eff_stop)), 2)

            if rec:
                pct, r_val = deviation(p, rec)
                band = band_deviation(p, rec)
                plan_qty = Decimal(str(rec["qty"] or 0))
                out["plan"] = {
                    "entry_low": float(from_micro(rec["entry_low"])),
                    "entry_high": float(from_micro(rec.get("entry_high") or rec["entry_low"])),
                    "stop": float(from_micro(rec["stop"])),
                    "target1": float(from_micro(rec["target1"])),
                    "qty": float(plan_qty),
                    "risk_amount": float(from_micro(rec.get("risk_amount") or 0)),
                    "deviation_pct": round(pct, 2) if pct is not None else None,
                    "deviation_r": round(r_val, 2) if r_val is not None else None,
                    "in_zone": band["in_zone"],
                    "band_pct": band["band_pct"],
                    "qty_diff": float(q - plan_qty),
                }
                if band["in_zone"]:
                    out["summary"] = (
                        f"Fill {p} is inside the planned entry band. Actual risk stays aligned."
                        if en
                        else f"实际成交价 {p} 落在建议入场区间内，风险收益比基本未失真。"
                    )
                elif band["band_pct"] and band["band_pct"] > 0:
                    out["summary"] = (
                        f"Chased {band['band_pct']:+.2f}% above entry band — stop distance gets compressed."
                        if en
                        else f"高于建议区间的追高幅度 {band['band_pct']:+.2f}%，会吃掉部分止损缓冲，注意控制股数。"
                    )
                else:
                    out["summary"] = (
                        f"Lower than planned band ({band['band_pct']:+.2f}%) — verify support still holds."
                        if en
                        else f"低于建议区间 {band['band_pct']:+.2f}% 接货，请确认趋势并未走坏而非接飞刀。"
                    )
        elif side_norm == SIDE_SELL and p > 0 and q > 0 and trade_row:
            entry = from_micro(trade_row["entry_price"])
            held = Decimal(str(trade_row["qty"]))
            orig_buy_fee = from_micro(trade_row.get("entry_fees") or trade_row.get("fees") or 0)
            buy_fee_share = orig_buy_fee * min(q, held) / held if held > 0 else Decimal("0")
            est_net = (p - entry) * q - buy_fee_share - actual_fees
            init_stop = (
                from_micro(trade_row["initial_stop"])
                if trade_row.get("initial_stop")
                else (from_micro(trade_row["stop_price"]) if trade_row.get("stop_price") else None)
            )
            r_realized = (
                float((p - entry) / (entry - init_stop))
                if init_stop and entry > init_stop
                else None
            )
            out["est_net"] = float(est_net)
            out["est_net_cny"] = float(to_cny(est_net, cur))
            out["r_realized"] = round(r_realized, 2) if r_realized is not None else None
            if mkt == "CN" and trade_row.get("source") != "calibrated":
                sell_date = _shanghai_date(iso(utcnow()))
                buy_date = _shanghai_date(trade_row.get("filled_at") or trade_row.get("opened_at"))
                if sell_date == buy_date:
                    out["warnings"].append(
                        "CN A-share T+1 rule: shares bought today cannot be sold until the next trading day."
                        if en
                        else "A 股 T+1 提示：该笔为今日买入持仓，今日在真实券商无法卖出。"
                    )
            out["summary"] = (
                f"Est. net P&L {est_net:+.2f} {cur}" + (f" ({r_realized:+.2f}R)" if r_realized is not None else "")
                if en
                else f"按当前卖出价预计净盈亏 {est_net:+.2f} {cur}" + (f"（约 {r_realized:+.2f}R）" if r_realized is not None else "")
            )
        return out
    finally:
        conn.close()


def suggest_fees(market: str, amount: Decimal, qty, broker: str | None) -> Decimal:
    """卖出时帮用户估算费用，允许他覆盖。"""
    try:
        return quote_fees(market.upper(), amount, Decimal(str(qty)), True, broker).total
    except Exception:  # noqa: BLE001
        return Decimal("0")


def held_qty(market: str, code: str) -> Decimal:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(qty),0) AS q FROM trade"
            " WHERE market=? AND code=? AND status='open'",
            (market.upper(), normalize_code(market.upper(), code)),
        ).fetchone()
        return Decimal(str(row["q"] or 0))
    finally:
        conn.close()


# --------------------------------------------------------------------- brokers


class BrokerAdapter:
    """券商适配器标准接口。将来对接真实交易 API 时只需继承实现此接口。

    当前产品明确不做自动实盘下单，但通过 :meth:`pre_check` -> :meth:`submit_order`
    -> :meth:`sync_account` 的契约闭环，使所有风控、台账、偏离归因保持无缝一致。
    """

    code = "manual"
    name = "手工录入"
    can_trade = False

    def pre_check(self, intent: OrderIntent) -> PreTradeCheck:
        return pre_trade_check(intent)

    def execute(self, fill: Fill) -> dict:
        raise NotImplementedError

    def submit_order(self, intent: OrderIntent) -> OrderTicket:
        check = self.pre_check(intent)
        if not check.ok:
            intent_id = record_intent(
                intent, status="REJECTED", reject_reason="; ".join(check.errors)
            )
            return OrderTicket(
                ok=False,
                client_order_id=intent.client_order_id,
                intent_id=intent_id,
                status="REJECTED",
                message="; ".join(check.errors),
                check=check,
            )
        intent_id = record_intent(intent, status="PENDING_MANUAL")
        return OrderTicket(
            ok=True,
            client_order_id=intent.client_order_id,
            intent_id=intent_id,
            status="PENDING_MANUAL",
            message="已生成手动委托单，请在券商 App 下单后录入实际成交结果",
            check=check,
        )

    def cancel_order(self, client_order_id: str) -> dict:
        conn = connect()
        try:
            with transaction(conn):
                cur = conn.execute(
                    "UPDATE order_intent SET status='CANCELLED', updated_at=datetime('now')"
                    " WHERE client_order_id=? AND status IN ('PENDING_SUBMIT','PENDING_MANUAL')",
                    (client_order_id,),
                )
            return {"ok": cur.rowcount > 0, "client_order_id": client_order_id}
        finally:
            conn.close()

    def open_orders(self) -> list[dict]:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT * FROM order_intent WHERE status IN ('PENDING_SUBMIT','PENDING_MANUAL')"
                " ORDER BY id DESC LIMIT 50"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def sync_account(self) -> dict:
        from kestrel.services import portfolio

        return portfolio.snapshot(include_quotes=True)


class ManualBroker(BrokerAdapter):
    """用户自己在券商 App 下单，回到这里登记事实。"""

    code = "manual"
    name = "手工录入"

    def execute(self, fill: Fill) -> dict:
        return record(fill)


class PaperBroker(BrokerAdapter):
    """按最近一次行情价成交的模拟盘。"""

    code = "paper"
    name = "模拟成交"

    def execute(self, fill: Fill) -> dict:
        norm = normalize_code(fill.market.upper(), fill.code)
        conn = connect()
        try:
            row = conn.execute(
                """
                SELECT q.price FROM quote_latest q
                JOIN symbol s ON s.id = q.symbol_id
                WHERE s.market=? AND s.code=?
                """,
                (fill.market.upper(), norm),
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row["price"]:
            return {"ok": False, "error": "还没有现价，先刷新一次行情"}
        filled = dataclasses.replace(fill, price=from_micro(row["price"]), source="paper")
        return record(filled)


ADAPTERS: dict[str, BrokerAdapter] = {
    ManualBroker.code: ManualBroker(),
    PaperBroker.code: PaperBroker(),
}


def adapter(code: str | None = None) -> BrokerAdapter:
    return ADAPTERS.get(str(code or "manual"), ADAPTERS["manual"])


def recent_executions(limit: int = 30) -> list[dict]:
    """最近的成交（买入行 + 平仓行），供今日页展示。"""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, market, code, name, currency, qty, entry_price, exit_price,
                   fees, status, source, opened_at, filled_at, closed_at, rec_id
            FROM trade ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def quality_report() -> dict:
    """执行质量：建议有没有被执行、执行得准不准、What-if 盈亏归因。"""
    conn = connect()
    try:
        decided = conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM recommendation
            WHERE status IN ('executed','skipped','expired')
            GROUP BY status
            """
        ).fetchall()
        # 按 rec_id 聚合买入总股数、加权均价，避免分批卖出造成同条建议重复计算或误判部分成交。
        rows = conn.execute(
            """
            SELECT r.id, r.market, r.code, r.name, r.currency,
                   r.entry_low, r.entry_high, r.stop, r.target1, r.qty,
                   r.est_fees,
                   SUM(t.qty) AS fill_qty,
                   SUM(t.qty * t.entry_price) / SUM(t.qty) AS fill_price
            FROM recommendation r
            JOIN trade t ON t.rec_id = r.id
            WHERE t.status IN ('open','closed')
            GROUP BY r.id
            ORDER BY r.id DESC
            """
        ).fetchall()
        closed_linked = conn.execute(
            """
            SELECT r.id AS rec_id, r.market, r.code, r.name, r.currency,
                   r.entry_low, r.entry_high, r.stop, r.target1, r.qty AS plan_qty,
                   t.qty AS fill_qty, t.entry_price, t.exit_price, t.fees, t.exit_reason, t.rule_break
            FROM recommendation r
            JOIN trade t ON t.rec_id = r.id
            WHERE t.status='closed' AND t.exit_price IS NOT NULL
            ORDER BY t.closed_at DESC, t.id DESC
            """
        ).fetchall()
    finally:
        conn.close()

    counts = {row["status"]: int(row["n"]) for row in decided}
    executed = counts.get("executed", 0)
    skipped = counts.get("skipped", 0)
    expired = counts.get("expired", 0)
    total = executed + skipped + expired

    samples: list[dict] = []
    in_zone_count = 0
    for row in rows:
        entry = from_micro(row["entry_low"])
        entry_high = from_micro(row["entry_high"] or row["entry_low"])
        stop = from_micro(row["stop"])
        target1 = from_micro(row["target1"])
        actual = from_micro(int(round(row["fill_price"] or 0)))
        if not entry or not actual:
            continue
        pct = float((actual - entry) / entry * 100)
        r_dev = float((actual - entry) / (entry - stop)) if entry > stop else None
        band = band_deviation(actual, dict(row))
        if band["in_zone"]:
            in_zone_count += 1
        filled_qty = float(row["fill_qty"] or 0)
        planned_qty = float(row["qty"] or 0)
        size_ratio = (filled_qty / planned_qty) if planned_qty else None
        samples.append(
            {
                "rec_id": row["id"],
                "market": row["market"],
                "code": row["code"],
                "name": row["name"],
                "currency": row["currency"],
                "plan_entry_low": float(entry),
                "plan_entry_high": float(entry_high),
                "plan_stop": float(stop),
                "plan_target": float(target1),
                "actual_price": float(actual),
                "planned_qty": planned_qty,
                "filled_qty": filled_qty,
                "size_ratio": size_ratio,
                "deviation_pct": pct,
                "deviation_r": r_dev,
                "in_zone": bool(band["in_zone"]),
                "band_slippage_pct": band["band_pct"],
            }
        )

    # 卖出侧与 What-if 归因：按同一退出价，对比「按建议入场中位价 + 建议股数」的理论净收益。
    exit_samples: list[dict] = []
    actual_closed_cny = Decimal("0")
    planned_closed_cny = Decimal("0")
    for row in closed_linked:
        cur = row["currency"] or currency_for(row["market"])
        q_act = Decimal(str(row["fill_qty"] or 0))
        q_plan = Decimal(str(row["plan_qty"] or 0))
        ent_act = from_micro(row["entry_price"])
        exit_p = from_micro(row["exit_price"])
        fees_act = from_micro(row["fees"] or 0)
        ent_low = from_micro(row["entry_low"])
        ent_high = from_micro(row["entry_high"] or row["entry_low"])
        ent_mid = (ent_low + ent_high) / Decimal("2") if ent_low else ent_act

        net_act_local = (exit_p - ent_act) * q_act - fees_act
        net_plan_local = (exit_p - ent_mid) * q_plan - fees_act
        act_cny = to_cny(net_act_local, cur)
        plan_cny = to_cny(net_plan_local, cur)
        actual_closed_cny += act_cny
        planned_closed_cny += plan_cny

        stop_p = from_micro(row["stop"])
        target_p = from_micro(row["target1"])
        if target_p and exit_p >= target_p:
            exit_tag = "target_hit"
        elif stop_p and exit_p < stop_p * Decimal("0.98"):
            exit_tag = "stopped_late"
        elif row["rule_break"]:
            exit_tag = "rule_break"
        else:
            exit_tag = row["exit_reason"] or "planned_exit"

        exit_samples.append(
            {
                "rec_id": row["rec_id"],
                "market": row["market"],
                "code": row["code"],
                "name": row["name"],
                "currency": cur,
                "actual_entry": float(ent_act),
                "plan_entry_mid": float(ent_mid),
                "exit_price": float(exit_p),
                "actual_net_cny": float(act_cny),
                "planned_net_cny": float(plan_cny),
                "diff_cny": float(act_cny - plan_cny),
                "exit_tag": exit_tag,
            }
        )

    avg_pct = sum(s["deviation_pct"] for s in samples) / len(samples) if samples else None
    r_values = [s["deviation_r"] for s in samples if s["deviation_r"] is not None]
    avg_r = sum(r_values) / len(r_values) if r_values else None
    partial = sum(1 for s in samples if s["size_ratio"] is not None and s["size_ratio"] < 0.999)
    oversized = sum(1 for s in samples if s["size_ratio"] is not None and s["size_ratio"] > 1.001)
    drag_cny = (actual_closed_cny - planned_closed_cny) if exit_samples else None

    return {
        "executed": executed,
        "skipped": skipped,
        "expired": expired,
        "advice_total": total,
        "follow_rate": (executed / total) if total else None,
        "samples": samples,
        "in_zone_count": in_zone_count,
        "in_zone_rate": (in_zone_count / len(samples)) if samples else None,
        "avg_deviation_pct": avg_pct,
        "avg_deviation_r": avg_r,
        "worst": max(samples, key=lambda s: s["deviation_pct"]) if samples else None,
        "best": min(samples, key=lambda s: s["deviation_pct"]) if samples else None,
        "partial_fills": partial,
        "oversized_fills": oversized,
        "exit_samples": exit_samples,
        "actual_closed_cny": float(actual_closed_cny) if exit_samples else None,
        "planned_closed_cny": float(planned_closed_cny) if exit_samples else None,
        "execution_drag_cny": float(drag_cny) if drag_cny is not None else None,
    }


def lesson(report: dict, locale: str = "zh-CN") -> str:
    """把上面的数字翻译成一句人话，让新手一眼看懂是选股问题还是执行纪律问题。"""
    english = str(locale).lower().startswith("en")
    if not report["advice_total"]:
        return (
            "No advice acted on yet. Run a scan and take one you understand."
            if english
            else "还没有执行过任何建议。先跑一次扫描，挑一条看得懂的做。"
        )

    rate = report["follow_rate"] or 0
    pieces: list[str] = []
    if rate < 0.2:
        pieces.append(
            f"You followed {rate * 100:.0f}% of the advice. Either it does not suit you, "
            "or you hesitate — both are worth fixing before anything else."
            if english
            else (
                f"你只执行了 {rate * 100:.0f}% 的建议。这可能说明建议不合你的口味，"
                "也可能是你在犹豫——两种情况都值得先把它调成你会照做的样子。"
            )
        )
    elif rate > 0.8:
        pieces.append(
            f"You followed {rate * 100:.0f}% of the advice. That is real discipline."
            if english
            else f"你执行了 {rate * 100:.0f}% 的建议，纪律很好。"
        )
    else:
        pieces.append(
            f"You followed {rate * 100:.0f}% of the advice — a normal range."
            if english
            else f"你执行了 {rate * 100:.0f}% 的建议，属于正常范围。"
        )

    avg_r = report.get("avg_deviation_r")
    if avg_r is not None:
        if avg_r > 0.15:
            pieces.append(
                f"But you paid {avg_r:.2f}R above plan on average — that ate "
                f"{avg_r * 100:.0f}% of your stop distance. Use limit orders."
                if english
                else (
                    f"但平均买贵了 {avg_r:.2f}R——等于你的止损距离被吃掉 {avg_r * 100:.0f}%。"
                    "挂限价单，或者干脆等回踩，比追进去划算。"
                )
            )
        elif avg_r < -0.15:
            pieces.append(
                f"You paid {abs(avg_r):.2f}R less than plan. Make sure that was a better "
                "entry and not a falling knife."
                if english
                else (
                    f"平均买得比计划便宜 {abs(avg_r):.2f}R，执行得比计划还好。"
                    "注意这可能是价格已经走坏才等到你的单，别把接刀当成讲价。"
                )
            )
        else:
            pieces.append(
                f"Fills averaged {avg_r:+.2f}R away from plan — essentially on plan."
                if english
                else f"成交价平均偏离 {avg_r:+.2f}R，基本贴着计划走。"
            )

    if report.get("partial_fills"):
        pieces.append(
            f"{report['partial_fills']} fill(s) were partial — the journal records what "
            "actually filled, not what was planned."
            if english
            else f"有 {report['partial_fills']} 笔只成交了一部分——台账记的就是实际数量，不是计划数量。"
        )
    if report.get("oversized_fills"):
        pieces.append(
            f"{report['oversized_fills']} trade(s) exceeded planned shares — mind position sizing."
            if english
            else f"另有 {report['oversized_fills']} 笔超出了建议股数，小心单笔风险悄悄放大。"
        )
    drag = report.get("execution_drag_cny")
    if drag is not None:
        if drag < -1:
            pieces.append(
                f"Execution drift cost ~¥{abs(drag):,.0f} vs strict plan execution."
                if english
                else f"对比严格照计划建仓，实际执行差价与仓位变化共少赚/多亏了约 ¥{abs(drag):,.0f}。"
            )
        elif drag > 1:
            pieces.append(
                f"Disciplined/better fills added ~¥{drag:,.0f} vs the base plan."
                if english
                else f"实际执行比原计划基准多赚了约 ¥{drag:,.0f}。"
            )
    return " ".join(pieces)
