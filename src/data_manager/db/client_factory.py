from __future__ import annotations

from src.common.config import Settings
from src.data_manager.db.database_client import DatabaseClient
from src.data_manager.db.supabase_client import SupabaseDatabaseClient


def get_database_client(settings: Settings):
    provider = (settings.database_provider or "").lower()
    # Prefer Supabase whenever a connection string is available,
    # unless Azure SQL is explicitly requested.
    if provider == "azure_sql":
        return DatabaseClient(settings)
    if settings.supabase_conn_str:
        return SupabaseDatabaseClient(settings)
    if provider == "supabase":
        raise RuntimeError("DATABASE_PROVIDER=supabase but SUPABASE_CONN_STR is not set.")
    return DatabaseClient(settings)
