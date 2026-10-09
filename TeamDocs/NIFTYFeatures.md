# NIFTY Features

`SignalFeatureDaily` is the production feature store for NIFTY. It is built from
underlying OHLC/volume inputs plus derived technical indicators and support /
resistance context.

## Feature Groups

- Price and return history.
- Moving-average slope and trend context.
- RSI and Bollinger context.
- Volume and volatility context.
- Support/resistance and range-position fields.
- Global-index audit context joined during prediction.
- Point-in-time macro context: Brent crude, US 10Y yield, USD/INR, and
  provisional FII/DII cash flows.
- Open-gap and drift-probe inputs from morning 5-minute candles.

## Macro Timing and Calculations

`daily_macro_refresh.py` runs at 9:00 AM IST before the D+1 prediction. It stores
the completed D values against signal date D:

- Brent (`BZ=F`), US 10Y (`^TNX`), and USD/INR (`INR=X`) use Yahoo daily closes.
- FII/DII uses NSE provisional cash activity. The live endpoint covers current
  publication data; historical backfills accept an official NSE archive CSV.
- FII index-futures positioning uses NSE participant-wise open-interest archive
  files published after each session. `fii_idx_long_ratio` is FII index-futures
  long contracts divided by long plus short contracts.

Five-session inputs use each source's own non-missing session history. US 10Y
changes are basis-point differences; crude and USD/INR are decimal returns.
FII cash flow is a five-session rupee-crore sum.
`fii_idx_long_ratio_chg5` is the five-session difference in the FII
index-futures long ratio. `fii_idx_5d_z` robust-normalizes that change against
the prior `MACRO_TRAILING_WINDOW` observations using median and
`1.4826 * MAD`, clips it to +/-3, and negates it so new FII short positioning
is positive risk-off. The one-session shift excludes the current observation
from its own normalization history.

Crude and USD/INR five-session changes use the same shifted median/MAD robust
normalization and +/-3 clipping. US 10Y retains its trailing mean/std z-score.
`macro_risk_off` is the weighted sum of crude (0.25), US 10Y (0.50), and
USD/INR (0.25). Missing inputs are dropped and the surviving weights are
renormalized. `fii_idx_5d_z`, `fii_net_1d_cr`, `fii_net_5d_cr`, DII cash
values, and `fii_net_5d_z` remain research/diagnostic columns but do not enter
the composite.

## Usage Contract

- Signal rows must only use information available at signal time.
- Future label and quality fields are for grading only.
- Macro values for D must not be written before they are observable prior to
  the D+1 NSE open.
- Feature rebuilds should be done before prediction/backtest reruns.
- Strategy code should consume named feature columns rather than recomputing
  ad hoc indicators in multiple places.
- Features are common across all dates; there is no regime-specific feature
  branch in production.
