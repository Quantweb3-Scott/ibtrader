# IBTrader — MAG7 Turnover 隔夜交易系统

按 [turnover_strategy_design.md](docs/turnover_strategy_design.md) 实现的 IB Gateway 美股隔夜交易服务。DRY_RUN 会同时运行进攻版（ZTurnover120 Top1 + QQQ SMA200）和稳健版（上涨股票 ZTurnover120 等权 Top3 + QQQ SMA100），收盘入场统一使用 MOC，下一交易日开盘以 MKT DAY 模拟出场。

> 重要：默认 `trading_enabled: false` 且 `dry_run: true`。未显式修改两项配置前不会向 IB 发送订单。先使用 Paper 账户完整运行至少 20 个交易日。

## 快速启动

首次使用先安装 `uv`（Windows PowerShell）：

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

随后在项目目录运行：

```powershell
Copy-Item config.example.yaml config.yaml
uv sync
uv run ibtrader
```

`uv sync` 会根据 `.python-version` 和 `uv.lock` 自动选择 Python、创建 `.venv` 并同步所有运行及开发依赖，不需要手工激活虚拟环境。CI/生产环境建议使用 `uv sync --frozen --no-dev`，确保严格使用锁文件。

打开 `http://127.0.0.1:8089`。IB Gateway Paper 默认 API 端口通常为 `4002`，实盘通常为 `4001`；请以 Gateway 配置为准，并开启 API、配置白名单和只读/交易权限。

## 实盘解锁

1. 设置 `ib.account`，确认独立 `client_id`。
2. 配置 `alerts.webhook_url` 并调用 `POST /api/operations/test-alert` 验证通知。
3. Paper 验证 DST、提前收盘跳过、Gateway 重启、LOC 未成交、开盘 fallback、资金超限。
4. 将 `risk.trading_enabled` 改为 `true`，但保持 `dry_run: true` 验证完整信号和订单记录。
5. 仅经人工批准后将 `dry_run` 改为 `false`，初始资金建议 `$1,000`。

运维写接口支持 Bearer Token。请把 `app.api_token` 改成强随机值；保持默认 `change-me` 时仅适合绑定本机测试。

## 报警

Webhook 接收 JSON：`event`、`severity`、`message`、`timestamp`、`payload`。断线超过 60 秒、时间偏差、拒单、开盘未平仓、对账异常会产生严重事件；相同事件按 cooldown 去重。所有报警无论 Webhook 是否成功都会写入 `risk_event`。

## 账户监控与盈亏

控制台运行在 `8089`，每 60 秒从 IB Gateway 更新账户净值、结算现金、购买力、全部股票持仓、策略 executions 和 commission report。账户中的其他持仓会展示，但只有配置股票池内的持仓参与策略风控、自动平仓和策略盈亏计算。

策略盈亏按实际成交现金流、当前策略持仓市值和 IB 返回的手续费计算。控制台的“重置净值 / 盈亏”会清空净值曲线，并将当前账户净值归一到 `1.0000`，同时重置账户和策略盈亏基准；该操作需要 `app.api_token`。

## 数据与恢复

唯一持久库为 `sharedata/oversea.db`，启用 WAL。服务启动后从 IB 拉取持仓和 open orders 与本地 `order_ref` 对账；发现未知策略订单或意外持仓立即进入 `HALTED`，不自动开新仓。

## 测试

```powershell
uv run pytest -q
uv run ruff check src tests
```

测试不连接 IB，覆盖安全默认、资金上限、行情新鲜度、DST、提前收盘和 dry-run 不下单。

## 手工选股与收盘订单测试

在你选择的时间打开网页并点击“启动测试”，系统会立刻读取股票池各股票当前的 RTH 累计成交量和最新价，用 `最新价 × 累计成交量` 计算当日成交额并返回排名，不等待固定窗口，也不会自动定时触发。也可调用：

```powershell
$headers = @{ Authorization = "Bearer <api-token>"; "Content-Type" = "application/json" }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8089/api/operations/turnover-test -Headers $headers
```

测试只读取一次行情并立即完成。它只读行情，不会下单。结果可从网页或 `GET /api/turnover-tests` 查看。

DRY_RUN 完整流程使用：

```yaml
ib:
  readonly_mode: true
risk:
  trading_enabled: true
  dry_run: true
```

收盘时为进攻版、稳健版分别模拟 MOC 成交并维护独立账本；每个版本各自使用 `initial_strategy_capital_usd`，稳健版在三个标的间等权。下一交易日开盘检查时使用开盘后行情代理成交，分别计算模拟手续费、盈亏和净值。DRY_RUN 不会调用 IB `placeOrder`。

手工 REAL LOC/MOC 支持 `BUY` 和 `SELL`，只允许股票池内股票，并要求 `readonly_mode=false`、`trading_enabled=true`。手工 REAL 专用门禁默认已开启。`SELL` 只能减少当前 IB 多头持仓，数量超过持仓会被拒绝，不允许借此开空。它可以与计划任务的 `dry_run=true` 同时运行；此时计划策略仍为 DRY_RUN，只有手工端点越过模拟开关。系统不会自动平掉手工 REAL 仓位，测试人员必须自行提交 SELL。自动开盘退出只根据自动策略的 IB 成交记录计算数量，明确排除手工 REAL 订单和账户其他持仓。接口还会检查最新行情、活动订单、结算现金和订单金额上限。网页提交前会再次确认；启用前请先完成 DRY_RUN 和 Paper 验证。

建议今晚混合测试使用以下配置（修改后重启服务）：

```yaml
ib:
  readonly_mode: false
risk:
  trading_enabled: true
  dry_run: true
  manual_real_order_enabled: true
```

重置接口通过 `mode` 区分：

```text
POST /api/operations/reset-performance?mode=REAL
POST /api/operations/reset-performance?mode=DRY_RUN
```

REAL 重置账户/策略绩效基准和曲线；DRY_RUN 重置模拟成交、模拟仓位、盈亏和净值。两者互不影响。

## 使用 NSSM 运行 Windows 服务

项目内置 NSSM 脚本，服务名为 `IBTrader`。安装脚本让 NSSM 直接运行 `uv run --frozen ibtrader`，工作目录和 `IBTRADER_CONFIG` 都使用项目绝对路径；因此依赖仍由 uv 和 `uv.lock` 管理。服务设为延迟自动启动，进程异常后 5 秒自动重启，日志写入 `logs/service.stdout.log` 和 `logs/service.stderr.log`，单文件达到 10 MB 后滚动。

在管理员 PowerShell 中执行：

```powershell
Set-Location "H:\code\github\Quantweb3-com\ibtrader"
.\scripts\install-service.ps1
.\scripts\manage-service.ps1 status
.\scripts\manage-service.ps1 restart
.\scripts\manage-service.ps1 stop
.\scripts\manage-service.ps1 start
```

也可以直接使用 NSSM：

```powershell
nssm status IBTrader
nssm restart IBTrader
nssm stop IBTrader
nssm start IBTrader
```

卸载服务但保留代码、数据库和日志：

```powershell
.\scripts\uninstall-service.ps1
```
