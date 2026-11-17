"""动态资金账本与资产/持仓手动校准服务。

核心原则：
1. 用户填写的 ``account.capital`` 是**初始资金**，不是写死的常数。
2. 每一笔确认的买入与卖出都会实时更新「可用现金」「持仓市值」与「账户总资产」：
   ``可用现金 = 初始资金 + 资金校准/出入金净额 + 已实现净盈亏 - 持仓占用成本(含买费)``
   ``总资产   = 可用现金 + 当前持仓市值``
3. 允许用户随时手动校准**资金总量（可用现金/总资产/银证转入转出）**与**手头股票
   （修改股数、成本均价、止损目标，或补录已有底仓）**。
"""

from __future__ import annotations

import logging
from decimal import Decimal

from kestrel.core.clock import iso, utcnow
from kestrel.core.money import currency_for, from_micro, to_cny, to_micro
from kestrel.core.quantity import db_value, display, to_qty
from kestrel.db.engine import connect, transaction
from kestrel.services import config as config_service

log = logging.getLogger(__name__)


def _latest_prices(conn, keys: list[tuple[str, str]]) -> dict[str, Decimal]:
    if not keys:
        return {}
    prices: dict[str, Decimal] = {}
    for market, code in set(keys):
        row = conn.execute(
            """
            SELECT q.price FROM quote_latest q
            JOIN symbol s ON s.id = q.symbol_id
            WHERE s.market=? AND s.code=?
            """,
            (market, code),
        ).fetchone()
        if row and row["price"]:
            prices[f"{market}:{code}"] = from_micro(row["price"])
    return prices


