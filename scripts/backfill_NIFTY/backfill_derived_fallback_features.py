"""Add and backfill derived strategy-support features in SignalFeatureDaily."""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.common.config import get_settings
from src.data_manager.db.client_factory import get_database_client


def main() -> None:
    db = get_database_client(get_settings())
    db.connect()
    try:
        with db.conn.cursor() as cur:
            migration = ROOT / "src/data_manager/db/migrations/020_add_derived_fallback_features.sql"
            cur.execute(migration.read_text(encoding="utf-8"))
            cur.execute('''
                SELECT COUNT(*),
                       COUNT(volume_hybrid),
                       COUNT(ma_slope_combo),
                       COUNT(resistance_distance_10d)
                FROM "SignalFeatureDaily"
                WHERE UPPER(symbol) = 'NIFTY'
            ''')
            total, volume_count, slope_count, resistance_count = cur.fetchone()
            cur.execute('''
                SELECT COUNT(*), COUNT(volume_hybrid), COUNT(ma_slope_combo),
                       COUNT(resistance_distance_10d)
                FROM "SignalFeatureDaily"
                WHERE UPPER(symbol) = 'NIFTY' AND signal_date >= DATE '2025-01-01'
            ''')
            production_counts = cur.fetchone()
        db.conn.commit()
    finally:
        db.close()
    print(
        f"NIFTY rows={total}; volume_hybrid={volume_count}; "
        f"ma_slope_combo={slope_count}; resistance_distance_10d={resistance_count}"
    )
    print(
        "NIFTY since 2025-01-01 rows="
        f"{production_counts[0]}; volume_hybrid={production_counts[1]}; "
        f"ma_slope_combo={production_counts[2]}; "
        f"resistance_distance_10d={production_counts[3]}"
    )


if __name__ == "__main__":
    main()
