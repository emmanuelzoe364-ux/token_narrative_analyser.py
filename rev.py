"""
Token Project Health Scorer
---------------------------
Paste a contract address → get our Project Health Scorecard
(Delivery / Schedule / External Traction / Transparency / Sustainability)

Special handling for 1F916. Designed to be extended to more projects.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Token Project Health Scorer",
    page_icon="📊",
    layout="wide",
)

st.title("Token Project Health Scorer")
st.caption(
    "Paste contract → evaluate underlying project health using roadmap delivery, "
    "schedule performance, external traction, transparency and sustainability."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_float(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_usd(x) -> str:
    v = to_float(x)
    if v is None or v <= 0:
        return "N/A"
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.2f}"


def same_address(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    return a.strip().lower() == b.strip().lower()


# ---------------------------------------------------------------------------
# DexScreener
# ---------------------------------------------------------------------------

@st.cache_data(ttl=90, show_spinner=False)
def fetch_token(contract: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
    except Exception as e:
        return None, f"DexScreener error: {e}"

    if not pairs:
        return None, "No pairs found for this contract"

    # Prefer pairs where this address is the base token
    base_pairs = [
        p for p in pairs
        if same_address((p.get("baseToken") or {}).get("address"), contract)
    ]
    if not base_pairs:
        base_pairs = pairs

    pair = max(base_pairs, key=lambda p: to_float((p.get("liquidity") or {}).get("usd")) or 0)

    base = pair.get("baseToken") or {}
    info = pair.get("info") or {}
    websites = info.get("websites") or []
    socials = info.get("socials") or []

    website = websites[0].get("url") if websites else None
    twitter = None
    for s in socials:
        u = (s.get("url") or "").lower()
        if "x.com" in u or "twitter.com" in u:
            twitter = s.get("url")
            break

    created_ms = to_float(pair.get("pairCreatedAt"))
    age_days = None
    if created_ms and created_ms > 0:
        age_days = (time.time() * 1000 - created_ms) / 86_400_000

    return {
        "symbol": base.get("symbol"),
        "name": base.get("name"),
        "address": base.get("address") or contract,
        "chain": pair.get("chainId"),
        "price_usd": pair.get("priceUsd"),
        "liquidity_usd": to_float((pair.get("liquidity") or {}).get("usd")),
        "volume_24h": to_float((pair.get("volume") or {}).get("h24")),
        "market_cap": to_float(pair.get("marketCap")),
        "fdv": to_float(pair.get("fdv")),
        "website": website,
        "twitter": twitter,
        "dex_url": pair.get("url"),
        "age_days": age_days,
    }, None


# ---------------------------------------------------------------------------
# Known project profiles (starting with 1F916)
# ---------------------------------------------------------------------------

KNOWN_PROJECTS = {
    # Base contract for 1F916
    "0x9e00fc92493451eba1c63dd3880d68b622037ba3": {
        "
... 
