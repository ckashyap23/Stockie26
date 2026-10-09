# scripts/backfill_NIFTY/backfill_underlying.py
"""
Shared underlying backfill for INDEX/STOCK/NIFTY50 watched instruments.

Writes:
  - dbo.UnderlyingSnapshot
  - dbo.UnderlyingCandle5m
"""

import sys
from pathlib import Path
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, List, Tuple

from dotenv import load_dotenv

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

from src.common.config import get_settings
from src.common.models import WatchedInstrument
from src.data_manager.db.database_client import DatabaseClient
from src.data_manager.db.client_factory import get_database_client
from src.data_manager.kite_client import KiteClient

load_dotenv()

BACKFILL_START = date(2025, 1, 1)
BACKFILL_END = date(2025, 12, 31)
DEFAULT_MISSING_START = date(2026, 1, 1)

KITE_TO_CANONICAL_INDEX: Dict[str, str] = {
    "NIFTY 50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
}


def _resolve_tokens(
    kite_client: KiteClient,
    instruments: List[WatchedInstrument],
    instrument_type: str,
) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    need_lookup: List[WatchedInstrument] = []

    for inst in instruments:
        if inst.instrument_token:
            mapping[inst.tradingsymbol] = inst.instrument_token
        else:
            need_lookup.append(inst)

    if not need_lookup:
        return mapping

    exchange = need_lookup[0].exchange or "NSE"
    need_set = {inst.tradingsymbol for inst in need_lookup}
    print(f"Looking up Kite tokens for {instrument_type}: {need_set}")
    kite_instr = kite_client.kite.instruments(exchange)

    for ki in kite_instr:
        ts = ki.get("tradingsymbol", "")
        canonical = KITE_TO_CANONICAL_INDEX.get(ts, ts) if instrument_type == "INDEX" else ts
        if canonical in need_set and canonical not in mapping:
            mapping[canonical] = ki["instrument_token"]

    missing = need_set - mapping.keys()
    if missing:
        print(f"[WARN] Could not resolve Kite tokens for: {missing}")

    return mapping


def upsert_underlying_snapshots(
    db: DatabaseClient,
    rows: List[Tuple[str, date, datetime, float | None, float | None, float | None, float | None, int | None]],
) -> Dict[str, int]:
    if not rows:
        print("No underlying snapshot rows to upsert.")
        return {"prepared": 0, "updated": 0, "inserted": 0, "skipped_duplicates": 0}

    if getattr(db, "db_kind", "") == "postgres":
        return db.upsert_underlying_snapshots(rows)

    cursor = db.conn.cursor()
    cursor.fast_executemany = True

    existing_keys = set()
    unique_keys = list({(row[0], row[1]) for row in rows})
    for i in range(0, len(unique_keys), 25):
        batch = unique_keys[i:i + 25]
        conditions = " OR ".join("(underlying = ? AND trade_date = ?)" for _ in batch)
        params = [v for pair in batch for v in pair]
        cursor.execute(f"SELECT underlying, trade_date FROM dbo.UnderlyingSnapshot WHERE {conditions}", params)
        for row in cursor.fetchall():
            td = row[1].date() if isinstance(row[1], datetime) else row[1]
            existing_keys.add((row[0], td))

    to_insert = [r for r in rows if (r[0], r[1]) not in existing_keys]
    to_update = [r for r in rows if (r[0], r[1]) in existing_keys]
    updated_count = inserted_count = skipped_count = 0

    if to_update:
        cursor.executemany(
            """
            UPDATE dbo.UnderlyingSnapshot
            SET loaded_at = ?, open_price = ?, high_price = ?, low_price = ?,
                close_price = ?, volume = ?
            WHERE underlying = ? AND trade_date = ?
            """,
            [(r[2], r[3], r[4], r[5], r[6], r[7], r[0], r[1]) for r in to_update],
        )
        updated_count = len(to_update)
        db.conn.commit()
        print(f"Updated {updated_count} existing rows")

    if to_insert:
        batch_size = 500
        for i in range(0, len(to_insert), batch_size):
            batch = to_insert[i:i + batch_size]
            try:
                cursor.executemany(
                    """
                    INSERT INTO dbo.UnderlyingSnapshot
                        (underlying, trade_date, loaded_at, open_price, high_price, low_price, close_price, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                inserted_count += len(batch)
                db.conn.commit()
            except Exception as e:
                err = str(e).lower()
                if "unique" in err or "duplicate" in err or "violation" in err:
                    for row in batch:
                        try:
                            cursor.execute(
                                """
                                INSERT INTO dbo.UnderlyingSnapshot
                                    (underlying, trade_date, loaded_at, open_price, high_price, low_price, close_price, volume)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                row,
                            )
                            inserted_count += 1
                            db.conn.commit()
                        except Exception as e2:
                            if any(k in str(e2).lower() for k in ("unique", "duplicate", "violation")):
                                skipped_count += 1
                            else:
                                raise
                else:
                    raise
        print(f"Inserted {inserted_count} new rows")
        if skipped_count:
            print(f"Skipped {skipped_count} duplicate rows")

    db.conn.commit()
    cursor.close()
    return {"prepared": len(rows), "updated": updated_count, "inserted": inserted_count, "skipped_duplicates": skipped_count}


