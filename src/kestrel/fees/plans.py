"""支持在界面中自定义与编辑的券商费用套餐服务。"""

from __future__ import annotations

import logging
from decimal import Decimal

from kestrel.db.engine import connect
from kestrel.fees.calculator import _BUILTIN_PLANS, PLANS, BrokerPlan

log = logging.getLogger(__name__)


def seed() -> int:
    """将内置券商套餐写入数据库（仅补齐尚未存在的套餐）。"""
    conn = connect()
    added = 0
    try:
        existing = {
            r["key"] for r in conn.execute("SELECT key FROM broker_plan").fetchall()
        }
        for key, plan in _BUILTIN_PLANS.items():
            if key in existing:
                continue
            currency = "HKD" if plan.market == "HK" else "CNY" if plan.market == "CN" else "USD"
            conn.execute(
                """
                INSERT OR IGNORE INTO broker_plan
                    (key, label, market, currency, commission, commission_min,
                     platform, platform_min, platform_per_share, per_order_min,
                     builtin, enabled, odd_lot_buy, fractional_us, fractional_min)
                VALUES (?,?,?,?,?,?,?,?,?,?,1,1,?,?,?)
                """,
                (
                    key,
                    plan.name,
                    plan.market,
                    currency,
                    float(plan.commission_rate),
                    float(plan.commission_min),
                    float(plan.platform_fixed),
                    float(plan.platform_min),
                    float(plan.platform_per_share),
                    0.0,
                    int(plan.odd_lot_buy),
                    int(plan.fractional_us),
                    float(plan.fractional_min),
                ),
            )
            added += 1
        conn.commit()
    finally:
        conn.close()
    return added


def list_plans(market: str | None = None) -> list[dict]:
    seed()
    conn = connect()
    try:
        if market:
            rows = conn.execute(
                "SELECT * FROM broker_plan WHERE market=? ORDER BY enabled DESC, label",
                (market.upper(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM broker_plan ORDER BY market, enabled DESC, label"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _num_from(payload: dict, *keys: str) -> float:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            try:
                return max(0.0, float(payload[key]))
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是数字") from None
    return 0.0


def upsert(payload: dict) -> str:
    """新建或更新券商套餐，并立即同步刷新内存中的费率字典。"""
    label = str(payload.get("label") or "").strip()
    market = str(payload.get("market") or "").strip().upper()
    if not label:
        raise ValueError("名称不能为空")
    if market not in ("HK", "US", "CN"):
        raise ValueError("市场必须是 HK / US / CN")

    key = str(payload.get("key") or "").strip()
    if not key:
        slug = "".join(ch if ch.isalnum() else "_" for ch in label.lower())[:24]
        key = f"custom_{market.lower()}_{slug or 'plan'}"

    currency = {"HK": "HKD", "US": "USD", "CN": "CNY"}[market]
    # 兼容前端键名（commission / platform）与接口语义键名（commission_rate / platform_fixed）
    commission = _num_from(payload, "commission", "commission_rate")
    commission_min = _num_from(payload, "commission_min")
    platform = _num_from(payload, "platform", "platform_fixed")
    platform_min = _num_from(payload, "platform_min")
    platform_per_share = _num_from(payload, "platform_per_share")
    per_order_min = _num_from(payload, "per_order_min")
    fractional_min = _num_from(payload, "fractional_min")

    # A 股固定整手，不允许通过券商开关修改
    odd_lot = bool(payload.get("odd_lot_buy")) and market == "HK"
    fractional = bool(payload.get("fractional_us")) and market == "US"

    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO broker_plan
                (key, label, market, currency, commission, commission_min,
                 platform, platform_min, platform_per_share, per_order_min,
                 builtin, enabled, notes, updated_at,
                 odd_lot_buy, fractional_us, fractional_min)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,1,?,datetime('now'),?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                label=excluded.label, market=excluded.market, currency=excluded.currency,
                commission=excluded.commission, commission_min=excluded.commission_min,
                platform=excluded.platform, platform_min=excluded.platform_min,
                platform_per_share=excluded.platform_per_share,
                per_order_min=excluded.per_order_min,
                odd_lot_buy=excluded.odd_lot_buy,
                fractional_us=excluded.fractional_us,
                fractional_min=excluded.fractional_min,
                enabled=1,
                notes=excluded.notes, updated_at=datetime('now')
            """,
            (
                key,
                label,
                market,
                currency,
                commission,
                commission_min,
                platform,
                platform_min,
                platform_per_share,
                per_order_min,
                str(payload.get("notes") or ""),
                int(odd_lot),
                int(fractional),
                fractional_min,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    hydrate()
    return key


def delete(key: str) -> bool:
    """删除自定义套餐，或停用内置套餐，并立即刷新内存费率字典。"""
    conn = connect()
    try:
        row = conn.execute("SELECT builtin FROM broker_plan WHERE key=?", (key,)).fetchone()
        if row is None:
            return False
        if row["builtin"]:
            conn.execute("UPDATE broker_plan SET enabled=0 WHERE key=?", (key,))
        else:
            conn.execute("DELETE FROM broker_plan WHERE key=?", (key,))
        conn.commit()
    finally:
        conn.close()
    hydrate()
    return True


def hydrate() -> int:
    """从数据库全量同步所有已启用的券商套餐到内存费率引擎。"""
    from kestrel.fees import calculator

    seed()
    rows = list_plans()
    refreshed: dict[str, BrokerPlan] = {}
    for row in rows:
        if not row["enabled"]:
            continue
        refreshed[row["key"]] = BrokerPlan(
            code=row["key"],
            name=row["label"],
            market=row["market"],
            commission_rate=Decimal(str(row["commission"])),
            commission_min=Decimal(str(row["commission_min"])),
            platform_fixed=Decimal(str(row["platform"])),
            platform_per_share=Decimal(str(row["platform_per_share"])),
            platform_min=Decimal(str(row["platform_min"])),
            odd_lot_buy=bool(row.get("odd_lot_buy")),
            fractional_us=bool(row.get("fractional_us")),
            fractional_min=Decimal(str(row.get("fractional_min") or 0)),
        )
    calculator.PLANS.clear()
    calculator.PLANS.update(refreshed)
    return len(refreshed)


def as_broker_plan(key: str) -> BrokerPlan | None:
    """读取单条套餐配置，优先使用数据库中的最新编辑。"""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM broker_plan WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return PLANS.get(key) or _BUILTIN_PLANS.get(key)

    def _value(name: str, default=0):
        try:
            return row[name]
        except (IndexError, KeyError):
            return default

    return BrokerPlan(
        code=row["key"],
        name=row["label"],
        market=row["market"],
        commission_rate=Decimal(str(row["commission"])),
        commission_min=Decimal(str(row["commission_min"])),
        platform_fixed=Decimal(str(row["platform"])),
        platform_per_share=Decimal(str(row["platform_per_share"])),
        platform_min=Decimal(str(row["platform_min"])),
        odd_lot_buy=bool(_value("odd_lot_buy")),
        fractional_us=bool(_value("fractional_us")),
        fractional_min=Decimal(str(_value("fractional_min") or 0)),
    )
