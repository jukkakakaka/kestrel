"""Data source contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable


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
    """Raised when a provider cannot return usable data."""


@runtime_checkable
class QuoteProvider(Protocol):
    """A provider returns a mapping of ``MARKET:CODE`` to :class:`Quote`."""

    name: str

    async def fetch(self, symbols: list[tuple[str, str]]) -> dict[str, Quote]:
        """Fetch quotes for ``[(market, code), ...]``."""
        ...


def normalize_code(market: str, code: str) -> str:
    """Normalise a user supplied ticker for a given market."""
    code = code.strip().upper()
    if market == "HK":
        return code.zfill(5) if code.isdigit() else code
    if market == "CN":
        return code.split(".")[0].zfill(6)
    return code


def guess_market(code: str) -> str:
    """Best-effort market inference from a raw ticker."""
    code = code.strip().upper()
    if code.isdigit():
        return "CN" if len(code) == 6 else "HK"
    return "US"
