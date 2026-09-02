# MAG7 Overnight Turnover Strategy Design

Updated: 2026-09-02

Status: daily-bar research design completed. Historical 15:45/15:50 executable snapshot validation is deferred to live dry run because the historical intraday turnover data requirement is high.

## Objective

Build two daily-bar strategy candidates for MAG7 close-to-next-open trading:

- **进攻版**：maximize return while keeping train/test performance acceptable.
- **稳健版**：maximize robustness, Sharpe, drawdown control, and suitability for dry-run monitoring.

The strategy still requires live dry-run execution validation before production capital deployment.

## Universe

Validated universe:

```text
AAPL, MSFT, GOOGL, AMZN, META, NVDA, TSLA
```

Current optimization results are only validated on this MAG7 universe. Expanded names such as `MU` or `SPCX` must be tested separately before inclusion.

## Data Inputs

Required daily data:

| Asset | Fields | Usage |
| --- | --- | --- |
| MAG7 stocks | open, close, adj_close, volume | overnight returns, intraday direction, turnover scores |
| QQQ | open, close, adj_close | market trend gate and QQQ overnight benchmark |
| VIX | close | regime diagnostics only; not used by selected production candidates |

Current data source:

- MAG7 daily OHLCV: `sharedata/oversea.db`.
- QQQ OHLCV: Alpaca daily stock bars, refreshed into `sharedata/oversea.db`.
- VIX: Cboe `VIX_History.csv`, refreshed into `sharedata/oversea.db`.

## Date Convention

Let `d` be the entry date and `d+1` be the next trading session.

Backtest trade:

```text
Compute signal using day d daily bar
-> buy at Close_d
-> sell at Open_d+1
```

Backtest return:

```text
OvernightReturn_i,d+1 = AdjOpen_i,d+1 / AdjClose_i,d - 1
```

Important limitation:

- Daily-bar research uses final day `d` turnover and close/intraday direction.
- In real trading, final day `d` turnover and final close are not fully known before MOC/LOC cutoff.
- Therefore production validation must compare live `15:45/15:50` frozen signal vs final daily signal during dry run.

## Common Feature Definitions

Dollar turnover:

```text
Turnover_i,d = RawClose_i,d * Volume_i,d
```

Log turnover:

```text
L_i,d = log(Turnover_i,d)
```

ZTurnover:

```text
ZTurnoverN_i,d = (L_i,d - mean(L_i,d-1 ... L_i,d-N)) / std(L_i,d-1 ... L_i,d-N)
```

Abnormal turnover:

```text
ATN_i,d = Turnover_i,d / mean(Turnover_i,d-1 ... Turnover_i,d-N)
```

Entry-day intraday return:

```text
Intraday_i,d = AdjClose_i,d / AdjOpen_i,d - 1
```

QQQ trend gate:

```text
QQQAboveSMAN_d = AdjClose_QQQ,d > SMA_N(AdjClose_QQQ)_d
```

Data validity:

- Exclude a ticker if `Turnover <= 0`, `volume` missing, `open/close/adj_close` missing, or `ZTurnover` denominator is missing/zero.
- If a strategy cannot find enough eligible tickers for its `TopN`, it holds cash overnight.
- All returns are close-to-next-open only; no intraday holding after the next open.

## Strategy A: Aggressive Version

Strategy id:

```text
mag7_overnight_z120_top1_qqq_sma200
```

Backtest candidate name:

```text
z120__top1__qqq_above_sma200
```

Purpose:

- Highest-return daily-bar candidate.
- Best fit when the objective is maximum CAGR and the account can tolerate single-name overnight gap risk.

Parameters:

| Parameter | Value |
| --- | --- |
| Universe | MAG7 |
| Score | `ZTurnover120` |
| Z-score lookback | `120` trading days |
| Selection | highest score |
| TopN | `1` |
| Individual direction gate | none |
| Market gate | `QQQ close > QQQ SMA200` |
| Position count | 1 stock |
| Weighting | 100% in selected stock |
| Cash rule | cash if QQQ below SMA200 or no valid score |
| Entry | close of day `d` |
| Exit | next trading day open |
| Rebalance | every valid trading day |
| Cost assumption for reporting | `2 bps/side`, also check `1` and `5 bps/side` |

