import os
from pathlib import Path
from dotenv import load_dotenv

_repo_root_env = Path(__file__).resolve().parents[2] / ".env"
# Load repo-root .env deterministically so running from different working dirs still works.
# Do not override existing environment variables (so deployment env wins).
load_dotenv(dotenv_path=_repo_root_env if _repo_root_env.exists() else None, override=False)


def _normalize_azure_sql_conn_str(conn_str: str) -> str:
    """
    Normalize common Azure SQL ODBC connection string formats.

    Common mistake: missing the 'Driver=' prefix, e.g.
      "{ODBC Driver 18 for SQL Server};Server=..."
    pyodbc/ODBC expects:
      "Driver={ODBC Driver 18 for SQL Server};Server=..."
    """
    s = (conn_str or "").strip()
    if not s:
        return ""

    # If a driver is already specified (any case), keep as-is.
    if "driver=" in s.lower():
        return s

    # If it looks like it starts with a driver name in braces or plain text, prepend Driver=
    lowered = s.lower()
    if lowered.startswith("{odbc driver") or lowered.startswith("{sql server}"):
        return f"Driver={s}"
    if lowered.startswith("odbc driver") or lowered.startswith("sql server"):
        return f"Driver={s}"

    return s


def _split_conn_str(conn_str: str) -> list[str]:
    return [part.strip() for part in (conn_str or "").split(";") if part.strip()]


def _join_conn_str(parts: list[str]) -> str:
    return ";".join(parts) + (";" if parts else "")


def _set_conn_attr(parts: list[str], key: str, value: str) -> list[str]:
    lowered_key = key.lower()
    updated: list[str] = []
    replaced = False
    for part in parts:
        if "=" not in part:
            updated.append(part)
            continue
        current_key, _ = part.split("=", 1)
        if current_key.strip().lower() == lowered_key:
            updated.append(f"{key}={value}")
            replaced = True
        else:
            updated.append(part)
    if not replaced:
        updated.append(f"{key}={value}")
    return updated


def _remove_tcp_prefix(parts: list[str]) -> list[str]:
    updated: list[str] = []
    for part in parts:
        if "=" not in part:
            updated.append(part)
            continue
        current_key, current_value = part.split("=", 1)
        if current_key.strip().lower() == "server" and current_value.lower().startswith("tcp:"):
            updated.append(f"{current_key}={current_value[4:]}")
        else:
            updated.append(part)
    return updated


def get_azure_sql_conn_str_variants(conn_str: str) -> list[str]:
    normalized = _normalize_azure_sql_conn_str(conn_str)
    if not normalized:
        return []

    parts = _split_conn_str(normalized)
    variants: list[str] = [_join_conn_str(parts)]

    trust_cert_parts = _set_conn_attr(parts, "TrustServerCertificate", "yes")
    variants.append(_join_conn_str(trust_cert_parts))

    trust_cert_no_tcp = _remove_tcp_prefix(trust_cert_parts)
    variants.append(_join_conn_str(trust_cert_no_tcp))

    deduped: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        if variant and variant not in seen:
            deduped.append(variant)
            seen.add(variant)
    return deduped


def _normalize_supabase_conn_str(conn_str: str) -> str:
    value = (conn_str or "").strip().strip('"').strip("'")
    for prefix in ("SUPABASE_CONN_STR=", "DATABASE_URL="):
        if value.upper().startswith(prefix):
            value = value[len(prefix):].strip().strip('"').strip("'")
            break
    return value


class Settings:
    def __init__(self) -> None:
        self.kite_api_key = os.getenv("KITE_API_KEY", "")
        self.kite_api_secret = os.getenv("KITE_API_SECRET", "")

        # we don't store access token in env, we read it from file
        self.kite_access_token_path = Path(
            os.getenv("KITE_ACCESS_TOKEN_PATH", "kite_access_token.txt")
        )

        self.azure_sql_conn_str = _normalize_azure_sql_conn_str(
            os.getenv("AZURE_SQL_CONN_STR", "")
        )
        self.supabase_conn_str = _normalize_supabase_conn_str(
            os.getenv("SUPABASE_CONN_STR", "")
        )
        self.database_provider = os.getenv("DATABASE_PROVIDER", "").strip().lower()
        self.target_underlyings = os.getenv(
            "TARGET_UNDERLYINGS", "NIFTY,BANKNIFTY"
        ).split(",")


