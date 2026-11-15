"""Explicit runnable workflows. Importing the service starts no pipeline."""
TASKS = {
    "research": "backtest.vectorbt_research.strategy_grid",
    "production.predict": "scripts.daily_NIFTY.daily_nifty_prediction",
    "production.signal": "scripts.daily_NIFTY.daily_nifty_signal",
    "production.options": "scripts.daily_NIFTY.daily_option_selection",
    "production.backtest": "backtest.production.pipeline_backtest_pnl",
    "production.option-backfill": "backtest.production.pipeline_upsert_option_selections",
    "paper.entry": "scripts.daily_NIFTY.daily_paper_entry",
    "paper.monitor": "scripts.daily_NIFTY.daily_paper_monitor",
    "paper.report": "scripts.daily_NIFTY.daily_paper_report",
    "trades.replay": "backtest.vectorbt_trades.cli",
    "market.refresh": "scripts.daily_NIFTY.daily_market_refresh",
    "market.global": "scripts.Common.load_daily_index_data",
    "market.macro": "scripts.daily_NIFTY.daily_macro_refresh",
    "market.gift": "scripts.daily_NIFTY.daily_NIFTYGift_snapshot",
    "market.open-gap": "scripts.daily_NIFTY.daily_open_gap",
    "options.instruments": "scripts.daily_NIFTY.daily_optionInstrument_refresh",
    "options.snapshot": "scripts.daily_NIFTY.daily_NIFTYoption_snapshot",
    "options.ohlc": "scripts.daily_NIFTY.daily_NIFTYoption_OHLC",
    "features.underlying": "scripts.Common.calculate_underlying_features",
    "features.options": "scripts.Common.calculate_option_snapshot_calc",
    "features.macro": "scripts.Common.calculate_macro_features",
}


def task_module(task: str) -> str:
    try:
        return "technical_analysis_service." + TASKS[task]
    except KeyError:
        raise ValueError(f"Unknown task: {task}") from None