Signal rule:

```text
if QQQAboveSMA200_d is false:
    hold cash
else:
    selected = argmax_i(ZTurnover120_i,d)
    buy selected at Close_d
    sell selected at Open_d+1
```

Backtest performance:

| Metric | Value |
| --- | ---: |
| Sample | `2016-01-05 -> 2026-08-31` |
| Exposure | `84.02%` |
| Full CAGR | `66.65%` |
| Full Sharpe | `1.92` |
| Full MaxDD | `-33.14%` |
| Train CAGR / Sharpe | `57.96% / 1.88` |
| Test CAGR / Sharpe | `83.50% / 2.00` |
| Net CAGR, 1 bps/side | `59.75%` |
| Net CAGR, 2 bps/side | `53.14%` |
| Net CAGR, 5 bps/side | `34.90%` |
| Worst calendar year | `2026: -7.12%` |

Selection profile:

```text
AAPL:263; MSFT:244; GOOGL:232; AMZN:233; META:222; NVDA:506; TSLA:450
```

Characteristics:

- High CAGR and high exposure.
- Single-name concentration is the main risk.
- Stronger in 2023-2025 than in 2016-2022; this is not sample-out failure, but it may reflect the recent MAG7/AI trend regime.
- No individual up-day filter. The main risk control is the QQQ SMA200 trend gate.
- Better suited as an aggressive sleeve or shadow strategy than as the only core strategy.

Failure modes:

- A single overnight gap can materially hit NAV.
- QQQ SMA200 is a slow trend filter and may react late during fast drawdowns.
- If MAG7 leadership changes, ZTurnover120 may concentrate on stale winners or event-driven losers.

## Strategy B: Robust Version

Strategy id:

```text
mag7_overnight_z120_top3_intraday_up_qqq_sma100
```

Backtest candidate name:

```text
z120__top3__entry_intraday_up__qqq_above_sma100
```

Purpose:

- Best robust daily-bar candidate by train/test Sharpe and drawdown.
- Best fit for initial dry run and conservative capital ramp.

Parameters:

| Parameter | Value |
| --- | --- |
| Universe | MAG7 |
| Score | `ZTurnover120` |
| Z-score lookback | `120` trading days |
| Selection pool | stocks with `Intraday_i,d > 0` |
| TopN | `3` |
| Market gate | `QQQ close > QQQ SMA100` |
| Position count | 3 stocks when active |
| Weighting | equal weight, one third each |
| Cash rule | cash if QQQ below SMA100 or fewer than 3 eligible up-day names |
| Entry | close of day `d` |
| Exit | next trading day open |
| Rebalance | every valid trading day |
| Cost assumption for reporting | `2 bps/side`, also check `1` and `5 bps/side` |

Signal rule:

```text
if QQQAboveSMA100_d is false:
    hold cash
else:
    eligible = {i in MAG7 where Intraday_i,d > 0 and ZTurnover120_i,d is valid}
    if len(eligible) < 3:
        hold cash
    else:
        selected = top 3 eligible names by ZTurnover120_i,d
        buy selected equal weight at Close_d
        sell selected at Open_d+1
```

Backtest performance:

| Metric | Value |
| --- | ---: |
| Sample | `2016-01-05 -> 2026-08-31` |
| Exposure | `54.08%` |
| Full CAGR | `33.60%` |
| Full Sharpe | `2.36` |
| Full MaxDD | `-13.98%` |
| Train CAGR / Sharpe | `29.09% / 2.28` |
| Test CAGR / Sharpe | `42.11% / 2.50` |
| Net CAGR, 1 bps/side | `30.02%` |
| Net CAGR, 2 bps/side | `26.53%` |
| Net CAGR, 5 bps/side | `16.61%` |
| Worst calendar year | `2022: -12.09%` |

Selection profile:

```text
AAPL:567; MSFT:636; GOOGL:598; AMZN:567; META:559; NVDA:651; TSLA:574
```

Characteristics:

