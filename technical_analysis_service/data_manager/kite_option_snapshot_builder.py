# src/data_manager/kite_option_snapshot_builder.py
import math
from datetime import date, datetime
from typing import Iterable, List, Set, Dict, Literal

from technical_analysis_service.common.models import OptionInstrument, OptionData
from technical_analysis_service.data_manager.kite_client import KiteClient


def _to_date(value) -> date:
    """
    Convert a value to a date object.
    Accepts date, datetime, or 'YYYY-MM-DD' string.
    Falls back to today's date on failure.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass
    return date.today()


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# Common alias mapping so that user-facing underlyings like
# "NIFTY50", "NIFTY 50", "BANK NIFTY" etc. resolve to a canonical form.
_UNDERLYING_ALIASES = {
    "NIFTY": "NIFTY",
    "NIFTY50": "NIFTY",
    "NIFTY 50": "NIFTY",

    "BANKNIFTY": "BANKNIFTY",
    "BANK NIFTY": "BANKNIFTY",
    "NIFTY BANK": "BANKNIFTY",

    "FINNIFTY": "FINNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
}


def _normalize_underlying(value: str) -> str:
    """
    Normalize an underlying name/symbol for comparison.

    - Uppercases
    - Trims spaces
    - Applies alias mapping (NIFTY50 -> NIFTY, BANK NIFTY -> BANKNIFTY, etc.)
    - Returns a compact, canonical token.
    """
    raw = (value or "").strip().upper()
    if not raw:
        return ""

    # Direct alias match (with spaces)
    if raw in _UNDERLYING_ALIASES:
        return _UNDERLYING_ALIASES[raw]

    # Try again after removing spaces
    collapsed = raw.replace(" ", "")
    if collapsed in _UNDERLYING_ALIASES:
        return _UNDERLYING_ALIASES[collapsed]

    return collapsed


def _extract_underlying_candidates(inst: dict) -> Set[str]:
    """
    Extract possible underlying identifiers from an instrument:
    - Normalized 'name' field
    - Normalized alphabetic prefix of 'tradingsymbol'
      (e.g. NIFTY25DEC24000CE -> NIFTY)
    """
    candidates: Set[str] = set()

    # 1) From 'name'
    name = inst.get("name")
    if isinstance(name, str) and name.strip():
        norm_name = _normalize_underlying(name)
        if norm_name:
            candidates.add(norm_name)

    # 2) From 'tradingsymbol' prefix (until first digit)
    tradingsymbol = inst.get("tradingsymbol")
    if isinstance(tradingsymbol, str) and tradingsymbol:
        prefix_chars = []
        for ch in tradingsymbol:
            if ch.isdigit():
                break
            prefix_chars.append(ch)
        if prefix_chars:
            prefix = "".join(prefix_chars)
            norm_prefix = _normalize_underlying(prefix)
            if norm_prefix:
                candidates.add(norm_prefix)

    return candidates


def filter_options_for_underlyings(
    instruments_dump: Iterable[dict],
    underlyings: Iterable[str],
) -> List[OptionInstrument]:
    """
    From the full NFO instruments dump, keep only options (CE/PE)
    for specific underlyings, and map to OptionInstrument model.

    - Underlyings are normalized (handles NIFTY50/NIFTY 50 -> NIFTY, etc.)
    - Matching is done against both 'name' and the 'tradingsymbol' prefix.
    """
    normalized_underlyings = {
        _normalize_underlying(u)
        for u in underlyings
        if isinstance(u, str) and u.strip()
    }

    # If nothing valid was passed, return early
    if not normalized_underlyings:
        return []

    results: List[OptionInstrument] = []

    for inst in instruments_dump:
        # Only NFO options
        if inst.get("exchange") != "NFO":
            continue

        instrument_type = inst.get("instrument_type")
        if instrument_type not in ("CE", "PE"):
            continue

        # Get possible underlying candidates for this instrument
        inst_underlyings = _extract_underlying_candidates(inst)
        if not inst_underlyings:
            continue

        # Intersection with requested underlyings
        common = inst_underlyings & normalized_underlyings
        if not common:
            continue

        # Pick one canonical underlying (any from the intersection)
        matched_underlying = next(iter(common))

        expiry_date = _to_date(inst.get("expiry"))
        fetch_date = date.today()  # Date when this instrument data was fetched

        option = OptionInstrument(
            fetch_date=fetch_date,
            instrument_token=_to_int(inst.get("instrument_token")),
            underlying=matched_underlying,
            exchange=inst.get("exchange", "") or "",
            tradingsymbol=inst.get("tradingsymbol", "") or "",
            name=inst.get("name"),
            strike=_to_float(inst.get("strike")),
            expiry=expiry_date,
            instrument_type=instrument_type,
            lot_size=_to_int(inst.get("lot_size")),
            tick_size=(
                _to_float(inst.get("tick_size"))
                if inst.get("tick_size") is not None
                else None
            ),
            segment=inst.get("segment"),
        )
        results.append(option)

    return results

# --------------------------------------------------------
# NEW: IV + Greeks helpers (Black-Scholes)
# --------------------------------------------------------

OptionType = Literal["C", "P"]


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x)


def _bs_price(
    S: float, K: float, T: float, r: float, q: float, sigma: float, opt_type: OptionType
) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        # fallback to discounted intrinsic value
        if opt_type == "C":
            return max(0.0, S * math.exp(-q * T) - K * math.exp(-r * T))
        else:
            return max(0.0, K * math.exp(-r * T) - S * math.exp(-q * T))

    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if opt_type == "C":
        return S * math.exp(-q * T) * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * math.exp(-q * T) * _norm_cdf(-d1)


def _bs_greeks(
    S: float, K: float, T: float, r: float, q: float, sigma: float, opt_type: OptionType
) -> Dict[str, float]:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None}

    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    Nd1 = _norm_cdf(d1)
    pdf_d1 = _norm_pdf(d1)
    disc_q = math.exp(-q * T)
    disc_r = math.exp(-r * T)

    if opt_type == "C":
        delta = disc_q * Nd1
        theta = (
            - (S * disc_q * pdf_d1 * sigma) / (2 * math.sqrt(T))
            - r * K * disc_r * _norm_cdf(d2)
            + q * S * disc_q * Nd1
        )
    else:
        delta = disc_q * (Nd1 - 1.0)
        theta = (
            - (S * disc_q * pdf_d1 * sigma) / (2 * math.sqrt(T))
            + r * K * disc_r * _norm_cdf(-d2)
            - q * S * disc_q * _norm_cdf(-d1)
        )

    gamma = disc_q * pdf_d1 / (S * sigma * math.sqrt(T))
    vega = S * disc_q * pdf_d1 * math.sqrt(T)

    return {
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
    }


def _implied_volatility(
    price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    opt_type: OptionType,
    tol: float = 1e-4,
    max_iter: int = 100,
) -> float | None:
    """
    Simple bisection-based IV solver. Returns None if it can't converge.
    """
    if price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None

    low, high = 1e-4, 5.0
    for _ in range(max_iter):
        mid = 0.5 * (low + high)
        mid_price = _bs_price(S, K, T, r, q, mid, opt_type)
        diff = mid_price - price
        if abs(diff) < tol:
            return mid
        if diff > 0:
            high = mid
        else:
            low = mid
    return None


def _years_to_expiry(expiry: date, as_of: datetime) -> float:
    if isinstance(expiry, datetime):
        expiry_date = expiry.date()
    elif isinstance(expiry, date):
        expiry_date = expiry
    elif isinstance(expiry, str):
        # Try ISO first, then fallback to simple YYYY-MM-DD
        try:
            expiry_date = datetime.fromisoformat(expiry).date()
        except ValueError:
            expiry_date = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
    else:
        raise TypeError(f"Unsupported expiry type: {type(expiry)}")

    days = (expiry_date - as_of.date()).days
    return max(days, 0) / 365.0


# Mapping from canonical underlying -> NSE spot symbol
INDEX_SPOT_SYMBOL: Dict[str, str] = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
}

# --------------------------------------------------------
# NEW: build OptionData (raw + calculated) from Kite
# --------------------------------------------------------

def build_option_data_snapshot(
    kite_client: KiteClient,
    option_instruments: List[OptionInstrument],
    risk_free_rate: float = 0.07,
) -> List[OptionData]:
    """
    Fetch a live snapshot for the given option instruments:

      - Uses KiteClient.fetch_ltp_bulk() for underlying spot prices
      - Uses KiteClient.fetch_quote_bulk() for option quotes (last, depth, OI, volume)
      - Computes IV + Greeks via Black-Scholes (non-dividend index / stock)

    Returns a List[OptionData] ready to pass into DbClient.bulk_insert_option_data().

    NOTE: Here we use OptionData.option_instrument_id = instrument_token.
    In SQL, make sure OptionSnapshot.option_instrument_id is aligned
    (either referencing OptionInstrument.instrument_token, or you can
    change this to use your DB id instead).
    """
    if not option_instruments:
        return []

    now = datetime.utcnow()

    # 1) Underlying spot prices
    underlyings = sorted({inst.underlying for inst in option_instruments})
    underlying_symbols: List[str] = []
    for u in underlyings:
        sym = INDEX_SPOT_SYMBOL.get(u, f"NSE:{u}")
        underlying_symbols.append(sym)

    ltp_resp = kite_client.fetch_ltp_bulk(underlying_symbols)
    underlying_spot: Dict[str, float | None] = {}
    for u in underlyings:
        sym = INDEX_SPOT_SYMBOL.get(u, f"NSE:{u}")
        data = ltp_resp.get(sym)
        if data and isinstance(data, dict) and "last_price" in data:
            try:
                underlying_spot[u] = float(data["last_price"])
            except (ValueError, TypeError):
                underlying_spot[u] = None
        else:
            underlying_spot[u] = None

    # 2) Quotes for all options
    option_symbols = [f"{inst.exchange}:{inst.tradingsymbol}" for inst in option_instruments]
    quotes = kite_client.fetch_quote_bulk(option_symbols)

    # 3) Build OptionData list
    results: List[OptionData] = []

    for inst in option_instruments:
        key = f"{inst.exchange}:{inst.tradingsymbol}"
        q = quotes.get(key)
        if not q:
            continue

        spot = underlying_spot.get(inst.underlying)
        last_price = q.get("last_price")

        # Extract depth data - Kite Connect returns depth as a dict with 'buy' and 'sell' arrays
        depth = q.get("depth")
        bid_price = bid_qty = ask_price = ask_qty = None

        if depth and isinstance(depth, dict):
            buy_depth = depth.get("buy")
            sell_depth = depth.get("sell")

            # Extract bid (buy side - highest bid)
            if buy_depth and isinstance(buy_depth, list) and len(buy_depth) > 0:
                first_buy = buy_depth[0]
                if isinstance(first_buy, dict):
                    bid_price = first_buy.get("price")
                    bid_qty = first_buy.get("quantity")
                    # Convert to proper types, but keep 0 as 0 (valid value from Kite)
                    if bid_price is not None:
                        try:
                            bid_price = float(bid_price)
                        except (ValueError, TypeError):
                            bid_price = None
                    if bid_qty is not None:
                        try:
                            bid_qty = int(bid_qty)
                        except (ValueError, TypeError):
                            bid_qty = None

            # Extract ask (sell side - lowest ask)
            if sell_depth and isinstance(sell_depth, list) and len(sell_depth) > 0:
                first_sell = sell_depth[0]
                if isinstance(first_sell, dict):
                    ask_price = first_sell.get("price")
                    ask_qty = first_sell.get("quantity")
                    # Convert to proper types, but keep 0 as 0 (valid value from Kite)
                    if ask_price is not None:
                        try:
                            ask_price = float(ask_price)
                        except (ValueError, TypeError):
                            ask_price = None
                    if ask_qty is not None:
                        try:
                            ask_qty = int(ask_qty)
                        except (ValueError, TypeError):
                            ask_qty = None

        # Extract volume and open interest
        volume = q.get("volume")
        oi = q.get("oi")

        # Convert to proper types, but keep 0 as 0 (valid value from Kite)
        if volume is not None:
            try:
                volume = int(volume)
            except (ValueError, TypeError):
                volume = None
        if oi is not None:
            try:
                oi = int(oi)
            except (ValueError, TypeError):
                oi = None

        # Debug logging for first few instruments to see what we're getting
        import logging
        logger = logging.getLogger(__name__)
        if len(results) < 3:  # Log first 3 for debugging
            logger.info(
                f"Quote data for {key}: "
                f"last_price={last_price}, bid={bid_price}@{bid_qty}, "
                f"ask={ask_price}@{ask_qty}, volume={volume}, oi={oi}, "
                f"depth_present={depth is not None}, "
                f"buy_depth_len={len(buy_depth) if buy_depth and isinstance(buy_depth, list) else 0}, "
                f"sell_depth_len={len(sell_depth) if sell_depth and isinstance(sell_depth, list) else 0}"
            )

        # IV + Greeks
        iv = delta = gamma = theta = vega = None
        if spot and last_price and inst.expiry:
            T = _years_to_expiry(inst.expiry, now)
            if T > 0:
                opt_type: OptionType = "C" if inst.instrument_type == "CE" else "P"
                iv_val = _implied_volatility(
                    price=float(last_price),
                    S=float(spot),
                    K=float(inst.strike),
                    T=T,
                    r=risk_free_rate,
                    q=0.0,  # assume no dividend; fine for indices
                    opt_type=opt_type,
                )
                if iv_val:
                    g = _bs_greeks(
                        S=float(spot),
                        K=float(inst.strike),
                        T=T,
                        r=risk_free_rate,
                        q=0.0,
                        sigma=iv_val,
                        opt_type=opt_type,
                    )
                    iv = iv_val
                    delta = g["delta"]
                    gamma = g["gamma"]
                    theta = g["theta"]
                    vega = g["vega"]

        # IMPORTANT: option_instrument_id -> using instrument_token here.
        od = OptionData(
            option_instrument_id=inst.instrument_token,
            snapshot_time=now,
            underlying_price=spot,
            last_price=last_price,
            bid_price=bid_price,
            bid_qty=bid_qty,
            ask_price=ask_price,
            ask_qty=ask_qty,
            volume=volume,
            open_interest=oi,
            implied_volatility=iv,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
        )
        results.append(od)

    return results
