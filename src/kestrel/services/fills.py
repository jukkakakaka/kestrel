"""手动记一笔成交时，把用户没填的东西算出来。

用户的原话是「我为什么要填这么多」。这个模块的回答是：真的只需要代码、
方向、数量、价格四件事，剩下的都能推：

* **市场**——代码本身就说明了（6 位数字是 A 股，5 位以内是港股，字母是美股），
  只有歧义时才需要用户指定；
* **名称**——数据库里有就用数据库的，没有就去解析一次；
* **手续费**——按他选的券商套餐现算，允许覆盖；
* **止损 / 目标**——如果这只票有 K 线，用和扫描器同一套 ATR 逻辑给出建议，
  并说明这是建议值；没有数据就明说算不出来，不会编一个数字。

这里所有函数都不写数据库以外的状态，方便直接单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from kestrel.datasources.base import guess_market, normalize_code
from kestrel.db.engine import connect
from kestrel.fees.calculator import quote_fees
from kestrel.indicators.engine import Series, compute

DEFAULT_HORIZON = "short"


@dataclass(slots=True)
class ResolvedFillTarget:
    market: str
    code: str
    name: str
    currency: str
    lot_size: int
    known: bool


def _looks_like(code: str) -> list[str]:
    """这个代码在哪些市场说得通。

    ``str.isalpha()`` 对中文也是 True，所以必须限定 ASCII 字母——否则
    「不存在的名字」会被当成美股代码写进台账。
    """
    raw = str(code or "").strip().upper()
    if not raw:
        return []
    if raw.isdigit() and len(raw) == 6:
        return ["CN"]
    if raw.isdigit() and len(raw) <= 5:
        return ["HK"]
    if raw.isascii() and raw.replace(".", "").isalpha() and len(raw) <= 7:
        return ["US"]
    return []


def resolve_target(code: str, market: str | None = None, name: str = "") -> ResolvedFillTarget:
    """把「代码 + 可选市场」变成一条可以写进台账的标的。

    用户只填名称也可以：先查数据库里的代码、名称和别名，查不到就按代码形状
    推断市场。**不会因此写错市场**——把「腾讯」当美股代码存进台账，比报错更糟。
    """
    raw = str(code or "").strip()
    asked = str(market or "").strip().upper() or None

    conn = connect()
    try:
        query = raw or name
        if query:
            # 代码优先，其次名称，最后别名。三者都能命中「用户心里那只票」。
            row = conn.execute(
                "SELECT market, code, name, currency, lot_size FROM symbol"
                " WHERE code=? OR name=? OR name LIKE ?"
                " ORDER BY (code=?) DESC, (market=?) DESC, (market=?) DESC LIMIT 1",
                (
                    normalize_code(asked, query) if asked else query,
                    query,
                    f"%{query}%",
                    query,
                    asked or "",
                    "HK",
                ),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT s.market, s.code, s.name, s.currency, s.lot_size FROM symbol s"
                    " JOIN symbol_alias a ON a.symbol_id = s.id WHERE a.alias=?"
                    " ORDER BY (s.market=?) DESC LIMIT 1",
                    (query, asked or ""),
                ).fetchone()
            if row:
                return ResolvedFillTarget(
                    market=row["market"], code=row["code"], name=row["name"],
                    currency=row["currency"], lot_size=int(row["lot_size"] or 1), known=True,
                )
    finally:
        conn.close()

    if not raw:
        # 只有名称、库里也没有：不猜市场，让调用方说明「先去加自选」。
        return ResolvedFillTarget(
            market=asked or "", code="", name=name or "", currency="",
            lot_size=1, known=False,
        )

    # 中文输入是名称，不是代码；字母数字混排（如 BRK.B、0700.HK）才算代码。
    # 库里查不到时宁可说「找不到」，也不要把它当成美股代码写进台账。
    if not asked and not _looks_like(raw):
        return ResolvedFillTarget(
            market="", code="", name=raw, currency="", lot_size=1, known=False,
        )

    candidates = _looks_like(raw)
    if asked:
        guess = asked
    elif len(candidates) == 1:
        guess = candidates[0]
    else:
        guess = guess_market(raw)
    code_norm = normalize_code(guess, raw)
    currency = {"CN": "CNY", "HK": "HKD"}.get(guess, "USD")
    lot = 100 if guess in ("CN", "HK") else 1
    return ResolvedFillTarget(
        market=guess, code=code_norm, name=name or code_norm or raw,
        currency=currency, lot_size=lot, known=False,
    )


def estimate_fees(
    market: str,
    price,
    qty,
    broker: str | None = None,
    side: str = "buy",
) -> Decimal:
    """按券商套餐算这一笔的费用。数量可能是小数股，所以全程用 Decimal。"""
    amount = Decimal(str(price or 0)) * Decimal(str(qty or 0))
    if amount <= 0:
        return Decimal("0")
    try:
        return quote_fees(
            str(market).upper(), amount, Decimal(str(qty or 0)),
            str(side).lower().startswith("s"), broker,
        ).total
    except Exception:  # noqa: BLE001 - 算不出来就返回 0，不要让记账失败
        return Decimal("0")


def suggest_levels(market: str, code: str, entry, horizon: str | None = None) -> dict:
    """用缓存里的日线给一个止损/目标建议。

    返回字段：
    * ``ok``        有没有足够的 K 线
    * ``stop`` / ``target`` / ``atr_pct``
    * ``basis``     一句话说明这个数字是怎么来的（页面直接展示）
    """
    from kestrel.services import timeframe

    horizon_code = str(horizon or DEFAULT_HORIZON)
    cfg = timeframe.get(horizon_code)
    price = Decimal(str(entry or 0))
    if price <= 0:
        return {"ok": False, "reason": "没有成交价，先填价格"}

    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM symbol WHERE market=? AND code=?",
            (str(market).upper(), normalize_code(str(market).upper(), str(code))),
        ).fetchone()
        if row is None:
            return {"ok": False, "reason": "这只标的还没缓存 K 线"}
        from kestrel.services import bars as bars_service

        daily = bars_service.cached_bars(conn, row["id"], cfg.lookback)
    finally:
        conn.close()

    folded = timeframe.prepare(daily, cfg.code)
    if len(folded) < cfg.min_bars:
        return {
            "ok": False,
            "reason": f"缓存里的 K 线不够（{len(folded)} 根），算不出可信的止损",
            "bars": len(folded),
        }

    ind = compute(Series.from_bars(folded))
    atr = Decimal(str(ind.get("atr14") or 0))
    if atr <= 0:
        return {"ok": False, "reason": "波动数据缺失，算不出止损"}

    stop = (price - atr * Decimal("2")).quantize(Decimal("0.001"))
    floor, ceiling = cfg.stop_band
    lowest = (price * (Decimal("1") - Decimal(str(ceiling)))).quantize(Decimal("0.001"))
    highest = (price * (Decimal("1") - Decimal(str(floor)))).quantize(Decimal("0.001"))
    stop = min(max(stop, lowest), highest)

    risk = price - stop
    target = (price + risk * Decimal("2")).quantize(Decimal("0.001"))
    return {
        "ok": True,
        "stop": float(stop),
        "target": float(target),
        "atr_pct": round(float(atr / price * 100), 2),
        "risk_pct": round(float(risk / price * 100), 2),
        "r_multiple": 2.0,
        "horizon": cfg.code,
        "bars": len(folded),
        "basis": (
            f"按{cfg.zh}最近 {len(folded)} 根 K 线的波动（ATR {atr:.2f}）给 2 倍 ATR 止损，"
            "目标按 2:1 计算。这是建议值，不是你唯一能填的数。"
        ),
    }
