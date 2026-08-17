"""Get drift and gap values for Aug 11 from GiftNiftySnapshot and UnderlyingCandle5m."""
from src.common.config import get_settings
from src.data_manager.db.supabase_client import SupabaseDatabaseClient

settings = get_settings()
db = SupabaseDatabaseClient(settings)
db.connect()

# Get SignalFeatureDaily full row for Aug 11
with db.conn.cursor() as cur:
    cur.execute('SELECT * FROM "SignalFeatureDaily" WHERE signal_date = %s', ("2026-08-11",))
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    if row:
        data = dict(zip(cols, row))
        print("=== SignalFeatureDaily 2026-08-11 ===")
        for k, v in sorted(data.items()):
            print(f"  {k}: {v}")
    else:
        print("NO ROW in SignalFeatureDaily for 2026-08-11")
