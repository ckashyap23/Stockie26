"""Get feature values for 2026-08-11 to diagnose PUT strategy misses."""
from src.common.config import get_settings
from src.data_manager.db.supabase_client import SupabaseDatabaseClient

settings = get_settings()
db = SupabaseDatabaseClient(settings)
db.connect()

PUT_FIELDS = [
    "signal_date", "ret_3d", "atr14", "close_1515",
    "ma5d_slope", "ma20_slope", "ma10d_slope",
    "range_position_10d", "range_position_20d", "bb_width",
    "rsi5", "rsi14",
    "support_broken_10d", "resistance_distance_10d",
    "vix_close", "volatility_10d",
    "nifty_drift_pct", "nifty_gap_pct", "volume_day",
]

# Try NiftyFeatures table first
tables_tried = []
for tbl in ["NiftyFeatures", "nifty_features", "NiftyBaseFeatures", "nifty_base_features"]:
    try:
        with db.conn.cursor() as cur:
            cur.execute(f'SELECT * FROM "{tbl}" WHERE signal_date = %s', ("2026-08-11",))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            if row:
                data = dict(zip(cols, row))
                print(f"=== {tbl} for 2026-08-11 ===")
                for f in PUT_FIELDS:
                    print(f"  {f}: {data.get(f, 'NOT IN TABLE')}")
                print("\n--- All columns ---")
                print(list(data.keys()))
                break
            else:
                tables_tried.append(f"{tbl} (no row)")
    except Exception as e:
        db.conn.rollback()
        tables_tried.append(f"{tbl}: {e}")

# Also try reading from NiftyPrediction which may store features too
try:
    with db.conn.cursor() as cur:
        cur.execute('SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position', ("NiftyPrediction",))
        cols = [r[0] for r in cur.fetchall()]
        print(f"\n=== NiftyPrediction columns ({len(cols)}) ===")
        print(cols)
except Exception as e:
    db.conn.rollback()
    print(f"NiftyPrediction column query failed: {e}")

# Get all values from NiftyPrediction for Aug 11 that match PUT_FIELDS
try:
    with db.conn.cursor() as cur:
        cur.execute('SELECT * FROM "NiftyPrediction" WHERE signal_date = %s', ("2026-08-11",))
        cols2 = [d[0] for d in cur.description]
        row2 = cur.fetchone()
        if row2:
            data2 = dict(zip(cols2, row2))
            print("\n=== NiftyPrediction feature cols for 2026-08-11 ===")
            for f in PUT_FIELDS:
                val = data2.get(f, "NOT IN TABLE")
                print(f"  {f}: {val}")
except Exception as e:
    db.conn.rollback()
    print(f"NiftyPrediction feature query failed: {e}")

print("\nTables tried:", tables_tried)
