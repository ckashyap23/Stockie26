from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GLOBAL_INDEX_OUTPUT_DIR = PROJECT_ROOT / "output" / "intelligence" / "global_index_ohlc"

# Asian indexes whose production OHLC is stored as partial intraday data
# (open → 9:20 AM IST = 03:50 UTC) rather than full-day bars.
ASIA_PARTIAL_INDEXES: frozenset[str] = frozenset({
    "NIKKEI225", "HANG_SENG", "SHANGHAI", "KOSPI", "ASX200",
})
# Cutoff: 9:20 AM IST = 03:50 UTC
_PARTIAL_CUTOFF = pd.Timedelta(hours=3, minutes=50)


GLOBAL_INDEX_UNIVERSE: tuple[dict[str, str], ...] = (
    {"index_code": "NIFTY50", "index_name": "Nifty 50", "yahoo_symbol": "^NSEI", "region": "India", "currency": "INR"},
    {"index_code": "SENSEX", "index_name": "BSE Sensex", "yahoo_symbol": "^BSESN", "region": "India", "currency": "INR"},
    {"index_code": "INDIA_VIX", "index_name": "India VIX", "yahoo_symbol": "^INDIAVIX", "region": "India", "currency": "INR"},
    {"index_code": "SP500", "index_name": "S&P 500", "yahoo_symbol": "^GSPC", "region": "United States", "currency": "USD"},
    {"index_code": "NASDAQ", "index_name": "NASDAQ Composite", "yahoo_symbol": "^IXIC", "region": "United States", "currency": "USD"},
    {"index_code": "DOW", "index_name": "Dow Jones Industrial Average", "yahoo_symbol": "^DJI", "region": "United States", "currency": "USD"},
    {"index_code": "RUSSELL2000", "index_name": "Russell 2000", "yahoo_symbol": "^RUT", "region": "United States", "currency": "USD"},
    {"index_code": "FTSE100", "index_name": "FTSE 100", "yahoo_symbol": "^FTSE", "region": "United Kingdom", "currency": "GBP"},
    {"index_code": "DAX", "index_name": "DAX", "yahoo_symbol": "^GDAXI", "region": "Germany", "currency": "EUR"},
    {"index_code": "CAC40", "index_name": "CAC 40", "yahoo_symbol": "^FCHI", "region": "France", "currency": "EUR"},
    {"index_code": "HANG_SENG", "index_name": "Hang Seng", "yahoo_symbol": "^HSI", "region": "Hong Kong", "currency": "HKD"},
    {"index_code": "NIKKEI225", "index_name": "Nikkei 225", "yahoo_symbol": "^N225", "region": "Japan", "currency": "JPY"},
    {"index_code": "SHANGHAI", "index_name": "Shanghai Composite", "yahoo_symbol": "000001.SS", "region": "China", "currency": "CNY"},
    {"index_code": "KOSPI", "index_name": "KOSPI", "yahoo_symbol": "^KS11", "region": "South Korea", "currency": "KRW"},
    {"index_code": "ASX200", "index_name": "ASX 200", "yahoo_symbol": "^AXJO", "region": "Australia", "currency": "AUD"},
)


