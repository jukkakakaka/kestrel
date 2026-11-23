"""Trade journal, positions and the statistics that matter."""

from __future__ import annotations

from decimal import Decimal

from kestrel.core.clock import iso, utcnow
from kestrel.core.money import FX_TO_CNY, from_micro, to_micro
from kestrel.core.money import to_cny as _to_cny
from kestrel.core.quantity import db_value, display
from kestrel.db.engine import connect, transaction
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
    """Close one open lot through the same accounting path as manual fills."""
    from kestrel.services import execution
    from kestrel.services.fills import estimate_fees

    conn = connect()
    try:
        with transaction(conn, immediate=True):
            trade = conn.execute("SELECT * FROM trade WHERE id=? AND status='open'", (trade_id,)).fetchone()
            if trade is None:
                return None
            brk = broker or config_service.get(conn, f"broker.{trade['market']}")
            sell_qty = trade["qty"] if qty in (None, "") else qty
            if fees_override in (None, ""):
                try:
                    fees_override = estimate_fees(trade["market"], exit_price, sell_qty, brk, "sell")
                except ArithmeticError:
                    return {"ok": False, "error": "请核对成交价格与数量"}
            result = execution.record(execution.Fill(
                market=trade["market"], code=trade["code"], name=trade["name"], side="sell",
                qty=sell_qty, price=exit_price, fees=fees_override, broker=brk, trade_id=trade_id,
                note=review, rule_break=rule_break, exit_reason=exit_reason, confirmed=True,
            ), conn=conn)
            if not result["ok"]:
                return result
            return dict(conn.execute("SELECT * FROM trade WHERE id=?", (result["closed_trades"][0],)).fetchone())
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
