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
    return int(os.getenv("UNDERLYING_LOOKBACK_DAYS", "3"))


def get_nifty_target_pct(regime: str) -> float:
    """NIFTY underlying move required for actual_trade_label per volatility regime.

    This is independent of option-premium targets and stops.
    """
    if str(regime or "").lower() == "stress":
        return float(os.getenv("STRESS_NIFTY_TARGET_PCT", "0.005"))
    return float(os.getenv("CALM_NIFTY_TARGET_PCT", "0.003"))


def get_regime_threshold(regime: str) -> float:
    """Backward-compatible alias for the NIFTY label target."""
    return get_nifty_target_pct(regime)


def get_target_pcts_for_regime(regime: str | None) -> tuple[float, float]:
    """Return (target_1_pct, target_2_pct) for the given volatility regime.

    Reads from env variables:
      STRESS_TARGET_1_PCT, STRESS_TARGET_2_PCT  (stress regime)
      CALM_TARGET_1_PCT,   CALM_TARGET_2_PCT    (calm regime)
    """
    if str(regime or "").lower() == "stress":
        t1 = float(os.getenv("STRESS_TARGET_1_PCT", "0.005"))
        t2 = float(os.getenv("STRESS_TARGET_2_PCT", "0.007"))
    else:
        t1 = float(os.getenv("CALM_TARGET_1_PCT", "0.003"))
        t2 = float(os.getenv("CALM_TARGET_2_PCT", "0.005"))
    return (t1, t2)


def get_sl_pct_for_regime(regime: str | None) -> float:
    """Return stop_loss_pct for the given volatility regime.

    Reads from env variables:
      STRESS_SL_PCT  (stress regime, default 0.03 = 3%)
      CALM_SL_PCT    (calm regime,   default 0.03 = 3%)
    """
    if str(regime or "").lower() == "stress":
        return float(os.getenv("STRESS_SL_PCT", "0.03"))
    return float(os.getenv("CALM_SL_PCT", "0.03"))


def get_settings() -> Settings:
    return Settings()
