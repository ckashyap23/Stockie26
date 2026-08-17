"""List DB tables and find feature values for Aug 11."""
from src.common.config import get_settings
from src.data_manager.db.supabase_client import SupabaseDatabaseClient

settings = get_settings()
db = SupabaseDatabaseClient(settings)
db.connect()

with db.conn.cursor() as cur:
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    tables = [r[0] for r in cur.fetchall()]
    print("DB Tables:", tables)

# Look for feature-like tables that might have Aug 11 data
FEATURE_COLS = ["ret_3d", "atr14", "ma5d_slope", "ma20_slope", "range_position_10d", "bb_width", "rsi14"]
for tbl in tables:
    try:
        with db.conn.cursor() as cur:
            cur.execute(f'SELECT column_name FROM information_schema.columns WHERE table_name = %s', (tbl,))
            cols = [r[0] for r in cur.fetchall()]
            overlap = [c for c in FEATURE_COLS if c in cols]
            if len(overlap) >= 2:
                print(f"\n*** {tbl} has feature cols: {overlap}")
                # Try to get Aug 11 row
                date_cols = [c for c in cols if "date" in c.lower() or "signal" in c.lower()]
                if date_cols:
                    dc = date_cols[0]
                    cur2 = db.conn.cursor()
                    cur2.execute(f'SELECT * FROM "{tbl}" WHERE "{dc}" = %s LIMIT 1', ("2026-08-11",))
                    r = cur2.fetchone()
                    if r:
                        data = dict(zip(cols, r))
                        for f in FEATURE_COLS + ["vix", "vix_close", "support_broken_10d", "resistance_distance_10d", "rsi5", "range_position_20d", "ma10d_slope", "volatility_10d"]:
                            print(f"  {f}: {data.get(f, 'N/A')}")
    except Exception as e:
        db.conn.rollback()
