import yfinance as yf
import pandas as pd



def _normalize_yfinance_columns(df: pd.DataFrame) -> pd.DataFrame:


    
    """Flatten yfinance column headers to a single level."""
    if not isinstance(df.columns, pd.MultiIndex):
        return df

    flattened_columns = []
    for column in df.columns.to_flat_index():
        parts = [str(part) for part in column if part not in (None, "", " ")]
        flattened_columns.append(parts[0] if parts else "")

    normalized_df = df.copy()
    normalized_df.columns = flattened_columns
    return normalized_df


def get_index_ohlc_data(start_date: str, end_date: str) -> pd.DataFrame:
    """
    Returns OHLC data for all indexes in index_list.

    Parameters:
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        

    Returns:
        DataFrame with columns:
        date, index_name, symbol, open, high, low, close, volume
    """

    index_list = {
    "Nifty 50": "^NSEI",
    "Sensex": "^BSESN",
    "India VIX": "^INDIAVIX",
    "S&P 500": "^GSPC",
    "NASDAQ Composite": "^IXIC",
    "Dow Jones": "^DJI",
    "Russell 2000": "^RUT",
    "FTSE 100": "^FTSE",
    "DAX": "^GDAXI",
    "CAC 40": "^FCHI",
    "Hang Seng": "^HSI",
    "Nikkei 225": "^N225",
    "Shanghai Composite": "000001.SS",
    "KOSPI": "^KS11",
    "ASX 200": "^AXJO",
}


    all_data = []

    for index_name, symbol in index_list.items():
        print(f"Downloading {index_name} ({symbol})")

        try:
            df = yf.download(
                symbol,
                start=start_date,
                end=end_date,
                auto_adjust=False,
                progress=False,
            )

            if df.empty:
                print(f"No data found for {index_name} ({symbol})")
                continue

            df = _normalize_yfinance_columns(df)
            df = df.reset_index()
            df = _normalize_yfinance_columns(df)

            df["index_name"] = index_name
            df["symbol"] = symbol

            df = df.rename(
                columns={
                    "Date": "date",
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                }
            )

            df = df[
                [
                    "date",
                    "index_name",
                    "symbol",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                ]
            ]

            all_data.append(df)

        except Exception as e:
            print(f"Failed to download {index_name} ({symbol}): {e}")

    if not all_data:
        return pd.DataFrame()

    combined_df = pd.concat(all_data, ignore_index=True)
    combined_df["date"] = pd.to_datetime(combined_df["date"])
    combined_df = combined_df.sort_values(
        by=["date", "index_name", "symbol"],
        ignore_index=True,
    )
    combined_df["date"] = combined_df["date"].dt.strftime("%Y-%m-%d")

    

    # Save to CSV - enter the path to the file or comment out to not save

    output_path = "global_index_ohlc.csv"
    combined_df.to_csv(output_path, index=False)

    return combined_df

if __name__ == "__main__":
    start_date = "2026-06-19"
    end_date = "2026-06-24"

    ohlc_df = get_index_ohlc_data(
        start_date=start_date,
        end_date=end_date,
 
    )

    if ohlc_df.empty:
        print("No index data found.")
    else:
        print(ohlc_df.to_string(index=False))

        
        