def fetch_global_index_ohlc(
    start_date: date,
    end_date: date,
    index_universe: tuple[dict[str, str], ...] = GLOBAL_INDEX_UNIVERSE,
) -> list[dict[str, Any]]:
    """Fetch OHLC for all indices over [start_date, end_date] using a 3-tier strategy.

    For each (index, date) pair:
      Tier 1  Complete 1d bar exists for that date   -> is_final=True,  source="yfinance_1d"
      Tier 2  Market open but not closed (today only)-> is_final=False, source="yfinance_5m"
              partial OHLC reconstructed from 5m bars
      Tier 3  No data for that date at all           -> most-recent prior trading session,
                                                        is_final=True,  source="yfinance_1d",
                                                        trade_date = that prior session's date
    """
    if start_date > end_date:
        raise ValueError("start_date must be <= end_date")

    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for global index loading. Install requirements.txt first.") from exc

    today = date.today()
    fetched_at = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []

    for index_meta in index_universe:
        symbol = index_meta["yahoo_symbol"]

        # Batch-download 1d bars; extend lookback so Tier 3 can find a prior session.
        batch_start = start_date - timedelta(days=10)
        try:
            frame = yf.download(
                symbol,
                start=batch_start.isoformat(),
                end=(end_date + timedelta(days=1)).isoformat(),
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            if isinstance(frame.columns, pd.MultiIndex):
                frame.columns = frame.columns.get_level_values(0)
        except Exception as exc:
            print(f"[{index_meta['index_code']}] Batch 1d download failed: {exc}")
            frame = pd.DataFrame()

        # Build date -> completed-bar lookup from the 1d download.
        completed: dict[date, Any] = {}
        for ts, r in frame.iterrows():
            d = pd.Timestamp(ts).date()
            if _float_or_none(r.get("Close")) is not None:
                completed[d] = r

        base = {
            "index_code": index_meta["index_code"],
            "index_name": index_meta["index_name"],
            "yahoo_symbol": symbol,
            "region": index_meta.get("region"),
            "currency": index_meta.get("currency"),
            "fetched_at": fetched_at,
        }

        target = start_date
        while target <= end_date:

            # ── Tier 1: completed 1d bar ─────────────────────────────────────
            if target in completed:
                r = completed[target]
                rows.append({
                    **base,
                    "trade_date": target,
                    "open_price":  _float_or_none(r.get("Open")),
                    "high_price":  _float_or_none(r.get("High")),
                    "low_price":   _float_or_none(r.get("Low")),
                    "close_price": _float_or_none(r.get("Close")),
                    "adj_close":   _float_or_none(r.get("Adj Close")),
                    "volume":      _int_or_none(r.get("Volume")),
                    "source":   "yfinance_1d",
                    "is_final": True,
                })
                target += timedelta(days=1)
                continue

            # ── Tier 2: partial 5m reconstruction (today only) ───────────────
            if target == today:
                partial = _fetch_5m_partial(index_meta, today, fetched_at)
                if partial:
                    rows.append(partial)
                    target += timedelta(days=1)
                    continue

            # ── Tier 3: most-recent prior completed session ───────────────────
            prior_dates = sorted((d for d in completed if d < target), reverse=True)
            if prior_dates:
                prev_d = prior_dates[0]
                r = completed[prev_d]
                rows.append({
                    **base,
                    "trade_date": prev_d,
                    "open_price":  _float_or_none(r.get("Open")),
                    "high_price":  _float_or_none(r.get("High")),
                    "low_price":   _float_or_none(r.get("Low")),
                    "close_price": _float_or_none(r.get("Close")),
                    "adj_close":   _float_or_none(r.get("Adj Close")),
                    "volume":      _int_or_none(r.get("Volume")),
                    "source":   "yfinance_1d",
                    "is_final": True,
                })
            else:
                print(f"[{index_meta['index_code']}] No data available for {target} or any prior session")

            target += timedelta(days=1)

    return rows


def _fetch_5m_partial(
    index_meta: dict[str, str],
    target_date: date,
    fetched_at: datetime,
    cutoff_utc: pd.Timedelta = _PARTIAL_CUTOFF,
) -> dict[str, Any] | None:
    """Tier 2: reconstruct partial OHLC from intraday 5-minute bars.

    Filters bars to those starting at or before `cutoff_utc` from midnight UTC
    on `target_date`.  Returns a row dict with is_final=False, or None if no
    bars are available for target_date within the cutoff window.
    """
    try:
        import yfinance as yf
        ticker = yf.Ticker(index_meta["yahoo_symbol"])
        intraday = ticker.history(period="1d", interval="5m", auto_adjust=True, prepost=False)
    except Exception as exc:
        print(f"[{index_meta['index_code']}] Tier 2 (5m) download failed: {exc}")
        return None

    if intraday.empty:
        return None

    intraday = intraday.dropna(subset=["Open", "High", "Low", "Close"])

    # Convert index to UTC for reliable date + cutoff filtering.
    idx_utc = intraday.index.tz_convert("UTC") if intraday.index.tzinfo else intraday.index.tz_localize("UTC")
    cutoff_ts = pd.Timestamp(target_date, tz="UTC") + cutoff_utc
    mask = (pd.DatetimeIndex(idx_utc).date == target_date) & (idx_utc <= cutoff_ts)
    today_bars = intraday[mask]
    if today_bars.empty:
        return None

    return {
        "index_code": index_meta["index_code"],
        "index_name": index_meta["index_name"],
        "yahoo_symbol": index_meta["yahoo_symbol"],
        "region":   index_meta.get("region"),
        "currency": index_meta.get("currency"),
        "trade_date":  target_date,
        "open_price":  float(today_bars["Open"].iloc[0]),
        "high_price":  float(today_bars["High"].max()),
        "low_price":   float(today_bars["Low"].min()),
        "close_price": float(today_bars["Close"].iloc[-1]),
        "adj_close":   None,
        "volume":      float(today_bars["Volume"].fillna(0).sum()) if "Volume" in today_bars.columns else None,
        "source":   "yfinance_5m",
        "is_final": False,
        "fetched_at": fetched_at,
    }


def normalize_yfinance_frame(
    frame: pd.DataFrame,
    index_meta: dict[str, str],
    fetched_at: datetime | None = None,
    is_final: bool = True,
    source: str = "yfinance_1d",
) -> list[dict[str, Any]]:
    """Convert a yf.download 1d frame to row dicts. Kept for backward compatibility."""
    if frame is None or frame.empty:
        return []

    normalized = frame.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        normalized.columns = normalized.columns.get_level_values(0)

    fetched_at = fetched_at or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for raw_trade_date, row in normalized.iterrows():
        trade_date = pd.Timestamp(raw_trade_date).date()
        rows.append({
            "index_code": index_meta["index_code"],
            "index_name": index_meta["index_name"],
            "yahoo_symbol": index_meta["yahoo_symbol"],
            "region":   index_meta.get("region"),
            "currency": index_meta.get("currency"),
            "trade_date":  trade_date,
            "open_price":  _float_or_none(row.get("Open")),
            "high_price":  _float_or_none(row.get("High")),
            "low_price":   _float_or_none(row.get("Low")),
            "close_price": _float_or_none(row.get("Close")),
            "adj_close":   _float_or_none(row.get("Adj Close")),
            "volume":      _int_or_none(row.get("Volume")),
            "source":   source,
            "is_final": is_final,
            "fetched_at": fetched_at,
        })
    return rows


def write_global_index_ohlc_csv(
    rows: list[dict[str, Any]],
    end_date: date,
    output_dir: Path = DEFAULT_GLOBAL_INDEX_OUTPUT_DIR,
) -> Path | None:
    if not rows:
        return None
    partition_dir = output_dir / end_date.strftime("%d-%m-%Y")
    partition_dir.mkdir(parents=True, exist_ok=True)
    output_path = partition_dir / "global_index_ohlc.csv"
    pd.DataFrame(rows).sort_values(["trade_date", "index_code"]).to_csv(output_path, index=False)
    return output_path


def _float_or_none(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _int_or_none(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)