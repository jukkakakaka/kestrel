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


def test_ocr_is_not_pretended_to_exist():
    """不做 OCR 就干脆没有这个入口——对用户说「本机没有 OCR」毫无用处。"""
    assert not hasattr(statement, "ocr_image")
    assert not hasattr(statement, "ocr_available")


def test_tsv_export_with_headers_and_any_column_order():
    """券商导出的表格：表头认出来以后，列顺序无所谓，多一列也无所谓。"""
    text = "\n".join(
        [
            "成交时间\t证券代码\t证券名称\t买卖方向\t成交数量\t成交均价\t成交金额\t手续费",
            "2026-11-06 10:12:03\t00700\t腾讯控股\t买入\t100\t419.80\t41980.00\t18.50",
            "2026-11-06 14:02:11\t600519\t贵州茅台\t卖出\t100\t1450.00\t145000.00\t5.00",
        ]
    )
    result = statement.parse_table(text)
    rows = result["rows"]
    assert len(rows) == 2
    assert result["mapping"]["code"] == 1
    assert "认出了表头" in result["note"]
    assert rows[0]["market"] == "HK" and rows[0]["qty"] == 100
    assert rows[0]["fee"] == pytest.approx(18.5)
    assert rows[0]["trade_date"] == "2026-11-06"
    assert rows[1]["market"] == "CN" and rows[1]["side"] == "sell"


def test_headers_in_english_and_reordered():
    text = "\n".join(
        [
            "Date,Symbol,Side,Quantity,Price,Fees",
            "2026-11-05,AAPL,Buy,1,342.37,0.35",
        ]
    )
    rows = statement.parse_table(text)["rows"]
    assert len(rows) == 1
    assert rows[0]["market"] == "US"
    assert rows[0]["code"] == "AAPL"
    assert rows[0]["fee"] == pytest.approx(0.35)


def test_amount_only_table_derives_price_and_says_so():
    text = "\n".join(
        [
            "代码\t方向\t数量\t成交金额",
            "00700\t买入\t100\t41980",
        ]
    )
    rows = statement.parse_table(text)["rows"]
    assert rows[0]["price"] == pytest.approx(419.8)
    assert any("推算" in p for p in rows[0]["problems"])


def test_unrecognised_lines_are_counted_not_dropped_silently():
    text = "\n".join(
        [
            "成交时间\t证券代码\t买卖方向\t成交数量\t成交均价",
            "2026-11-06\t00700\t买入\t100\t419.80",
            "这一行不是成交记录",
        ]
    )
    result = statement.parse_table(text)
    assert len(result["rows"]) == 1
    assert result["skipped"] == 1
    assert "跳过" in result["note"]


def test_fractional_us_quantity_survives_parsing():
    text = "代码\t方向\t数量\t价格\nAAPL\t买入\t0.5\t342.37"
    rows = statement.parse_table(text)["rows"]
    assert rows[0]["qty"] == pytest.approx(0.5)

