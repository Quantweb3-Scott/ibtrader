# Turnover Top1 IB Gateway 自动交易设计

## 目标

实现一个通过 IB Gateway 自动获取数据、自动下单的美股隔夜策略：

- 每个交易日接近收盘时，在候选池中选择当日美元成交额最高的 1 只股票。
- 收盘买入，下一交易日开盘卖出。
- 严格限制最大投入资金、账户风险、订单风险和异常状态下的行为。
- 调度必须正确处理美国夏令时/冬令时、假日和提前收盘。

本文档默认账户规模约 `$8,500`，可用资金约 `$6,000`。实盘建议先用 `$3,000` 试运行，稳定后再上调，硬上限不超过 `$6,000`。

## 非目标

- 不做融资融券，不使用杠杆。
- 不交易期权，不做盘前盘后主动交易。
- 不追求完全复刻历史回测里的最终日成交额信号；实盘必须在收盘订单截止前冻结信号。
- 不把 CSV/Parquet/cache 当作数据源。持久市场数据统一进入 `sharedata/oversea.db`。

## 策略定义

### 候选池

初始候选池：

```text
AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA, MU, SPCX
```

暂不启用：

```text
SKHY
```

原因：

- `MU` 在历史 Top1 Turnover 测试中贡献明显。
- `SPCX` 样本很短，但已出现增益，可以小资金观察。
- `SKHY` 在当前 Top1 Turnover 扩展池测试中没有入选，先禁用。

生产规则：

- `min_listed_trading_days = 20`，上市不足 20 个交易日不参与排名。
- `min_avg_dollar_volume_20d = 1_000_000_000`，20 日平均美元成交额不足则剔除。
- `min_price = 5`，避免低价股微结构风险。
- 所有 ticker 必须显式 allowlist，禁止自动加入新股票。

### 信号

`Turnover Top1` 的实盘定义：

```text
dollar_turnover_live = last_price * cumulative_rth_volume
selected = argmax(dollar_turnover_live)
```

信号冻结时间：

```text
signal_freeze_time = market_close - 15 minutes
```

正常交易日即 `15:44 America/New_York`。提前收盘日默认不交易。

注意：历史回测中的 `entry_close_turnover` 使用买入当天最终成交额，实盘无法等到最终成交额出来后再提交 MOC/LOC。因此实盘版本必须使用接近收盘时的实时累计成交额近似。系统每天收盘后要记录“15:44 选择”和“最终日成交额选择”的差异，用于评估信号漂移。

## 系统架构

```text
IB Gateway
  |
  |-- market data / historical bars / account / orders
  v
BrokerAdapter
  |
  +-- DataIngestor  ---> sharedata/oversea.db
  +-- AccountSync   ---> account_snapshots / positions / open_orders
  +-- OrderManager  ---> orders / fills
  |
  v
StrategyEngine
  |
  +-- UniverseFilter
  +-- TurnoverRanker
  +-- RiskManager
  +-- ExecutionPlanner
  |
  v
Scheduler + StateMachine + Alerting
```

建议 Python 实现：

- `ib_insync` 或官方 TWS API。IB 连接细节必须封装在 `BrokerAdapter`，策略代码不直接调用 IB API。
- SQLite 作为唯一持久数据源。
- 调度使用 `APScheduler` 或常驻事件循环，所有交易时间用 `America/New_York` 时区计算。
- 交易日历使用 `exchange_calendars` 或 `pandas_market_calendars`，禁止手写节假日。

## SQLite 数据模型

使用 `sharedata/oversea.db`。

已有表：

- `ohlcv`：日线 OHLCV。
- `asset_meta`：标的元信息。
- `market_data`：可复用的实时或快照市场数据表。

新增建议表：

