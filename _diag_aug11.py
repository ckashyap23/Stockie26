"""Diagnose why no strategy fired on 2026-08-11 (trade date 2026-08-12)."""
from src.common.config import get_settings
from src.data_manager.db.supabase_client import SupabaseDatabaseClient

settings = get_settings()
db = SupabaseDatabaseClient(settings)
db.connect()

# Fields we care about for strategy diagnosis
KEY_FIELDS = [
    "signal_date", "symbol", "regime",
    "final_prediction", "effective_prediction",
    "drift_effective_prediction", "drift_overrule_reason",
    "event_gate_reason",
    "strategy_family_audit",
    # feature fields
    "vix_close", "volatility_10d",
    "range_position_20d", "range_position_10d",
    "ma20_slope", "ma10d_slope", "ma5d_slope",
    "rsi5", "rsi14",
    "bb_width",
    "resistance_distance_10d", "support_broken_10d",
    "ret_3d", "atr14",
    "nifty_drift_pct", "nifty_gap_pct",
    "watch_signal", "watch_seeded_date",
]

sql = 'SELECT * FROM "NiftyPrediction" WHERE signal_date = %s'
with db.conn.cursor() as cur:
    cur.execute(sql, ("2026-08-11",))
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    if not row:
        print("NO ROW FOUND for 2026-08-11")
    else:
        data = dict(zip(cols, row))
        print("=== NiftyPrediction for 2026-08-11 ===")
        for f in KEY_FIELDS:
            if f in data:
                print(f"  {f}: {data[f]}")
        print("\n=== All other fields ===")
        for k, v in data.items():
            if k not in KEY_FIELDS:
                print(f"  {k}: {v}")

# Check NiftyOptionSelection for trade_date 2026-08-12
sql2 = 'SELECT trade_date, prediction_direction, no_trade_reason, strategy_direction, selected_strategy FROM "NiftyOptionSelection" WHERE trade_date = %s'
with db.conn.cursor() as cur:
    cur.execute(sql2, ("2026-08-12",))
    cols2 = [d[0] for d in cur.description]
    rows2 = cur.fetchall()
    print(f"\n=== NiftyOptionSelection for trade_date=2026-08-12: {len(rows2)} rows ===")
    for r in rows2:
        print(dict(zip(cols2, r)))

# Check PaperExecutionSignal for 2026-08-12
sql3 = 'SELECT signal_trade_date, paper_trade_date, direction, status, source_final_prediction, drift_position_size_pct FROM "PaperExecutionSignal" WHERE paper_trade_date = %s OR signal_trade_date = %s'
with db.conn.cursor() as cur:
    cur.execute(sql3, ("2026-08-12", "2026-08-11"))
    cols3 = [d[0] for d in cur.description]
    rows3 = cur.fetchall()
    print(f"\n=== PaperExecutionSignal for 2026-08-12: {len(rows3)} rows ===")
    for r in rows3:
        print(dict(zip(cols3, r)))
