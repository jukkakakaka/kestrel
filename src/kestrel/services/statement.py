"""Turn a broker statement into trade rows.

Two inputs are supported:

* **Pasted text** — the primary path. Broker apps let you copy a fill line, and
  text is exact where OCR is approximate.
* **A screenshot** — only when a local OCR engine exists. Rather than pretend,
  this reports that the engine is missing and tells the user what to install.

The parser is deliberately tolerant: brokers format fills differently, so it
looks for the four things that actually matter (side, ticker, quantity, price)
instead of assuming a column layout.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal

from kestrel.db.engine import connect

SIDE_BUY = ("买入", "买进", "买入成交", "BUY", "BOT", "Bought")
SIDE_SELL = ("卖出", "卖出成交", "SELL", "SLD", "Sold")

NUMBER = r"\d[\d,]*(?:\.\d+)?"

RE_SIDE = re.compile("|".join(SIDE_BUY + SIDE_SELL), re.IGNORECASE)
RE_CODE_HK = re.compile(r"\b(\d{5})\b")
RE_CODE_CN = re.compile(r"\b(\d{6})\b")
RE_CODE_US = re.compile(r"\b([A-Z]{1,5})\b")
RE_QTY = re.compile(rf"({NUMBER})\s*(?:股|shares?|units?)", re.IGNORECASE)
RE_PRICE = re.compile(rf"(?:@|均价|成交价|价格|price)\s*[:：]?\s*({NUMBER})", re.IGNORECASE)
RE_AMOUNT = re.compile(rf"(?:金额|成交额|amount)\s*[:：]?\s*({NUMBER})", re.IGNORECASE)
RE_DATE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
RE_FEE = re.compile(rf"(?:手续费|佣金|费用|fee|commission)\s*[:：]?\s*({NUMBER})", re.IGNORECASE)


@dataclass
class ParsedFill:
    market: str
    code: str
    name: str
    side: str            # buy / sell
    qty: int
    price: float
    fee: float = 0.0
    trade_date: str = ""
    raw: str = ""
    confidence: float = 0.0
    problems: list[str] = field(default_factory=list)


def _to_float(text: str) -> float:
    return float(text.replace(",", ""))


def _detect_market(code: str, line: str) -> str:
    if re.fullmatch(r"\d{6}", code):
        return "CN"
    if re.fullmatch(r"\d{1,5}", code):
        return "HK"
    if "HK" in line.upper() and re.search(r"\bHK\.?\d", line, re.IGNORECASE):
        return "HK"
    return "US"


def _pick_code(line: str) -> str | None:
    upper = line.upper()
    m = re.search(r"\b(?:HK|US)\.?(\d{4,6}|[A-Z]{1,5})\b", upper)
    if m:
        return m.group(1)
    m = RE_CODE_CN.search(line)
    if m:
        return m.group(1)
    m = RE_CODE_HK.search(line)
    if m:
        return m.group(1).zfill(5)
    for token in re.findall(r"\b[A-Z]{2,5}\b", upper):
        if token in {"USD", "HKD", "CNY", "RMB", "BUY", "SELL", "BOT", "SLD", "HK", "US", "CN"}:
            continue
        return token
    return None


def parse_line(line: str) -> ParsedFill | None:
    text = line.strip()
    if not text:
        return None

    side_match = RE_SIDE.search(text)
    if side_match is None:
        return None
    side = "buy" if side_match.group(0).upper() in [s.upper() for s in SIDE_BUY] else "sell"

    code = _pick_code(text)
    if not code:
        return None

    qty_match = RE_QTY.search(text)
    qty = int(_to_float(qty_match.group(1))) if qty_match else 0

    price_match = RE_PRICE.search(text)
    price = _to_float(price_match.group(1)) if price_match else 0.0

    if not price:
        amount = RE_AMOUNT.search(text)
        if amount and qty:
            price = _to_float(amount.group(1)) / qty

    fee_match = RE_FEE.search(text)
    fee = _to_float(fee_match.group(1)) if fee_match else 0.0

    date_match = RE_DATE.search(text)
    trade_date = (
        f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
        if date_match
        else date.today().isoformat()
    )

    market = _detect_market(code, text)
    name = ""
    words = re.findall(r"[\u4e00-\u9fff]{2,8}", text)
    for word in words:
        if word in ("买入", "卖出", "成交", "股", "手", "手续费", "佣金", "金额", "均价"):
            continue
        name = word
        break

    problems: list[str] = []
    if qty <= 0:
        problems.append("没找到数量")
    if price <= 0:
        problems.append("没找到成交价")

    confidence = 1.0 - 0.35 * len(problems)
    return ParsedFill(
        market=market,
        code=code,
        name=name or code,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        trade_date=trade_date,
        raw=text,
        confidence=round(max(0.1, confidence), 2),
        problems=problems,
    )


def parse(text: str) -> list[dict]:
    out: list[dict] = []
    for line in (text or "").splitlines():
        parsed = parse_line(line)
        if parsed:
            out.append(asdict(parsed))
    return out


def save(rows: list[dict]) -> dict:
    """把解析出来的成交行写进台账。

    卖出会按 FIFO 平掉已有持仓，而不是凭空开一个"做空"——早期版本这么做，
    导一份月度对账单就能把持仓数翻倍。返回明细而不只是条数，是因为
    "3 条里 2 条成功"必须说清楚哪条失败、为什么。
    """
    from kestrel.services import execution

    if not rows:
        return {"saved": 0, "failed": 0, "items": []}

    items: list[dict] = []
    saved = 0
    failed = 0
    for row in rows:
        try:
            market = str(row.get("market") or "US").upper()
            code = str(row.get("code") or "").strip()
            qty = int(row.get("qty") or 0)
            price = float(row.get("price") or 0)
            if not code or qty <= 0 or price <= 0:
                failed += 1
                items.append({"code": code or "?", "ok": False, "error": "缺少代码、数量或价格"})
                continue
            side = "buy" if str(row.get("side", "buy")).lower().startswith("b") else "sell"
            result = execution.record(
                execution.Fill(
                    market=market,
                    code=code,
                    side=side,
                    qty=qty,
                    price=Decimal(str(price)),
                    fees=Decimal(str(row.get("fee") or 0)),
                    filled_at=None,
                    source="import",
                    note="由成交单导入",
                    name=str(row.get("name") or code),
                )
            )
            # 导入行自带的交易日期要覆盖默认的"现在"，否则持仓天数全是错的。
            trade_date = str(row.get("trade_date") or "").strip()
            if result.get("ok") and trade_date:
                _stamp(result, trade_date)
            if result.get("ok"):
                saved += 1
            else:
                failed += 1
            items.append({"code": code, "side": side, "ok": bool(result.get("ok")),
                          "error": result.get("error"), "message": result.get("message")})
        except Exception as exc:  # noqa: BLE001
            failed += 1
            items.append({"code": str(row.get("code") or "?"), "ok": False, "error": str(exc)})
    return {"saved": saved, "failed": failed, "items": items}


def _stamp(result: dict, trade_date: str) -> None:
    """把成交日期写回刚建的那笔交易。"""
    
    ids = [result.get("trade_id")] if result.get("trade_id") else []
    ids += list(result.get("closed_trades") or [])
    if not ids:
        return
    conn = connect()
    try:
        for trade_id in ids:
            conn.execute(
                "UPDATE trade SET filled_at=?, opened_at=? WHERE id=?",
                (trade_date, trade_date, trade_id),
            )
        conn.commit()
    finally:
        conn.close()


def ocr_available() -> tuple[bool, str]:
    if shutil.which("tesseract"):
        return True, "tesseract"
    return False, ""


def ocr_image(path: str) -> tuple[str, str]:
    """Return (text, engine). Text is empty when no engine is installed."""
    ok, engine = ocr_available()
    if not ok:
        return "", ""
    try:
        result = subprocess.run(  # noqa: S603
            ["tesseract", path, "stdout", "-l", "chi_sim+eng"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.stdout or "", engine
    except Exception:  # noqa: BLE001
        return "", engine
