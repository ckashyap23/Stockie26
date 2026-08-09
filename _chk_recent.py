from dotenv import load_dotenv; load_dotenv('.env')
from src.common.config import get_settings
import psycopg2

settings = get_settings()
with psycopg2.connect(settings.supabase_conn_str) as conn:
    with conn.cursor() as cur:
        cur.execute(
            'SELECT signal_date, effective_prediction, drift_effective_prediction, drift_position_size_pct, actual_trade_label '
            'FROM "NiftyPrediction" '
            'WHERE symbol=%s AND model_version=%s AND signal_date >= %s '
            'ORDER BY signal_date',
            ('NIFTY', 'cascade_v1', '2026-07-18')
        )
        print("NiftyPrediction rows >= 2026-07-18:")
        for r in cur.fetchall():
            print(f"  {r[0]}  eff={r[1]:>12}  drift={str(r[2]):>12}  size={r[3]}  actual={r[4]}")

        # re-run the exact pipeline query
        cur.execute(
            'SELECT signal_date, effective_prediction, drift_effective_prediction '
            'FROM "NiftyPrediction" '
            'WHERE symbol=%s AND model_version=%s '
            "AND signal_date >= '2026-07-20' "
            "AND ((drift_effective_prediction IS NOT NULL AND drift_effective_prediction IN ('CALL','PUT')) "
            "     OR (drift_effective_prediction IS NULL AND effective_prediction IN ('CALL','PUT')))",
            ('NIFTY', 'cascade_v1')
        )
        rows = cur.fetchall()
        print(f"\nPipeline query for signal_date >= 2026-07-20: {len(rows)} rows")
        for r in rows:
            print(f"  {r}")
