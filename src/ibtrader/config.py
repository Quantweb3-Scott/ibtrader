from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class AppConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8089
    database_path: str = "sharedata/oversea.db"
    log_level: str = "INFO"
    api_token: str = "change-me"


class IBConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 71
    account: str = ""
    readonly_mode: bool = False
    connect_timeout_seconds: float = 8
    market_data_wait_seconds: float = Field(default=8, gt=0, le=30)
    reconnect_backoff_seconds: list[int] = [1, 2, 5, 10, 30]


class StrategyConfig(BaseModel):
    timezone: str = "America/New_York"
    trade_early_close_days: bool = False
    universe: list[str] = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "MU", "SPCX"]
    min_listed_trading_days: int = 20
    min_avg_dollar_volume_20d: float = 1_000_000_000
    min_price: float = 5
    snapshot_interval_seconds: int = 10
    signal_freeze_minutes_before_close: int = 16
    mag7_universe: list[str] = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"]
    benchmark_ticker: str = "QQQ"
    turnover_zscore_lookback: int = 120


class ExecutionConfig(BaseModel):
    # Both documented turnover variants track the research close with MOC.
    close_entry_mode: Literal["MOC"] = "MOC"
    open_exit_mode: Literal["MKT"] = "MKT"
    max_entry_slippage_bps: float = 20
    max_expected_cost_bps: float = 5
    exit_submit_after_open_seconds: int = 60
    fallback_exit_after_open_seconds: int = 120
    force_exit_after_open_seconds: int = 300
    dry_run_commission_bps_per_side: float = Field(default=0.6, ge=0)

    @model_validator(mode="after")
    def exit_timeline_is_ordered(self):
        if not (
            0
            < self.exit_submit_after_open_seconds
            < self.fallback_exit_after_open_seconds
            < self.force_exit_after_open_seconds
        ):
            raise ValueError("exit timing must satisfy 0 < submit < fallback < force")
        return self


class RiskConfig(BaseModel):
    trading_enabled: bool = False
    dry_run: bool = True
    manual_real_order_enabled: bool = True
    cash_reserve_usd: float = 2500
    initial_strategy_capital_usd: float = 3000
    max_strategy_capital_usd: float = 6000
    max_account_exposure_pct: float = 0.70
    max_leverage: float = 1.0
    min_order_notional_usd: float = 1000
    max_single_order_notional_usd: float = 6000
    max_position_notional_usd: float = 6000
    max_one_day_strategy_loss_usd: float = 500
    max_strategy_drawdown_pct: float = 0.20
    max_account_drawdown_pct: float = 0.10

    @model_validator(mode="after")
    def safe_limits(self):
        if self.initial_strategy_capital_usd > self.max_strategy_capital_usd:
            raise ValueError("initial_strategy_capital_usd exceeds maximum")
        if self.max_leverage > 1:
            raise ValueError("leverage is forbidden")
        return self


class AlertConfig(BaseModel):
    gateway_down_after_seconds: int = 60
    webhook_url: str | None = None
    webhook_bearer_token: str | None = None
    cooldown_seconds: int = 300


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    ib: IBConfig = Field(default_factory=IBConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> Settings:
        source = Path(path)
        if not source.exists():
            source = Path("config.example.yaml")
        data = yaml.safe_load(source.read_text(encoding="utf-8")) if source.exists() else {}
        return cls.model_validate(data or {})
