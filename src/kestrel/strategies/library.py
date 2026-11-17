"""内置规则定义（纯 JSON 数据形态，用户可在界面查看、复制、微调及回测）。"""

from __future__ import annotations

from typing import Any

RULES: list[dict[str, Any]] = [
    {
        "code": "trend_pullback",
        "name": "趋势回踩",
        "note": "上涨趋势里回踩 20 日线，收阳止跌且波动收敛，用更近的止损换更好盈亏比。",
        "spec": {
            "code": "trend_pullback",
            "name": "趋势回踩",
            "conditions": [
                {"left": "trend_up", "op": "is_true"},
                {"left": "slope20", "op": ">", "right": 0},
                {"left": "dist_sma20", "op": "between", "value": [-4, 1.5]},
                {"left": "rsi14", "op": "between", "value": [35, 62]},
            ],
            "score": {
                "base": 0.42,
                "bonus": [
                    {"when": {"left": "dist_sma20", "op": "between", "value": [-1.2, 0.5]}, "add": 0.18},
                    {"when": {"left": "bull_candle", "op": "is_true"}, "add": 0.12},
                    {"when": {"left": "vcp_ratio", "op": "<", "right": 0.9}, "add": 0.12},
                    {"when": {"left": "vol_today_ratio", "op": "<", "right": 1.1}, "add": 0.1},
                ],
            },
            "stop": {"field": "low20"},
            "target": {"field": "high20"},
        },
    },
    {
        "code": "breakout_20",
        "name": "20 日放量突破",
        "note": "中长期趋势向上、波动前期收敛后当日真阳放量突破 20 日前高。",
        "spec": {
            "code": "breakout_20",
            "name": "20 日放量突破",
            "conditions": [
                {"left": "trend_up", "op": "is_true"},
                {"left": "last", "op": ">", "right": "high20"},
                {"left": "vol_today_ratio", "op": ">", "right": 1.4},
                {"left": "bull_candle", "op": "is_true"},
                {"left": "atr_pct", "op": "<", "right": 6.5},
            ],
            "score": {
                "base": 0.48,
                "bonus": [
                    {"when": {"left": "vol_today_ratio", "op": ">", "right": 2.0}, "add": 0.16},
                    {"when": {"left": "vcp_ratio", "op": "<", "right": 1.0}, "add": 0.14},
                    {"when": {"left": "close_pos", "op": ">", "right": 0.7}, "add": 0.12},
                ],
            },
            "stop": {"field": "sma20"},
            "target": {"field": None},
        },
    },
    {
        "code": "mean_reversion",
        "name": "超跌反弹",
        "note": "RSI 超卖但仍在 60 日线上方且中期均线走平/向上，博弈企稳修复。",
        "spec": {
            "code": "mean_reversion",
            "name": "超跌反弹",
            "conditions": [
                {"left": "rsi14", "op": "<", "right": 32},
                {"left": "last", "op": ">", "right": "sma60"},
                {"left": "close_pos", "op": ">", "right": 0.5},
            ],
            "score": {
                "base": 0.45,
                "bonus": [
                    {"when": {"left": "rsi14", "op": "<", "right": 25}, "add": 0.2},
                    {"when": {"left": "bull_candle", "op": "is_true"}, "add": 0.15},
                    {"when": {"left": "close_pos", "op": ">", "right": 0.65}, "add": 0.1},
                ],
            },
            "stop": {"field": "low20"},
            "target": {"field": "sma20"},
        },
    },
    {
        "code": "pullback_quiet",
        "name": "缩量回踩",
        "note": "均线多头排列，缩量回踩到 20 日线附近且波动收缩，收盘走强。",
        "spec": {
            "code": "pullback_quiet",
            "name": "缩量回踩",
            "conditions": [
                {"left": "ma_stack", "op": "is_true"},
                {"left": "dist_sma20", "op": "between", "value": [-2.5, 0.5]},
                {"left": "rsi14", "op": "between", "value": [40, 62]},
                {"left": "vol_today_ratio", "op": "<", "right": 1.05},
                {"left": "close_pos", "op": ">", "right": 0.45},
            ],
            "score": {
                "base": 0.45,
                "bonus": [
                    {"when": {"left": "dist_sma20", "op": "between", "value": [-1.2, 0]}, "add": 0.2},
                    {"when": {"left": "ret60", "op": ">", "right": 5}, "add": 0.15},
                    {"when": {"left": "vcp_ratio", "op": "<", "right": 0.85}, "add": 0.12},
                ],
            },
            "stop": {"field": "low20"},
            "target": {"field": "high20"},
        },
    },
    {
        "code": "momentum_20",
        "name": "温和动量",
        "note": "20 日涨幅温和、价格略高于均线、MACD 保持正值且 RSI 未过热。",
        "spec": {
            "code": "momentum_20",
            "name": "温和动量",
            "conditions": [
                {"left": "trend_up", "op": "is_true"},
                {"left": "ret20", "op": "between", "value": [3, 16]},
                {"left": "dist_sma20", "op": "between", "value": [0, 5]},
                {"left": "rsi14", "op": "between", "value": [50, 70]},
            ],
            "score": {
                "base": 0.42,
                "bonus": [
                    {"when": {"left": "ma_stack", "op": "is_true"}, "add": 0.18},
                    {"when": {"left": "macd_hist", "op": ">", "right": 0}, "add": 0.12},
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
        "note": "低波动标的沿着均线稳步上行，适合拿住不折腾。",
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
                "base": 0.42,
                "bonus": [
                    {"when": {"left": "ret60", "op": ">", "right": 10}, "add": 0.15},
                    {"when": {"left": "vcp_ratio", "op": "<", "right": 0.9}, "add": 0.15},
                    {"when": {"left": "vol_ratio", "op": "<", "right": 1.1}, "add": 0.12},
                ],
            },
            "stop": {"field": "low20"},
            "target": {"field": "high20"},
        },
    },
    {
        "code": "washout_bounce",
        "name": "超跌企稳",
        "note": "中期仍在 60 日线上方，短线急跌后收出长下影阳线企稳拉回。",
        "spec": {
            "code": "washout_bounce",
            "name": "超跌企稳",
            "conditions": [
                {"left": "last", "op": ">", "right": "sma60"},
                {"left": "range_pos20", "op": "<", "right": 25},
                {"left": "close_pos", "op": ">", "right": 0.62},
                {"left": "bull_candle", "op": "is_true"},
                {"left": "ret20", "op": "<", "right": -2},
            ],
            "score": {
                "base": 0.45,
                "bonus": [
                    {"when": {"left": "rsi14", "op": "<", "right": 38}, "add": 0.2},
                    {"when": {"left": "vol_today_ratio", "op": ">", "right": 1.1}, "add": 0.15},
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