def get_trade_horizon_days() -> int:
    """Max trading days an option position is held (paper trades, vectorBT, PnL backtest).

    Reads TRADE_HORIZON_DAYS from the environment (set in .env).
    """
    return int(os.getenv("TRADE_HORIZON_DAYS", "1"))


def get_underlying_lookback_days() -> int:
    """Lookback window (in trading days) for NIFTY prediction quality assessment.

    Controls actual_trade_label (future_high_nd / future_low_nd window in dataset.py)
    and signal quality score horizon in signal_strength.py.
    Reads UNDERLYING_LOOKBACK_DAYS from the environment (set in .env).
    """
    return int(os.getenv("UNDERLYING_LOOKBACK_DAYS", "1"))


def get_regime_config() -> dict[str, dict[str, float]]:
    """Return a structured config dict keyed by regime name.

    All values read from environment variables so they can be overridden per
    deployment without touching code.  Strategies should import this once and
    use the returned dict instead of hard-coding threshold constants.

    Structure:
        {
          'calm':   {'target_pct': float, 'bb_width_min': float, 'vix_quiet_max': float},
          'stress': {'target_pct': float, 'bb_width_min': float, 'vix_quiet_max': float},
        }

    Env variables (all decimal fractions unless noted):
        CALM_NIFTY_TARGET_PCT   NIFTY move threshold for calm label   (default 0.005)
        STRESS_NIFTY_TARGET_PCT NIFTY move threshold for stress label  (default 0.010)
        CALM_BB_WIDTH_MIN       min Bollinger width for calm entries    (default 0.040)
        STRESS_BB_WIDTH_MIN     min Bollinger width for stress entries  (default 0.040)
        CALM_VIX_QUIET_MAX      VIX ceiling for 'calm quiet' condition  (default 13.0)
        STRESS_VIX_QUIET_MAX    VIX ceiling for 'stress quiet' window   (default 14.0)
    """
    return {
        "calm": {
            "target_pct":   _pct_env("CALM_NIFTY_TARGET_PCT", 0.005),
            "bb_width_min": _pct_env("CALM_BB_WIDTH_MIN",    0.040),
            "vix_quiet_max": float(os.getenv("CALM_VIX_QUIET_MAX",  "13.0")),
        },
        "stress": {
            "target_pct":   _pct_env("STRESS_NIFTY_TARGET_PCT", 0.010),
            "bb_width_min": _pct_env("STRESS_BB_WIDTH_MIN",    0.040),
            "vix_quiet_max": float(os.getenv("STRESS_VIX_QUIET_MAX", "14.0")),
        },
    }


def get_nifty_target_pct(regime: str) -> float:
    """NIFTY underlying move required for actual_trade_label per volatility regime.

    This is independent of option-premium targets and stops.
    """
    if str(regime or "").lower() == "stress":
        return _pct_env("STRESS_NIFTY_TARGET_PCT", 0.010)
    return _pct_env("CALM_NIFTY_TARGET_PCT", 0.005)


def get_regime_threshold(regime: str) -> float:
    """Backward-compatible alias for the NIFTY label target."""
    return get_nifty_target_pct(regime)


