"""Built-in rule definitions, as data.

These are the same shapes as a user-written rule. Keeping them in JSON means the
UI can show them, copy them, tweak them, and measure them with the same code.
"""

from __future__ import annotations

from typing import Any

RULES: list[dict[str, Any]] = [
    {
        "code": "trend_pullback",
        "name": "趋势回踩",
        "note": "上涨趋势里回踩 20 日线，用更近的止损换更好的位置。",
        "spec": {
            "code": "trend_pullback",
            "name": "趋势回踩",
            "conditions": [
                {"left": "trend_up", "op": "is_true"},
                {"left": "dist_sma20", "op": "between", "value": [-4, 1.5]},
                {"left": "rsi14", "op": "between", "value": [32, 62]},
            ],
            "score": {
                "base": 0.4,
                "bonus": [
                    {"when": {"left": "dist_sma20", "op": "between", "value": [-1, 0.5]}, "add": 0.2},
                    {"when": {"left": "rsi14", "op": "between", "value": [40, 55]}, "add": 0.15},
                    {"when": {"left": "vol_ratio", "op": "<", "right": 1.2}, "add": 0.1},
                ],
            },
            "stop": {"field": "low20"},
            "target": {"field": "high20"},
        },
    },
    {
        "code": "breakout_20",
        "name": "20 日突破",
        "note": "创 20 日新高且放量。实测长期亏钱，保留作为对照。",
        "spec": {
            "code": "breakout_20",
            "name": "20 日突破",
            "conditions": [
                {"left": "last", "op": ">", "right": "high20"},
                {"left": "vol_ratio", "op": ">", "right": 1.5},
            ],
            "score": {
                "base": 0.5,
                "bonus": [
                    {"when": {"left": "vol_ratio", "op": ">", "right": 2.0}, "add": 0.2},
                    {"when": {"left": "ret20", "op": ">", "right": 5}, "add": 0.15},
                ],
            },
            "stop": {"field": "high20"},
            "target": {"field": None},
        },
    },
    {
        "code": "mean_reversion",
        "name": "超跌反弹",
        "note": "RSI 超卖但仍在 60 日线上方，赌一次修复。",
        "spec": {
            "code": "mean_reversion",
            "name": "超跌反弹",
            "conditions": [
                {"left": "rsi14", "op": "<", "right": 30},
                {"left": "last", "op": ">", "right": "sma60"},
            ],
            "score": {
                "base": 0.45,
                "bonus": [
                    {"when": {"left": "rsi14", "op": "<", "right": 25}, "add": 0.2},
                    {"when": {"left": "close_pos", "op": ">", "right": 0.6}, "add": 0.15},
                ],
            },
            "stop": {"field": "low20"},
            "target": {"field": "sma20"},
        },
    },
    {
        "code": "pullback_quiet",
        "name": "缩量回踩",
        "note": "均线多头排列，缩量回踩到 20 日线附近，收盘走强。",
        "spec": {
            "code": "pullback_quiet",
            "name": "缩量回踩",
            "conditions": [
                {"left": "ma_stack", "op": "is_true"},
                {"left": "dist_sma20", "op": "between", "value": [-2.5, 0.5]},
                {"left": "rsi14", "op": "between", "value": [40, 62]},
                {"left": "vol_ratio", "op": "<", "right": 1.15},
                {"left": "close_pos", "op": ">", "right": 0.45},
            ],
            "score": {
                "base": 0.45,
                "bonus": [
                    {"when": {"left": "dist_sma20", "op": "between", "value": [-1.2, 0]}, "add": 0.2},
                    {"when": {"left": "ret60", "op": ">", "right": 5}, "add": 0.15},
                    {"when": {"left": "atr_pct", "op": "<", "right": 2.5}, "add": 0.1},
                ],
            },
            "stop": {"field": "low20"},
            "target": {"field": "high20"},
        },
    },
    {
        "code": "momentum_20",
        "name": "温和动量",
        "note": "20 日涨幅温和、价格略高于均线、RSI 未过热——跟随强势但不追高。",
        "spec": {
            "code": "momentum_20",
            "name": "温和动量",
            "conditions": [
                {"left": "ret20", "op": "between", "value": [3, 15]},
                {"left": "dist_sma20", "op": "between", "value": [0, 5]},
                {"left": "rsi14", "op": "between", "value": [52, 70]},
            ],
            "score": {
                "base": 0.42,
                "bonus": [
                    {"when": {"left": "ma_stack", "op": "is_true"}, "add": 0.18},
                    {"when": {"left": "range_pos20", "op": ">", "right": 70}, "add": 0.12},
                ],
            },
            "stop": {"field": "sma20"},
            "target": {"field": "high20"},
        },
    },
    {
        "code": "low_vol_trend",
        "name": "低波趋势",
        "note": "波动小的票沿着均线上行，适合拿住不折腾。",
        "spec": {
            "code": "low_vol_trend",
            "name": "低波趋势",
            "conditions": [
                {"left": "ma_stack", "op": "is_true"},
                {"left": "atr_pct", "op": "<", "right": 3.2},
                {"left": "dist_sma20", "op": "between", "value": [-1, 3]},
                {"left": "ret60", "op": ">", "right": 4},
            ],
            "score": {
                "base": 0.4,
                "bonus": [
                    {"when": {"left": "ret60", "op": ">", "right": 10}, "add": 0.15},
                    {"when": {"left": "vol_ratio", "op": "<", "right": 1.1}, "add": 0.15},
                ],
            },
            "stop": {"field": "low20"},
            "target": {"field": "high20"},
        },
    },
    {
        "code": "washout_bounce",
        "name": "超跌长下影",
        "note": "跌到 20 日区间底部但收盘拉回，说明有人在接。",
        "spec": {
            "code": "washout_bounce",
            "name": "超跌长下影",
            "conditions": [
                {"left": "range_pos20", "op": "<", "right": 22},
                {"left": "close_pos", "op": ">", "right": 0.62},
                {"left": "ret20", "op": "<", "right": -4},
            ],
            "score": {
                "base": 0.45,
                "bonus": [
                    {"when": {"left": "rsi14", "op": "<", "right": 35}, "add": 0.2},
                    {"when": {"left": "last", "op": ">", "right": "sma60"}, "add": 0.15},
                ],
            },
            "stop": {"field": "low20"},
            "target": {"field": "sma20"},
        },
    },
]


def get(code: str) -> dict[str, Any] | None:
    for item in RULES:
        if item["code"] == code:
            return item
    return None
