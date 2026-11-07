"""Statement parsing: the four things a fill line must contain."""

from __future__ import annotations

import pytest

from kestrel.services import statement


@pytest.mark.parametrize(
    ("line", "side", "market", "qty", "price"),
    [
        ("2026-11-06 买入 00700 腾讯控股 100股 @419.80", "buy", "HK", 100, 419.80),
        ("2026-11-06 卖出 600519 贵州茅台 100股 @1450.00", "sell", "CN", 100, 1450.0),
        ("2026-11-06 BUY AAPL 1 shares @342.37", "buy", "US", 1, 342.37),
        ("卖出 NVDA 5股 均价 186.64", "sell", "US", 5, 186.64),
    ],
)
def test_parse_common_fill_lines(line, side, market, qty, price):
    parsed = statement.parse_line(line)
    assert parsed is not None, f"should parse: {line}"
    assert parsed.side == side
    assert parsed.market == market
    assert parsed.qty == qty
    assert parsed.price == pytest.approx(price)


def test_line_without_a_side_is_ignored():
    assert statement.parse_line("2026-11-06 00700 腾讯控股 100股 @419.80") is None


def test_missing_quantity_is_reported_not_guessed():
    parsed = statement.parse_line("买入 00700 @419.80")
    assert parsed is not None
    assert parsed.qty == 0
    assert "没找到数量" in parsed.problems
    assert parsed.confidence < 1.0


def test_amount_only_line_derives_price():
    parsed = statement.parse_line("买入 00700 100股 金额 41,980")
    assert parsed is not None
    assert parsed.price == pytest.approx(419.8)


def test_parse_returns_every_recognisable_line():
    text = "\n".join(
        [
            "2026-11-06 买入 00700 腾讯控股 100股 @419.80",
            "随便一行说明文字",
            "2026-11-06 卖出 AAPL 1股 @342.37",
        ]
    )
    rows = statement.parse(text)
    assert len(rows) == 2
    assert rows[0]["market"] == "HK"
    assert rows[1]["market"] == "US"


def test_ocr_reports_absence_instead_of_faking_it():
    ok, engine = statement.ocr_available()
    if not ok:
        assert engine == ""
        text, _ = statement.ocr_image("/tmp/does-not-exist.png")
        assert text == ""
