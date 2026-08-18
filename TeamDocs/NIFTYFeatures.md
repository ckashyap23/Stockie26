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
- Open-gap and drift-probe inputs from morning 5-minute candles.

## Usage Contract

- Signal rows must only use information available at signal time.
- Future label and quality fields are for grading only.
- Feature rebuilds should be done before prediction/backtest reruns.
- Strategy code should consume named feature columns rather than recomputing
  ad hoc indicators in multiple places.
- Features are common across all dates; there is no regime-specific feature
  branch in production.