- Much smoother than the aggressive Top1 version.
- Lower exposure because it requires QQQ uptrend and at least 3 MAG7 names with positive entry-day intraday return.
- The direction filter explicitly avoids many high-turnover down/flat days, where prior tests showed much worse overnight performance.
- Equal-weight Top3 reduces single-name event risk.
- Better dry-run candidate because signal behavior, fill quality, and slippage can be observed without relying on one name.

Failure modes:

- Lower CAGR than the aggressive version.
- It can miss large overnight continuation moves when fewer than 3 names satisfy the up-day gate.
- QQQ SMA100 is more responsive than SMA200 but may whipsaw more.
- Uses final daily intraday direction in research; live dry run must measure whether `15:45/15:50` direction is close enough.

## Version Comparison

| Attribute | Aggressive | Robust |
| --- | --- | --- |
| Candidate | `z120__top1__qqq_above_sma200` | `z120__top3__entry_intraday_up__qqq_above_sma100` |
| Main objective | CAGR | Sharpe and drawdown |
| Score | `ZTurnover120` | `ZTurnover120` |
| TopN | `1` | `3` |
| Individual direction gate | none | `Intraday_i,d > 0` |
| Market gate | QQQ above SMA200 | QQQ above SMA100 |
| Exposure | `84.02%` | `54.08%` |
| CAGR | `66.65%` | `33.60%` |
| Sharpe | `1.92` | `2.36` |
| MaxDD | `-33.14%` | `-13.98%` |
| 2 bps/side net CAGR | `53.14%` | `26.53%` |
| Train Sharpe | `1.88` | `2.28` |
| Test Sharpe | `2.00` | `2.50` |
| Main risk | single-name overnight gap | lower participation and missed rallies |
| Suggested role | aggressive sleeve / shadow signal | primary dry-run candidate |

## Supporting Candidates

Use these as shadow benchmarks during dry run:

| Role | Candidate | CAGR | Sharpe | MaxDD | Net 2 bps CAGR |
| --- | --- | ---: | ---: | ---: | ---: |
| Raw turnover benchmark | `turnover__top1` | `46.55%` | `1.37` | `-33.96%` | `32.53%` |
| Raw turnover robust backup | `turnover__top3__entry_intraday_up__qqq_above_sma100` | `28.73%` | `2.26` | `-13.57%` | `22.04%` |
| AT20 robust backup | `at20__top3__entry_close_to_close_up__qqq_above_sma100` | `27.00%` | `2.04` | `-12.10%` | `20.22%` |

Reason:

- `turnover__top1` keeps continuity with the original discovery.
- `turnover__top3__entry_intraday_up__qqq_above_sma100` tests whether the effect survives without z-score normalization.
- `AT20` tests the shorter abnormal-turnover hypothesis, but it is not the current best performer.

## Portfolio Allocation Design

Research allocation:

- Backtest each variant as a standalone fully allocated strategy.
- Do not combine variants when reporting core statistics, otherwise exposure and risk attribution become harder to read.

Dry-run allocation:

- Run all variants as paper/shadow signals first.
- If using live pilot capital, start with the robust version only.
- Keep aggressive version as shadow signal until its live signal match rate and slippage are measured.

Suggested pilot capital policy, based on the existing IB Gateway design:

| Phase | Capital | Enabled Strategy | Purpose |
| --- | ---: | --- | --- |
| Shadow dry run | `$0` | robust + aggressive + benchmarks | measure signal drift and fills |
| Small live pilot | up to `$3,000` | robust only | validate real costs and auction behavior |
| Expanded pilot | up to `$6,000` hard cap | robust primary, aggressive optional sleeve | only after dry-run metrics pass |

No leverage. No shorting. No options. No premarket discretionary trades.

## Execution Design

Research assumption:

```text
Entry: Close_d
Exit: Open_d+1
```

Dry-run/live approximation:

```text
15:45 or 15:50 ET:
    compute provisional turnover, ZTurnover120, intraday direction, QQQ trend gate
    freeze signal
    submit MOC or LOC order if enabled

next trading day open:
    submit MOO/LOO or marketable open exit order according to broker support
```

Order preference:

