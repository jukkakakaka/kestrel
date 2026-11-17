# Kestrel 项目核心上下文 (`contexts/context.md`)

> 版本：`v0.4.0` · 技术栈：Python 3.11+ · FastAPI + Jinja2 + SQLite（微单位整型账本）+ PyInstaller / pywebview 原生桌面与跨平台系统托盘常驻终端

## 1. 产品定位与核心能力

Kestrel 是一款本地优先、多市场（A 股 CN / 港股 HK / 美股 US）、多周期（`fast` 快进快出 / `short` 短线 / `medium` 中线 / `long` 长线 / `auto` 智能顾问）的智能炒股、量化证伪与交易风控终端。
当前定位为**「准实盘交易完整闭环（不含真实资金自动下单）」**：

- **三栏自适应金融工作台与全局线性 SVG 视觉体系**：采用左侧可折叠三分组导航栏（`aside.app-rail`，`220px ↔ 64px` 图标栏 / `<960px` 侧滑抽屉）+ 顶部 `56px` 毛玻璃吸顶工具栏（`header.workspace-bar`）+ 右侧 `380px` 专业炒股 Agent 抽屉（`aside.copilot-drawer`），基于 CSS Container Queries（`@container main`）实现任意窗口尺寸与右侧栏开关下的丝滑自适应排版；全站统一采用 1.75px 线性 SVG 图标宏（零粗糙 emoji），支持太阳/月亮/系统显示器三态即时无闪烁主题切换与中英双语切换（1098 keys 100% 对齐）。
- **一键金额隐私打码（`data-privacy` / `data-priv`）**：顶栏眼睛按钮（`#toggle-privacy`）或快捷键 `Alt+P` 一键开启隐私保护并持久化至 `ui.privacy`；SSR 首屏纯 CSS 即时将个人资金/总资产/现金/市值/盈亏（`[data-priv="money"]`）与持仓股数（`[data-priv="qty"]`）遮罩为等宽 `••••••` / `••••`，未聚焦金额输入框（`[data-priv-input="1"]`）自动模糊，同时保留行情股价、涨跌幅 `%` 与 R 倍数清晰可读。
- **跨平台原生系统托盘与后台常驻盯盘**：`desktop/tray.py` 在 macOS 通过 `PyObjCTools.AppHelper.callAfter` 于 Cocoa 主线程安全创建 `NSStatusItem` 菜单栏图标与 7 项双语菜单（Windows / Linux 使用 `pystray` 守护线程，`KESTREL_TRAY=0` 支持无头禁用）；关闭主窗口默认隐藏至后台（`desktop.close_to_tray=True`），保持本地 `uvicorn` 与 `APScheduler` 盯盘运行，支持从托盘或 macOS Dock 图标随时唤醒、`kestrel desktop --minimized` 静默启动及跨平台开机自启管理（`desktop/autostart.py`）。
- **动态资金账本与手动校准**：`account.capital` 作为初始本金，随每笔确认买入自动扣减现金与买费、FIFO 卖出回笼现金并按比例分摊买费；支持随时通过顶栏资产胶囊手动校准可用现金、总资产、银证出入金，以及补录/修改/清零手头已有股票底仓。
- **准实盘风控契约与实际成交归因**：具备完整的 `OrderIntent` → `pre_trade_check` → `BrokerAdapter` 接口契约，强制执行 A 股 100 股、科创板/北交所 `200+1`、A 股 `T+1` 锁定、港股差异每手与碎股、美股小数股及 `Tick` 对齐；提供新手限价单实操指引，允许录入与建议不同的**实际成交价、实际股数与离场原因**，量化计算实时偏差与 **What-if 盈亏归因**。
- **四关量化证伪与统计检验体系**：`backtest.py` 默认次日开盘防前视成交（`next_open=True`），支持可配置手续费率 `fee_bps` 与单边滑点 `slippage_bps`、输出 `profit_factor` / 逐 K 年化夏普 `bar_sharpe` / 16 位确定性指纹 `determinism_hash`、固定随机种子（`seed=42`, `n_perms=500`）极速置换检验 `p` 值与 `3×3` 参数高原稳健性热力矩阵；`rules.py` 串联 **G1 扣费滑点筛选 → G2 多折滚动前推 → G3 置换显著性降级保护 → G4 实盘表现偏离监控** 四关证伪流水线。
- **右侧专业炒股 Agent（Copilot）与 75+ 术语「边做边学」**：Copilot 需配置用户自有 OpenAI 兼容 API，实时注入账户持仓、活跃建议与 `strategy_falsification` 证伪统计且严禁编造回测指标，双层守卫严格限制仅回答股票交易与 Kestrel 相关问题；顶栏开启「边做边学」后全站自然出现的 75+ 交易术语柔和虚线标注、悬停/点击释义并支持追问 AI，关闭即零下划线零打扰。

