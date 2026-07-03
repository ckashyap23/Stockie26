import time
from pathlib import Path
from typing import Any, List, Iterable, Dict

from kiteconnect import KiteConnect

from src.common.config import Settings

def _chunked(seq: Iterable[str], size: int) -> Iterable[List[str]]:
    items = list(seq)
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _clean_token(token: str) -> str:
    """Clean token by removing whitespace and newlines."""
    return str(token).strip().replace('\n', '').replace('\r', '').replace(' ', '')


def _get_token_path(settings) -> Path:
    """Resolve token file path to absolute path."""
    token_path = settings.kite_access_token_path
    return token_path.resolve() if token_path.is_absolute() else Path.cwd() / token_path


class KiteClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not self.settings.kite_api_key:
            raise RuntimeError("KITE_API_KEY is missing in environment/.env")

        self.kite = KiteConnect(api_key=self.settings.kite_api_key)

    def authenticate(self) -> None:
        """Set the access token from the configured database, with file fallback."""
        access_token = self._load_token()
        if not access_token:
            token_path = _get_token_path(self.settings)
            raise RuntimeError(
                f"Access token not found in database or file ({token_path}).\n"
                "Run: python scripts/daily_NIFTY/daily_get_kite_access_token.py"
            )
        self.kite.set_access_token(access_token)

    def re_authenticate(self) -> bool:
        """Reload access token from file or database. Returns True if successful."""
        try:
            time.sleep(0.5)  # Wait for file write to complete
            access_token = self._load_token()
            if access_token and len(access_token) >= 10:
                self.kite.set_access_token(access_token)
                time.sleep(0.1)
                return True
            return False
        except Exception:
            return False

    def _load_token(self) -> str | None:
        """Load token from the configured database first, then local file cache."""
        token_path = _get_token_path(self.settings)

        # Try database first. With DATABASE_PROVIDER=supabase or SUPABASE_CONN_STR
        # configured, client_factory returns SupabaseDatabaseClient.
        try:
            from src.data_manager.db.client_factory import get_database_client

            db_client = get_database_client(self.settings)
            db_client.connect()
            try:
                db_token = db_client.get_kite_access_token()
            finally:
                db_client.close()

            access_token = _clean_token(str(db_token or ""))
            if access_token and len(access_token) >= 10:
                try:
                    token_path.parent.mkdir(parents=True, exist_ok=True)
                    token_path.write_text(access_token, encoding="utf-8")
                except Exception:
                    pass
                return access_token
        except Exception:
            pass

        if token_path.exists():
            try:
                file_token = _clean_token(token_path.read_text(encoding="utf-8"))
                if file_token and len(file_token) >= 10:
                    return file_token
            except Exception:
                pass

        return None

    def fetch_instruments_nfo(self) -> List[dict[str, Any]]:
        """Return full instruments dump for NFO (F&O segment)."""
        return self.kite.instruments("NFO")

    def fetch_instruments_equity_indices(self) -> List[dict[str, Any]]:
        """
        Return full instruments dump for NSE and BSE equities and indices.
        Combines data from both exchanges.
        """
        nse_instruments = self.kite.instruments("NSE")
        bse_instruments = self.kite.instruments("BSE")
        return nse_instruments + bse_instruments

# ------------ live market data helpers ------------

    def fetch_ltp_bulk(self, symbols: List[str]) -> Dict[str, Any]:
        """
        Wrapper over kite.ltp for a list of symbols like 'NSE:NIFTY 50',
        'NFO:NIFTY25D0926000CE', etc.
        """
        if not symbols:
            return {}

        result: Dict[str, Any] = {}
        # ltp can handle quite a few symbols, but we still chunk + throttle a bit
        for chunk in _chunked(symbols, 400):
            resp = self.kite.ltp(chunk)
            result.update(resp)
            time.sleep(0.25)
        return result

    def fetch_quote_bulk(self, symbols: List[str]) -> Dict[str, Any]:
        """
        Wrapper over kite.quote for a list of symbols like 'NFO:NIFTY25D0926000CE'.

        Kite docs: max 500 instruments per quote call; we respect that and
        add a small sleep to stay inside rate limits.
        """
        import logging
        logger = logging.getLogger(__name__)

        if not symbols:
            return {}

        result: Dict[str, Any] = {}
        chunks = list(_chunked(symbols, 500))
        total_chunks = len(chunks)
        logger.info(f"Fetching quotes for {len(symbols)} symbols in {total_chunks} chunks...")

        for idx, chunk in enumerate(chunks, 1):
            logger.info(f"Fetching chunk {idx}/{total_chunks} ({len(chunk)} symbols)...")
            try:
                resp = self.kite.quote(chunk)
                result.update(resp)
                if idx < total_chunks:  # Don't sleep after last chunk
                    time.sleep(0.35)
            except Exception as e:
                logger.error(f"Error fetching chunk {idx}: {e}")
                # Continue with other chunks even if one fails
                continue

        logger.info(f"Fetched quotes for {len(result)} symbols")
        return result

    def calculate_order_charges(self, orders: List[dict[str, Any]]) -> List[dict[str, Any]]:
        """Return Kite virtual contract-note charges for executed or imaginary orders."""
        if not orders:
            return []
        response = self.kite.get_virtual_contract_note(orders)
        return list(response or [])
