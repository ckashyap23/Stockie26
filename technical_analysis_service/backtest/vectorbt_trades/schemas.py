from __future__ import annotations

from technical_analysis_service.runtime import output_dir as service_output_dir

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class StockieVectorBTRequest:
    underlying: str = "NIFTY"
    model_version: str = "cascade_v1"
    mode: str = "paper"         # paper | live — only paper is implemented
    start_date: date | None = None
    end_date: date | None = None
    initial_cash: float = 100_000.0
    fees: float = 0.0
    slippage: float = 0.0
    output_dir: Path = service_output_dir() / "backtest" / "NIFTY" / "vectorbt"


@dataclass
class StockieVectorBTResult:
    trade_plans: pd.DataFrame
    price: pd.DataFrame
    entries: pd.DataFrame
    exits: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict[str, Any]
    used_vectorbt: bool
    output_paths: dict[str, Path]
