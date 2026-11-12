"""成交：把「一条建议」变成「一笔真实持仓」。

这是整个产品最关键的闭环，也是将来接入券商 API 的唯一接口点。

设计原则：

* **建议和成交是两件事。** 建议说「在 400–404 之间买 100 股」；成交可能是
  402.5 买了 100 股，也可能是 403 只买到 60 股，甚至根本没买。系统必须同时
  记住两者，并算得出差多少 —— 这个差值比选股准确率更影响最终收益。
* **成交只记事实。** 价格、数量、费用、时间都来自用户或券商回执，本地不猜。
* **卖出按 FIFO 平仓。** 用户可能分批买、分批卖，台账要能对上。
* **超出持仓的卖出直接拒绝。** 小白最容易犯的错是数量填错一位，静默接受会让
  台账变成一本假账。

将来接券商 API 时，只需要在 ``BrokerAdapter`` 后面实现一个真实适配器，
把回报转成 :class:`Fill` 调 :func:`record` —— 上面的规则一行都不用改。
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from decimal import Decimal

from kestrel.core.clock import iso, utcnow
from kestrel.core.money import from_micro, to_micro
from kestrel.db.engine import connect
from kestrel.fees.calculator import quote_fees

log = logging.getLogger(__name__)

CURRENCY = {"CN": "CNY", "HK": "HKD", "US": "USD"}
SIDE_BUY = "buy"
SIDE_SELL = "sell"


@dataclass(slots=True)
class Fill:
    """一笔成交事实。所有字段都由外部提供，本地只做校验。"""

    market: str
    code: str
    side: str
    qty: int
    price: Decimal
    fees: Decimal = Decimal("0")
    filled_at: str | None = None
    rec_id: int | None = None
    source: str = "manual"
    note: str = ""
    name: str = ""
    # 手工建仓时用户自己定的止损/目标。没有它，持仓监控就无从判断风险，
    # 所以它和成交本身一样重要，允许和成交一起填。
    stop: Decimal | None = None
    target: Decimal | None = None

    def normalised(self) -> Fill:
        return Fill(
            market=self.market.upper().strip(),
            code=self.code.strip(),
            side=SIDE_BUY if str(self.side).lower().startswith("b") else SIDE_SELL,
            qty=int(self.qty),
            price=Decimal(str(self.price)),
            fees=Decimal(str(self.fees or 0)),
            filled_at=self.filled_at or iso(utcnow()),
            rec_id=self.rec_id,
            source=self.source,
            note=self.note or "",
            name=self.name or "",
            stop=Decimal(str(self.stop)) if self.stop not in (None, "") else None,
            target=Decimal(str(self.target)) if self.target not in (None, "") else None,
        )


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
        "horizon": rec.get("horizon") or "short",
        "strategy": rec.get("strategy"),
        "source": rec.get("source"),
        "valid_until": rec.get("valid_until"),
    }
    return rec, snapshot


def deviation(price: Decimal, rec: dict | None) -> tuple[float | None, float | None]:
    """成交价相对建议入场价的偏差：百分比和 R 倍数。

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


def _ensure_symbol(conn, fill: Fill) -> dict:
    """没有这只标的时补一行，避免成交因为「没加自选」而丢失。"""
    found = _symbol(conn, fill.market, fill.code)
    if found:
        return found
    conn.execute(
        "INSERT OR IGNORE INTO symbol (market, code, name, currency, lot_size)"
        " VALUES (?,?,?,?,?)",
        (
            fill.market,
            fill.code,
            fill.name or fill.code,
            CURRENCY.get(fill.market, "USD"),
            100 if fill.market in ("CN", "HK") else 1,
        ),
    )
    return _symbol(conn, fill.market, fill.code)


