"""Compute actual_trade_label from build_base() and compare to DB."""
import sys, os
os.chdir(r'c:\Users\ckashyap\OneDrive - Microsoft\Github_Copilot\Stockie26')
sys.path.insert(0, os.getcwd())
from dotenv import load_dotenv; load_dotenv('.env')

from src.technical_analysis.cascade.dataset import build_base
import pandas as pd

print("Loading build_base()...")
df = build_base()
df = df[df["next_open"].notna()].copy()

atl_dist = df["actual_trade_label"].value_counts(dropna=False)
print(f"\nactual_trade_label from build_base() (in-memory, {len(df)} resolved rows):")
for val, cnt in atl_dist.items():
    print(f"  {repr(val)}: {cnt}")

# Check a specific date
sample = df[df["actual_trade_label"] == "NO_POSITION"].copy()
sample["up"] = (sample["future_high_nd"] - sample["next_open"]) / sample["next_open"] * 100
sample["dn"] = (sample["next_open"] - sample["future_low_nd"]) / sample["next_open"] * 100
# Show NO_POSITION days that had big moves
big_moves = sample[(sample["up"] > 0.4) | (sample["dn"] > 0.4)].sort_values("signal_date")
print(f"\nNO_POSITION days with >0.4% move (should be CALL/PUT/BOTH) - {len(big_moves)} rows:")
print(big_moves[["signal_date","regime","actual_trade_label","up","dn","future_high_nd","future_low_nd","next_open"]].head(5).to_string(index=False))

# Also show the threshold for a few rows
df["atr_threshold_pct"] = (0.55 * pd.to_numeric(df["atr14"], errors="coerce") / pd.to_numeric(df["close_1515"], errors="coerce")).clip(0.004, 0.012) * 100
print(f"\nATR threshold stats:")
print(f"  mean threshold: {df['atr_threshold_pct'].mean():.3f}%")
print(f"  min: {df['atr_threshold_pct'].min():.3f}%  max: {df['atr_threshold_pct'].max():.3f}%")
