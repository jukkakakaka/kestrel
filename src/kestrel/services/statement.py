"""把券商导出的成交记录变成台账条目。

券商的格式不统一，而且会变：同一家券商不同版本、不同市场的列顺序都可能不同。
所以这里不写死列位置，而是分两层：

1. **表格解析（首选）**：券商 App 的「复制」通常是一段带表头的 TSV/CSV。
   先认表头——「成交时间 / 证券代码 / 买卖方向 / 成交数量 / 成交均价 / 手续费」
   这些词在各家叫法略有不同，但都能列一张同义词表覆盖。认出来了，列顺序
   怎么排都无所谓，多一列少一列也不影响。
2. **逐行兜底**：不是表格（比如手打一行）时，回到原来的宽松规则：只找
   方向、代码、数量、价格四件事，不假设列布局。

解析结果**永远带一段说明**：认出了哪些列、哪些字段是推算的、哪几行有疑问。
用户看得见，才知道能不能直接写入。识别不出来的行不会静默丢弃。

不支持的只有一件事：图片。这里不做 OCR，也不假装能做——截图里的数字
不如复制出来的文字准，而错误的价格比没有价格更糟。页面上不会出现
「本机没有 OCR」这种对用户毫无用处的提示，只会告诉用户「可以直接粘贴」。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

from kestrel.datasources.base import normalize_code

SIDE_BUY = ("买入", "买进", "买入成交", "BUY", "BOT", "Bought", "证券买入")
SIDE_SELL = ("卖出", "卖出成交", "SELL", "SLD", "Sold", "证券卖出")

NUMBER = r"[-+]?\d[\d,]*(?:\.\d+)?"

RE_SIDE = re.compile("|".join(SIDE_BUY + SIDE_SELL), re.IGNORECASE)
RE_CODE_CN = re.compile(r"\b(\d{6})\b")
RE_CODE_HK = re.compile(r"\b(\d{4,5})\b")
RE_QTY = re.compile(rf"({NUMBER})\s*(?:股|shares?|units?)", re.IGNORECASE)
RE_PRICE = re.compile(rf"(?:@|均价|成交价|成交均价|价格|price)\s*[:：]?\s*({NUMBER})", re.IGNORECASE)
RE_AMOUNT = re.compile(rf"(?:金额|成交额|成交金额|amount)\s*[:：]?\s*({NUMBER})", re.IGNORECASE)
RE_DATE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
RE_FEE = re.compile(rf"(?:手续费|佣金|费用|fee|commission)\s*[:：]?\s*({NUMBER})", re.IGNORECASE)

# ---------------------------------------------------------------- 表头同义词
#
# 每家公司叫法不一样，但字段就这么几个。匹配按关键字包含，长关键字优先，
# 所以「成交均价」不会被「成交」抢走，「手续费」不会被「费」抢走。

COLUMNS: dict[str, tuple[str, ...]] = {
    "date": ("成交时间", "成交日期", "交易时间", "交易日期", "委托时间", "日期",
             "time", "date", "trade date", "datetime", "filled at"),
    "code": ("证券代码", "股票代码", "代码", "symbol", "ticker", "code", "security code"),
    "name": ("证券名称", "股票名称", "名称", "name", "security name"),
    "side": ("买卖方向", "买卖", "方向", "交易类型", "业务类型", "side", "action",
             "type", "buy/sell"),
    "qty": ("成交数量", "成交股数", "数量", "股数", "quantity", "qty", "shares",
            "filled quantity", "executed quantity"),
    "price": ("成交均价", "成交价格", "成交价", "价格", "均价", "price", "avg price",
              "average price", "executed price"),
    "amount": ("成交金额", "成交额", "金额", "amount", "notional", "turnover"),
    "fee": ("手续费", "佣金", "费用", "交易费用", "fee", "fees", "commission"),
    "market": ("市场", "交易市场", "market", "exchange"),
    "currency": ("币种", "货币", "currency"),
}

_HEADER_HINTS = ("代码", "code", "ticker", "symbol", "数量", "qty", "quantity",
                 "价格", "price", "方向", "side", "名称", "name")


@dataclass
class ParsedFill:
    market: str
    code: str
    name: str
    side: str            # buy / sell
    qty: float
    price: float
    fee: float = 0.0
    trade_date: str = ""
    raw: str = ""
    confidence: float = 0.0
    problems: list[str] = field(default_factory=list)


def _to_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(str(text).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _to_float(text: str) -> float:
    value = _to_decimal(text)
    return float(value) if value is not None else 0.0


def _detect_market(code: str, line: str) -> str:
    raw = str(code or "").strip().upper()
    if re.fullmatch(r"\d{6}", raw):
        return "CN"
    if re.fullmatch(r"\d{1,5}", raw):
        return "HK"
    upper = line.upper()
    if re.search(r"\bHK\.?\d", upper):
        return "HK"
    if re.search(r"\bCN\.?\d", upper):
        return "CN"
    return "US"


def _market_from_text(text: str) -> str | None:
    raw = str(text or "").strip().upper()
    if not raw:
        return None
    if raw in ("CN", "SH", "SZ", "SSE", "SZSE") or "沪" in raw or "深" in raw or "A股" in raw:
        return "CN"
    if raw in ("HK", "HKEX") or "港" in raw:
        return "HK"
    if raw in ("US", "NASDAQ", "NYSE", "AMEX") or "美" in raw:
        return "US"
    return None


def _pick_code(line: str) -> str | None:
    upper = line.upper()
    m = re.search(r"\b(?:HK|US|CN)\.?(\d{4,6}|[A-Z]{1,5})\b", upper)
    if m:
        return m.group(1)
    m = RE_CODE_CN.search(line)
    if m:
        return m.group(1)
    # 代码形状比「是不是 1–5 位数字」可靠：`买 100 股` 里的 100 不是代码。
    # 先找显式的交易所前缀，再找 6 位（A 股），最后才考虑 5 位港股。
    if re.search(r"\bHK\b", upper):
        m = re.search(r"\b(\d{4,5})\b", line)
        if m:
            return m.group(1).zfill(5)
    for token in re.findall(r"\b[A-Z]{2,5}\b", upper):
        if token in {"USD", "HKD", "CNY", "RMB", "BUY", "SELL", "BOT", "SLD",
                     "HK", "US", "CN", "TAF", "SEC"}:
            continue
        return token
    m = re.search(r"\b(\d{4,5})\b", line)
    if m:
        return m.group(1).zfill(5)
    return None


# ---------------------------------------------------------------- 表格识别

def _split(line: str) -> list[str]:
    """按最可能的分隔符切一行。TSV 优先，其次逗号/分号/竖线/多空格。"""
    for sep in ("\t", ",", ";", "|"):
        if sep in line:
            return [cell.strip().strip('"').strip("'") for cell in line.split(sep)]
    return [cell.strip() for cell in re.split(r"\s{2,}", line.strip()) if cell.strip()]


def _header_map(cells: list[str]) -> dict[str, int] | None:
    """认出表头就返回 ``{字段: 列号}``，否则 None。

    只认「至少命中两个关键列」的表头，避免把一条普通成交行误判成表头。
    """
    lowered = [str(c).strip().lower() for c in cells]
    mapping: dict[str, int] = {}
    for index, cell in enumerate(lowered):
        if not cell:
            continue
        best: tuple[int, str] | None = None
        for name, words in COLUMNS.items():
            if name in mapping:
                continue
            for word in words:
                token = word.lower()
                if token and token in cell and (best is None or len(token) > best[0]):
                    best = (len(token), name)
        if best is not None:
            mapping.setdefault(best[1], index)
    hits = sum(1 for key in ("code", "qty", "price", "date", "side") if key in mapping)
    if hits < 2:
        return None
    # 一列既是名称又是代码的情况：代码列必须真的能解析出代码，交给调用方判断。
    return mapping


def _looks_like_header(line: str) -> bool:
    cells = _split(line)
    if len(cells) < 2:
        return False
    if _header_map(cells):
        return True
    joined = " ".join(cells).lower()
    return any(hint.lower() in joined for hint in _HEADER_HINTS) and not RE_SIDE.search(line)


def _row_from_cells(cells: list[str], mapping: dict[str, int], raw: str) -> ParsedFill | None:
    def cell(name: str) -> str:
        index = mapping.get(name)
        if index is None or index >= len(cells):
            return ""
        return cells[index]

    side_text = cell("side")
    side_match = RE_SIDE.search(side_text) or RE_SIDE.search(raw)
    if side_match is None:
        return None
    side = "buy" if side_match.group(0).upper() in [s.upper() for s in SIDE_BUY] else "sell"

    code_raw = cell("code").strip()
    name = cell("name").strip()
    # 有些券商把「代码 名称」放在同一格。
    if code_raw and " " not in code_raw and not re.fullmatch(r"[A-Za-z0-9.]{1,10}", code_raw):
        code_raw = _pick_code(code_raw) or ""
    if not code_raw:
        code_raw = _pick_code(raw) or ""
    if not code_raw:
        return None

    market = _market_from_text(cell("market")) or _market_from_text(cell("currency"))
    if market is None:
        market = _detect_market(code_raw, raw)
    code = normalize_code(market, code_raw)

    qty_text = cell("qty")
    qty_value = _to_decimal(qty_text) if qty_text else None
    price_text = cell("price")
    price_value = _to_decimal(price_text) if price_text else None
    amount_text = cell("amount")
    amount_value = _to_decimal(amount_text) if amount_text else None

    problems: list[str] = []
    # 金额 ÷ 数量 = 价格：券商有时只给金额，这一格学费由我们替用户算。
    if price_value is None and amount_value and qty_value:
        price_value = amount_value / qty_value
        problems.append("价格由金额÷数量推算")
    if qty_value is None and amount_value and price_value:
        qty_value = amount_value / price_value
        problems.append("数量由金额÷价格推算")
    qty = float(qty_value) if qty_value is not None else 0.0
    price = float(price_value) if price_value is not None else 0.0

    fee_text = cell("fee")
    fee = _to_float(fee_text) if fee_text else 0.0

    date_text = cell("date")
    date_match = RE_DATE.search(date_text) if date_text else None
    if date_match is None:
        date_match = RE_DATE.search(raw)
    trade_date = (
        f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-"
        f"{int(date_match.group(3)):02d}"
        if date_match
        else ""
    )

    if qty <= 0:
        problems.append("没找到数量")
    if price <= 0:
        problems.append("没找到成交价")

    return ParsedFill(
        market=market,
        code=code,
        name=name or code,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        trade_date=trade_date,
        raw=raw,
        confidence=round(max(0.1, 1.0 - 0.3 * len(problems)), 2),
        problems=problems,
    )


def parse_line(line: str) -> ParsedFill | None:
    """单行兜底：不假设列位置，只找方向、代码、数量、价格。"""
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
    qty = _to_float(qty_match.group(1)) if qty_match else 0.0

    price_match = RE_PRICE.search(text)
    price = _to_float(price_match.group(1)) if price_match else 0.0

    problems: list[str] = []
    if not price:
        amount = RE_AMOUNT.search(text)
        if amount and qty:
            price = _to_float(amount.group(1)) / qty
            problems.append("价格由金额÷数量推算")

    fee_match = RE_FEE.search(text)
    fee = _to_float(fee_match.group(1)) if fee_match else 0.0

    date_match = RE_DATE.search(text)
    trade_date = (
        f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-"
        f"{int(date_match.group(3)):02d}"
        if date_match
        else ""
    )

    market = _detect_market(code, text)
    code = normalize_code(market, code)

    name = ""
    for word in re.findall(r"[一-鿿]{2,8}", text):
        if word in ("买入", "卖出", "成交", "股", "手", "手续费", "佣金", "金额",
                    "均价", "成交价", "证券", "股票"):
            continue
        name = word
        break

    if qty <= 0:
        problems.append("没找到数量")
    if price <= 0:
        problems.append("没找到成交价")

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
        confidence=round(max(0.1, 1.0 - 0.35 * len(problems)), 2),
        problems=problems,
    )


def parse_table(text: str) -> dict:
    """解析并使用表头映射；返回行、映射和一段给用户看的说明。"""
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return {"rows": [], "mapping": {}, "note": "", "skipped": 0, "mode": "empty"}

    header_index = None
    mapping: dict[str, int] = {}
    for index, line in enumerate(lines[:5]):
        candidate = _split(line)
        guessed = _header_map(candidate)
        if guessed and (index == 0 or len(guessed) >= 3):
            header_index, mapping = index, guessed
            break

    rows: list[ParsedFill] = []
    skipped = 0
    for line in lines:
        if header_index is not None and line is lines[header_index]:
            continue
        parsed = None
        if mapping:
            parsed = _row_from_cells(_split(line), mapping, line)
        if parsed is None:
            parsed = parse_line(line)
        if parsed is None:
            skipped += 1
            continue
        rows.append(parsed)

    mode = "table" if mapping else "line"
    note = ""
    if mapping:
        labels = {
            "date": "时间", "code": "代码", "name": "名称", "side": "方向",
            "qty": "数量", "price": "价格", "amount": "金额", "fee": "费用",
            "market": "市场", "currency": "币种",
        }
        found = "、".join(labels[key] for key in labels if key in mapping)
        note = f"认出了表头：{found}。列顺序不影响结果。"
    elif rows:
        note = "没有表头，按每行里的方向、代码、数量和价格解析。"
    if skipped:
        note = (note + " " if note else "") + f"{skipped} 行没认出，已跳过。"

    return {
        "rows": [asdict(row) for row in rows],
        "mapping": mapping,
        "note": note,
        "skipped": skipped,
        "mode": mode,
    }


def parse(text: str) -> list[dict]:
    return parse_table(text)["rows"]


def save(rows: list[dict]) -> dict:
    """把解析出来的成交行写进台账。

    卖出会按 FIFO 平掉已有持仓，而不是凭空开一个"做空"——早期版本这么做，
    导一份月度对账单就能把持仓数翻倍。返回明细而不只是条数，是因为
    "3 条里 2 条成功"必须说清楚哪条失败、为什么。
    """
    from kestrel.core.quantity import to_qty
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
            qty = to_qty(row.get("qty") or 0)
            price = Decimal(str(row.get("price") or 0))
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
                    price=price,
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
    from kestrel.db.engine import connect

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