def record(fill: Fill) -> dict:
    """记一笔成交。买入开仓，卖出按 FIFO 平仓。

    返回的 ``ok=False`` 不是异常，而是给用户看的解释：数量不对、持仓不够、
    价格不合理。静默丢弃会让台账和现实脱节。
    """
    fill = fill.normalised()
    if fill.qty <= 0:
        return {"ok": False, "error": "成交数量必须大于 0"}
    if fill.price <= 0:
        return {"ok": False, "error": "成交价格必须大于 0"}

    conn = connect()
    try:
        symbol = _ensure_symbol(conn, fill)
        rec, snapshot = _plan_snapshot(conn, fill.rec_id)
        if fill.side == SIDE_BUY:
            result = _buy(conn, fill, symbol, snapshot)
        else:
            result = _sell(conn, fill, symbol)
        if result.get("ok") and fill.rec_id:
            _mark_recommendation(conn, fill, result)
        conn.commit()
        result["symbol"] = {"market": symbol["market"], "code": symbol["code"],
                            "name": symbol["name"]}
        if rec:
            pct, r = deviation(fill.price, rec)
            result["deviation_pct"] = pct
            result["deviation_r"] = r
        return result
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        log.exception("record fill failed")
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def _buy(conn, fill: Fill, symbol: dict, snapshot: dict | None) -> dict:
    lots = max(1, int(symbol["lot_size"] or 1))
    if fill.market in ("CN", "HK") and fill.qty % lots:
        # 港股每手股数各不相同。不是整手的成交多数是填错了。
        return {
            "ok": False,
            "error": f"{symbol['name']} 每手 {lots} 股，成交数量必须是 {lots} 的整数倍（你填了 {fill.qty}）",
        }

    cur = conn.execute(
        """
        INSERT INTO trade (market, code, name, currency, side, qty, entry_price,
                           fees, opened_at, filled_at, status, rec_id, review,
                           source, plan_snapshot)
        VALUES (?,?,?,?, 'long', ?,?,?,?,?, 'open', ?,?,?,?)
        """,
        (
            fill.market,
            fill.code,
            symbol["name"],
            symbol["currency"],
            fill.qty,
            to_micro(fill.price),
            to_micro(fill.fees),
            fill.filled_at,
            fill.filled_at,
            fill.rec_id,
            fill.note or None,
            fill.source,
            json.dumps(snapshot, ensure_ascii=False) if snapshot else None,
        ),
    )
    trade_id = cur.lastrowid

    if snapshot:
        stop = from_micro(snapshot["stop"])
        target = from_micro(snapshot["target"])
        conn.execute(
            "UPDATE trade SET stop_price=?, initial_stop=?, target_price=? WHERE id=?",
            (to_micro(stop), to_micro(stop), to_micro(target), trade_id),
        )
    elif fill.stop or fill.target:
        # 手工建仓也要有止损，否则「距止损」和风险提醒全是空的。
        conn.execute(
            "UPDATE trade SET stop_price=?, initial_stop=?, target_price=? WHERE id=?",
            (
                to_micro(fill.stop) if fill.stop else None,
                to_micro(fill.stop) if fill.stop else None,
                to_micro(fill.target) if fill.target else None,
                trade_id,
            ),
        )
    return {"ok": True, "trade_id": trade_id, "side": SIDE_BUY, "qty": fill.qty,
            "message": f"已记录买入 {symbol['name']} {fill.qty} 股 @ {fill.price}"}


def _sell(conn, fill: Fill, symbol: dict) -> dict:
    rows = conn.execute(
        "SELECT * FROM trade WHERE market=? AND code=? AND status='open'"
        " ORDER BY COALESCE(filled_at, opened_at), id",
        (fill.market, fill.code),
    ).fetchall()
    if not rows:
        return {"ok": False, "error": f"没有 {symbol['name']} 的持仓可卖，先记一笔买入"}

    available = sum(int(r["qty"]) for r in rows)
    if fill.qty > available:
        return {
            "ok": False,
            "error": f"卖出 {fill.qty} 股超过现有持仓 {available} 股。如果你确实做空，这个工具不支持。",
        }

    remaining = fill.qty
    closed: list[int] = []
    proceeds = Decimal("0")
    cost = Decimal("0")
    for row in rows:
        if remaining <= 0:
            break
        take = min(remaining, int(row["qty"]))
        entry = from_micro(row["entry_price"])
        # 卖出费用按数量分摊到被平的这几笔，避免第一笔承担全部费用。
        fee_share = (fill.fees * Decimal(take) / Decimal(fill.qty)).quantize(Decimal("0.01"))
        cost += entry * take
        proceeds += fill.price * take

        if take == int(row["qty"]):
            _close_row(conn, row["id"], fill, fee_share, int(row["qty"]))
            closed.append(row["id"])
        else:
            # 部分平仓：原行数量减少，另插一条已平记录，历史不会被改写成假的。
            conn.execute("UPDATE trade SET qty=qty-? WHERE id=?", (take, row["id"]))
            cur = conn.execute(
                """
                INSERT INTO trade (market, code, name, currency, side, qty, entry_price,
                                   exit_price, fees, opened_at, filled_at, closed_at,
                                   status, rec_id, review, source, plan_snapshot,
                                   stop_price, target_price)
                SELECT market, code, name, currency, side, ?, entry_price, ?, ?,
                       opened_at, filled_at, ?, 'closed', rec_id, review, source,
                       plan_snapshot, stop_price, target_price
                FROM trade WHERE id=?
                """,
                (take, to_micro(fill.price), to_micro(fee_share), fill.filled_at, row["id"]),
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
        "message": f"已记录卖出 {symbol['name']} {fill.qty} 股 @ {fill.price}，"
                   f"平掉 {len(closed)} 笔持仓",
    }


def _close_row(conn, trade_id: int, fill: Fill, fee_share: Decimal, qty: int) -> None:
    conn.execute(
        """
        UPDATE trade SET exit_price=?, closed_at=?, status='closed',
                         fees=COALESCE(fees,0)+?
        WHERE id=?
        """,
        (to_micro(fill.price), fill.filled_at, to_micro(fee_share), trade_id),
    )
    if fill.note:
        conn.execute(
            "UPDATE trade SET review=COALESCE(review,'') || ? WHERE id=?",
            (("\n" if fill.note else "") + fill.note, trade_id),
        )