def upsert_underlying_candles_5m(
    db: DatabaseClient,
    rows: List[Tuple[str, date, datetime, float, float, float, float, int | None]],
) -> Dict[str, int]:
    if not rows:
        print("No 5-minute candle rows to upsert.")
        return {"prepared": 0, "updated": 0, "inserted": 0, "skipped_duplicates": 0}

    if getattr(db, "db_kind", "") == "postgres":
        return db.upsert_underlying_candles_5m(rows)

    cursor = db.conn.cursor()
    cursor.fast_executemany = True

    existing_keys = set()
    unique_keys = list({(row[0], row[2]) for row in rows})
    for i in range(0, len(unique_keys), 25):
        batch = unique_keys[i:i + 25]
        conditions = " OR ".join("(underlying = ? AND candle_time = ?)" for _ in batch)
        params = [v for pair in batch for v in pair]
        cursor.execute(f"SELECT underlying, candle_time FROM dbo.UnderlyingCandle5m WHERE {conditions}", params)
        for row in cursor.fetchall():
            ct = row[1].replace(tzinfo=None) if hasattr(row[1], "tzinfo") else row[1]
            existing_keys.add((row[0], ct))

    to_insert = [r for r in rows if (r[0], r[2]) not in existing_keys]
    to_update = [r for r in rows if (r[0], r[2]) in existing_keys]
    updated_count = inserted_count = skipped_count = 0

    if to_update:
        batch_size = 500
        for i in range(0, len(to_update), batch_size):
            batch = to_update[i:i + batch_size]
            cursor.executemany(
                """
                UPDATE dbo.UnderlyingCandle5m
                SET trade_date = ?, open_price = ?, high_price = ?, low_price = ?,
                    close_price = ?, volume = ?
                WHERE underlying = ? AND candle_time = ?
                """,
                [(r[1], r[3], r[4], r[5], r[6], r[7], r[0], r[2]) for r in batch],
            )
            updated_count += len(batch)
            db.conn.commit()
        print(f"Updated {updated_count} existing 5-minute candle rows")

    if to_insert:
        batch_size = 500
        for i in range(0, len(to_insert), batch_size):
            batch = to_insert[i:i + batch_size]
            try:
                cursor.executemany(
                    """
                    INSERT INTO dbo.UnderlyingCandle5m
                        (underlying, trade_date, candle_time, open_price, high_price, low_price, close_price, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                inserted_count += len(batch)
                db.conn.commit()
            except Exception as e:
                err = str(e).lower()
                if "unique" in err or "duplicate" in err or "violation" in err:
                    for row in batch:
                        try:
                            cursor.execute(
                                """
                                INSERT INTO dbo.UnderlyingCandle5m
                                    (underlying, trade_date, candle_time, open_price, high_price, low_price, close_price, volume)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                row,
                            )
                            inserted_count += 1
                            db.conn.commit()
                        except Exception as e2:
                            if any(k in str(e2).lower() for k in ("unique", "duplicate", "violation")):
                                skipped_count += 1
                            else:
                                raise
                else:
                    raise
            if len(to_insert) > batch_size:
                print(f"  Inserted batch {i // batch_size + 1}/{(len(to_insert) - 1) // batch_size + 1} ({inserted_count + skipped_count}/{len(to_insert)} rows done)")

        print(f"Inserted {inserted_count} new 5-minute candle rows")
        if skipped_count:
            print(f"Skipped {skipped_count} duplicate 5-minute candle rows")

    db.conn.commit()
    cursor.close()
    return {"prepared": len(rows), "updated": updated_count, "inserted": inserted_count, "skipped_duplicates": skipped_count}