```sql
CREATE TABLE IF NOT EXISTS strategy_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_turnover_snapshot (
    trade_date TEXT NOT NULL,
    snapshot_ts_utc TEXT NOT NULL,
    snapshot_ts_ny TEXT NOT NULL,
    ticker TEXT NOT NULL,
    last_price REAL,
    cumulative_volume REAL,
    dollar_turnover REAL,
    source TEXT NOT NULL,
    PRIMARY KEY (trade_date, snapshot_ts_utc, ticker)
);

CREATE TABLE IF NOT EXISTS strategy_signal (
    trade_date TEXT PRIMARY KEY,
    selected_ticker TEXT,
    signal_ts_utc TEXT NOT NULL,
    signal_ts_ny TEXT NOT NULL,
    rank_json TEXT NOT NULL,
    is_tradeable INTEGER NOT NULL,
    skip_reason TEXT,
    final_turnover_selected_ticker TEXT,
    signal_matches_final INTEGER
);

CREATE TABLE IF NOT EXISTS strategy_order (
    local_order_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    leg TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    order_type TEXT NOT NULL,
    tif TEXT,
    quantity INTEGER NOT NULL,
    limit_price REAL,
    order_ref TEXT NOT NULL,
    ib_order_id INTEGER,
    ib_perm_id INTEGER,
    status TEXT NOT NULL,
    submitted_ts_utc TEXT,
    updated_ts_utc TEXT,
    reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS strategy_fill (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_order_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    commission REAL,
    fill_ts_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_nav (
    trade_date TEXT PRIMARY KEY,
    nav REAL NOT NULL,
    strategy_equity REAL NOT NULL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    drawdown REAL,
    updated_ts_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    severity TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT
);
```

## 时间和夏令时处理

必须使用 IANA 时区：

```python
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
now_ny = datetime.now(tz=timezone.utc).astimezone(NY)
```

规则：

- 所有调度规则以 `America/New_York` 的交易所本地时间表达。
- 数据库存储 UTC 时间戳，同时保存 NY 本地时间字符串用于审计。
- 禁止使用固定偏移如 `UTC-5` 或 `UTC-4`。
- 交易日、开盘、收盘、提前收盘全部来自交易所日历。
- 默认 `trade_early_close_days = false`，提前收盘日跳过，降低 MOC/LOC 截止时间处理风险。

典型正常交易日流程：

| NY 时间 | 任务 |
| --- | --- |
| 09:20 | 确认昨夜持仓、准备卖单 |
| 09:31 | 开盘竞价结束后提交 MKT DAY 卖出单 |
| 09:32-09:35 | 校验卖出成交，必要时强制平仓 |
| 15:30 | 收盘前 preflight |
| 15:40 | 刷新实时成交额快照 |
| 15:44 | 冻结 Top1 信号并提交收盘买单 |
| 16:02-16:10 | 校验买入成交和实际持仓 |
| 16:15 | 记录日线、最终成交额、信号漂移 |

IB 官方文档中，MOC 是尽量接近收盘成交的市价收盘单；MOO 可用 `MKT + TIF=OPG` 表达；LOC 是带价格限制的收盘单，LOO 是 `LMT + TIF=OPG`。NYSE MOC 通常要求 `15:50 ET` 前提交，Nasdaq MOC 通常要求 `15:55 ET` 前提交且 `15:50 ET` 后不可取消/修改。系统统一在 `15:44 ET` 冻结信号并立即提交。

## IB Gateway 配置

运行要求：

- IB Gateway 常驻，使用独立 API `clientId`。
- 启用 API 连接，只允许本机或内网白名单 IP。
- TWS/Gateway 登录时区设置为 `America/New_York`，或所有返回时间统一转 UTC 后再落库。
- 机器启用 NTP 时间同步。
- 每天启动后先调用 IB server time，检查本机时间偏差。
- Paper 账户至少跑 20 个交易日，但 auction fill 仍需用小资金真钱验证。

连接参数建议：

```yaml
ib:
  host: "127.0.0.1"
  port: 4002
  client_id: 71
  account: "DUxxxxxx"
  readonly_mode: false
  reconnect_backoff_seconds: [1, 2, 5, 10, 30]
```

