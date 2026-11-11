"""Trade journal, positions and the statistics that matter."""

from __future__ import annotations

from decimal import Decimal

from kestrel.core.clock import iso, utcnow
from kestrel.core.money import from_micro, to_micro
from kestrel.db.engine import connect
from kestrel.fees.calculator import quote_fees

FX = {"CNY": Decimal("1"), "HKD": Decimal("0.92"), "USD": Decimal("7.1")}


def to_cny(amount: Decimal, currency: str) -> Decimal:
    return amount * FX.get(currency, Decimal("1"))


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
        cur = conn.execute(
            """
            INSERT INTO trade (market, code, name, currency, qty, entry_price, stop_price,
                               target_price, strategy, rec_id, opened_at, fees)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                kw["market"].upper(), kw["code"], kw.get("name") or kw["code"],
                kw.get("currency") or {"CN": "CNY", "HK": "HKD", "US": "USD"}[kw["market"].upper()],
                int(kw["qty"]), to_micro(kw["entry_price"]),
                to_micro(kw["stop_price"]) if kw.get("stop_price") else None,
                to_micro(kw["target_price"]) if kw.get("target_price") else None,
                kw.get("strategy"), kw.get("rec_id"), kw.get("opened_at") or iso(utcnow()),
                to_micro(kw.get("fees") or 0),
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
    qty: int | None = None,
) -> dict | None:
    """平掉某一条持仓（可部分平仓）。

    卖出走的是同一套费用模型：买入时的费用已经在建仓时记过，这里补卖出侧。
    部分平仓不修改原记录的数量，而是复制出一条已平记录——历史持仓的建仓价
    和数量不该被后来的操作改写，否则复盘时看到的是一本假账。
    """
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM trade WHERE id=?", (trade_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None

    trade = dict(row)
    sell_qty = int(qty or trade["qty"])
    if sell_qty <= 0:
        return {"ok": False, "error": "卖出数量必须大于 0"}
    if sell_qty > int(trade["qty"]):
        return {"ok": False, "error": f"卖出数量超过持仓（{trade['qty']} 股）"}

    price = Decimal(str(exit_price))
    amount_out = price * sell_qty
    broker = None
    try:
        fee_sell = quote_fees(trade["market"], amount_out, Decimal(sell_qty), True, broker).total
    except Exception:  # noqa: BLE001
        fee_sell = Decimal("0")
    fee_share = (fee_sell * Decimal(sell_qty) / Decimal(trade["qty"])).quantize(Decimal("0.01"))

    conn = connect()
    try:
        if sell_qty == int(trade["qty"]):
            conn.execute(
                "UPDATE trade SET exit_price=?, closed_at=?, status='closed',"
                " fees=COALESCE(fees,0)+?, review=COALESCE(?,review), rule_break=? WHERE id=?",
                (
                    to_micro(price), iso(utcnow()), to_micro(fee_share),
                    review or None, 1 if rule_break else 0, trade_id,
                ),
            )
            closed_id = trade_id
        else:
            conn.execute("UPDATE trade SET qty=qty-? WHERE id=?", (sell_qty, trade_id))
            cur = conn.execute(
                """
                INSERT INTO trade (market, code, name, currency, side, qty, entry_price,
                                   exit_price, fees, opened_at, filled_at, closed_at,
                                   status, rec_id, review, rule_break, source, plan_snapshot,
                                   stop_price, target_price)
                SELECT market, code, name, currency, side, ?, entry_price, ?, ?,
                       opened_at, filled_at, ?, 'closed', rec_id, ?, ?, source, plan_snapshot,
                       stop_price, target_price
                FROM trade WHERE id=?
                """,
                (
                    sell_qty, to_micro(price), to_micro(fee_share), iso(utcnow()),
                    review or None, 1 if rule_break else 0, trade_id,
                ),
            )
            closed_id = cur.lastrowid
        conn.commit()
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
                "qty": t["qty"], "entry": from_micro(t["entry_price"]),
                "stop": from_micro(t["stop_price"]) if t["stop_price"] else None,
                "target": from_micro(t["target_price"]) if t["target_price"] else None,
                "currency": t["currency"], "opened_at": t["opened_at"],
                "fees": from_micro(t["fees"] or 0),
                "source": t.get("source") or "manual",
                "rec_id": t.get("rec_id"),
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