def snapshot(conn=None, include_quotes: bool = True, limit_ledger: int = 15) -> dict:
    """计算当前账户资金、持仓市值、总资产及最近校准流水的完整快照。"""
    own = conn is None
    if own:
        conn = connect()
    try:
        initial_raw = config_service.get(conn, "account.capital", 50000.0)
        initial_cny = Decimal(str(initial_raw or 0))
        exposure_cap_pct = float(
            config_service.get(conn, "risk.max_total_exposure_pct", 60.0) or 60.0
        )

        # 1. 资金流水（出入金、手工校准、底仓对齐对冲）
        ledger_rows = conn.execute(
            "SELECT id, kind, amount_micro, note, created_at FROM cash_ledger"
            " ORDER BY id DESC LIMIT ?",
            (max(1, limit_ledger),),
        ).fetchall()
        sums = conn.execute(
            "SELECT kind, COALESCE(SUM(amount_micro), 0) AS s FROM cash_ledger GROUP BY kind"
        ).fetchall()
        kind_sums = {r["kind"]: from_micro(r["s"]) for r in sums}
        adjustments_cny = sum(kind_sums.values(), Decimal("0"))
        contributed_cny = (
            initial_cny
            + kind_sums.get("deposit", Decimal("0"))
            + kind_sums.get("withdraw", Decimal("0"))
            + kind_sums.get("position_sync", Decimal("0"))
        )

        # 2. 已平仓交易净盈亏（统一折算为 CNY）
        closed_rows = conn.execute(
            "SELECT market, currency, qty, entry_price, exit_price, fees"
            " FROM trade WHERE status='closed' AND exit_price IS NOT NULL"
        ).fetchall()
        realized_cny = Decimal("0")
        for r in closed_rows:
            qty = Decimal(str(r["qty"] or 0))
            entry = from_micro(r["entry_price"])
            exit_p = from_micro(r["exit_price"])
            fees = from_micro(r["fees"] or 0)
            net_local = (exit_p - entry) * qty - fees
            realized_cny += to_cny(net_local, r["currency"] or currency_for(r["market"]))

        # 3. 未平仓持仓（成本占用 + 当前市值 + 浮动盈亏）
        open_rows = conn.execute(
            "SELECT id, market, code, name, currency, qty, entry_price, stop_price,"
            " target_price, fees, source, filled_at, opened_at"
            " FROM trade WHERE status='open' ORDER BY id DESC"
        ).fetchall()
        price_map = (
            _latest_prices(conn, [(r["market"], r["code"]) for r in open_rows])
            if include_quotes
            else {}
        )

        invested_cost_cny = Decimal("0")
        market_value_cny = Decimal("0")
        unrealized_cny = Decimal("0")
        unpriced = 0
        positions: list[dict] = []

        for r in open_rows:
            cur = r["currency"] or currency_for(r["market"])
            qty = Decimal(str(r["qty"] or 0))
            entry = from_micro(r["entry_price"])
            fees = from_micro(r["fees"] or 0)
            cost_local = entry * qty + fees
            invested_cost_cny += to_cny(cost_local, cur)

            live = price_map.get(f"{r['market']}:{r['code']}")
            if live is None:
                unpriced += 1
                val_price = entry
            else:
                val_price = live
            mv_local = val_price * qty
            mv_cny = to_cny(mv_local, cur)
            market_value_cny += mv_cny
            pnl_local = (val_price - entry) * qty - fees
            unrealized_cny += to_cny(pnl_local, cur)

            positions.append(
                {
                    "id": r["id"],
                    "market": r["market"],
                    "code": r["code"],
                    "name": r["name"],
                    "currency": cur,
                    "qty": display(qty),
                    "qty_num": float(qty),
                    "entry": float(entry),
                    "price": float(live) if live is not None else None,
                    "stop": float(from_micro(r["stop_price"])) if r["stop_price"] else None,
                    "target": float(from_micro(r["target_price"])) if r["target_price"] else None,
                    "fees": float(fees),
                    "market_value_cny": float(mv_cny),
                    "source": r["source"] or "manual",
                }
            )

        available_cash_cny = initial_cny + adjustments_cny + realized_cny - invested_cost_cny
        total_equity_cny = available_cash_cny + market_value_cny
        total_return_cny = (
            realized_cny + unrealized_cny + kind_sums.get("calibration", Decimal("0"))
        )
        base_for_pct = contributed_cny if contributed_cny > 0 else initial_cny
        total_return_pct = (
            float(total_return_cny / base_for_pct * 100) if base_for_pct > 0 else 0.0
        )
        exposure_pct = (
            float(market_value_cny / total_equity_cny * 100)
            if total_equity_cny > 0
            else 0.0
        )

        return {
            "initial_capital_cny": float(initial_cny),
            "adjustments_cny": float(adjustments_cny),
            "contributed_cny": float(contributed_cny),
            "realized_pnl_cny": float(realized_cny),
            "unrealized_pnl_cny": float(unrealized_cny),
            "invested_cost_cny": float(invested_cost_cny),
            "market_value_cny": float(market_value_cny),
            "available_cash_cny": float(available_cash_cny),
            "total_equity_cny": float(total_equity_cny),
            "total_return_cny": float(total_return_cny),
            "total_return_pct": round(total_return_pct, 2),
            "exposure_pct": round(exposure_pct, 1),
            "exposure_cap_pct": exposure_cap_pct,
            "open_count": len(positions),
            "unpriced": unpriced,
            "positions": positions,
            "ledger": [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "amount_cny": float(from_micro(row["amount_micro"])),
                    "note": row["note"] or "",
                    "created_at": row["created_at"],
                }
                for row in ledger_rows
            ],
        }
    finally:
        if own:
            conn.close()


