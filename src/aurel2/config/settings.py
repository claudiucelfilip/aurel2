"""Configuration settings for Aurel2."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class AssetConfig(BaseModel):
    """Configuration for a single asset."""
    symbol: str
    name: str
    isin: str | None = None
    yahoo_symbol: str | None = None


class StrategyConfig(BaseModel):
    """Strategy configuration."""
    name: str = "dual_momentum"
    lookback_months: int = 12
    rebalance_frequency: str = "quarterly"  # monthly, quarterly
    switch_threshold: float = 0.10  # 10% threshold to switch


class AssetsConfig(BaseModel):
    """Assets configuration."""
    us_stocks: AssetConfig
    global_stocks: AssetConfig
    bonds: AssetConfig
    cash_rate: float = 0.04


class BrokerConfig(BaseModel):
    """Broker configuration."""
    type: str = "ibkr"
    host: str = "127.0.0.1"
    port: int = 7497


class RiskConfig(BaseModel):
    """Risk management configuration."""
    max_position_pct: float = 1.0
    transaction_cost_pct: float = 0.001  # 0.1% per trade


class LoggingConfig(BaseModel):
    """Logging configuration."""
    level: str = "INFO"


class Settings(BaseSettings):
    """Main application settings."""
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    assets: AssetsConfig | None = None
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @classmethod
    def from_yaml(cls, path: Path | str) -> "Settings":
        """Load settings from YAML file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path) as f:
            data = yaml.safe_load(f)

        return cls(**data)

    @classmethod
    def load(cls, config_path: Path | str | None = None) -> "Settings":
        """Load settings from default or specified path."""
        if config_path is None:
            # Look for config in standard locations
            search_paths = [
                Path("config/default.yaml"),
                Path(__file__).parent.parent.parent.parent / "config" / "default.yaml",
            ]
            for path in search_paths:
                if path.exists():
                    return cls.from_yaml(path)
            # Return defaults if no config found
            return cls()

        return cls.from_yaml(config_path)


def load_settings(config_path: Path | str | None = None) -> Settings:
    """Convenience function to load settings."""
    return Settings.load(config_path)
