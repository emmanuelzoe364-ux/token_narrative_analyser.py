"""
Token Narrative Analyzer — upgraded
Safety gate → separate scores (Safety / Market / Narrative / Community) → veto on red flags
LLM-style structured narrative extraction · logging · batch mode
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Token Narrative Analyzer",
    page_icon="🧭",
    layout="wide",
)

st.title("Token Narrative Analyzer")
st.caption(
    "Paste contract(s) → safety gate first → separate scores (Safety / Market / Narrative / Community) "
    "→ red-flag veto → official + community narrative + on-chain metrics. Logs every run for forward-return validation."
)

# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "analysis_log.db"
ENTRY_THRESHOLDS = {
    "min_liquidity_usd": 50_000,
    "min_volume_24h_usd": 100_000,
    "max_fdv_usd": 50_000_000,
    "min_vol_liq": 0.5,
    "max_fdv_liq": 20.0,
    "min_liq_mcap": 0.05,
}

# ---------------------------------------------------------------------------
# Helpers: numeric & formatting
# ---------------------------------------------------------------------------

def to_float(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_price_usd(x) -> str:
    v = to_float(x)
    if v is None or v <= 0:
        return "N/A"
    if v >= 1:
        return f"${v:.4f}"
    if v >= 0.01:
        return f"${v:.5f}"
    return f"${v:.8f}"


def fmt_usd_compact(x) -> str:
    v = to_float(x)
    if v is None or v <= 0:
        return "N/A"
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.2f}K"
    if v >= 1:
        return f"${v:.2f}"
    return f"${v:.6f}"


def fmt_pct_change(x) -> str:
    v = to_float(x)
    return "N/A" if v is None else f"{v:+.1f}%"


def interpret_vol_liq(v: Optional[float]) -> str:
    val = to_float(v)
    if val is None:
        return "N/A"
    if val >= 5:
        return f"{val:.1f}x/day (very high turnover)"
    if val >= 2:
        return f"{val:.1f}x/day (high turnover)"
    if val >= 1:
        return f"{val:.1f}x/day (normal turnover)"
    if val >= 0.5:
        return f"{val:.2f}x/day (low turnover)"
    return f"{val:.2f}x/day (very low turnover)"


def interpret_vol_mcap(v: Optional[float]) -> str:
    val = to_float(v)
    if val is None:
        return "N/A"
    pct = val * 100
    if pct >= 100:
        return f"{pct:.0f}% of MCap/day (extreme activity)"
    if pct >= 50:
        return f"{pct:.0f}% of MCap/day (very high activity)"
    if pct >= 20:
        return f"{pct:.0f}% of MCap/day (high activity)"
    if pct >= 5:
        return f"{pct:.1f}% of MCap/day (normal activity)"
    return f"{pct:.1f}% of MCap/day (low activity)"


def interpret_liq_mcap(v: Optional[float]) -> str:
    val = to_float(v)
    if val is None:
        return "N/A"
    pct = val * 100
    if pct >= 50:
        return f"{pct:.0f}% of MCap in liq (very strong)"
    if pct >= 20:
        return f"{pct:.0f}% of MCap in liq (strong)"
    if pct >= 10:
        return f"{pct:.0f}% of MCap in liq (decent)"
    if pct >= 5:
        return f"{pct:.1f}% of MCap in liq (low)"
    return f"{pct:.1f}% of MCap in liq (very low)"


def interpret_fdv_liq(v: Optional[float]) -> str:
    val = to_float(v)
    if val is None:
        return "N/A"
    if val >= 20:
        return f"{val:.1f}x (very high unlock risk)"
    if val >= 10:
        return f"{val:.1f}x (h
... 