def calibrate_cash(
    *,
    target_available_cny: Decimal | float | str | None = None,
    target_equity_cny: Decimal | float | str | None = None,
    delta_cny: Decimal | float | str | None = None,
    initial_capital_cny: Decimal | float | str | None = None,
    kind: str = "calibration",
    note: str = "",
) -> dict:
    """手动校准资金总量（可用现金 / 总资产 / 出入金 / 初始本金）。"""
    conn = connect()
    try:
        with transaction(conn):
            if initial_capital_cny not in (None, ""):
                cap = Decimal(str(initial_capital_cny))
                if cap <= 0:
                    return {"ok": False, "error": "初始资金必须大于 0"}
                config_service.put(conn, "account.capital", float(cap))

            snap = snapshot(conn=conn, include_quotes=True)
            diff: Decimal | None = None
            entry_kind = kind if kind in ("deposit", "withdraw", "calibration") else "calibration"

            if target_available_cny not in (None, ""):
                target_avail = Decimal(str(target_available_cny))
                diff = target_avail - Decimal(str(snap["available_cash_cny"]))
                entry_kind = "calibration"
                note = note or f"校准可用现金至 ¥{target_avail:,.2f}"
            elif target_equity_cny not in (None, ""):
                target_eq = Decimal(str(target_equity_cny))
                target_avail = target_eq - Decimal(str(snap["market_value_cny"]))
                diff = target_avail - Decimal(str(snap["available_cash_cny"]))
                entry_kind = "calibration"
                note = note or f"校准账户总资产至 ¥{target_eq:,.2f}"
            elif delta_cny not in (None, ""):
                diff = Decimal(str(delta_cny))
                if entry_kind == "withdraw" and diff > 0:
                    diff = -diff
                if entry_kind == "deposit" and diff < 0:
                    diff = abs(diff)
                note = note or ("银证转入" if diff >= 0 else "银证转出")

            if diff is not None and abs(diff) >= Decimal("0.01"):
                conn.execute(
                    "INSERT INTO cash_ledger (kind, amount_micro, note) VALUES (?,?,?)",
                    (entry_kind, to_micro(diff), note or None),
                )
            conn.execute(
                "INSERT INTO audit_log (action, target, detail) VALUES ('portfolio.calibrate_cash', ?, ?)",
                (entry_kind, note or str(diff or initial_capital_cny or "")),
            )
        after = snapshot(conn=conn, include_quotes=True)
        return {"ok": True, "delta_cny": float(diff or 0), "snapshot": after}
    except Exception as exc:  # noqa: BLE001
        log.exception("calibrate cash failed")
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def calibrate_position(
    trade_id: int | None = None,
    *,
    market: str = "",
    code: str = "",
    name: str = "",
    qty: Decimal | float | str = 0,
    entry_price: Decimal | float | str = 0,
    stop_price: Decimal | float | str | None = None,
    target_price: Decimal | float | str | None = None,
    affect_cash: bool = False,
    note: str = "",
) -> dict:
    """手动校准已有持仓（修改/清零）或补录手头已有股票底仓。"""
    from kestrel.datasources.base import default_lot_size, normalize_code
    from kestrel.services import fills as fill_service

    new_qty = to_qty(qty)
    try:
        new_entry = Decimal(str(entry_price or 0))
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "成本价必须填数字"}
    new_stop = Decimal(str(stop_price)) if stop_price not in (None, "") else None
    new_target = Decimal(str(target_price)) if target_price not in (None, "") else None

    if new_qty < 0:
        return {"ok": False, "error": "持仓数量不能为负数"}
    if new_qty > 0 and new_entry <= 0:
        return {"ok": False, "error": "持仓成本价必须大于 0"}
    if new_stop is not None and new_qty > 0 and new_stop >= new_entry:
        return {"ok": False, "error": "止损价必须低于成本价"}

    conn = connect()
    try:
        with transaction(conn):
            if trade_id is not None:
                row = conn.execute(
                    "SELECT * FROM trade WHERE id=? AND status='open'", (trade_id,)
                ).fetchone()
                if row is None:
                    return {"ok": False, "error": "找不到该未平仓持仓"}
                old_qty = Decimal(str(row["qty"]))
                old_entry = from_micro(row["entry_price"])
                old_fees = from_micro(row["fees"] or 0)
                cur = row["currency"] or currency_for(row["market"])
                old_cost_cny = to_cny(old_entry * old_qty + old_fees, cur)

                if new_qty == 0:
                    conn.execute("DELETE FROM trade WHERE id=?", (trade_id,))
                    cost_diff_cny = -old_cost_cny
                    msg = f"已移除持仓 {row['name']} ({row['code']})"
                else:
                    new_cost_cny = to_cny(new_entry * new_qty + old_fees, cur)
                    cost_diff_cny = new_cost_cny - old_cost_cny
                    stop_micro = to_micro(new_stop) if new_stop is not None else row["stop_price"]
                    target_micro = (
                        to_micro(new_target) if new_target is not None else row["target_price"]
                    )
                    initial_stop = row["initial_stop"] or stop_micro
                    conn.execute(
                        """
                        UPDATE trade SET qty=?, entry_price=?, stop_price=?, initial_stop=?,
                                         target_price=?, review=COALESCE(?, review)
                        WHERE id=?
                        """,
                        (
                            db_value(new_qty),
                            to_micro(new_entry),
                            stop_micro,
                            initial_stop,
                            target_micro,
                            note or None,
                            trade_id,
                        ),
                    )
                    msg = f"已校准 {row['name']}：{display(new_qty)} 股 @ {new_entry}"

                # 默认仅校准底仓账面，不对可用现金产生冲击
                if not affect_cash and abs(cost_diff_cny) >= Decimal("0.01"):
                    conn.execute(
                        "INSERT INTO cash_ledger (kind, amount_micro, note) VALUES (?,?,?)",
                        (
                            "position_sync",
                            to_micro(cost_diff_cny),
                            note or f"持仓校准对冲 {row['code']}",
                        ),
                    )
            else:
                target = fill_service.resolve_target(code, market or None, name=name)
                if not target.code:
                    return {"ok": False, "error": f"无法识别股票代码「{code}」，请指定市场后重试"}
                if new_qty <= 0:
                    return {"ok": False, "error": "补录底仓数量必须大于 0"}
                mkt = target.market.upper()
                norm_code = normalize_code(mkt, target.code)
                cur = currency_for(mkt)
                lot = default_lot_size(mkt, norm_code)
                conn.execute(
                    "INSERT OR IGNORE INTO symbol (market, code, name, currency, lot_size)"
                    " VALUES (?,?,?,?,?)",
                    (mkt, norm_code, target.name or norm_code, cur, lot),
                )
                now = iso(utcnow())
                stop_micro = to_micro(new_stop) if new_stop else None
                target_micro = to_micro(new_target) if new_target else None
                insert_cur = conn.execute(
                    """
                    INSERT INTO trade (market, code, name, currency, side, qty, entry_price,
                                       stop_price, initial_stop, target_price, fees, entry_fees,
                                       opened_at, filled_at, status, review, source)
                    VALUES (?,?,?,?,'long',?,?,?, ?,?,0,0, ?,?,'open',?,'calibrated')
                    """,
                    (
                        mkt,
                        norm_code,
                        target.name or norm_code,
                        cur,
                        db_value(new_qty),
                        to_micro(new_entry),
                        stop_micro,
                        stop_micro,
                        target_micro,
                        now,
                        now,
                        note or "手动补录已有持仓",
                    ),
                )
                trade_id = insert_cur.lastrowid
                cost_cny = to_cny(new_entry * new_qty, cur)
                if not affect_cash and cost_cny >= Decimal("0.01"):
                    conn.execute(
                        "INSERT INTO cash_ledger (kind, amount_micro, note) VALUES (?,?,?)",
                        (
                            "position_sync",
                            to_micro(cost_cny),
                            note or f"补录已有底仓 {mkt}:{norm_code}",
                        ),
                    )
                msg = f"已补录持仓 {target.name or norm_code}：{display(new_qty)} 股 @ {new_entry}"

            conn.execute(
                "INSERT INTO audit_log (action, target, detail) VALUES ('portfolio.calibrate_pos', ?, ?)",
                (str(trade_id or code), msg),
            )
        after = snapshot(conn=conn, include_quotes=True)
        return {"ok": True, "trade_id": trade_id, "message": msg, "snapshot": after}
    except Exception as exc:  # noqa: BLE001
        log.exception("calibrate position failed")
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()