## 资金和仓位控制

核心原则：策略只能使用显式分配给它的资金，不能自动吃满账户。

建议初始配置：

```yaml
risk:
  trading_enabled: false
  dry_run: true

  account_nav_reference_usd: 8500
  cash_reserve_usd: 2500

  initial_strategy_capital_usd: 3000
  max_strategy_capital_usd: 6000
  max_account_exposure_pct: 0.70
  max_leverage: 1.0

  min_order_notional_usd: 1000
  max_single_order_notional_usd: 6000
  max_position_notional_usd: 6000
```

下单金额：

```text
available_for_strategy =
    min(
        max_strategy_capital_usd,
        net_liquidation_usd * max_account_exposure_pct,
        settled_cash_usd - cash_reserve_usd,
        buying_power_usd / max_leverage
    )

target_notional =
    min(available_for_strategy, max_single_order_notional_usd)
```

股票数量：

```text
shares = floor(target_notional / protected_entry_price)
```

默认不使用碎股，因为 auction 类订单和碎股支持存在限制与不确定性。若 `shares < 1`，当天跳过。

资金上调规则：

```text
第 1-5 个交易日：$1,000
第 6-15 个交易日：$2,000
第 16-30 个交易日：$3,000
30 个交易日后：若滑点、拒单、回撤均达标，再提高到 $5,000-$6,000
```

禁止自动复利扩大投入。策略盈利后，除非手动修改配置，否则仍按 `max_strategy_capital_usd` 限制下单。

## 风险控制

### 交易前检查

满足全部条件才允许下单：

- 今天是完整正常交易日。
- IB Gateway 已连接，账户、持仓、open orders 已同步。
- 没有策略遗留持仓或未完成订单。
- 实时市场数据新鲜度 `< 5s`。
- 候选池至少有 3 只股票可排名。
- 选中股票通过 allowlist、上市天数、价格、成交额过滤。
- 订单金额不超过所有资金上限。
- 预计佣金和费用不超过 `max_expected_cost_bps = 5`。

### 入场保护

默认入场使用 `LOC`：

```text
action = BUY
orderType = LOC
lmtPrice = min(last_price * (1 + max_entry_slippage_bps / 10000), daily_price_limit_guard)
```

建议参数：

```yaml
execution:
  close_entry_mode: "LOC"
  max_entry_slippage_bps: 20
  submit_close_order_minutes_before_close: 15
```

如果 LOC 未成交，当天不持仓，不追单。若你更重视成交率，可切换 `MOC`，但必须接受无价格保护。

### 出场保护

默认等开盘竞价结束后再出场，避免 09:25 提交 OPG/MOO 被交易所或 IB 拒绝：

```text
action = SELL
orderType = MKT
tif = DAY
```

该订单在 `09:31 ET` 提交，不参与 09:30 开盘竞价。平仓时间线：

```text
09:31 提交 MKT DAY
09:32 未完全成交 -> 报警并继续检查
09:35 仍未成交 -> 提交 MKT 平仓并报警
```

### 回撤和亏损熔断

建议初始参数：

```yaml
risk:
  max_one_day_strategy_loss_usd: 500
  max_strategy_drawdown_pct: 0.20
  max_account_drawdown_pct: 0.10
  halt_after_consecutive_order_rejects: 1
  halt_after_unexpected_position: true
  halt_after_open_exit_failed: true
```

处理：

- 单日策略亏损超过阈值：停止下一交易日入场。
- 策略权益从高点回撤超过 `20%`：自动停机，需要人工恢复。
- 账户权益从高点回撤超过 `10%`：自动停机。
- 发现非策略创建的同 ticker 持仓：不自动合并，报警并停机。
- 发现策略持仓但 DB 无状态：报警，默认只允许人工确认后处理。

### 事件风险

可配置事件过滤：

