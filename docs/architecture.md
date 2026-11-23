# 系统架构（v0.4.0）

Kestrel 采用**单 Python 守护进程 + 可插拔外壳**架构：同一套后端服务与前端工作台既可运行为原生桌面客户端（`pywebview` 窗口 + 跨平台系统托盘常驻），也可作为本地浏览器应用或反向代理后的 Docker 容器运行。

```
                  ┌──────────────── daemon (FastAPI + APScheduler + SQLite WAL) ──────────────┐
 公开行情数据源 →  │ datasources → indicators → strategies / backtest → rules (G1–G4 四关证伪) │
 (东财/腾讯/新浪)  │                                      ↓                                    │
                  │                     planning → recommendation (含费/整手/现金约束)         │
                  │                                      ↓                                    │
                  │               notification outbox → 推送渠道 / 签名回执页 /r/{code}        │
                  │                                      ↓                                    │
                  │         execution (OrderIntent/T+1/FIFO) → portfolio & journal → 实盘偏离  │
                  └───────────────────────────────────────────────────────────────────────────┘
                                                      ↑
                   三栏 Web 工作台 (`data-privacy` / `@container`) · pywebview 原生窗口 · 系统托盘
```

## 模块分层

| 分层 | 模块路径 | 核心职责 |
|---|---|---|
| 桌面外壳与托盘 | `desktop/app.py`<br>`desktop/tray.py`<br>`desktop/autostart.py` | `1360×860` 原生窗口（最小 `960×640`）、`--minimized` 静默启动、关闭窗口隐藏驻留后台；macOS Cocoa 主线程 `NSStatusItem` + Windows/Linux `pystray` 双后端 7 项双语托盘菜单；跨平台开机自启配置同步（`LaunchAgents` / `Startup` / XDG `.desktop`） |
| 三栏工作台与隐私 | `web/templates/`<br>`web/static/` | `aside.app-rail` 可折叠分组 SVG 导航栏 + `header.workspace-bar` 顶栏 + `aside.copilot-drawer` 右侧 380px AI 抽屉；`@container main` 容器查询自适应栅格；`html[data-privacy="on"]` 与 `data-priv="money"\|"qty"` 首屏纯 CSS 零闪烁隐私遮罩 |
| 行情数据层 | `datasources/` | 多源适配器与 5 分钟熔断降级链、日线缓存 `bar_daily`、港股真实每手股数与带点号美股代码解析 |
| 多周期聚合 | `services/timeframe.py` | `fast`（快进快出）/ `short`（日线）/ `medium`（周线）/ `long`（月线）K 线折叠、预热根数、时间止损与止损带宽映射 |
| 动态资金账本 | `services/portfolio.py` | 初始本金 `account.capital` → 买卖确认自动扣现/回笼与费用入账、可用现金/总资产/银证转账及存量底仓手动校准 |
| 智能市场与频率顾问 | `services/advisor.py` | 结合本金规模、整手门槛、最低佣金拖累与各市场牛熊环境推荐市场组合与交易周期 |
| 右侧炒股 Agent | `services/copilot.py` | OpenAI 兼容模型 API 门禁、本地正则 + System Prompt 双层话题守卫、实时注入账户持仓与 `strategy_falsification` 证伪上下文 |
| 技术指标引擎 | `indicators/engine.py` | Wilder 平滑 `RSI` / `ATR`、均线与斜率、盘中交易时段归一化量比、VCP 波动收缩率、前后根交叉判定 |
| 策略与规则库 | `strategies/` | JSON 声明式规则编译器、7 条内置策略与 AI 分析师输出 JSON Schema |
| 量化回测与高原 | `services/backtest.py` | 严格历史前缀计算、默认 `next_open=True` 次日开盘防前视成交、`fee_drag_pct` / `slippage_bps` 摩擦计入、`profit_factor`、逐 K 年化夏普 `bar_sharpe`、16 位 `determinism_hash`、固定种子（`seed=42`, `n_perms=500`）极速置换检验与 `3×3` 参数高原稳健性热力矩阵 |
| 四关证伪与偏离 | `services/rules.py` | 串联 G1 扣费滑点样本筛选 → G2 多折滚动前推 → G3 置换显著性检验（`p >= 0.10` 强制降级防伪优势）→ G4 实盘/纸面表现偏离监控（`live_drift_report`） |
| 建仓风控规划 | `planning/builder.py` | 信号转可执行计划：自适应入场区间、最小报价 `Tick` 对齐、可用现金与整手/碎股/科创板 `200+1` 约束算仓 |
| 费率模型 | `fees/` | 三市场交易所规费模型与可编辑券商套餐（含港股碎股 `odd_lot_buy`、美股小数股 `fractional_us`） |
| 准实盘执行与归因 | `services/execution.py` | `OrderIntent` / `PreTradeCheck` / `BrokerAdapter` 契约、A 股 `T+1` 锁定、FIFO 分批卖出、买费按比例分摊、实时偏差预演与 What-if 盈亏归因 |
| 持仓盯盘与退出 | `services/positions.py`<br>`services/exits.py` | 扣费净浮盈、距止损百分比、去重推送提醒；每笔持仓唯一动作建议（破止损离场 / A 股 `T+1` 锁仓预警 / 到 T1 分批止盈 / `+1R` 保本与 `+2R` 移动止损 / 时间止损） |
| 全局术语学习 | `services/glossary.py` | 75+ 分类交易词条与中英文自然文本匹配，支撑全站可开关柔和虚线悬停查词 |
| 后台调度器 | `scheduler/service.py` | `APScheduler` 定时市场扫描与持仓现价刷新、止损/止盈触发监控（窗口隐藏至托盘期间持续运行） |