def _get_missing_ranges(
    db: DatabaseClient,
    symbol: str,
    start_date: date,
    end_date: date,
) -> Tuple[List[Tuple[date, date]], List[Tuple[date, date]]]:
    def _to_date(val):
        if val is None:
            return None
        return val.date() if isinstance(val, datetime) else val

    def _gaps(db_min, db_max) -> List[Tuple[date, date]]:
        if db_min is None:
            return [(start_date, end_date)]
        gaps = []
        if db_min > start_date:
            gaps.append((start_date, db_min - timedelta(days=1)))
        if db_max < end_date:
            gaps.append((db_max + timedelta(days=1), end_date))
        return gaps

    is_postgres = getattr(db, "db_kind", "") == "postgres"
    ph = "%s" if is_postgres else "?"
    snap_table = '"UnderlyingSnapshot"' if is_postgres else "dbo.UnderlyingSnapshot"
    candle_table = '"UnderlyingCandle5m"' if is_postgres else "dbo.UnderlyingCandle5m"

    cursor = db.conn.cursor()

    if is_postgres:
        cursor.execute(
            f"SELECT MIN(trade_date), MAX(trade_date) FROM {snap_table} "
            f"WHERE underlying = {ph} AND trade_date >= {ph} AND trade_date <= {ph}",
            (symbol, start_date, end_date),
        )
    else:
        cursor.execute(
            f"SELECT MIN(trade_date), MAX(trade_date) FROM {snap_table} "
            f"WHERE underlying = {ph} AND trade_date >= {ph} AND trade_date <= {ph}",
            symbol, start_date, end_date,
        )
    r = cursor.fetchone()
    snap_gaps = _gaps(_to_date(r[0]), _to_date(r[1]))

    try:
        if is_postgres:
            cursor.execute(
                f"SELECT MIN(trade_date), MAX(trade_date) FROM {candle_table} "
                f"WHERE underlying = {ph} AND trade_date >= {ph} AND trade_date <= {ph}",
                (symbol, start_date, end_date),
            )
        else:
            cursor.execute(
                f"SELECT MIN(trade_date), MAX(trade_date) FROM {candle_table} "
                f"WHERE underlying = {ph} AND trade_date >= {ph} AND trade_date <= {ph}",
                symbol, start_date, end_date,
            )
        r = cursor.fetchone()
        candle_gaps = _gaps(_to_date(r[0]), _to_date(r[1]))
    except Exception:
        # Table may not exist yet on first run; treat as full gap
        if is_postgres:
            db.conn.rollback()
        candle_gaps = [(start_date, end_date)]

    cursor.close()
    return snap_gaps, candle_gaps