def _mark_recommendation(conn, fill: Fill, result: dict) -> None:
    conn.execute(
        "UPDATE recommendation SET status='executed', fill_price=?, decided_at=?,"
        " decided_note=? WHERE id=?",
        (
            to_micro(fill.price),
            iso(utcnow()),
            f"实际成交 {fill.qty} 股 @ {fill.price}",
            fill.rec_id,
        ),
    )


def suggest_fees(market: str, amount: Decimal, qty: int, broker: str | None) -> Decimal:
    """卖出时帮用户估算费用，允许他覆盖。"""
    try:
        return quote_fees(market.upper(), amount, Decimal(qty), True, broker).total
    except Exception:  # noqa: BLE001
        return Decimal("0")


def held_qty(market: str, code: str) -> int:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(qty),0) AS q FROM trade"
            " WHERE market=? AND code=? AND status='open'",
            (market.upper(), code),
        ).fetchone()
        return int(row["q"] or 0)
    finally:
        conn.close()


# --------------------------------------------------------------------- brokers

class BrokerAdapter:
    """券商适配器的接口。真实 API 接进来时只实现这个类。

    当前只有手工与模拟两种实现：产品明确不做自动下单，但所有成交都从
    :meth:`execute` 进来，将来换实现不需要动台账、提醒和统计。
    """

    code = "manual"
    name = "手工录入"
    can_trade = False

    def execute(self, fill: Fill) -> dict:
        raise NotImplementedError


class ManualBroker(BrokerAdapter):
    """用户自己在券商 App 下单，回到这里登记事实。"""

    code = "manual"
    name = "手工录入"

    def execute(self, fill: Fill) -> dict:
        return record(fill)


class PaperBroker(BrokerAdapter):
    """按最近一次行情价成交的模拟盘。

    它存在的意义是把「计划 → 风控 → 成交 → 台账」整条链路跑通，
    等真实 API 到位时，替换的就是这一个类。
    """

    code = "paper"
    name = "模拟成交"

    def execute(self, fill: Fill) -> dict:
        conn = connect()
        try:
            row = conn.execute(
                """
                SELECT q.price FROM quote_latest q
                JOIN symbol s ON s.id = q.symbol_id
                WHERE s.market=? AND s.code=?
                """,
                (fill.market.upper(), fill.code),
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
    """执行质量：建议有没有被执行、执行得准不准。

    这是产品最想让人看懂的一件事——
    「你赚不赚钱」很大程度上不取决于选股，而取决于你有没有照计划做、
    以及成交价偏离了多少。所以这里把两者分开算：

    * **执行率** = 真的做了的建议 / 到期的建议。低执行率说明建议不可信、
      或者你不敢照做，两种都是问题。
    * **偏离度** = 成交价相对入场价的百分比，以 R 为单位。买贵 0.5R 意味着
      你的止损距离被吃掉一半，长期会显著拖累期望。
    """
    conn = connect()
    try:
        decided = conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM recommendation
            WHERE status IN ('executed','skipped','expired')
            GROUP BY status
            """
        ).fetchall()
        rows = conn.execute(
            """
            SELECT r.id, r.market, r.code, r.name, r.currency, r.entry_low, r.stop,
                   r.target1, r.qty, t.qty AS fill_qty, t.entry_price AS fill_price,
                   t.entry_price AS actual
            FROM recommendation r
            JOIN trade t ON t.rec_id = r.id
            WHERE t.status IN ('open','closed')
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
    for row in rows:
        entry = from_micro(row["entry_low"])
        stop = from_micro(row["stop"])
        actual = from_micro(row["actual"])
        if not entry or not actual:
            continue
        pct = float((actual - entry) / entry * 100)
        r_dev = float((actual - entry) / (entry - stop)) if entry > stop else None
        size_ratio = (row["fill_qty"] / row["qty"]) if row["qty"] else None
        samples.append(
            {
                "rec_id": row["id"],
                "market": row["market"],
                "code": row["code"],
                "name": row["name"],
                "currency": row["currency"],
                "planned_qty": row["qty"],
                "filled_qty": row["fill_qty"],
                "size_ratio": size_ratio,
                "deviation_pct": pct,
                "deviation_r": r_dev,
            }
        )

    avg_pct = sum(s["deviation_pct"] for s in samples) / len(samples) if samples else None
    r_values = [s["deviation_r"] for s in samples if s["deviation_r"] is not None]
    avg_r = sum(r_values) / len(r_values) if r_values else None
    partial = sum(1 for s in samples if s["size_ratio"] is not None and s["size_ratio"] < 0.999)

    return {
        "executed": executed,
        "skipped": skipped,
        "expired": expired,
        "advice_total": total,
        "follow_rate": (executed / total) if total else None,
        "samples": samples,
        "avg_deviation_pct": avg_pct,
        "avg_deviation_r": avg_r,
        "worst": max(samples, key=lambda s: s["deviation_pct"]) if samples else None,
        "best": min(samples, key=lambda s: s["deviation_pct"]) if samples else None,
        "partial_fills": partial,
    }


def lesson(report: dict, locale: str = "zh-CN") -> str:
    """把上面的数字翻译成一句人话。新手需要的是结论，不是表格。

    英文页面不能显示中文结论，所以两种语言都生成，调用方按 locale 取。
    """
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
    return " ".join(pieces)
