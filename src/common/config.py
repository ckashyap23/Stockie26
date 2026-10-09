import os
from pathlib import Path
from dotenv import load_dotenv

_repo_root_env = Path(__file__).resolve().parents[2] / ".env"
# Load repo-root .env deterministically so running from different working dirs still works.
# Do not override existing environment variables (so deployment env wins).
load_dotenv(dotenv_path=_repo_root_env if _repo_root_env.exists() else None, override=False)

# Hardcoded production thresholds (no longer sourced from .env)
VIX_QUIET_MAX = 14.0
BB_WIDTH_MIN = 0.040
DRIFT_PROBE_MIN_PCT = 0.004
GAP_GUARD_PCT = 0.003


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
    """Legacy option holding-window setting retained for maintenance tooling.

    Production backtests now exit on the execution day, while paper trades have
    no time exit.
    """
    return int(os.getenv("TRADE_HORIZON_DAYS", "1"))


def get_underlying_lookback_days() -> int:
    """Lookback window (in trading days) for NIFTY prediction quality assessment.

    Controls actual_trade_label (future_high_nd / future_low_nd window in dataset.py)
    and signal quality score horizon in signal_strength.py.
    Reads UNDERLYING_LOOKBACK_DAYS from the environment (set in .env).
    """
    return int(os.getenv("UNDERLYING_LOOKBACK_DAYS", "1"))


def get_strategy_config() -> dict[str, float]:
    """Return shared NIFTY strategy thresholds with no regime split."""
    return {
        "target_pct": get_nifty_target_pct(),
        "bb_width_min": BB_WIDTH_MIN,
        "vix_quiet_max": VIX_QUIET_MAX,
    }


def get_nifty_target_pct(*_legacy_args) -> float:
    """NIFTY underlying move required for actual_trade_label."""
    return _pct_env("NIFTY_TARGET_PCT", 0.010)


def get_nifty_target_pct_relaxed() -> float:
    """NIFTY underlying move required for actual_trade_label_relaxed."""
    return _pct_env("NIFTY_TARGET_PCT_RELAXED", 0.004)


def get_target_pct() -> float:
    """Return the NIFTY move percentage used for the delta-adjusted target."""
    return _pct_env_required_any(("TARGET_PCT_EFFECTIVE", "TARGET_PCT"))


def get_probe_target_pct() -> float:
    """Return the shared NIFTY move percentage used for the target."""
    return get_target_pct()


def get_target_pct_for_strategy(primary_strategy: str | None) -> float:
    """Return the shared option target for any prediction strategy."""
    _ = primary_strategy
    return get_target_pct()


def get_target_pcts() -> tuple[float, None]:
    """Production option trading uses one target pct; target 2 is disabled."""
    return (get_target_pct(), None)


def get_sl_pct() -> float:
    """Return the NIFTY move percentage used for the delta-adjusted stop."""
    return _pct_env_required_any(("SL_PCT_EFFECTIVE", "SL_PCT"))


def get_sl_pct_probe() -> float:
    """Return the shared NIFTY move percentage used for the stop."""
    return get_sl_pct()


def get_sl_pct_for_strategy(
    primary_strategy: str | None,
    atr14: float | None = None,
    close_price: float | None = None,
) -> float:
    """Return the shared option stop-loss for any prediction strategy."""
    _ = primary_strategy, atr14, close_price
    return get_sl_pct()


def get_sl_divider() -> float:
    """Return the cascade stop-loss widening divider."""
    value = float(os.getenv("SL_DIVIDER", "10"))
    if value <= 0:
        raise ValueError("SL_DIVIDER must be greater than zero")
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
    """Minimum |nifty_drift_pct| required to fire a NO_POSITION drift probe."""
    return DRIFT_PROBE_MIN_PCT


def get_drift_probe_half_min_pct() -> float:
    """Minimum |nifty_drift_pct| required when the probe would fire at HALF_SIZE
    (i.e. gap does not align with drift).  A higher floor here filters low-conviction
    probes where drift is weak AND gap is contradicting.
    Set DRIFT_PROBE_HALF_MIN_PCT in .env.  Default: 0.002 (0.20%).
    Must be >= DRIFT_PROBE_MIN_PCT; if set lower it is clamped to that value.
    """
    val = _pct_env("DRIFT_PROBE_HALF_MIN_PCT", 0.002)
    return max(val, get_drift_probe_min_pct())


def get_gap_guard_pct() -> float:
    """Return the same-direction open-gap threshold that suppresses chase entries."""
    return GAP_GUARD_PCT


def get_strategy_feature_cleanup_enabled() -> bool:
    """Toggle strategy-facing feature cleanup in cascade.dataset.build_base.

    Set STRATEGY_FEATURE_CLEANUP_ENABLED=0 for emergency rollback to legacy
    behavior without code changes.
    """
    raw = os.getenv("STRATEGY_FEATURE_CLEANUP_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def get_macro_trailing_window() -> int:
    """Trading-session window used to normalize macro regime inputs."""
    raw = os.getenv("MACRO_TRAILING_WINDOW", "60").strip()
    try:
        window = int(raw)
    except ValueError as exc:
        raise ValueError("MACRO_TRAILING_WINDOW must be an integer") from exc
    if window < 2:
        raise ValueError("MACRO_TRAILING_WINDOW must be >= 2")
    return window


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


def _pct_env_required(name: str) -> float:
    """Read a required percentage env var and normalize it."""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        raise RuntimeError(f"Missing required env var: {name}")
    return _pct_env(name, 0.0)


def _pct_env_required_any(names: tuple[str, ...]) -> float:
    """Read the first configured required percentage env var from a list of aliases."""
    for name in names:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            return _pct_env(name, 0.0)
    joined = ", ".join(names)
    raise RuntimeError(f"Missing required env var. Set one of: {joined}")


def normalize_pct(value: float, is_percent_literal: bool = False) -> float:
    """Normalize decimal or whole-percent user input to a decimal fraction."""
    value = float(value)
    if is_percent_literal or abs(value) > 1:
        value = value / 100.0
    return value


def get_settings() -> Settings:
    return Settings()