- Entry: MOC for maximum close-price tracking, or LOC with conservative limit if fill control is more important.
- Exit: MOO/LOO or regular market order just after open, depending on observed fill quality.
- Skip early-close days until scheduling and order cutoffs are explicitly tested.

Execution references:

- IBKR describes MOC as an order intended to execute as close to the closing price as possible and LOC as a close order that executes only if the closing price satisfies the limit.
- Nasdaq Closing Cross imbalance information begins near 15:50 ET and the closing cross occurs at 16:00 ET on regular sessions.
- Exact order cutoff, cancellation, and venue behavior must be rechecked against current broker/exchange rules before live activation.

## Dry-Run Validation Metrics

Record the following every trading day:

| Metric | Why it matters |
| --- | --- |
| `snapshot_signal` at 15:45/15:50 | verifies executable pre-close signal |
| `final_daily_signal` | measures look-ahead drift |
| `signal_match` | primary signal feasibility metric |
| `selected_tickers_by_variant` | compares robust/aggressive/benchmarks |
| `snapshot_turnover_rank` vs final rank | detects closing-auction rank flips |
| `snapshot_intraday_direction` vs final direction | checks direction gate reliability |
| QQQ SMA gate value | verifies regime state |
| simulated entry price vs official close | estimates entry slippage |
| simulated exit price vs official open | estimates exit slippage |
| missed/canceled/rejected orders | validates operational reliability |
| realized cost bps | compares with 1/2/5 bps stress tests |

Minimum dry-run promotion criteria:

- At least `40` active strategy nights for the robust version.
- Signal match rate vs final daily signal should be high enough to explain most of the daily-bar edge.
- Realized round-trip cost should stay near or below the `2 bps/side` stress assumption for liquid MAG7 names.
- No unresolved order-state, stale-data, or position reconciliation failures.

## Risk Controls

Common controls:

- Trade only allowlisted tickers.
- Hold cash if market data is stale or incomplete.
- Hold cash if QQQ gate cannot be computed.
- Hold cash on early-close days until separately validated.
- Disable trading on detected stock halt, missing quote, abnormal spread, or broker/API failure.
- Never pyramid positions across nights; every position must be closed the next open.

Aggressive version controls:

- Single position maximum: `100%` of allocated sleeve.
- Use only as a separate sleeve with hard capital cap.
- If live drawdown exceeds `15%` in pilot, pause and review.

Robust version controls:

- Position count: exactly 3 names when active.
- Per-name target: one third of allocated sleeve.
- If fewer than 3 valid eligible names, hold cash rather than concentrating.
- If live drawdown exceeds `10%` in pilot, pause and review.

## Monitoring Dashboard Fields

Daily summary should show:

```text
trade_date
variant
market_gate_pass
eligible_count
selected_tickers
scores
snapshot_signal
final_daily_signal
signal_match
entry_order_type
entry_fill_price
official_close
exit_fill_price
official_open
gross_return
net_return
cost_bps
nav
drawdown
skip_reason
```

## Implementation Files

Research scripts:

- `mag7_overnight/validate_turnover_alpha.py`
- `mag7_overnight/optimize_turnover_strategy.py`

Reports:

- `mag7_overnight/output/turnover_alpha_validation.md`
- `mag7_overnight/output/daily_strategy_optimization_report.md`

Key CSV outputs:

- `mag7_overnight/output/daily_strategy_optimization_summary.csv`
- `mag7_overnight/output/daily_strategy_optimization_robust.csv`
- `mag7_overnight/output/daily_strategy_optimization_annual.csv`
- `mag7_overnight/output/daily_strategy_optimization_best_daily_returns.csv`

Execution system reference:

- `mag7_overnight/docs/turnover_top1_ib_gateway_design.md`

## Decision

Use both candidates going forward:

1. **Primary dry-run strategy**: `mag7_overnight_z120_top3_intraday_up_qqq_sma100`.
2. **Aggressive shadow strategy**: `mag7_overnight_z120_top1_qqq_sma200`.

Do not choose the final production version until dry run measures:

- signal drift between `15:45/15:50` and final daily signal,
- real MOC/LOC and open-exit fill quality,
- realized cost/slippage,
- behavior around earnings/news/high-volatility days.