```yaml
risk:
  skip_selected_ticker_on_earnings_window: false
  skip_if_selected_ticker_gap_risk_event: true
  skip_if_market_volatility_high: false
```

说明：财报过滤可能降低极端跳空风险，但也可能砍掉策略收益来源。初始建议只记录事件，不默认过滤；等实盘样本足够后再决定。

## 订单状态机

```text
IDLE
  -> PREFLIGHT_CLOSE
  -> SIGNAL_FROZEN
  -> BUY_SUBMITTED
  -> BUY_FILLED | BUY_NOT_FILLED | BUY_REJECTED
  -> HOLD_OVERNIGHT
  -> SELL_SUBMITTED
  -> FLAT_CONFIRMED | EXIT_FAILED
  -> POST_TRADE_RECONCILED
  -> IDLE
```

状态规则：

- 每个状态变更必须写 SQLite。
- 每个订单必须有唯一 `order_ref`：

```text
turnover_top1:{trade_date}:{leg}:{ticker}:{sequence}
```

- 重启后先从 IB 拉 open orders、positions、executions，再和 SQLite 状态比对。
- `BUY_SUBMITTED` 后如果 Gateway 断线，不重复提交；必须用 `order_ref` 和 IB open orders 恢复。
- `SELL_SUBMITTED` 后如果部分成交，剩余数量必须进入 fallback 平仓流程。

## 数据流程

### 实时数据

收盘前订阅候选池：

- last price
- cumulative RTH volume
- bid/ask
- trading status

每 10 秒写一次 `live_turnover_snapshot`。

### 历史日线

每日收盘后或下一交易日前更新：

- `ohlcv.open/high/low/close/vol`
- 若有复权数据源，保存 `adj_close`

IB 历史日线通过 `reqHistoricalData` 获取，`whatToShow="TRADES"`，`useRTH=1`。IB 文档说明 historical bars 的时区取决于 TWS 登录时区，且 IB 历史数据会过滤部分远离 NBBO 的成交，因此成交量可能与未过滤数据源不同。策略内排名必须统一使用同一数据源，不能混用。

## 调度伪代码

```python
def schedule_for_session(session_date):
    cal = get_exchange_calendar("XNYS")
    open_dt, close_dt = cal.open_close(session_date, tz="America/New_York")

    if is_early_close(session_date) and not config.trade_early_close_days:
        schedule_job(close_dt - timedelta(minutes=30), record_skip)
        return

    schedule_job(open_dt - timedelta(minutes=10), submit_open_exit)
    schedule_job(open_dt + timedelta(minutes=5), verify_exit)
    schedule_job(close_dt - timedelta(minutes=30), preflight_close)
    schedule_job(close_dt - timedelta(minutes=20), refresh_turnover_snapshots)
    schedule_job(close_dt - timedelta(minutes=16), freeze_signal_and_submit_entry)
    schedule_job(close_dt + timedelta(minutes=10), verify_entry)
```

所有 `schedule_job` 内部保存 UTC 触发时间；展示和审计时同时显示 NY 时间。

## 失败处理

| 场景 | 处理 |
| --- | --- |
| IB Gateway 断线 | 自动重连；若接近下单窗口仍未恢复，跳过入场 |
| 市场数据 stale | 跳过入场 |
| 下单被拒 | 记录 `risk_event`，停机 |
| 买入未成交 | 当天结束，不追单 |
| 买入部分成交 | 记录实际持仓，次日只卖实际持仓 |
| 开盘卖单未成交 | 09:31 marketable limit，09:35 MKT 平仓 |
| 持仓与 DB 不一致 | 停机并报警，不开新仓 |
| 系统重启 | 先 reconcile，再恢复状态机 |
| 今天提前收盘 | 默认跳过 |

## 配置示例

