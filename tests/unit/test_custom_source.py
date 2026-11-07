"""Custom data sources: dot-path extraction and validation."""

from __future__ import annotations

import pytest

from kestrel.datasources.custom import CustomProvider, _dig, _to_decimal


def test_dig_walks_dicts_and_lists():
    payload = {"data": {"quote": {"price": 12.5}}, "items": [{"last": 9.1}, {"last": 8.2}]}
    assert _dig(payload, "data.quote.price") == 12.5
    assert _dig(payload, "items.1.last") == 8.2
    assert _dig(payload, "missing.path") is None
    assert _dig(payload, "items.9.last") is None


def test_decimal_parsing_tolerates_strings_and_commas():
    from decimal import Decimal

    assert _to_decimal("1,234.50") == Decimal("1234.50")
    assert _to_decimal("价格 419.80 HKD") == Decimal("419.80")
    assert _to_decimal(None) is None
    assert _to_decimal("n/a") is None


def test_provider_only_claims_its_markets():
    provider = CustomProvider({"key": "x", "label": "X", "url": "https://e/{code}", "markets": "US"})
    assert provider.supports("US")
    assert not provider.supports("HK")


def test_url_placeholders_are_substituted():
    provider = CustomProvider({"key": "x", "label": "X", "url": "https://e/{market}/{code}"})
    assert provider._url_for("us", "AAPL") == "https://e/US/AAPL"


def test_save_rejects_a_url_without_scheme():
    from kestrel.datasources import custom

    with pytest.raises(ValueError):
        custom.save({"label": "bad", "url": "example.com/quote"})


def test_save_rejects_an_empty_label():
    from kestrel.datasources import custom

    with pytest.raises(ValueError):
        custom.save({"label": "", "url": "https://example.com/q?c={code}"})