def get_target_pct_for_regime(regime: str | None) -> float:
    """Return the single option-premium profit target for the given regime.

    Reads from env variables:
      STRESS_TARGET_PCT  (stress regime, default 0.10 = 10%)
      CALM_TARGET_PCT    (calm regime,   default 0.07 = 7%)

    Legacy STRESS_TARGET_1_PCT/CALM_TARGET_1_PCT are accepted as fallbacks.

    Values are normalized as percentages: 0.05, 5%, and 5 all mean 5%.
    """
    if str(regime or "").lower() == "stress":
        return _pct_env_any(("STRESS_TARGET_PCT", "STRESS_TARGET_1_PCT"), 0.10)
    return _pct_env_any(("CALM_TARGET_PCT", "CALM_TARGET_1_PCT"), 0.07)


def get_target_pcts_for_regime(regime: str | None) -> tuple[float, None]:
    """Backward-compatible wrapper for callers that still expect a tuple.

    Production option trading now uses one target pct. The second target is
    intentionally disabled and returned as None.
    """
    return (get_target_pct_for_regime(regime), None)


def get_sl_pct_for_regime(regime: str | None) -> float:
    """Return stop_loss_pct for the given volatility regime.

    Reads from env variables:
      STRESS_SL_PCT  (stress regime, default 0.05 = 5%)
      CALM_SL_PCT    (calm regime,   default 0.03 = 3%)

    Values are normalized as percentages: 0.05, 5%, and 5 all mean 5%.
    """
    if str(regime or "").lower() == "stress":
        return _pct_env("STRESS_SL_PCT", 0.05)
    return _pct_env("CALM_SL_PCT", 0.03)


def get_sl_divider_for_regime(regime: str | None) -> float:
    """Return the regime-specific cascade stop-loss widening divider."""
    is_stress = str(regime or "").lower() == "stress"
    name = "STRESS_SL_DIVIDER" if is_stress else "CALM_SL_DIVIDER"
    value = float(os.getenv(name, "5" if is_stress else "10"))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def get_cascade_n_cap() -> int:
    """Maximum completed-target count used in stop-loss widening."""
    value = int(os.getenv("N_CAP", "5"))
    if value < 0:
        raise ValueError("N_CAP must be zero or greater")
    return value


def get_paper_trading_capital() -> float:
    value = float(os.getenv("PAPER_TRADING_CAPITAL", "100000"))
    if value <= 0:
        raise ValueError("PAPER_TRADING_CAPITAL must be greater than zero")
    return value


def get_paper_capital_per_trade_pct() -> float:
    value = _pct_env("PAPER_CAPITAL_PER_TRADE_PCT", 1.0)
    if not 0 < value <= 1:
        raise ValueError("PAPER_CAPITAL_PER_TRADE_PCT must be between 0 and 1")
    return value


def get_drift_probe_min_pct() -> float:
    """Minimum |nifty_drift_pct| required to fire a NO_POSITION drift probe.
    Set DRIFT_PROBE_MIN_PCT in .env (decimal or whole percent, e.g. 0.0015 or 0.15%).
    Default: 0.0015 (0.15%).
    """
    return _pct_env("DRIFT_PROBE_MIN_PCT", 0.0015)


def _pct_env(name: str, default: float) -> float:
    """Read a percentage env var and normalize common operator formats.

    The canonical format remains decimal fractions (`0.05` = 5%), but cron/env
    dashboards often invite `5` or `5%`. Treat values greater than 1 as whole
    percentages to avoid accidentally turning a 5% intent into a 500% threshold.
    """
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)

    text = str(raw).strip()
    is_percent_literal = text.endswith("%")
    if is_percent_literal:
        text = text[:-1].strip()

    return normalize_pct(float(text), is_percent_literal=is_percent_literal)


def _pct_env_any(names: tuple[str, ...], default: float) -> float:
    """Read the first configured percentage env var from a list of aliases."""
    for name in names:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            return _pct_env(name, default)
    return float(default)


def normalize_pct(value: float, is_percent_literal: bool = False) -> float:
    """Normalize decimal or whole-percent user input to a decimal fraction."""
    value = float(value)
    if is_percent_literal or abs(value) > 1:
        value = value / 100.0
    return value


def get_settings() -> Settings:
    return Settings()