## 桌面系统托盘与后台常驻契约

1. **双后端主线程安全托盘（`desktop/tray.py`）**：
   - **macOS**：由于 `pywebview` 在主线程持有 Cocoa `NSApplication` 事件循环，子线程创建状态栏图标会触发 `BoardServices` 断言崩溃。`TrayController` 通过 `PyObjCTools.AppHelper.callAfter` 将 `NSStatusItem`（18×18 品牌图标，`隼` 字符兜底）、7 项中英文动态菜单（打开主窗口、在浏览器中打开、后台盯盘状态指示、立即扫描市场、切换金额隐私打码、关闭窗口时驻留后台勾选、彻底退出 Kestrel）以及 Dock 点击重开代理安全投递至主线程执行。
   - **Windows / Linux**：采用 `pystray` 守护线程驱动系统托盘图标与同等 7 项菜单。
   - **无头环境隔离**：设置环境变量 `KESTREL_TRAY=0`（如 CI 测试与打包构建冒烟校验）时无条件跳过托盘初始化，确保零阻塞。
2. **关闭驻留与静默自启（`desktop/app.py` & `desktop/autostart.py`）**：
   - 窗口绑定 `window.events.closing`：当托盘激活且 `desktop.close_to_tray` 为 `True`（默认开启）时，点击窗口关闭按钮仅调用 `window.hide()` 并返回 `False` 阻止进程退出，本地 FastAPI 与 `APScheduler` 继续后台盯盘；仅在托盘菜单点击「退出 Kestrel」或触发 `⌘Q` 退出代理时才设置 `quitting=True` 并优雅停止服务。
   - `kestrel desktop --minimized`（或 `--background`）支持初始隐藏主窗口直接驻留托盘；`autostart.sync_autostart()` 自动管理 macOS `~/Library/LaunchAgents/com.kestrel.desktop.plist`、Windows `Startup\Kestrel.cmd` 与 Linux `~/.config/autostart/kestrel.desktop`（单元测试通过 `KESTREL_AUTOSTART_DIR` 完全隔离）。

## UI 三栏工作台与隐私属性契约

1. **响应式三栏与 Container Queries**：
   - 外壳由左栏 `aside.app-rail`（`220px`，可折叠为 `64px` 图标栏；`960px–1220px` 或展开 Copilot 且窗口 `<1400px` 时自动收窄；`<960px` 转为毛玻璃抽屉）、顶栏 `header.workspace-bar` 与右侧 `aside.copilot-drawer`（`380px`）组成。
   - 主内容区 `main.workspace-main` 声明 `container: main / inline-size`，内部 `.stat-grid`、`.grid.g2/.g3/.g4`、`.kpi`、`.advice-figs`、`.settings-layout` 等组件均依据主区实际可用宽度平滑重排，彻底消除展开右侧 Copilot 时的卡片挤压或横向滚动条。
