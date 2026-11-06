"""Money helpers.

All monetary values are stored as integer micro-units (1 unit = 1e-6) so that
SQLite never sees a float rounding error. Conversion to Decimal happens only at
the presentation boundary.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

MICRO = Decimal("1000000")


def to_micro(value: Decimal | float | int | str) -> int:
    """Convert an amount to integer micro-units."""
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((amount * MICRO).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_micro(value: int | None) -> Decimal:
    """Convert integer micro-units back to a Decimal amount."""
    if value is None:
        return Decimal("0")
    return (Decimal(int(value)) / MICRO).quantize(Decimal("0.000001"))


def quantize(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def format_amount(value: int | Decimal | float, places: int = 2) -> str:
    """Format for display with thousands separators."""
    amount = from_micro(value) if isinstance(value, int) else Decimal(str(value))
    spec = f"{{:,.{places}f}}"
    return spec.format(amount)
