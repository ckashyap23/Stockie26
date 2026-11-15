"""Deterministic strategy settings; tests never load the developer's parent .env."""
import os

os.environ.setdefault("NIFTY_TARGET_PCT", "0.006")
os.environ.setdefault("TARGET_PCT_EFFECTIVE", "0.006")
os.environ.setdefault("SL_PCT_EFFECTIVE", "0.004")
