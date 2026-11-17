"""Trade journal, positions and the statistics that matter."""

from __future__ import annotations

from decimal import Decimal

from kestrel.core.clock import iso, utcnow
from kestrel.core.money import FX_TO_CNY, from_micro, to_micro
from kestrel.core.money import to_cny as _to_cny
from kestrel.core.quantity import db_value, display, validate_order_qty
from kestrel.db.engine import connect, transaction
from kestrel.fees.calculator import quote_fees
from kestrel.services import config as config_service

FX = FX_TO_CNY


def to_cny(amount: Decimal, currency: str) -> Decimal:
    return _to_cny(amount, currency)


def list_trades(status: str | None = None, limit: int = 200) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT * FROM trade"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = conn.execute(sql, params + (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_trade(**kw) -> int:
    conn = connect()
    try:
        fees_micro = to_micro(kw.get("fees") or 0)
        cur = conn.execute(
            """
            INSERT INTO trade (market, code, name, currency, qty, entry_price, stop_price,
                               initial_stop, target_price, strategy, rec_id, opened_at,
                               filled_at, fees, entry_fees)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                kw["market"].upper(), kw["code"], kw.get("name") or kw["code"],
                kw.get("currency") or {"CN": "CNY", "HK": "HKD", "US": "USD"}[kw["market"].upper()],
                db_value(kw["qty"]), to_micro(kw["entry_price"]),
                to_micro(kw["stop_price"]) if kw.get("stop_price") else None,
                to_micro(kw["stop_price"]) if kw.get("stop_price") else None,
                to_micro(kw["target_price"]) if kw.get("target_price") else None,
                kw.get("strategy"), kw.get("rec_id"), kw.get("opened_at") or iso(utcnow()),
                kw.get("opened_at") or iso(utcnow()),
                fees_micro,
                fees_micro,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def close_trade(
    trade_id: int,
    exit_price,
    review: str = "",
    rule_break: bool = False,
    qty: Decimal | None = None,
    *,
    exit_reason: str = "",
    fees_override=None,
    broker: str | None = None,
) -> dict | None:
    """平掉某一条持仓（可部分平仓）。

    卖出按用户实际配置的券商套餐计算卖出侧费用；部分平仓按股数比例把买入侧费用
    拆分到已平记录中，保留 ``initial_stop`` 且不改写剩余持仓成本。
    """
    from kestrel.datasources.base import default_lot_size
    from kestrel.services.execution import _shanghai_date

    conn = connect()
    try:
        row = conn.execute("SELECT * FROM trade WHERE id=?", (trade_id,)).fetchone()
        if row is None:
            return None

        trade = dict(row)
        held_qty = Decimal(str(trade["qty"]))
        sell_qty = Decimal(str(qty)) if qty not in (None, "") else held_qty
        brk = broker or config_service.get(conn, f"broker.{trade['market']}", "") or None
        lot = default_lot_size(trade["market"], trade["code"])
        ok_qty, err_qty = validate_order_qty(
            trade["market"],
            trade["code"],
            sell_qty,
            lot,
            side="sell",
            broker=brk,
            held_qty=held_qty,
        )
        if not ok_qty:
            return {"ok": False, "error": err_qty}

        now_iso = iso(utcnow())
        if (
            trade["market"] == "CN"
            and trade.get("source") != "calibrated"
            and _shanghai_date(trade.get("filled_at") or trade.get("opened_at"))
            == _shanghai_date(now_iso)
        ):
            return {
                "ok": False,
                "error": "受 A 股 T+1 规则限制，今日新买入的股票需下一交易日方可卖出",
            }

        price = Decimal(str(exit_price))
        amount_out = price * sell_qty
        if fees_override not in (None, ""):
            fee_sell = Decimal(str(fees_override))
        else:
            try:
                fee_sell = quote_fees(trade["market"], amount_out, sell_qty, True, brk).total
            except Exception:  # noqa: BLE001
                fee_sell = Decimal("0")

        with transaction(conn):
            if sell_qty == held_qty:
                conn.execute(
                    "UPDATE trade SET exit_price=?, closed_at=?, status='closed',"
                    " fees=COALESCE(fees,0)+?, review=COALESCE(?,review),"
                    " rule_break=?, exit_reason=COALESCE(?, exit_reason) WHERE id=?",
                    (
                        to_micro(price),
                        now_iso,
                        to_micro(fee_sell),
                        review or None,
                        1 if rule_break else 0,
                        exit_reason or None,
                        trade_id,
                    ),
                )
                closed_id = trade_id
            else:
                orig_fees = int(trade.get("fees") or 0)
                orig_entry_fees = int(
                    trade["entry_fees"] if trade.get("entry_fees") is not None else orig_fees
                )
                buy_fee_share_micro = int(
                    (Decimal(orig_entry_fees) * sell_qty / held_qty).to_integral_value()
                )
                rem_fees_micro = max(0, orig_fees - buy_fee_share_micro)
                rem_entry_fees_micro = max(0, orig_entry_fees - buy_fee_share_micro)
                closed_fees_micro = buy_fee_share_micro + to_micro(fee_sell)

                conn.execute(
                    "UPDATE trade SET qty=?, fees=?, entry_fees=? WHERE id=?",
                    (
                        db_value(held_qty - sell_qty),
                        rem_fees_micro,
                        rem_entry_fees_micro,
                        trade_id,
                    ),
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
                        db_value(sell_qty),
                        to_micro(price),
                        closed_fees_micro,
                        buy_fee_share_micro,
                        now_iso,
                        review or None,
                        1 if rule_break else 0,
                        exit_reason or None,
                        trade_id,
                    ),
                )
                closed_id = cur.lastrowid
        return dict(conn.execute("SELECT * FROM trade WHERE id=?", (closed_id,)).fetchone())
    finally:
        conn.close()


def delete_trade(trade_id: int) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM trade WHERE id=?", (trade_id,))
        conn.commit()
    finally:
        conn.close()


def pnl(trade: dict) -> dict:
    entry = from_micro(trade["entry_price"])
    qty = trade["qty"]
    if trade["status"] == "closed" and trade["exit_price"]:
        exit_price = from_micro(trade["exit_price"])
        gross = (exit_price - entry) * qty
        net = gross - from_micro(trade["fees"] or 0)
        cost = entry * qty
        return {
            "gross": gross, "net": net, "fees": from_micro(trade["fees"] or 0),
            "ret": (net / cost) if cost else Decimal("0"),
            "net_cny": to_cny(net, trade["currency"]),
        }
    return {
        "gross": Decimal("0"), "net": Decimal("0"), "fees": Decimal("0"),
        "ret": Decimal("0"), "net_cny": Decimal("0"),
    }


def stats() -> dict:
    trades = list_trades(limit=1000)
    closed = [t for t in trades if t["status"] == "closed"]
    wins = losses = 0
    sum_win = sum_loss = Decimal("0")
    net_cny = Decimal("0")
    broken = clean = 0
    broken_pnl = clean_pnl = Decimal("0")
    for t in closed:
        p = pnl(t)
        net_cny += p["net_cny"]
        if p["net"] >= 0:
            wins += 1
            sum_win += p["ret"]
        else:
            losses += 1
            sum_loss += abs(p["ret"])
        if t["rule_break"]:
            broken += 1
            broken_pnl += p["net_cny"]
        else:
            clean += 1
            clean_pnl += p["net_cny"]
    total = len(closed)
    avg_win = (sum_win / wins) if wins else Decimal("0")
    avg_loss = (sum_loss / losses) if losses else Decimal("0")
    return {
        "open": len([t for t in trades if t["status"] == "open"]),
        "closed": total,
        "win_rate": (wins / total) if total else 0.0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "rr": float(avg_win / avg_loss) if avg_loss else 0.0,
        "net_cny": net_cny,
        "broken": broken,
        "broken_pnl": broken_pnl,
        "clean": clean,
        "clean_pnl": clean_pnl,
    }


def positions() -> list[dict]:
    out = []
    for t in list_trades(status="open", limit=500):
        out.append(
            {
                "id": t["id"], "market": t["market"], "code": t["code"], "name": t["name"],
                "qty": t["qty"], "qty_display": display(t["qty"]), "entry": from_micro(t["entry_price"]),
                "stop": from_micro(t["stop_price"]) if t["stop_price"] else None,
                "initial_stop": from_micro(t["initial_stop"]) if t.get("initial_stop") else None,
                "target": from_micro(t["target_price"]) if t["target_price"] else None,
                "currency": t["currency"], "opened_at": t["opened_at"],
                "filled_at": t.get("filled_at") or t["opened_at"],
                "fees": from_micro(t["fees"] or 0),
                "source": t.get("source") or "manual",
                "rec_id": t.get("rec_id"),
            }
        )
    return out


def equity_curve(limit: int = 200) -> list[dict]:
    """按平仓时间顺序返回累计已实现净盈亏曲线（折合 CNY），用于复盘页 SVG。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, market, code, name, currency, qty, entry_price, exit_price,"
            " fees, status, closed_at, opened_at"
            " FROM trade WHERE status='closed' AND exit_price IS NOT NULL"
            " ORDER BY COALESCE(closed_at, opened_at) ASC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    cum = Decimal("0")
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        p = pnl(item)
        cum += p["net_cny"]
        out.append(
            {
                "date": str(item.get("closed_at") or item.get("opened_at") or "")[:10],
                "code": item["code"],
                "name": item["name"],
                "net_cny": float(p["net_cny"]),
                "cum_cny": float(cum),
            }
        )
    return out


# --- IPO ledger ---------------------------------------------------------

def list_ipos() -> list[dict]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM ipo_ledger ORDER BY id DESC").fetchall()]
    finally:
        conn.close()


def add_ipo(**kw) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO ipo_ledger (name, code, lot_size, offer_price, lots, fee, allot_qty,
                                    sell_price, listed_on, note)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                kw["name"], kw.get("code"), int(kw["lot_size"]), to_micro(kw["offer_price"]),
                int(kw.get("lots") or 1), to_micro(kw.get("fee") or 0),
                int(kw.get("allot_qty") or 0),
                to_micro(kw["sell_price"]) if kw.get("sell_price") else None,
                kw.get("listed_on"), kw.get("note"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def delete_ipo(ipo_id: int) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM ipo_ledger WHERE id=?", (ipo_id,))
        conn.commit()
    finally:
        conn.close()


def ipo_pnl(ipo: dict) -> dict:
    offer = from_micro(ipo["offer_price"])
    qty = ipo["allot_qty"] or 0
    fee = from_micro(ipo["fee"] or 0)
    if qty and ipo["sell_price"]:
        gross = (from_micro(ipo["sell_price"]) - offer) * qty
    else:
        gross = Decimal("0")
    return {"gross": gross, "net": gross - fee, "fee": fee, "qty": qty}


def ipo_stats() -> dict:
    rows = list_ipos()
    total_net = sum((ipo_pnl(r)["net"] for r in rows), Decimal("0"))
    total_fee = sum((ipo_pnl(r)["fee"] for r in rows), Decimal("0"))
    hits = len([r for r in rows if r["allot_qty"]])
    return {
        "count": len(rows), "hits": hits,
        "hit_rate": (hits / len(rows)) if rows else 0.0,
        "net": total_net, "fee": total_fee,
        "net_cny": to_cny(total_net, "HKD"),
    }