## 2. 核心目录结构

- `src/kestrel/core/`：`money.py`（微单位整型换算、多币种折算 CNY、三市场最小报价单位 Tick 规整）、`quantity.py`（A 股 100 股、科创板/北交所 `200+1`、港股差异每手、美股 `0.0001` 小数股及最低金额门槛校验）。
- `src/kestrel/db/`：`engine.py`（SQLite WAL + `busy_timeout=30000` + `transaction(conn)` 原子事务守卫）、`migrations/0001..0013`（含 `cash_ledger`、`copilot_message`、`order_intent`、`recommendation.fill_qty`、`trade.entry_fees`、`trade.exit_reason`）。
- `src/kestrel/desktop/`：`app.py`（`1360×860` 原生窗口、`--minimized` 静默启动与关闭驻留后台拦截）、`tray.py`（macOS Cocoa 主线程 `NSStatusItem` + Windows/Linux `pystray` 双后端 7 项菜单托盘控制器）、`autostart.py`（macOS `LaunchAgents` / Windows `Startup` / Linux XDG 开机自启同步与 `KESTREL_AUTOSTART_DIR` 测试隔离）。
- `src/kestrel/indicators/engine.py`：Wilder 平滑 `RSI` / `ATR`、均线与斜率、盘中时段折算量比、VCP 波动收缩、`cross_above`/`cross_below` 前值对比。
- `src/kestrel/services/`：
  - `portfolio.py`：动态资金与持仓市值核算、`calibrate_cash()` 与 `calibrate_position()`；
  - `execution.py`：准实盘委托意图与预检、FIFO 卖出、A 股 `T+1` 拦截、分批平仓买费分摊、实时偏差预演 `preview_fill_vs_plan()`、执行质量与 What-if 归因 `quality_report()`；
  - `backtest.py` & `rules.py`：次日开盘防前视回测、`fee_drag_pct` / `slippage_bps` 摩擦计入、500 次固定种子置换检验、`3×3` 参数高原热力矩阵、G1–G4 四关证伪与 `live_drift_report()` 实盘偏离预警；
  - `advisor.py`：`recommend_markets()`（按资金、费率、手数门槛推荐市场及周期）与多市场牛熊环境评估；
  - `copilot.py`：大模型 API 门禁、双层话题范围守卫、实时账户与策略证伪上下文组装、对话持久化；
  - `glossary.py`：75+ 高频术语（含分类与中英文匹配词表）；
  - `config.py` / `exits.py` / `positions.py` / `journal.py` / `statement.py` / `scan.py` / `analyst.py` / `updates.py`。
- `src/kestrel/web/`：FastAPI + Jinja2 模板（`base.html` 三栏工作台、`_page_icons.html` 40+ 线性 SVG 图标宏、12 个业务视图与 3 个全局抽屉/弹窗）+ 响应式 Container Queries 样式 `app.css` + 原生 JS（`app.js`、`fill.js`、`calibrate.js`、`copilot.js`）。
- `packaging/`、`scripts/` 与 `.github/workflows/`：PyInstaller `kestrel.spec`（含 PyObjC / tray / autostart 隐藏导入）、macOS `.app` & `.dmg` 独立 `.build-venv` 打包脚本、`make_screenshots.py`（`1440×900` 自包含演示截图生成）与 GitHub Actions 多版本 CI。

## 3. 开发与验证基线

- **语言规范**：解释、注释、技术文档与 Git 提交信息统一使用**中文**，代码标识与路径保持英文；
- **校验命令**：`bash scripts/check.sh`（依次执行 `uv run ruff check src tests scripts`、`uv run python scripts/check_locales.py`、`uv run python -m pytest -q`）；
- **人工测试文档**：外层工作区 `/Users/jukka/Desktop/Rich/需要人工协助的测试用例.md` 同步维护，每次版本演进必须覆盖全部桌面端与工作台人工验证场景。
