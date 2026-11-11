"""退出管理：决定什么时候卖，比决定什么时候买更影响结果。

买入只有一次，卖出贯穿整笔交易。新手最常见的两种亏法都在这里：

* 涨了不肯走，回到成本才跑；
* 跌破止损不肯认，一路拿到无法收拾。

所以这个模块把「该不该动」变成三个可执行的规则，每个都给出具体价位：

1. **移动止损** —— 按 R 倍数推进。+1R 把止损抬到保本，+2R 抬到 +1R。
   移动止损只能往有利方向走，永远不下移——下移止损等于取消止损。
2. **分批止盈** —— 到 T1 卖一半，到 T2 清掉剩余。剩下的用移动止损跟。
3. **时间止损** —— 超过该周期的持仓上限还没走出 +1R，说明这笔的前提没成立，
   占着资金和注意力比亏损更贵。

每个建议都带 **理由和具体价格**，用户点一下就能采纳；采纳会写进 stop_change
留痕，复盘时能看出当时是纪律还是临场改主意。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal

from kestrel.core.money import from_micro, to_micro
from kestrel.db.engine import connect

log = logging.getLogger(__name__)

# 移动止损的推进台阶：赚到这么多 R，止损就该抬到哪里。
STOP_LADDER: tuple[tuple[float, float], ...] = (
    (2.0, 1.0),   # +2R -> 止损抬到 +1R
    (1.0, 0.0),   # +1R -> 止损抬到保本
)

TIME_STOP_R = 1.0


@dataclass(slots=True)
class ExitPlan:
    trade_id: int
    action: str            # move_stop / take_partial / exit_all / hold
    stop: Decimal | None
    qty: Decimal | None
    reason_zh: str
    reason_en: str
    urgency: str = "normal"  # urgent / soon / normal

    def as_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "action": self.action,
            "stop": float(self.stop) if self.stop is not None else None,
            "qty": self.qty,
            "reason_zh": self.reason_zh,
            "reason_en": self.reason_en,
            "urgency": self.urgency,
        }


def _basis(trade: dict) -> tuple[Decimal, Decimal] | None:
    """这笔交易最初承担的风险：(入场价, 每股风险)。

    用 ``initial_stop`` 而不是当前止损：止损上移后，风险基数不能跟着变，
    否则 +1R 这个说法会自我循环。
    """
    entry = from_micro(trade["entry_price"])
    raw = trade.get("initial_stop") or trade.get("stop_price")
    if not raw:
        return None
    stop = from_micro(raw)
    risk = entry - stop
    if entry <= 0 or risk <= 0:
        return None
    return entry, risk


def _plan_from_snapshot(trade: dict) -> dict:
    try:
        return json.loads(trade.get("plan_snapshot") or "{}") or {}
    except (TypeError, ValueError):
        return {}


def suggest(trade: dict, position: dict, max_hold_days: int | None = None) -> ExitPlan | None:
    """给一笔持仓算出唯一一个「现在最该做的动作」。

    只返回一个，因为同时给三条建议等于没建议。优先级：
    跌破止损 > 到目标 > 移动止损 > 时间止损 > 无动作。
    """
    price = position.get("price")
    if price is None:
        return None
    price = Decimal(str(price))

    basis = _basis(trade)
    current_stop = from_micro(trade["stop_price"]) if trade.get("stop_price") else None
    target = from_micro(trade["target_price"]) if trade.get("target_price") else None
    entry = from_micro(trade["entry_price"])
    qty = int(trade["qty"])

    plan = _plan_from_snapshot(trade)
    currency = trade.get("currency") or "CNY"

    def money(value: Decimal) -> str:
        digits = 3 if currency in ("HKD", "USD") else 2
        return f"{value:,.{digits}f}"

    # 1) 已跌破止损：唯一的动作是认错
    if current_stop and price <= current_stop:
        return ExitPlan(
            trade_id=trade["id"],
            action="exit_all",
            stop=None,
            qty=Decimal(qty),
            reason_zh=f"现价 {money(price)} 已跌破止损 {money(current_stop)}，按计划应当离场。",
            reason_en=(
                f"Price {money(price)} is below the stop at {money(current_stop)}. "
                "The plan says exit."
            ),
            urgency="urgent",
        )

    if basis is None:
        return None
    _entry, risk = basis
    r_now = float((price - entry) / risk)

    # 2) 到达目标：分批止盈
    if target and price >= target:
        keep = int(qty // 2) if qty >= 2 else 0
        return ExitPlan(
            trade_id=trade["id"],
            action="take_partial",
            stop=current_stop,
            qty=Decimal(qty - keep),
            reason_zh=(
                f"现价 {money(price)} 已到达目标 {money(target)}。"
                f"按计划先卖出一半以上（{qty - keep} 股），剩余的用移动止损跟。"
            ),
            reason_en=(
                f"Price {money(price)} reached the target {money(target)}. "
                f"Sell at least half ({qty - keep} shares) and trail the rest."
            ),
            urgency="soon",
        )

    # 3) 移动止损：只往上抬，绝不下移
    if current_stop is not None:
        for trigger_r, lock_r in STOP_LADDER:
            if r_now < trigger_r:
                continue
            proposed = entry + risk * Decimal(str(lock_r))
            digits = Decimal("0.01") if currency == "CNY" else Decimal("0.001")
            proposed = proposed.quantize(digits)
            if proposed <= current_stop:
                break
            if lock_r == 0:
                zh = (
                    f"已经赚到 {r_now:+.1f}R。把止损从 {money(current_stop)} 抬到 "
                    f"{money(proposed)}（保本），这笔最差也就是不赚钱。"
                )
                en = (
                    f"Up {r_now:+.1f}R. Move the stop from {money(current_stop)} to "
                    f"{money(proposed)} — break-even. The worst case is now a scratch."
                )
            else:
                zh = (
                    f"已经赚到 {r_now:+.1f}R。把止损从 {money(current_stop)} 抬到 "
                    f"{money(proposed)}，锁定 {lock_r:+.0f}R 的利润。"
                )
                en = (
                    f"Up {r_now:+.1f}R. Move the stop from {money(current_stop)} to "
                    f"{money(proposed)} to lock in {lock_r:+.0f}R."
                )
            return ExitPlan(
                trade_id=trade["id"],
                action="move_stop",
                stop=proposed,
                qty=None,
                reason_zh=zh,
                reason_en=en,
                urgency="normal",
            )

    # 4) 时间止损：占着资金不动，比亏钱更贵
    held = position.get("held_days")
    if max_hold_days and held is not None and held > max_hold_days and r_now < TIME_STOP_R:
        return ExitPlan(
            trade_id=trade["id"],
            action="exit_all",
            stop=None,
            qty=Decimal(qty),
            reason_zh=(
                f"已经持有 {held} 天，超过这个周期通常的持仓上限（{max_hold_days} 天），"
                f"而且只走到 {r_now:+.1f}R。这笔的前提没有成立，资金应该换到更好的地方。"
            ),
            reason_en=(
                f"Held {held} days, past the usual {max_hold_days}-day limit for this horizon, "
                f"and only {r_now:+.1f}R to show for it. The thesis did not work; the capital "
                "belongs somewhere better."
            ),
            urgency="soon",
        )

    if plan.get("valid_until") and position.get("overdue"):
        return ExitPlan(
            trade_id=trade["id"],
            action="exit_all",
            stop=None,
            qty=Decimal(qty),
            reason_zh="这条计划的原始有效期已过，价格条件已经变了。要么重新评估，要么离场。",
            reason_en=(
                "The original plan has expired, so the price context it assumed is gone. "
                "Re-assess or exit."
            ),
            urgency="soon",
        )

    return None


def apply_stop(trade_id: int, new_stop: Decimal, reason: str = "") -> dict:
    """采纳一条移动止损建议。

    只允许朝有利方向移动：把止损往下调等于取消止损，那正是这个工具要防止的行为。
    """
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM trade WHERE id=?", (trade_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "没有这笔持仓"}
        trade = dict(row)
        if trade["status"] != "open":
            return {"ok": False, "error": "这笔已经平仓了"}

        old = trade["stop_price"]
        new_micro = to_micro(new_stop)
        if old is not None and new_micro <= old:
            return {
                "ok": False,
                "error": (
                    f"新止损 {new_stop} 不高于当前止损 {from_micro(old)}。"
                    "止损只能往有利方向移动，往下调等于取消止损。"
                ),
            }
        entry = from_micro(trade["entry_price"])
        # 正好等于入场价是「保本止损」，这是移动止损最常用的第一档，
        # 不能和「把止损设在入场价之上」混为一谈。
        if new_micro > to_micro(entry):
            return {
                "ok": False,
                "error": "止损不能设在入场价之上——那不是止损，是提前卖出。",
            }

        if trade.get("initial_stop") is None and old is not None:
            conn.execute("UPDATE trade SET initial_stop=? WHERE id=?", (old, trade_id))
        conn.execute(
            "UPDATE trade SET stop_price=?, stop_updated_at=datetime('now') WHERE id=?",
            (new_micro, trade_id),
        )
        conn.execute(
            "INSERT INTO stop_change (trade_id, old_stop, new_stop, reason) VALUES (?,?,?,?)",
            (trade_id, old, new_micro, reason or None),
        )
        conn.commit()
        return {
            "ok": True,
            "trade_id": trade_id,
            "old_stop": float(from_micro(old)) if old is not None else None,
            "new_stop": float(new_stop),
        }
    finally:
        conn.close()


def stop_history(trade_id: int) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM stop_change WHERE trade_id=? ORDER BY id", (trade_id,)
        ).fetchall()
        return [
            {
                "old_stop": float(from_micro(r["old_stop"])) if r["old_stop"] else None,
                "new_stop": float(from_micro(r["new_stop"])),
                "reason": r["reason"],
                "at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def plans_for_open_positions(max_hold_days: int | None = None) -> dict[int, dict]:
    """把建议批量算出来给页面用：``{trade_id: ExitPlan}``。"""
    from kestrel.services import journal, positions

    trades = {t["id"]: t for t in journal.list_trades(status="open", limit=500)}
    out: dict[int, dict] = {}
    for position in positions.open_positions():
        trade = trades.get(position["trade_id"])
        if trade is None:
            continue
        plan = suggest(trade, position, max_hold_days=max_hold_days)
        if plan is not None:
            out[position["trade_id"]] = plan.as_dict()
    return out