2. **零闪烁金额隐私遮罩（`data-privacy` / `data-priv`）**：
   - 服务端 `_ctx()` 读取 `ui.privacy` 并在首屏直接输出 `<html data-privacy="on|off">`；前端点击 `#toggle-privacy`、按 `Alt+P`、切换设置页开关或点击系统托盘菜单均实时翻转该属性并持久化至 `POST /api/ui/privacy`。
   - 个人敏感数值统一标记 `data-priv="money"`（本金、总资产、现金、持仓市值、手续费、盈亏金额）或 `data-priv="qty"`（持仓/成交股数），敏感输入框标记 `data-priv-input="1"`；开启隐私后由纯 CSS 即时遮罩为等宽 `••••••` / `••••`（输入框失焦模糊、聚焦可编辑），而公开行情股价、涨跌幅 `%`、胜率与 R 倍数完全保留可读。

## 四关量化证伪与防前视保障

1. **严格防前视与次日开盘成交（`services/backtest.py`）**：
   - 信号与指标仅在截止第 `i` 根 K 线的历史前缀窗口 `bars[:i+1]` 上计算（违背即抛 `LookaheadError`）；默认 `next_open=True`，第 `i` 根收盘确认信号后以第 `i+1` 根开盘价叠加单边滑点 `slippage_bps` 成交（若次日低开直接跌破止损位则放弃入场并计入 `skipped_gap_down`；若最后一根 K 线刚出信号尚无次日开盘价则归入 `pending_signal`）。
   - 同时扣除双边手续费率 `fee_drag_pct`（前端以基点 `fee_bps` 配置），输出盈亏比因子 `profit_factor`、逐根持仓状态年化夏普率 `bar_sharpe` 与 16 位 SHA-256 确定性校验指纹 `determinism_hash`。
2. **四关统计证伪流水线（`services/rules.py`）**：
   - **G1 扣费滑点筛选（`cost_sample`）**：检验扣除真实手续费与次日开盘滑点后是否具备足够有效交易样本（`≥ 12` 笔）与正期望；
   - **G2 多折滚动前推（`walk_forward`）**：多时间折分段回放并与同期同跨度无脑持有基准对比超额收益稳定性；
   - **G3 置换显著性检验（`permutation`）**：固定种子（`seed=42`, `n_perms=500`）随机打乱收益序列评估 `p` 值；若前两关为正但 `p ≥ 0.10`，强制将「有优势」降级为「不稳定」（`downgraded_by_perm=True`），防止随机噪声过拟合；辅以 `/sim` 页 `3×3`（3 档持仓上限 × 3 档 ATR 止损倍数）参数高原热力图识别「稳健高原」与「孤立尖刺」；
   - **G4 实盘偏离监控（`live_drift`）**：`live_drift_report()` 持续追踪该规则在历史推荐单与用户已平仓交易中的实际平均 R，当样本 `≥ 3` 且显著低于回测期望时自动触发偏离预警横幅。

## 不可妥协的工程铁律

1. **金额一律采用微单位整型（Micro-units）**：SQLite 中绝不存储浮点价格，仅在展示边界通过 `core/money.py` 转换。
2. **仓位与止损完全由本地 `planning` 决定**：无论使用规则引擎还是大模型/WorkBuddy，模型输出的股数一律丢弃，止损必须落在当前周期止损带内且净盈亏比 `≥ 1.5`。
3. **R 倍数永远锚定初始止损价**：上移保护性止损绝不篡改原始分母 `1R`。
4. **止损单向只升不降**：`exits.apply_stop` 严格拒绝下调止损价，每次上移完整记录于 `stop_change`。
5. **AI 严禁编造统计数字**：右侧 Copilot 仅可引用上下文注入的真实账户持仓与 `strategy_falsification` 数据，缺失样本或未验证时必须如实说明。

## 市场范围与成交记录

首次引导、行情、建议和扫描共享有效市场列表。用户保存的选择持久化，未保存时按初始资金推荐扫描范围。顾问使用同一套周期评分生成首次预览和运行建议。自动扫描在拉取数据后如需切换到更长周期，会先补齐更长历史。行情落后于该市场最近收盘日时，规则和模型引擎都排除相应标的。

录入、签名回执、指定持仓卖出和交割单导入以实际成交写台账。成交、关联建议和执行明细共用事务。带请求编号的录入支持重试，回执提交还在同一事务验证并扣减链接可用次数。指定卖出批次受该标的持仓和实际成交时点约束。跨批次的卖出费用在货币微单位上精确分摊。建议快照只用于承接合法风险位与偏差复盘。