```yaml
strategy:
  name: "turnover_top1"
  timezone: "America/New_York"
  trade_early_close_days: false
  universe:
    enabled: ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "MU", "SPCX"]
    disabled: ["SKHY"]
    min_listed_trading_days: 20
    min_avg_dollar_volume_20d: 1000000000
    min_price: 5

signal:
  rank_metric: "live_dollar_turnover"
  top_n: 1
  snapshot_interval_seconds: 10
  signal_freeze_minutes_before_close: 16

execution:
  pricing_plan_assumption: "IBKR Pro Tiered"
  close_entry_mode: "LOC"
  open_exit_mode: "MKT"
  max_entry_slippage_bps: 20
  max_expected_cost_bps: 5
  exit_submit_after_open_seconds: 60
  fallback_exit_after_open_seconds: 120
  force_exit_after_open_seconds: 300

risk:
  trading_enabled: false
  dry_run: true
  cash_reserve_usd: 2500
  initial_strategy_capital_usd: 3000
  max_strategy_capital_usd: 6000
  max_account_exposure_pct: 0.70
  max_leverage: 1.0
  min_order_notional_usd: 1000
  max_single_order_notional_usd: 6000
  max_position_notional_usd: 6000
  max_one_day_strategy_loss_usd: 500
  max_strategy_drawdown_pct: 0.20
  max_account_drawdown_pct: 0.10
```

## 上线流程

1. 离线回测：验证候选池、信号、成本模型。
2. Replay 测试：用历史快照模拟 `15:44` 信号冻结。
3. Paper 交易：至少 20 个完整交易日，验证调度、订单状态机、重连和报警。
4. 小资金真钱：`$1,000` 跑 5 天。
5. 扩到 `$2,000-$3,000`：跑满 20 天，统计真实成交偏差。
6. 扩到 `$5,000-$6,000`：仅在真实滑点、拒单、回撤都达标后手动调整。

上线前必须通过：

- DST 切换周测试。
- 提前收盘日跳过测试。
- Gateway 重启恢复测试。
- 买入未成交测试。
- 开盘卖出失败 fallback 测试。
- 超过资金上限拒单测试。

## 关键监控

每天至少发送这些通知：

- `15:44` 信号：rank、选中 ticker、目标金额、订单类型。
- 买入订单状态：submitted/filled/not filled/rejected。
- 次日卖出订单状态。
- 实际成交价 vs 参考 close/open。
- 当日策略 PnL、账户权益、剩余现金、策略回撤。
- 信号漂移：15:44 Top1 是否等于最终日成交额 Top1。

严重报警：

- IB 断线超过 60 秒且接近交易窗口。
- 下单被拒。
- 开盘后仍持仓。
- 持仓金额超过上限。
- DB 状态与 IB 持仓不一致。
- 触发回撤/亏损熔断。

## 实盘建议

以当前账户规模，默认不建议一开始投入全部 `$6,000`。

推荐：

```text
第 1 阶段：$1,000-$2,000，验证 IB Gateway、订单类型和真实成交。
第 2 阶段：$3,000，作为常规初始资金。
第 3 阶段：最多 $5,000-$6,000，必须手动批准。
```

如果使用 `LOC` 入场，可能出现未成交，这会降低回测收益但降低价格失控风险。如果使用 `MOC` 入场，成交率更高但无价格保护。对 `$6,000` 小账户，默认先用 `LOC`，收集真实成交数据后再决定是否切换 MOC。

## 官方参考

- IBKR TWS API Basic Orders：MOC、MOO、LOC、LOO 的 API 表达方式。  
  https://interactivebrokers.github.io/tws-api/basic_orders.html
- IBKR Historical Bars：historicalData 返回、bar 时间格式和时区说明。  
  https://www.interactivebrokers.com/docs/tws-api/doc/market-data-historical/historical-bars/receiving-historical-bars
- IBKR Historical Data：历史数据过滤和时区格式说明。  
  https://interactivebrokers.github.io/tws-api/historical_data.html
- IBKR Order Types：MOC/MOO 的交易所提交截止时间说明。  
  https://brokerage.ibkr.com/en/trading/ordertypes.php?m=goodAfterTimeDateModal
