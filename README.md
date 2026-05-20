# A 股全市场扫描 (cn_stock_scan)

参照 [Allanli1011/us-stock-scan](https://github.com/Allanli1011/us-stock-scan) 的逻辑，
把同一套 **「MACD 三重背离 + PA 三推 75% 回撤 + ICT 周/月 PDA」** 监控体系应用到 A 股。
支持沪深京全市场 ~5400 只，也可只跑沪深 300 + 中证 500（默认）。

## 核心思路

每日盘后跑一遍扫描，对每只股票同时计算 3 个独立信号，组合打分：

| 信号 | 满分 | 折扣档 | 描述 |
|---|---:|---:|---|
| **MACD 三重背离** | +1.0 (严格 5/5) | +0.5 (宽松 4/5) | 价创新高/新低、MACD 金/死叉值递减、回调 DIF 逼近 0 不破、柱面积每段衰减 ≥10%、第三推时效 |
| **PA 三推 + 75% 回撤** | +1.0 | — | 三段同向推浪，两次回撤都落在 60–90% 区间 |
| **HTF PDA (周/月)** | +1.0 (OB+FVG 重叠) | +0.5 (单 OB 或 FVG) | 第三推极值落在更高时间框架的 Order Block 或 Fair Value Gap 内 |

满分 **3.0**；得分 ≥ 2.5 为「三重共振」，最高质量信号。

## MACD 三态评分

MACD 不再是二元 hit/miss，而是按 5 条规则的通过情况分三档：

| 类型 | 条件 | 评分 | 强度 | 图表标识 |
|---|---|---:|---|---|
| **严格 (strict)** | 5/5 全过 | **+1.0** | 原始值 | 紫色 M1/M2/M3 标记 |
| **宽松 (loose)** | 4/5（差 1 条） | **+0.5** | 原始 × 0.5 | 橙色 M1/M2/M3 标记 |
| **未命中 (miss)** | ≤ 3/5 | 0 | 0 | 不画 M 标记 |

### 5 条规则定义

| 编号 | 名称 | 含义 |
|---|---|---|
| **R1** | 价格三推创新极值 | p1→p2→p3 单调推进 ≥ `min_price_increase_pct` |
| **R2** | DIF 金/死叉值单调收敛 | 顶背离 c1>c2>c3，底背离 c1<c2<c3 |
| **R3** | DIF 回调逼近零轴 | 两次回调 DIF 逼近 0 但不破零 |
| **R4** | 柱面积严格衰减 | 三段严格递减，且每段衰减 ≥ `min_area_reduction` |
| **R5** | 第三推时效 | 第三极值距今 ≤ `recency_bars` |

CSV 输出 `macd_kind` / `macd_passed` (如 `4/5`) / `macd_failed_rules` (如 `R4`)，
图表标题尾巴的 `MACD宽松 4/5 (差R4)` 一眼能看出差在哪条规则。

## 项目结构

```
cn_stock_scan/
├── config.yaml                # 全部阈值参数
├── requirements.txt
├── src/
│   ├── config.py
│   ├── universe.py            # akshare 拉股票池 (HS300/CSI500 或全 A 股)
│   ├── data_fetcher.py        # 多源日线抓取 (sina 优先, eastmoney 兜底)
│   ├── visualization.py       # 中文 K 线图 (¥ 货币符号、A 股红涨绿跌)
│   └── indicators/
│       ├── macd.py            # 三重背离 + 5 条规则结构化结果
│       ├── swing.py           # ZigZag 摆动检测
│       ├── three_push.py      # PA 三推
│       ├── ob_fvg.py          # ICT OB / FVG
│       └── pda.py             # 周/月 HTF PDA
├── scripts/
│   ├── scan_full.py           # 主扫描（三项组合得分）
│   └── scan_macd.py           # 只看 MACD 背离的轻量扫描
├── data/                      # 缓存 (git 忽略)
│   ├── universe.csv
│   └── prices/<6位代码>.parquet
└── output/                    # CSV + 图 (git 忽略)
    └── charts/
```

## 安装

```powershell
# Python >= 3.10
pip install -r requirements.txt
```

## 使用

### 1. 首次构建股票池 + 拉日线

```powershell
# 默认 hs300_zz500（共 ~764 只），6 并发约 1-2 分钟
python -m src.data_fetcher --force-universe

# 调试：只拉前 N 只
python -m src.data_fetcher --limit 50

# 切到全市场 ~5400 只：先改 config.yaml 中 universe.source 为 a_shares_all
# 全量首拉时间预计 10-15 分钟
```

> ⚠️ **代理注意**：akshare 走的是国内数据接口（sina/eastmoney/csindex），
> 不需要也不应该走科学上网代理。若环境里设置了 `HTTP_PROXY/HTTPS_PROXY`，
> 请先 `unset` 这些变量再跑，否则会报 `ProxyError` 或被远端拒连。

### 2. 日常扫描

```powershell
# 默认：双向扫描 + 自动出图 top 1
python scripts/scan_full.py

# 只看做多 (底部) 信号，最低 2 分，画 top 5 张图
python scripts/scan_full.py --direction bottom --min-score 2.0 --plot-top 5

# 扫描前先做增量价格更新（推荐每日盘后用法）
python scripts/scan_full.py --update-prices --min-score 1.5 --plot-top 3

# 强制重刷股票池 + 全量重拉价格
python scripts/scan_full.py --refresh-data
```

输出：
- `output/full_signals_<日期>.csv` — 全部入选信号，按得分 + 市值排序
- `output/charts/<日期>_<代码>_<方向>.png` — top N 信号的标注 K 线图

### CSV 关键列

| 列 | 含义 |
|---|---|
| `ticker` | 6 位代码 |
| `name` | 股票名称 |
| `direction` | top=做空候选 / bottom=做多候选 |
| `signal` | SHORT / LONG |
| `score` | 合成得分 (0–3) |
| `market_cap_yi` | 总市值（亿元） |
| `last_close` | 最新收盘价（¥） |
| `target_price` / `target_date` | 第三推目标价 / 日期 |
| `macd_kind` | `strict` / `loose` / `miss` |
| `macd_passed` | 通过规则数，如 `4/5` |
| `macd_failed_rules` | 失败规则编号，如 `R4` 或 `R1,R3` |
| `macd_strength` | 形态强度 0–1（宽松命中已 × 0.5） |
| `three_push_hit` / `three_push_quality` | 三推是否命中 + 质量分 |
| `pda_hit` / `pda_quality` / `pda_timeframe` / `pda_zone_low,high` | PDA 命中详情 |
| `notes` | 中文交易方案（含入场/止损/目标/RR） |

### 3. 仅扫 MACD 背离

```powershell
# 输出严格 + 宽松命中
python scripts/scan_macd.py

# 同时输出差 2 条的近似命中（写到 *_near_misses.csv）
python scripts/scan_macd.py --include-near-misses
```

CSV 字段包括 `kind` / `passed` / `failed_rules` / `failed_detail`，按严格优先、强度降序排序。

### 4. K 线图阅读指南

图表分两块：上面 K 线（带年/月线 PDA 区间、入场/止损/目标线），下面 MACD 子图。

**标记说明**：
- **三推 H1/H2/H3** (顶) 或 **L1/L2/L3** (底)：三角形 + ¥价格，由 PA 三推检测器输出
- **三推起点 ★**：五角星，第一个推浪的反方向起点（也是回归目标位）
- **MACD 三点 M1/M2/M3**：**菱形 + 虚线连接**，画在价格图与 MACD 子图上
  - **紫色** = MACD 严格命中
  - **橙色** = MACD 宽松命中
  - miss 时不画
- **背离形态的视觉读法**：
  - 顶背离：价格 M 线 ↗ 上升，MACD 子图 M 线 ↘ 下降 → 价涨但动量衰
  - 底背离：价格 M 线 ↘ 微降，MACD 子图 M 线 ↗ 上升（DIF 越来越接近 0）→ 价跌但动量已竭

例：[图表预览](output/charts/) 目录下 `688271_bottom.png` 是教科书级底背离案例。

## 关键配置 (`config.yaml`)

```yaml
universe:
  source: hs300_zz500           # hs300_zz500 (默认, 764 只) | a_shares_all (~5400) | by_market_cap
  min_market_cap_cny: 10_000_000_000   # 仅 by_market_cap 时生效
  exclude_st: false             # 是否剔除 ST/退市股
  exclude_bj: false             # 是否剔除北交所 (8/4 开头)
  refresh_days: 7               # universe 缓存有效期（天）

prices:
  lookback_days: 500            # 回看窗口 ~2 年（足够覆盖 HTF 上下文）
  adjust: qfq                   # qfq=前复权 / hfq / "" 不复权
  max_workers: 6                # akshare 并发请求数（过高会被限频）
  request_sleep_sec: 0.1
  sources:                      # 抓取源优先级，按顺序尝试
    - sina                      # 国内最稳定，字段全 (OHLCV)
    - eastmoney                 # 兜底，限频较严

macd:
  fast: 12
  slow: 26
  signal: 9
  divergence:
    min_area_reduction: 0.10    # 柱面积每段最低衰减幅度 (R4)
    dif_zero_tolerance: 0.0     # 回调中 DIF 允许穿越零轴的容差 (R3)
    dif_approach_zero_ratio: 0.50  # DIF 逼近 0 的要求（前段交叉值的 50%）(R3)
    min_price_increase_pct: 0.001  # 价格创极值的最小幅度 (R1)
    recency_bars: 30            # 第三推距今最大根数 (R5)

swing:
  pct_threshold: 0.03           # ZigZag 反转阈值 (3%)

ob_fvg:
  ob_displacement_atr: 2.0      # OB 有效需要后续位移 ≥ 2 × ATR
  fvg_min_size_atr: 0.3
  atr_period: 14

three_push:
  pullback_target_pct: 0.75     # 回撤目标 75%
  pullback_tolerance: 0.15      # ±15% → [60%, 90%]

runtime:
  currency_symbol: "¥"
```

## 设计要点 / 与美股版的差异

- **数据源**：`akshare`（无需 token、覆盖沪深京全市场）。原版用 `yfinance`。
- **多源回退**：`data_fetcher.py` 抓取顺序 `sina → eastmoney`。东方财富接口对突发请求
  限频严格，sina 端点（`stock_zh_a_daily`）稳定性更好且字段齐全。
- **股票池来源**：
  - HS300/CSI500：调用 `index_stock_cons_sina`（沪深 300 自带 mktcap，中证 500 缺市值）
  - 全 A 股：优先 `stock_zh_a_spot_em` 拿快照含市值；被限频时降级到 `stock_info_a_code_name`
    （无市值，只有代码+名称）
- **复权**：默认前复权 (qfq)，避免分红除权造成的虚假背离。
- **市值单位**：CSV 中的 `market_cap_yi` 单位是 **亿元**（中文习惯）。
- **K 线配色**：A 股习惯红涨绿跌，已在 `visualization.py` 中切换。
- **指标逻辑**：MACD / 三推 / OB+FVG / PDA 全部按原始 OHLC 计算，参数与美股版一致。
  如发现某些板块（如科创板高波动）误报较多，可调高 `swing.pct_threshold`（默认 3%）。

## 常见问题

**Q: `ProxyError: Unable to connect to proxy` 或 `RemoteDisconnected`？**

A: 关闭代理后重试：
```powershell
$env:HTTP_PROXY=""
$env:HTTPS_PROXY=""
python scripts/scan_full.py ...
```
或在 bash 里 `unset HTTP_PROXY HTTPS_PROXY`。akshare 是访问国内行情接口，不应走代理。

**Q: 第一次 `stock_zh_a_spot_em` 报 connection aborted？**

A: 东方财富对该端点有严格频控（特别是在跑过几次后）。当前实现已自动降级：
- universe 拉取会回退到 `stock_info_a_code_name`（不带市值）
- 价格抓取会回退到 sina 端点
跑 hs300_zz500 模式时**完全不需要** spot 端点（HS300 走中证指数公司，CSI500 走 sina）。

**Q: 为什么有些 CSI500 股票 `market_cap_yi` 是空的？**

A: sina 的 `index_stock_cons_sina(000905)` 返回的列里没有 `mktcap`，
而 HS300 端点返回了。这是 akshare 上游 schema 差异，不影响信号生成，仅影响排序中的市值字段。

**Q: 全市场扫描多久？**

A: HS300+CSI500（764 只）首拉 ~90 秒，每日增量更新 ~30 秒，扫描 ~40 秒。
全 A 股（5400 只）首拉约 10-15 分钟，增量 1-3 分钟。

## 注意

- **仅供个人学习研究**。akshare 走公开行情接口，建议遵守频控，不要把 `max_workers` 调太高。
- **不构成任何投资建议**。三重共振只是历史形态信号，不保证未来表现，未做严格回测。
- 北交所代码（8/4 开头）部分股票流动性极差，建议生产环境开启 `exclude_bj: true`。
- 宽松 MACD 命中（loose 4/5）的强度自动 × 0.5 折扣，但仍可能引入假背离信号（典型如柱面积
  R4 失败时第三段动量反而扩大）。若追求高胜率，可在筛选时只看 `macd_kind == "strict"`。