def run_backfill_underlying_data(
    instrument_type: str,
    start_date: date,
    end_date: date,
    underlyings: List[str] | None = None,
    include_daily: bool = True,
    include_candles: bool = True,
) -> Dict[str, object]:
    instrument_type = instrument_type.upper()
    if instrument_type not in {"INDEX", "STOCK", "NIFTY50"}:
        raise ValueError("instrument_type must be INDEX, STOCK, or NIFTY50")
    if not include_daily and not include_candles:
        raise ValueError("At least one of include_daily/include_candles must be True")

    settings = get_settings()

    tdb = get_database_client(settings)
    tdb.connect()
    try:
        watched = tdb.get_watched_instruments(instrument_type=instrument_type)
    finally:
        tdb.close()

    if underlyings and "ALL" not in {u.upper() for u in underlyings}:
        target_set = {u.upper() for u in underlyings}
        watched = [w for w in watched if w.tradingsymbol.upper() in target_set]

    if not watched:
        print(f"No active {instrument_type} instruments found in WatchedInstrument.")
        return {"underlyings": [], "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "daily": {}, "candles_5m": {}}

    target_underlyings = [w.tradingsymbol for w in watched]
    phases = []
    if include_daily:
        phases.append("daily snapshots")
    if include_candles:
        phases.append("5m candles")
    print(f"Backfilling {instrument_type} {', '.join(target_underlyings)} {' + '.join(phases)} for {start_date} to {end_date}")

    kite_client = KiteClient(settings)
    kite_client.authenticate()
    token_map = _resolve_tokens(kite_client, watched, instrument_type)
    if not token_map:
        print("[ERROR] No instrument tokens resolved. Aborting.")
        return {"underlyings": target_underlyings, "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "daily": {}, "candles_5m": {}}

    db = get_database_client(settings)
    db.connect()
    coverage: Dict[str, Tuple[List[Tuple[date, date]], List[Tuple[date, date]]]] = {}
    print("\nChecking DB coverage...")
    for symbol in token_map:
        snap_gaps, candle_gaps = _get_missing_ranges(db, symbol, start_date, end_date)
        if not include_daily:
            snap_gaps = []
        if not include_candles:
            candle_gaps = []
        coverage[symbol] = (snap_gaps, candle_gaps)
        if not snap_gaps and not candle_gaps:
            print(f"  {symbol}: fully covered, skipping Kite fetch")
        else:
            snap_str = ", ".join(f"{a} to {b}" for a, b in snap_gaps) or "none"
            candle_str = ", ".join(f"{a} to {b}" for a, b in candle_gaps) or "none"
            print(f"  {symbol}: snap gaps=[{snap_str}]  candle gaps=[{candle_str}]")
    db.close()

    current_time = datetime.now()
    market_start = dtime(9, 15)
    market_end = dtime(15, 25)
    chunk_size_days = 100

    daily_rows: List[Tuple[str, date, datetime, float | None, float | None, float | None, float | None, int | None]] = []
    candle_5m_rows: List[Tuple[str, date, datetime, float, float, float, float, int | None]] = []

    for symbol, token in token_map.items():
        snap_gaps, candle_gaps = coverage[symbol]

        if include_daily and snap_gaps:
            print(f"\n{symbol}: fetching daily snapshots...")
            for gap_start, gap_end in snap_gaps:
                print(f"  Gap: {gap_start} to {gap_end}")
                try:
                    daily_candles = kite_client.kite.historical_data(
                        token,
                        datetime.combine(gap_start, dtime(9, 15)),
                        datetime.combine(gap_end, dtime(15, 30)),
                        interval="day", continuous=False, oi=False,
                    )
                except Exception as e:
                    print(f"  [WARN] Failed daily fetch: {e}")
                    daily_candles = []
                for c in daily_candles:
                    c_dt = c["date"].replace(tzinfo=None)
                    trade_dt = c_dt.date()
                    if gap_start <= trade_dt <= gap_end:
                        daily_rows.append((
                            symbol, trade_dt, current_time,
                            float(c["open"]) if c.get("open") is not None else None,
                            float(c["high"]) if c.get("high") is not None else None,
                            float(c["low"]) if c.get("low") is not None else None,
                            float(c["close"]) if c.get("close") is not None else None,
                            int(c["volume"]) if c.get("volume") is not None else None,
                        ))
            print(f"  {symbol}: {len([r for r in daily_rows if r[0] == symbol])} daily rows queued")
        elif include_daily:
            print(f"\n{symbol}: daily snapshots fully covered, skipping")

        if include_candles and candle_gaps:
            print(f"{symbol}: fetching 5m candles...")
            for gap_start, gap_end in candle_gaps:
                print(f"  Gap: {gap_start} to {gap_end}")
                chunk_start = gap_start
                while chunk_start <= gap_end:
                    chunk_end = min(chunk_start + timedelta(days=chunk_size_days - 1), gap_end)
                    print(f"    Chunk: {chunk_start} to {chunk_end}...")
                    try:
                        chunk_candles = kite_client.kite.historical_data(
                            token,
                            datetime.combine(chunk_start, dtime(9, 15)),
                            datetime.combine(chunk_end, dtime(15, 30)),
                            interval="5minute", continuous=False, oi=False,
                        )
                        print(f"    Fetched {len(chunk_candles)} candles")
                        for c in chunk_candles:
                            c_dt = c["date"].replace(tzinfo=None)
                            trade_dt = c_dt.date()
                            if gap_start <= trade_dt <= gap_end and market_start <= c_dt.time() <= market_end:
                                candle_5m_rows.append((
                                    symbol, trade_dt, c_dt,
                                    float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]),
                                    int(c["volume"]) if c.get("volume") is not None else None,
                                ))
                    except Exception as e:
                        print(f"    [WARN] Failed chunk: {e}")
                    chunk_start = chunk_end + timedelta(days=1)
        elif include_candles:
            print(f"{symbol}: 5m candles fully covered, skipping")

    db.connect()

    snapshot_summary = {"prepared": 0, "updated": 0, "inserted": 0, "skipped_duplicates": 0}
    if include_daily:
        print(f"\nPrepared {len(daily_rows)} UnderlyingSnapshot rows")
        snapshot_summary = upsert_underlying_snapshots(db, daily_rows)
        print("UnderlyingSnapshot upsert complete.")

    candle_summary = {"prepared": 0, "updated": 0, "inserted": 0, "skipped_duplicates": 0}
    if include_candles:
        print(f"\nPrepared {len(candle_5m_rows)} UnderlyingCandle5m rows")
        candle_summary = upsert_underlying_candles_5m(db, candle_5m_rows)
        print("UnderlyingCandle5m upsert complete.")

    db.close()

    feature_summary: dict = {}
    if include_daily and snapshot_summary.get("inserted", 0) + snapshot_summary.get("updated", 0) > 0:
        from scripts.Common.calculate_underlying_features import run_calculate_underlying_features
        print("\nComputing underlying features for newly ingested snapshots...")
        feature_summary = run_calculate_underlying_features(
            start_date=start_date,
            end_date=end_date,
            underlyings=target_underlyings,
        )

    print(f"{instrument_type} underlying backfill done.")
    return {
        "underlyings": target_underlyings,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "daily": snapshot_summary,
        "candles_5m": candle_summary,
        "signal_features": feature_summary,
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Backfill underlying daily + 5m candles for watched INDEX/STOCK/NIFTY50 entries")
    parser.add_argument("--type", choices=["INDEX", "STOCK", "NIFTY50"], default="INDEX", help="WatchedInstrument instrument_type. Default: INDEX")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD. Default: 2026-01-01 missing-coverage scan.")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD. Default: today.")
    parser.add_argument("--underlying", default="NIFTY", help="Comma-separated underlyings. Default: NIFTY. Use ALL for every active watched instrument of --type.")
    phase_group = parser.add_mutually_exclusive_group()
    phase_group.add_argument("--daily-only", action="store_true", help="Fetch/upsert only daily snapshots")
    phase_group.add_argument("--candles-only", action="store_true", help="Fetch/upsert only 5-minute candles")
    args = parser.parse_args()
    start_date = date.fromisoformat(args.start) if args.start else DEFAULT_MISSING_START
    end_date = date.fromisoformat(args.end) if args.end else date.today()

    run_backfill_underlying_data(
        instrument_type=args.type,
        start_date=start_date,
        end_date=end_date,
        underlyings=[u.strip().upper() for u in args.underlying.split(",")] if args.underlying else None,
        include_daily=not args.candles_only,
        include_candles=not args.daily_only,
    )


if __name__ == "__main__":
    main()
