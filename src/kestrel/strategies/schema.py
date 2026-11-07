"""A machine-readable description of the rule language.

The rules page is for humans. This module is for other models and agents: it
publishes a JSON Schema plus a worked example and a copy-paste prompt, so an AI
can author a rule without reading this repository.

Everything here is generated from the same constants the compiler uses, so the
documentation cannot drift away from the implementation.
"""

from __future__ import annotations

from typing import Any

from kestrel.strategies import library
from kestrel.strategies.spec import FIELDS, OPS

FIELD_DOCS: dict[str, str] = {
    "last": "最新收盘价",
    "sma5": "5 日均线",
    "sma10": "10 日均线",
    "sma20": "20 日均线",
    "sma60": "60 日均线",
    "ema12": "12 日指数均线",
    "ema26": "26 日指数均线",
    "macd": "EMA12 − EMA26",
    "rsi14": "14 日 RSI，0–100",
    "atr14": "14 日平均真实波幅（价格单位）",
    "atr_pct": "ATR 占价格百分比，衡量波动",
    "vol_ratio": "近 5 日均量 / 近 20 日均量",
    "high20": "前 20 日最高价（不含当日）",
    "low20": "前 20 日最低价（不含当日）",
    "high60": "前 60 日最高价",
    "dist_sma20": "现价距 20 日线的百分比，负数=在线下",
    "dist_sma60": "现价距 60 日线的百分比",
    "dist_high20": "现价距 20 日高点的百分比，通常为负",
    "range_pos20": "现价在 20 日区间中的位置，0=最低 100=最高",
    "ret5": "近 5 日涨跌幅（%）",
    "ret20": "近 20 日涨跌幅（%）",
    "ret60": "近 60 日涨跌幅（%）",
    "close_pos": "当日收盘在当日振幅中的位置，0–1",
    "slope20": "20 日均线斜率",
    "ma_stack": "布尔：MA5 > MA20 > MA60",
    "trend_up": "布尔：MA20 > MA60 且价格在 MA60 上方",
}


def json_schema() -> dict[str, Any]:
    scalar = {"type": ["number", "string"]}
    condition = {
        "type": "object",
        "required": ["left", "op"],
        "properties": {
            "left": {"type": "string", "enum": sorted(FIELDS),
                     "description": "指标名；见 fields 说明"},
            "op": {"type": "string", "enum": sorted(OPS)},
            "right": {**scalar, "description": "数字，或另一个指标名"},
            "value": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
                "description": "仅 between 使用，[下限, 上限]",
            },
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Kestrel trading rule",
        "type": "object",
        "required": ["code", "name", "conditions"],
        "additionalProperties": True,
        "properties": {
            "code": {
                "type": "string",
                "pattern": "^[A-Za-z0-9_]+$",
                "description": "唯一标识，只能字母数字下划线",
            },
            "name": {"type": "string", "minLength": 1, "description": "给人看的名称"},
            "conditions": {
                "type": "array",
                "minItems": 1,
                "items": condition,
                "description": "全部满足才产生信号（逻辑与）",
            },
            "score": {
                "type": "object",
                "properties": {
                    "base": {"type": "number", "minimum": 0, "maximum": 1},
                    "bonus": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["when", "add"],
                            "properties": {
                                "when": condition,
                                "add": {"type": "number", "minimum": 0, "maximum": 0.5},
                            },
                        },
                        "description": "只影响排序，不决定是否进场",
                    },
                },
            },
            "stop": {"type": "object", "properties": {"field": {"type": "string", "enum": sorted(FIELDS)}},
                     "description": "止损参考指标，通常用 low20"},
            "target": {"type": "object", "properties": {"field": {"type": "string", "enum": sorted(FIELDS)}},
                       "description": "目标价参考指标，通常用 high20"},
        },
    }


def schema_bundle() -> dict[str, Any]:
    """Everything a model needs in one payload."""
    return {
        "version": 1,
        "json_schema": json_schema(),
        "fields": [{"name": name, "meaning": FIELD_DOCS.get(name, "")} for name in sorted(FIELDS)],
        "operators": {
            ">": "大于", ">=": "大于等于", "<": "小于", "<=": "小于等于",
            "between": "value 区间内（含端点）",
            "is_true": "布尔指标为真，无需 right",
            "is_false": "布尔指标为假，无需 right",
        },
        "semantics": {
            "conditions": "全部满足才触发，等价于 AND",
            "score": "score = base + 所有命中的 bonus，截断到 0–1，只用于排序",
            "stop": "引用一个指标作为止损参考价，必须低于入场价",
            "target": "引用一个指标作为目标价，必须高于入场价",
            "point_in_time": "所有指标只用当日及之前的数据，不会用到未来信息",
        },
        "constraints": {
            "stop_band_pct": [1.5, 15],
            "min_reward_risk": 1.5,
            "note": "验证器会用这些约束检查回测结果；越界的组合会被大量丢弃，导致样本不足",
        },
        "examples": [item["spec"] for item in library.RULES],
    }


def prompt_for_ai(goal: str = "") -> str:
    """A ready-to-paste prompt that lets another model author a rule."""
    bundle = schema_bundle()
    fields = "\n".join(f"- {f['name']}: {f['meaning']}" for f in bundle["fields"])
    import json as _json

    examples = "\n\n".join(
        f"示例 {i + 1}（{item['name']}）：\n```json\n"
        + _json.dumps(item["spec"], ensure_ascii=False, indent=2)
        + "\n```"
        for i, item in enumerate(library.RULES[:2])
    )
    return f"""你是一位量化策略作者。请为 Kestrel 生成一条选股规则，只输出 JSON。

{goal or "目标：找一种在上涨趋势中低风险进场的情形。"}

## 可用的指标
{fields}

## 操作符
> >= < <= between is_true is_false
（right 可以写数字，也可以写另一个指标名表示两者比较）

## 语义
- conditions 全部满足才触发（AND）
- score 只影响排序：score = base + 命中的 bonus，截断到 0~1
- stop / target 各引用一个指标作为参考价，必须分别低于 / 高于入场价

## 硬性约束
- 止损距离必须落在 1.5%~15%
- 盈亏比至少 1.5:1
- 条件不要超过 6 个，否则样本会太少

## 参考示例
{examples}

## 输出
只输出一条规则的 JSON 对象，字段：code, name, conditions, score, stop, target。
不要输出解释文字，不要 Markdown 代码块。"""
