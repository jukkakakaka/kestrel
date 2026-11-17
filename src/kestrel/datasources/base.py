"""数据源基础契约与多市场代码规范化。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

# 常见港股主流大市值标的每手股数表（弥补行情接口未返回每手股数时的缺口）
HK_LOT_OVERRIDES: dict[str, int] = {
    "00001": 500, "00002": 500, "00003": 1000, "00005": 400, "00006": 500,
    "00011": 100, "00012": 1000, "00016": 500, "00027": 1000, "00066": 500,
    "00175": 1000, "00267": 1000, "00285": 500, "00288": 500, "00291": 500,
    "00386": 2000, "00388": 100, "00669": 500, "00688": 500, "00700": 100,
    "00762": 2000, "00823": 100, "00857": 2000, "00883": 1000, "00939": 1000,
    "00941": 500, "00960": 500, "00968": 2000, "00981": 500, "00992": 2000,
    "01024": 100, "01038": 500, "01044": 500, "01088": 500, "01093": 2000,
    "01109": 500, "01177": 1000, "01211": 500, "01299": 200, "01398": 1000,
    "01810": 200, "01876": 100, "01928": 400, "01997": 1000, "02015": 100,
    "02020": 200, "02269": 500, "02313": 100, "02318": 500, "02319": 1000,
    "02331": 500, "02382": 100, "02388": 500, "02628": 1000, "02800": 500,
    "03690": 100, "03968": 500, "03988": 1000, "06618": 50, "06690": 200,
    "06862": 1000, "09618": 50, "09633": 200, "09866": 10, "09868": 100,
    "09888": 50, "09961": 50, "09988": 100, "09999": 100,
}


@dataclass(slots=True)
class Quote:
    market: str
    code: str
    name: str
    price: Decimal
    prev_close: Decimal | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: int | None = None
    turnover: Decimal | None = None
    change_pct: float | None = None
    source: str = ""
    quoted_at: datetime | None = None
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.market}:{self.code}"


class DataSourceError(RuntimeError):
    """数据源未能返回可用数据时抛出。"""


@runtime_checkable
class QuoteProvider(Protocol):
    """行情适配器协议：返回 ``MARKET:CODE -> Quote`` 映射。"""

    name: str

    async def fetch(self, symbols: list[tuple[str, str]]) -> dict[str, Quote]:
        ...


def cn_exchange_prefix(code: str) -> str:
    """识别 A 股交易所前缀：沪市 sh / 北交所 bj / 深市 sz。"""
    c = str(code or "").strip()
    if c.startswith(("8", "43", "920")):
        return "bj"
    if c.startswith(("6", "9", "5")):
        return "sh"
    return "sz"


def default_lot_size(market: str, code: str) -> int:
    """按市场及已知蓝筹表返回默认每手股数。"""
    mkt = str(market or "").upper()
    norm = normalize_code(mkt, code)
    if mkt == "CN":
        return 100
    if mkt == "HK":
        return HK_LOT_OVERRIDES.get(norm, 100)
    return 1


def normalize_code(market: str, code: str) -> str:
    """按目标市场规整股票代码格式。"""
    code = str(code or "").strip().upper()
    if market == "HK":
        return code.zfill(5) if code.isdigit() else code
    if market == "CN":
        return code.split(".")[0].zfill(6)
    return code


def guess_market(code: str) -> str:
    """根据代码形状尽最大努力推断所属市场。"""
    code = str(code or "").strip().upper()
    if code.isdigit():
        return "CN" if len(code) == 6 else "HK"
    return "US"
