"""Market classification for Eastmoney's suggestion endpoint.

Eastmoney spells the board ("沪A", "深A", "京A"), not the market. Assuming
"A股" meant A-share symbols never resolved, which is how 贵州茅台 ended up
unsearchable.
"""

from __future__ import annotations

import pytest

from kestrel.datasources.resolve import _has_cjk, _market_of


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("沪A", "CN"),
        ("深A", "CN"),
        ("京A", "CN"),
        ("A股", "CN"),
        ("港股", "HK"),
        ("美股", "US"),
        ("科创板", "CN"),
        ("日股", None),
        ("英股", None),
        ("", None),
    ],
)
def test_market_classification(type_name, expected):
    assert _market_of(type_name) == expected


def test_cjk_detection():
    assert _has_cjk("茅台")
    assert not _has_cjk("AAPL")
    assert _has_cjk("宁德时代A")
