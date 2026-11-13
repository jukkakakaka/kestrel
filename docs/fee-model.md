# Fee model

Rates change. Every number here was checked against public broker pages in
November 2026 and must be re-verified against a monthly statement before you trust a
P&L figure. If you find a discrepancy, fix `fees/calculator.py` and add a case to
`tests/unit/test_fees.py`.

## Two layers

1. **Exchange rules** — statutory charges nobody can waive.
2. **Broker plan** — commission and platform fee, which vary per broker and per promo.

`quote_fees(market, amount, shares, is_sell, broker)` combines them.

## Hong Kong

| Item | Rule |
|---|---|
| Stamp duty | 0.1 % per side, rounded **up** to the nearest HKD 1 |
| Trading fee | 0.00565 % |
| SFC transaction levy | 0.0027 % |
| FRC levy | 0.00015 % |
| CCASS settlement | 0.002 %, min HKD 2, max HKD 100 |
| Commission / platform | per broker plan |

A HKD 10,000 round trip on a zero-commission plan costs about **HKD 55.7 (0.56 %)**.
At HKD 1,000 it costs about **HKD 36.8 (3.7 %)** — the platform fee dominates. This is
the single most under-estimated cost for small accounts.

## United States

| Item | Rule |
|---|---|
| Platform fee | per share, with a per-order minimum |
| SEC fee | 0.0000206 × notional, sells only |
| FINRA TAF | 0.000195 per share, sells only, min 0.01 / max 9.79 |
| Stamp duty | none |

A USD 1,000 round trip costs roughly **USD 2 (0.2 %)**.

## Mainland China

| Item | Rule |
|---|---|
| Commission | ~0.025 %, min CNY 5 |
| Stamp duty | 0.05 %, **sells only** |
| Transfer fee | 0.001 % both sides |

## Built-in broker plans

| Code | Broker | Market | Commission | Platform |
|---|---|---|---|---|
| `fosun_hk` | 星财富（复星） | HK | 0 | HKD 15 / order |
| `usmart_hk` | 盈立香港 | HK | 0.03 % | HKD 12 / order |
| `za_hk` | 众安银行 | HK | 0.05 %, min 18 | — |
| `futu_hk` | 富途 | HK | 0.03 % | HKD 15 / order |
| `tiger_hk` | 老虎 | HK | 0.029 % | HKD 15 / order |
| `fosun_us` | 星财富（复星） | US | 0 | USD 0.005 / share, min 1 |
| `usmart_us` | 盈立香港 | US | 0 | USD 0.009 / share, min 1.88 |
| `za_us` | 众安银行 | US | 0 | USD 0.0099 / share, min 1.99 |
| `ibkr_us` | IBKR | US | 0 | USD 0.005 / share, min 1 |
| `cn_generic` | 通用 A 股 | CN | 0.025 %, min 5 | — |

Broker plans are editable in the UI (设置 → 成本计算 → 券商套餐); `fees/plans.py`
copies the built-ins into the database on first use and pushes the edited values
back into the calculator at startup. A code change is only needed for a new
*bulk* default:

Adding a broker is one dataclass line:

```python
"my_broker": BrokerPlan("my_broker", "我的券商", "HK",
                        commission_rate=Decimal("0.0003"),
                        platform_fixed=Decimal("10")),
```

## What the broker lets you buy

Fees decide what a trade costs; **capabilities decide whether the plan can be
executed at all**. On a 10,000 CNY account, one board lot of a 400 HKD stock
risks far more than 2 % — a broker that cannot buy an odd lot gets "one lot
exceeds the risk budget" and nothing else. The same account at a broker that
allows odd lots gets 17 shares and a plan.

Two switches live on every broker plan (成本计算 → 券商套餐 → 能买什么):

| Field | Meaning | Defaults on |
|---|---|---|
| `odd_lot_buy` | HK: buy **below one board lot** (碎股). Selling an odd lot is allowed by every broker and needs no switch. | 星财富（复星）、富途 |
| `fractional_us` | US: buy **fractional shares** (0.0001 share steps). | 老虎、盈立、IBKR、星财富（复星）、富途 |
| `fractional_min` | Minimum notional per fractional order; 0 means none. | USD 1 where enabled |

A-shares are always whole lots of 100 — no broker option changes that. When a
capability is off, plans round down to a whole board lot and a lot that breaks
the risk budget is refused with `lot_too_big`; when it is on, the scanner and the
AI path both size down to 1 share (HK) or 0.0001 share (US) instead.

These are defaults checked against public broker help pages in November 2026.
They are not promises: open your broker's fee/capability page and correct them —
the numbers you set are what the planner will use.


## Where the fee number shows up

- **Plan cards** — `est_fees` is a full round trip, and the break-even price
  includes it. A plan whose break-even is not clearly below target is rejected.
- **Journal** — a sell only adds the sell side, because the buy side was booked
  when the position was opened. Partial exits apportion fees by quantity, so the
  first slice does not carry the whole cost.
- **Cost page** — the same calculator, with an editable broker picker.

## Why this matters for a 10k account

At 10,000 CNY (~10,900 HKD) a single round trip on a Hong Kong lot costs 0.5–3.7 %
depending on notional. Two trades a month for a year is 12–80 % of the account in
fees alone if you trade small. The planner therefore refuses plans where one lot
already breaks the risk budget, and the scan page reports how many signals were
dropped for that reason.
