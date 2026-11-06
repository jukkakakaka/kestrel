"""Money must round-trip through micro-units without drift."""

from __future__ import annotations

from decimal import Decimal

from kestrel.core.money import from_micro, to_micro


def test_roundtrip():
    for raw in ("0", "1", "419.8", "1453", "0.000001", "123456.789"):
        assert from_micro(to_micro(raw)) == Decimal(raw)


def test_half_up():
    assert to_micro("0.0000005") == 1
    assert to_micro("0.0000004") == 0


def test_from_none():
    assert from_micro(None) == Decimal("0")
