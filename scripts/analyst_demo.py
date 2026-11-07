"""Exercise the analyst pipeline without spending a token.

Builds the real brief, prints the exact prompt a model would receive, then feeds a
canned reply through the same validator and sizing code the live path uses. The
canned reply deliberately contains good and bad ideas, so the rejection log is
visible.

    uv run python scripts/analyst_demo.py            # full run
    uv run python scripts/analyst_demo.py --prompt   # only print the prompt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kestrel.services import analyst  # noqa: E402


def canned_reply(brief: dict) -> dict:
    """A plausible model answer, including two mistakes on purpose."""
    picks = []
    if brief["candidates"]:
        first = brief["candidates"][0]
        price = float(first["price"])
        picks.append(
            {
                "market": first["market"],
                "code": first["code"],
                "action": "buy",
                "conviction": 62,
                "thesis": f"现价 {price:.2f}，距 20 日线 {first['dist_sma20']}%，RSI {first['rsi14']}，缩量回踩未破位",
                "catalysts": ["回踩到均线支撑", "成交量萎缩"],
                "risks": ["跌破前低则趋势破坏"],
                "entry_low": round(price * 0.997, 2),
                "entry_high": round(price * 1.003, 2),
                "stop": round(price * 0.955, 2),
                "target": round(price * 1.09, 2),
                "invalidation": "收盘跌破 20 日线且放量",
            }
        )
        # mistake 1: stop far too tight, outside the 1.5%-15% band
        picks.append(
            {
                "market": first["market"],
                "code": first["code"],
                "action": "buy",
                "conviction": 50,
                "thesis": "止损极窄的尝试，应该被风控拒绝",
                "entry_low": round(price * 0.997, 2),
                "entry_high": round(price * 1.003, 2),
                "stop": round(price * 0.999, 2),
                "target": round(price * 1.05, 2),
                "invalidation": "",
            }
        )
    # mistake 2: a ticker that was never offered
    picks.append(
        {
            "market": "US",
            "code": "TSLA",
            "action": "buy",
            "conviction": 90,
            "thesis": "凭空捏造的标的，应该被拒绝",
            "entry_low": 100,
            "entry_high": 101,
            "stop": 95,
            "target": 120,
            "invalidation": "",
        }
    )
    return {
        "market_view": "大盘在高位震荡，题材轮动快，只做缩量回踩不追突破。",
        "picks": picks,
        "pass_reason": "",
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", action="store_true", help="只打印提示词")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    brief = analyst.build_brief(["CN", "HK", "US"], limit=args.limit)
    print(f"候选 {len(brief['candidates'])} 只，账户 {brief['account']}\n")

    if args.prompt:
        print(analyst.SYSTEM)
        print("\n" + "-" * 70 + "\n")
        print(analyst.render_prompt(brief)[:4000])
        return 0

    if not brief["candidates"]:
        print("没有候选股，先跑一次扫描。")
        return 1

    sample = brief["candidates"][0]
    print("模型将看到的一条样例：")
    print(json.dumps(sample, ensure_ascii=False, indent=2))
    print()

    result = await analyst.analyse(dry_reply=canned_reply(brief), limit=args.limit)
    print("返回：")
    print(json.dumps({k: v for k, v in result.items() if k != "rejected"}, ensure_ascii=False, indent=2))
    print("\n风控拒绝明细：")
    for item in result.get("rejected") or []:
        print(f"  - {item.get('code')}: {item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
