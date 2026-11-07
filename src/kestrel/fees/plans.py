"""Broker fee plans that the user can edit from the UI.

The built-in table lives in ``calculator.PLANS``; this module copies it into the
database on first use so the numbers become data instead of code. Editing a plan
in the UI must change what the calculator charges, otherwise the screen is a lie.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from kestrel.db.engine import connect
from kestrel.fees.calculator import PLANS, BrokerPlan

log = logging.getLogger(__name__)

FIELDS = (
    "commission_rate",
    "commission_min",
    "platform_fixed",
    "platform_per_share",
    "platform_min",
)


def seed() -> int:
    """Copy built-ins into the database once. Existing rows are left alone."""
    conn = connect()
    added = 0
    try:
        existing = {
            r["key"] for r in conn.execute("SELECT key FROM broker_plan").fetchall()
        }
        for key, plan in PLANS.items():
            if key in existing:
                continue
            currency = "HKD" if plan.market == "HK" else "CNY" if plan.market == "CN" else "USD"
            conn.execute(
                """
                INSERT OR IGNORE INTO broker_plan
                    (key, label, market, currency, commission, commission_min,
                     platform, platform_min, platform_per_share, per_order_min,
                     builtin, enabled)
                VALUES (?,?,?,?,?,?,?,?,?,?,1,1)
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


def upsert(payload: dict) -> str:
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
    values = []
    for name in FIELDS:
        try:
            values.append(max(0.0, float(payload.get(name) or 0)))
        except (TypeError, ValueError):
            raise ValueError(f"{name} 必须是数字") from None

    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO broker_plan
                (key, label, market, currency, commission, commission_min,
                 platform, platform_min, platform_per_share, per_order_min,
                 builtin, enabled, notes, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,1,?,datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                label=excluded.label, market=excluded.market, currency=excluded.currency,
                commission=excluded.commission, commission_min=excluded.commission_min,
                platform=excluded.platform, platform_min=excluded.platform_min,
                platform_per_share=excluded.platform_per_share,
                per_order_min=excluded.per_order_min,
                notes=excluded.notes, updated_at=datetime('now')
            """,
            (key, label, market, currency, *values, str(payload.get("notes") or "")),
        )
        conn.commit()
    finally:
        conn.close()
    return key


def delete(key: str) -> bool:
    conn = connect()
    try:
        row = conn.execute("SELECT builtin FROM broker_plan WHERE key=?", (key,)).fetchone()
        if row is None:
            return False
        if row["builtin"]:
            # never delete a built-in; disable it instead
            conn.execute("UPDATE broker_plan SET enabled=0 WHERE key=?", (key,))
        else:
            conn.execute("DELETE FROM broker_plan WHERE key=?", (key,))
        conn.commit()
        return True
    finally:
        conn.close()


def hydrate() -> int:
    """Load every enabled plan into the fee engine's registry.

    The calculator must stay database-free (it is called inside plan building),
    so edited plans are pushed into its in-memory table at startup.
    """
    from kestrel.fees import calculator

    seed()
    count = 0
    for row in list_plans():
        if not row["enabled"]:
            continue
        calculator.PLANS[row["key"]] = BrokerPlan(
            code=row["key"],
            name=row["label"],
            market=row["market"],
            commission_rate=Decimal(str(row["commission"])),
            commission_min=Decimal(str(row["commission_min"])),
            platform_fixed=Decimal(str(row["platform"])),
            platform_per_share=Decimal(str(row["platform_per_share"])),
            platform_min=Decimal(str(row["platform_min"])),
        )
        count += 1
    return count


def as_broker_plan(key: str) -> BrokerPlan | None:
    """Load a plan in the shape the fee engine expects, honouring UI edits."""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM broker_plan WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return PLANS.get(key)
    return BrokerPlan(
        code=row["key"],
        name=row["label"],
        market=row["market"],
        commission_rate=Decimal(str(row["commission"])),
        commission_min=Decimal(str(row["commission_min"])),
        platform_fixed=Decimal(str(row["platform"])),
        platform_per_share=Decimal(str(row["platform_per_share"])),
        platform_min=Decimal(str(row["platform_min"])),
    )
