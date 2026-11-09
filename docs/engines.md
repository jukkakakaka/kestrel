# 引擎：规则 / 大模型 / WorkBuddy

只有一个开关：**设置 → 分析引擎 → 由谁产生建议**。操作建议页只有一个按钮，按这个开关执行。
不存在「业务扫描」和「AI 分析」并列的两套东西——那会让人不知道该信谁。

## 三种模式

### 规则引擎（`agent.provider = rules`）

条件写死在 JSON 规则里，全满足才产生信号。可复现、可审计、免费、秒出。

```
候选池 → 日线缓存 → 按周期折叠（日/周/月）→ 指标 → 每条启用的规则 evaluate()
      → 得分 ≥ 门槛 → build_ex 生成计划（止损带与有效期跟随周期）
```

规则见 `/rules`，每条都有实测结论。测下来没有超额收益的会自动停用。

### 大模型（`agent.provider = llm`）

需要 `agent.base_url`（OpenAI 兼容，如 `https://api.deepseek.com/v1`）和 `agent.model`。

流程：

1. `analyst.build_brief()` 组装简报：40 只候选 × 18 个字段 + 账户约束 + 持仓 + 每条规则的实测结论
2. 模型返回 `{picks:[{market,code,action,conviction,thesis,catalysts,risks,entry_low,entry_high,stop,target,invalidation}], market_view, pass_reason}`
3. `validate_pick()` 逐项校验，不合格的直接丢弃并记录原因
4. `size_position()` 由本地按风险预算算股数——**模型给的数量永远不采用**
5. 入库，`source='ai'`，卡片显示模型名和信心值

### WorkBuddy（`agent.provider = workbuddy`）

没有本地 HTTP 入口，所以把请求/响应倒过来做成文件队列：

```
data/bridge/pending/<id>.json   程序写入简报 + 输出契约
data/bridge/done/<id>.json      WorkBuddy 写入答案
data/bridge/archive/            已收取
data/bridge/failed/             读不出来的
```

- `POST /api/run` → 入队，返回 `{pending:true, request_id}`，页面提示「排队中」
- `POST /api/analyst/ingest` → 收取答案，走同一套校验和建仓逻辑
- 打开操作建议页会自动 ingest 一次
- `GET /api/analyst/pending` 返回队列内容和完整提示词，任何 agent 都能来消费

消费方是 WorkBuddy 的定时任务（自动化「Kestrel 队列分析」，每小时一次）。

## 风控（两种 AI 模式共用）

| 检查 | 规则 |
|---|---|
| 代码在白名单 | 必须在本次简报的候选里 |
| 止损位置 | 低于入场价，且落在当前周期的止损带内（日线 1.5%–15%、周线 3%–18%、月线 5%–30%） |
| 盈亏比 | (target − entry) / (entry − stop) ≥ 1.5 |
| 入场位置 | 在现价 ±15% 内 |
| 理由 | 至少 8 个字 |
| 行业集中 | 同一行业不超过 `risk.max_per_sector` |
| 总仓位 | 在途计划加已有持仓不超过 `risk.max_total_exposure_pct` |
| 可负担 | 一手买不起的直接拒绝并说明 |

被拒的建议全部带原因入库，在页面上可见。**一个会被过滤的模型比一个显得完美的模型可信。**

## 周期对三种模式都生效

`trade.horizon` 不是规则引擎专属：它先决定**看哪条 K 线**，然后三套引擎都在同一套 K 线上工作。

- 规则：`services/rules.py` 按周期聚合日线再验证，判定按周期分别存档；
  某条规则在日线亏钱但周线没测过，只会在日线被停用，不会整体下线。
- 回测的**止损带也随周期变**（日线 1.5–15%、周线 3–18%、月线 5–30%）。这一条不是装饰：
  月线的两个 ATR 止损经常超过 15%，用日线带宽回测会把月线信号全部拒掉，看起来像「没有信号」。
- 大模型 / WorkBuddy：`analyst.build_brief()` 里同样是聚合后的指标，
  简报带上 `horizon`（bar 大小、持仓根数、止损带），提示词里也写明——
  否则模型会对着日线指标给一个周线持仓的建议。
- 实测（100 只池、约 25 年历史，七条内置规则）：日线 5 条「无优势」、1 条「不稳定」；
  周线 2 条「不稳定」、3 条「无优势」；月线只有 `trend_pullback`（96 笔）与
  `washout_bounce`（42 笔）够样本，其余显示「样本不足」。**同一套规则在三个周期上给出三种答案，
  这正是分开存档的意义。**

## 预算

`agent.daily_budget` 限制每天调用次数。超了就**停止调用并明确报错**，
不会偷偷退回规则引擎——否则你以为在用 AI，其实在用规则。
