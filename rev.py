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
        return f"{val:.1f}x (high unlock risk)"
    if val >= 5:
        return f"{val:.1f}x (elevated risk)"
    if val >= 2:
        return f"{val:.1f}x (moderate)"
    return f"{val:.1f}x (low risk)"


# ---------------------------------------------------------------------------
# Chain detection & address matching
# ---------------------------------------------------------------------------

def detect_chain(contract: str) -> str:
    c = contract.strip()
    if not c.startswith("0x") and 30 <= len(c) <= 48:
        if re.match(r"^[1-9A-HJ-NP-Za-km-z]+$", c):
            return "solana"
    if c.startswith("0x") and len(c) == 42:
        return "evm"
    return "unknown"


def same_address(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    a, b = a.strip(), b.strip()
    if a.startswith("0x") or b.startswith("0x"):
        return a.lower() == b.lower()
    return a == b


# ---------------------------------------------------------------------------
# DexScreener
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def fetch_token_from_dexscreener(contract: str, chain: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
    except Exception as e:
        return None, f"DexScreener request failed: {e}"

    if chain == "solana":
        pairs = [p for p in pairs if p.get("chainId") == "solana"]
    elif chain == "evm":
        pairs = [p for p in pairs if p.get("chainId") != "solana"]

    if not pairs:
        return None, "Token not found or no liquidity on DexScreener"

    base_pairs = [
        p for p in pairs
        if same_address((p.get("baseToken") or {}).get("address"), contract)
    ]
    if not base_pairs:
        return None, "This address only appears as the quote token in its pairs."

    pair = max(base_pairs, key=lambda p: to_float((p.get("liquidity") or {}).get("usd")) or 0.0)

    base = pair.get("baseToken") or {}
    info = pair.get("info") or {}
    websites = info.get("websites") or []
    socials = info.get("socials") or []

    website = websites[0].get("url") if websites else None
    twitter = None
    telegram = None
    for s in socials:
        t = (s.get("type") or "").lower()
        u = s.get("url") or ""
        if t == "twitter" or "x.com" in u or "twitter.com" in u:
            twitter = u
        if t == "telegram":
            telegram = u

    created_ms = to_float(pair.get("pairCreatedAt"))
    age_days = age_months = None
    if created_ms and created_ms > 0:
        age_days = (time.time() * 1000 - created_ms) / 86_400_000.0
        age_months = age_days / 30.44

    liq_usd = to_float((pair.get("liquidity") or {}).get("usd")) or 0.0
    vol_h24 = to_float((pair.get("volume") or {}).get("h24")) or 0.0
    mcap = to_float(pair.get("marketCap")) or 0.0
    fdv = to_float(pair.get("fdv")) or 0.0
    txns24 = (pair.get("txns") or {}).get("h24") or {}

    return {
        "symbol": base.get("symbol"),
        "name": base.get("name"),
        "address": base.get("address"),
        "chain": pair.get("chainId"),
        "price_usd": pair.get("priceUsd"),
        "price_change_24h": (pair.get("priceChange") or {}).get("h24"),
        "buys_24h": txns24.get("buys"),
        "sells_24h": txns24.get("sells"),
        "liquidity_usd": liq_usd,
        "volume_24h_usd": vol_h24,
        "market_cap_usd": mcap,
        "fdv_usd": fdv,
        "pair_created_ts": created_ms,
        "website": website,
        "twitter": twitter,
        "telegram": telegram,
        "dex_url": pair.get("url"),
        "pairs_found": len(base_pairs),
        "age_days": age_days,
        "age_months": age_months,
        "vol_liq_24h": (vol_h24 / liq_usd) if liq_usd > 0 else None,
        "fdv_liq_ratio": (fdv / liq_usd) if liq_usd > 0 and fdv > 0 else None,
        "mcap_fdv_ratio": (mcap / fdv) if fdv > 0 and mcap > 0 else None,
        "vol_mcap_24h": (vol_h24 / mcap) if mcap > 0 else None,
        "liq_mcap_ratio": (liq_usd / mcap) if mcap > 0 and liq_usd > 0 else None,
    }, None


# ---------------------------------------------------------------------------
# Safety gate (GoPlus + heuristics + optional Solana checks)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def fetch_goplus_security(contract: str, chain: str) -> Tuple[Dict[str, Any], List[str]]:
    """
    Returns (flags_dict, red_flags_list).
    GoPlus chain IDs: 1=eth, 56=bsc, 137=polygon, 42161=arbitrum, 8453=base, solana uses solana endpoint.
    """
    flags: Dict[str, Any] = {
        "is_honeypot": None,
        "buy_tax": None,
        "sell_tax": None,
        "is_mintable": None,
        "is_proxy": None,
        "owner_change_balance": None,
        "can_take_back_ownership": None,
        "hidden_owner": None,
        "selfdestruct": None,
        "external_call": None,
        "is_open_source": None,
        "holder_count": None,
        "lp_holder_count": None,
        "is_in_dex": None,
        "slippage_modifiable": None,
        "transfer_pausable": None,
        "is_blacklisted": None,
        "is_whitelisted": None,
        "is_anti_whale": None,
        "trading_cooldown": None,
        "personal_slippage_modifiable": None,
        "raw": {},
    }
    red_flags: List[str] = []

    if chain == "solana":
        # GoPlus Solana token security
        url = f"https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={contract}"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
            result = (data.get("result") or {}).get(contract) or {}
            flags["raw"] = result

            # Solana-specific fields
            if result.get("mintable", {}).get("status") == "1":
                flags["is_mintable"] = True
                red_flags.append("Mint authority still enabled")
            else:
                flags["is_mintable"] = False

            if result.get("freezable", {}).get("status") == "1":
                flags["is_freezable"] = True
                red_flags.append("Freeze authority still enabled")
            else:
                flags["is_freezable"] = False

            # LP lock / burn
            holders = result.get("holders") or []
            lp_burned = any(
                (h.get("is_locked") == 1 or h.get("is_burn") == 1)
                for h in holders
                if "lp" in (h.get("tag") or "").lower() or "raydium" in (h.get("tag") or "").lower()
            )
            flags["lp_locked_or_burned"] = lp_burned
            if not lp_burned and holders:
                # heuristic: if top holder is not a known burn/lock address
                top = holders[0] if holders else {}
                if to_float(top.get("percent")) and to_float(top.get("percent")) > 20:
                    red_flags.append(f"Top holder holds {top.get('percent')}% — check if LP is locked")

            # Creator / deployer
            creator = result.get("creator") or result.get("deployer")
            if creator:
                flags["deployer"] = creator

            # Balance mutable etc.
            if result.get("balance_mutable_authority", {}).get("status") == "1":
                red_flags.append("Balance mutable authority present")

            # Default tax-like
            flags["buy_tax"] = 0.0
            flags["sell_tax"] = 0.0
            flags["is_honeypot"] = False

        except Exception as e:
            red_flags.append(f"GoPlus Solana fetch failed: {e}")
            flags["fetch_error"] = str(e)

        return flags, red_flags

    # EVM: map common chainIds
    chain_map = {
        "ethereum": "1",
        "eth": "1",
        "bsc": "56",
        "binance": "56",
        "polygon": "137",
        "matic": "137",
        "arbitrum": "42161",
        "base": "8453",
        "optimism": "10",
        "avalanche": "43114",
        "fantom": "250",
    }
    chain_id = chain_map.get((chain or "").lower())
    if not chain_id:
        # try to guess from dexscreener chainId later; for now skip
        red_flags.append(f"GoPlus: unsupported or unknown EVM chain '{chain}' — manual check required")
        return flags, red_flags

    url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={contract.lower()}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        result = (data.get("result") or {}).get(contract.lower()) or {}
        flags["raw"] = result

        def yn(key: str) -> Optional[bool]:
            v = result.get(key)
            if v is None:
                return None
            return str(v) == "1"

        flags["is_honeypot"] = yn("is_honeypot")
        flags["is_mintable"] = yn("is_mintable")
        flags["is_proxy"] = yn("is_proxy")
        flags["owner_change_balance"] = yn("owner_change_balance")
        flags["can_take_back_ownership"] = yn("can_take_back_ownership")
        flags["hidden_owner"] = yn("hidden_owner")
        flags["selfdestruct"] = yn("selfdestruct")
        flags["external_call"] = yn("external_call")
        flags["is_open_source"] = yn("is_open_source")
        flags["is_in_dex"] = yn("is_in_dex")
        flags["slippage_modifiable"] = yn("slippage_modifiable")
        flags["transfer_pausable"] = yn("transfer_pausable")
        flags["is_blacklisted"] = yn("is_blacklisted")
        flags["is_whitelisted"] = yn("is_whitelisted")
        flags["is_anti_whale"] = yn("is_anti_whale")
        flags["trading_cooldown"] = yn("trading_cooldown")
        flags["personal_slippage_modifiable"] = yn("personal_slippage_modifiable")

        flags["buy_tax"] = to_float(result.get("buy_tax"))
        flags["sell_tax"] = to_float(result.get("sell_tax"))
        flags["holder_count"] = to_float(result.get("holder_count"))
        flags["lp_holder_count"] = to_float(result.get("lp_holder_count"))

        # Red flags
        if flags["is_honeypot"]:
            red_flags.append("HONEYPOT detected")
        if flags["buy_tax"] is not None and flags["buy_tax"] > 10:
            red_flags.append(f"High buy tax: {flags['buy_tax']}%")
        if flags["sell_tax"] is not None and flags["sell_tax"] > 10:
            red_flags.append(f"High sell tax: {flags['sell_tax']}%")
        if flags["is_mintable"]:
            red_flags.append("Mintable — unlimited supply risk")
        if flags["owner_change_balance"]:
            red_flags.append("Owner can change balances")
        if flags["can_take_back_ownership"]:
            red_flags.append("Ownership can be taken back")
        if flags["hidden_owner"]:
            red_flags.append("Hidden owner")
        if flags["selfdestruct"]:
            red_flags.append("Self-destruct possible")
        if flags["transfer_pausable"]:
            red_flags.append("Transfers can be paused")
        if flags["is_blacklisted"]:
            red_flags.append("Blacklist function present")
        if flags["is_open_source"] is False:
            red_flags.append("Contract not open-source")

        # LP lock heuristic from holders
        lp_holders = result.get("lp_holders") or []
        locked = any(
            h.get("is_locked") == 1 or h.get("is_contract") == 1
            for h in lp_holders
        )
        flags["lp_locked_or_burned"] = locked
        if not locked and lp_holders:
            red_flags.append("LP may not be locked — verify on explorer")

    except Exception as e:
        red_flags.append(f"GoPlus EVM fetch failed: {e}")
        flags["fetch_error"] = str(e)

    return flags, red_flags


def compute_safety_score(flags: Dict[str, Any], red_flags: List[str]) -> Dict[str, Any]:
    """
    Safety score 0..1.
    Any critical red flag → score forced to 0 and veto=True.
    """
    critical = [
        "HONEYPOT",
        "Mint authority still enabled",
        "Freeze authority still enabled",
        "Owner can change balances",
        "Self-destruct possible",
        "Mintable — unlimited supply risk",
    ]
    veto = any(any(c.lower() in rf.lower() for c in critical) for rf in red_flags)

    if veto:
        return {"score": 0.0, "veto": True, "red_flags": red_flags, "flags": flags}

    # Soft penalties
    score = 1.0
    soft = [
        ("High buy tax", 0.25),
        ("High sell tax", 0.25),
        ("Ownership can be taken back", 0.15),
        ("Hidden owner", 0.20),
        ("Transfers can be paused", 0.15),
        ("Blacklist function", 0.15),
        ("Contract not open-source", 0.10),
        ("LP may not be locked", 0.20),
        ("Top holder holds", 0.10),
        ("Balance mutable", 0.15),
    ]
    for phrase, penalty in soft:
        if any(phrase.lower() in rf.lower() for rf in red_flags):
            score -= penalty

    # Positive signals
    if flags.get("is_open_source") is True:
        score += 0.05
    if flags.get("lp_locked_or_burned") is True:
        score += 0.10
    if flags.get("is_honeypot") is False:
        score += 0.05

    score = max(0.0, min(1.0, score))
    return {"score": score, "veto": False, "red_flags": red_flags, "flags": flags}


# ---------------------------------------------------------------------------
# Website text + structured narrative extraction (LLM-style rules)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_website_text(url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not url:
        return None, None
    if not url.startswith("http"):
        url = "https://" + url
    try:
        r = requests.get("https://r.jina.ai/" + url, timeout=25)
        r.raise_for_status()
        return r.text[:50000], None
    except Exception as e:
        return None, f"Website fetch failed: {e}"


def clean_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    return text


def split_into_sentences(text: str) -> List[str]:
    sentences: List[str] = []
    for line in text.splitlines():
        line = line.lstrip("#*->• ").strip()
        if not line:
            continue
        for part in re.split(r"(?<=[.!?])\s+", line):
            part = part.strip()
            if 20 <= len(part) <= 500:
                sentences.append(part)
    return sentences


# Structured extraction patterns (richer than pure regex dump)
PRODUCT_LIVE_RE = re.compile(
    r"\b(?:live\s+on\s+mainnet|mainnet\s+live|launched|now\s+live|production|available\s+now)\b", re.I
)
PRODUCT_BETA_RE = re.compile(r"\b(?:beta|testnet|coming\s+soon|in\s+development|roadmap)\b", re.I)
DOXXED_RE = re.compile(
    r"\b(?:doxxed|doxed|team\s+is\s+public|linkedin|founder\s+is|ceo\s+is|our\s+team|meet\s+the\s+team)\b", re.I
)
GITHUB_RE = re.compile(r"\b(?:github\.com/[\w\-]+/[\w\-]+|open[\-\s]?source|source\s+code)\b", re.I)
CLAIM_VERIFIABLE_RE = re.compile(
    r"\b(?:audited\s+by|certik|hacken|slowmist|openzeppelin|published\s+on|whitepaper|docs\.|documentation)\b", re.I
)
MISSION_RE = re.compile(
    r"\b(?:our\s+mission|we\s+are\s+building|we're\s+building|our\s+vision|founded\s+to|created\s+to)\b", re.I
)

THEME_PATTERNS = {
    "AI": re.compile(r"\b(?:ai|agents?|gpt|claude|grok|llm|machine\s+learning)\b", re.I),
    "trading": re.compile(r"\b(?:trade|trading|dex|swap|orderbook)\b", re.I),
    "education": re.compile(r"\b(?:education|tutorials?|learn|course|academy)\b", re.I),
    "gaming": re.compile(r"\b(?:games?|gaming|quests?|nfts?|play[\-\s]?to[\-\s]?earn)\b", re.I),
    "DeFi": re.compile(r"\b(?:defi|yield|staking|farming|liquidity\s+pool)\b", re.I),
    "meme": re.compile(r"\b(?:memes?|memecoin|dogs?|doge|cats?|pepe|frog)\b", re.I),
    "RWA": re.compile(r"\b(?:real[\-\s]?world\s+asset|rwa|tokenization)\b", re.I),
    "infra": re.compile(r"\b(?:infrastructure|layer[\-\s]?2|l2|rollup|bridge)\b", re.I),
}


def infer_themes(text: str) -> List[str]:
    return [name for name, rx in THEME_PATTERNS.items() if rx.search(text)]


def structured_narrative_extraction(text: str, source: str = "website") -> Dict[str, Any]:
    """
    Structured pull:
    - product description (first meaningful paragraph / mission)
    - is_live / is_beta
    - team_doxxed signal
    - github / open-source signal
    - verifiable claims
    - themes
    - raw key sentences
    """
    if not text:
        return {
            "product": None,
            "is_live": None,
            "is_beta": None,
            "team_doxxed": None,
            "has_github": None,
            "verifiable_claims": [],
            "themes": [],
            "key_sentences": [],
            "source": source,
        }

    cleaned = clean_markdown(text)
    sentences = split_into_sentences(cleaned)
    full = cleaned.lower()

    # Product / mission: first strong mission or first long sentence
    product = None
    for s in sentences:
        if MISSION_RE.search(s) or len(s) > 80:
            product = s
            break
    if not product and sentences:
        product = sentences[0]

    is_live = bool(PRODUCT_LIVE_RE.search(full))
    is_beta = bool(PRODUCT_BETA_RE.search(full)) and not is_live
    team_doxxed = bool(DOXXED_RE.search(full))
    has_github = bool(GITHUB_RE.search(full))

    verifiable = []
    for s in sentences:
        if CLAIM_VERIFIABLE_RE.search(s):
            verifiable.append(s[:200])

    key_sentences = []
    for s in sentences[:15]:
        if any(rx.search(s) for rx in [MISSION_RE, PRODUCT_LIVE_RE, DOXXED_RE, CLAIM_VERIFIABLE_RE]):
            key_sentences.append(s)

    themes = infer_themes(cleaned)

    return {
        "product": product,
        "is_live": is_live,
        "is_beta": is_beta,
        "team_doxxed": team_doxxed,
        "has_github": has_github,
        "verifiable_claims": verifiable[:5],
        "themes": themes,
        "key_sentences": key_sentences[:8],
        "source": source,
    }


# Sentiment helpers (kept for community + tone)
POSITIVE_WORDS = {
    "bullish", "moon", "pump", "buy", "undervalued", "gem", "alpha", "love",
    "great", "awesome", "amazing", "strong", "confident", "hold", "hodl",
    "solid", "based", "legitimate", "real", "utility",
}
NEGATIVE_WORDS = {
    "bearish", "dump", "scam", "rug", "rugged", "down", "sell", "weak", "trash",
    "hate", "bad", "terrible", "worst", "loss", "rekt", "hack", "hacked",
    "exploit", "exploited", "sorry", "honeypot", "exit",
}
NEGATORS = {"not", "no", "never", "isn't", "isnt", "aint", "ain't", "don't", "dont", "without", "hardly"}
NEGATIVE_PHRASES = ["exit scam", "exit liquidity", "rug pull"]


def sentiment_counts(text: str) -> Tuple[int, int]:
    t = text.lower().replace("’", "'")
    for ph in NEGATIVE_PHRASES:
        t = t.replace(ph, " scam ")
    tokens = re.findall(r"[a-z']+", t)
    pos = neg = 0
    for i, tok in enumerate(tokens):
        if tok in POSITIVE_WORDS:
            polarity = 1
        elif tok in NEGATIVE_WORDS:
            polarity = -1
        else:
            continue
        if any(x in NEGATORS for x in tokens[max(0, i - 3):i]):
            polarity = -polarity
        if polarity > 0:
            pos += 1
        else:
            neg += 1
    return pos, neg


def classify_post_sentiment(text: str) -> str:
    pos, neg = sentiment_counts(text)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def compute_css_from_posts(posts: List[str]) -> Optional[float]:
    if not posts:
        return None
    pos = neg = 0
    for p in posts:
        s = classify_post_sentiment(p)
        if s == "positive":
            pos += 1
        elif s == "negative":
            neg += 1
    css_raw = (pos - neg) / len(posts)
    return (css_raw + 1) / 2


# ---------------------------------------------------------------------------
# X profile (best-effort)
# ---------------------------------------------------------------------------

def extract_twitter_handle(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)", url)
    if not m:
        return None
    handle = m.group(1)
    if handle.lower() in {"i", "intent", "share", "home", "search"}:
        return None
    return handle


def _meta(page: str, name: str) -> Optional[str]:
    pat = r'<meta[^>]+(?:name|property)=["\']' + re.escape(name) + r'["\'][^>]*content=["\']([^"\']*)["\']'
    m = re.search(pat, page, re.I)
    return html.unescape(m.group(1)).strip() if m else None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_x_profile(handle: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    msg = "X bio could not be read automatically (X blocks most scrapers). Paste the bio manually below."
    try:
        r = requests.get(f"https://x.com/{handle}", headers=headers, timeout=20)
        r.raise_for_status()
        bio = _meta(r.text, "twitter:description") or _meta(r.text, "og:description")
        if not bio:
            return None, msg
        return {"bio": bio}, None
    except Exception:
        return None, msg


# ---------------------------------------------------------------------------
# Domain age vs token age (simple WHOIS-ish via free endpoint or heuristic)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=86400, show_spinner=False)
def estimate_domain_age_days(website: Optional[str]) -> Optional[float]:
    """Best-effort domain creation age. Returns days or None."""
    if not website:
        return None
    try:
        from urllib.parse import urlparse
        host = urlparse(website if website.startswith("http") else "https://" + website).netloc
        host = host.replace("www.", "")
        # free whoisjsonapi / similar often rate-limited; try a lightweight approach
        r = requests.get(f"https://api.whois.vu/?q={host}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            created = data.get("created") or data.get("creation_date")
            if created:
                # parse various formats
                for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d-%b-%Y"):
                    try:
                        dt = datetime.strptime(str(created)[:19], fmt).replace(tzinfo=timezone.utc)
                        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
                    except Exception:
                        continue
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Separate scores
# ---------------------------------------------------------------------------

def compute_market_structure_score(token: Dict[str, Any]) -> Dict[str, Any]:
    """
    Market structure score 0..1 based on liquidity, volume, ratios vs entry thresholds.
    """
    liq = to_float(token.get("liquidity_usd")) or 0
    vol = to_float(token.get("volume_24h_usd")) or 0
    fdv = to_float(token.get("fdv_usd")) or 0
    vol_liq = to_float(token.get("vol_liq_24h"))
    fdv_liq = to_float(token.get("fdv_liq_ratio"))
    liq_mcap = to_float(token.get("liq_mcap_ratio"))

    score = 0.0
    details = []

    # Liquidity
    if liq >= ENTRY_THRESHOLDS["min_liquidity_usd"] * 5:
        score += 0.25
        details.append("liq strong")
    elif liq >= ENTRY_THRESHOLDS["min_liquidity_usd"]:
        score += 0.15
        details.append("liq ok")
    elif liq > 0:
        score += 0.05
        details.append("liq low")

    # Volume
    if vol >= ENTRY_THRESHOLDS["min_volume_24h_usd"] * 3:
        score += 0.25
        details.append("vol strong")
    elif vol >= ENTRY_THRESHOLDS["min_volume_24h_usd"]:
        score += 0.15
        details.append("vol ok")
    elif vol > 0:
        score += 0.05
        details.append("vol low")

    # FDV not insane
    if fdv > 0 and fdv <= ENTRY_THRESHOLDS["max_fdv_usd"]:
        score += 0.15
        details.append("fdv reasonable")
    elif fdv > ENTRY_THRESHOLDS["max_fdv_usd"] * 2:
        score -= 0.10
        details.append("fdv very high")

    # Vol/Liq
    if vol_liq is not None:
        if 0.5 <= vol_liq <= 5:
            score += 0.15
            details.append("vol/liq healthy")
        elif vol_liq > 10:
            score -= 0.05
            details.append("vol/liq extreme")

    # Liq/MCap
    if liq_mcap is not None:
        if liq_mcap >= 0.15:
            score += 0.15
            details.append("liq/mcap strong")
        elif liq_mcap >= 0.05:
            score += 0.08
            details.append("liq/mcap ok")
        else:
            score -= 0.05
            details.append("liq/mcap thin")

    # FDV/Liq risk
    if fdv_liq is not None and fdv_liq > 20:
        score -= 0.15
        details.append("fdv/liq high risk")

    score = max(0.0, min(1.0, score))
    return {"score": score, "details": details}


def compute_narrative_score(struct: Dict[str, Any], x_bio: Optional[str] = None) -> Dict[str, Any]:
    """
    Narrative score 0..1 from structured extraction + optional X bio alignment.
    """
    score = 0.0
    details = []

    if struct.get("product"):
        score += 0.25
        details.append("product described")
    if struct.get("is_live"):
        score += 0.20
        details.append("claims live product")
    elif struct.get("is_beta"):
        score += 0.10
        details.append("beta / upcoming")
    if struct.get("team_doxxed"):
        score += 0.15
        details.append("team doxx signal")
    if struct.get("has_github"):
        score += 0.10
        details.append("github / open-source signal")
    if struct.get("verifiable_claims"):
        score += 0.15
        details.append(f"{len(struct['verifiable_claims'])} verifiable claims")
    if struct.get("themes"):
        score += 0.05
        details.append(f"themes: {', '.join(struct['themes'][:3])}")

    # X bio alignment (theme overlap)
    if x_bio:
        x_themes = set(infer_themes(x_bio))
        web_themes = set(struct.get("themes") or [])
        if x_themes & web_themes:
            score += 0.10
            details.append("web-X theme alignment")
        else:
            details.append("web-X themes diverge")

    score = max(0.0, min(1.0, score))
    return {"score": score, "details": details, "structured": struct}


def passes_entry_thresholds(token: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons = []
    liq = to_float(token.get("liquidity_usd")) or 0
    vol = to_float(token.get("volume_24h_usd")) or 0
    fdv = to_float(token.get("fdv_usd")) or 0
    vol_liq = to_float(token.get("vol_liq_24h"))
    fdv_liq = to_float(token.get("fdv_liq_ratio"))
    liq_mcap = to_float(token.get("liq_mcap_ratio"))

    if liq < ENTRY_THRESHOLDS["min_liquidity_usd"]:
        reasons.append(f"liq < ${ENTRY_THRESHOLDS['min_liquidity_usd']:,}")
    if vol < ENTRY_THRESHOLDS["min_volume_24h_usd"]:
        reasons.append(f"vol24h < ${ENTRY_THRESHOLDS['min_volume_24h_usd']:,}")
    if fdv > ENTRY_THRESHOLDS["max_fdv_usd"] and fdv > 0:
        reasons.append(f"fdv > ${ENTRY_THRESHOLDS['max_fdv_usd']:,}")
    if vol_liq is not None and vol_liq < ENTRY_THRESHOLDS["min_vol_liq"]:
        reasons.append(f"vol/liq < {ENTRY_THRESHOLDS['min_vol_liq']}")
    if fdv_liq is not None and fdv_liq > ENTRY_THRESHOLDS["max_fdv_liq"]:
        reasons.append(f"fdv/liq > {ENTRY_THRESHOLDS['max_fdv_liq']}")
    if liq_mcap is not None and liq_mcap < ENTRY_THRESHOLDS["min_liq_mcap"]:
        reasons.append(f"liq/mcap < {ENTRY_THRESHOLDS['min_liq_mcap']}")

    return (len(reasons) == 0), reasons


# ---------------------------------------------------------------------------
# Logging (SQLite)
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            contract TEXT NOT NULL,
            chain TEXT,
            symbol TEXT,
            name TEXT,
            price_usd REAL,
            liquidity_usd REAL,
            volume_24h_usd REAL,
            market_cap_usd REAL,
            fdv_usd REAL,
            safety_score REAL,
            market_score REAL,
            narrative_score REAL,
            community_score REAL,
            veto INTEGER,
            passes_entry INTEGER,
            red_flags TEXT,
            full_json TEXT
        )
    """)
    conn.commit()
    conn.close()


def log_analysis(result: Dict[str, Any]):
    init_db()
    token = result.get("token") or {}
    safety = result.get("safety") or {}
    market = result.get("market") or {}
    narrative = result.get("narrative") or {}
    css = result.get("css")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO analyses (
            ts_utc, contract, chain, symbol, name,
            price_usd, liquidity_usd, volume_24h_usd, market_cap_usd, fdv_usd,
            safety_score, market_score, narrative_score, community_score,
            veto, passes_entry, red_flags, full_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            token.get("address") or result.get("contract"),
            token.get("chain") or result.get("chain_detected"),
            token.get("symbol"),
            token.get("name"),
            to_float(token.get("price_usd")),
            to_float(token.get("liquidity_usd")),
            to_float(token.get("volume_24h_usd")),
            to_float(token.get("market_cap_usd")),
            to_float(token.get("fdv_usd")),
            safety.get("score"),
            market.get("score"),
            narrative.get("score"),
            css,
            1 if safety.get("veto") else 0,
            1 if result.get("passes_entry") else 0,
            json.dumps(safety.get("red_flags") or []),
            json.dumps(result, default=str)[:100000],
        ),
    )
    conn.commit()
    conn.close()


def load_log_df(limit: int = 500) -> pd.DataFrame:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        f"SELECT * FROM analyses ORDER BY id DESC LIMIT {limit}",
        conn,
    )
    conn.close()
    return df


# ---------------------------------------------------------------------------
# Main analyzer
# ---------------------------------------------------------------------------

def analyze_token(
    contract: str,
    chain: Optional[str] = None,
    x_bio_manual: str = "",
    community_posts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    contract = contract.strip()
    chain_eff = chain if chain and chain != "auto" else detect_chain(contract)

    token, err = fetch_token_from_dexscreener(contract, chain_eff)
    if err or not token:
        return {"error": err or "Token not found", "contract": contract, "chain_detected": chain_eff}

    warnings: List[str] = []

    # --- Safety gate first ---
    flags, red_flags = fetch_goplus_security(contract, token.get("chain") or chain_eff)
    safety = compute_safety_score(flags, red_flags)

    # --- Website + structured narrative ---
    website_text, w = fetch_website_text(token.get("website"))
    if w:
        warnings.append(w)
    struct = structured_narrative_extraction(website_text or "", source="website")

    # Domain age vs token age
    domain_age = estimate_domain_age_days(token.get("website"))
    token_age = token.get("age_days")
    if domain_age is not None and token_age is not None:
        if domain_age < token_age * 0.5 and domain_age < 30:
            warnings.append(
                f"Domain appears very young (~{domain_age:.0f}d) vs token age (~{token_age:.0f}d) — possible fresh domain"
            )
            struct["domain_age_days"] = domain_age
            struct["domain_vs_token_flag"] = True
        else:
            struct["domain_age_days"] = domain_age
            struct["domain_vs_token_flag"] = False

    # X bio
    x_bio = x_bio_manual.strip() or None
    if not x_bio:
        handle = extract_twitter_handle(token.get("twitter"))
        if handle:
            profile, w = fetch_x_profile(handle)
            if w:
                warnings.append(w)
            if profile and profile.get("bio"):
                x_bio = profile["bio"]

    narrative = compute_narrative_score(struct, x_bio)
    if x_bio:
        narrative["x_bio"] = x_bio

    # Market structure
    market = compute_market_structure_score(token)

    # Community
    posts = community_posts or []
    css = compute_css_from_posts(posts)

    # Entry thresholds
    passes, fail_reasons = passes_entry_thresholds(token)

    # Overall: if safety veto → do not present a "nice" narrative score
    if safety.get("veto"):
        narrative["score"] = 0.0
        narrative["details"].insert(0, "VETOED by safety red flags")

    result = {
        "token": token,
        "safety": safety,
        "market": market,
        "narrative": narrative,
        "css": css,
        "passes_entry": passes,
        "entry_fail_reasons": fail_reasons,
        "chain_detected": chain_eff,
        "warnings": warnings,
        "contract": contract,
    }

    # Log every run
    try:
        log_analysis(result)
    except Exception as e:
        warnings.append(f"Logging failed: {e}")
        result["warnings"] = warnings

    return result


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

tab_single, tab_batch, tab_log = st.tabs(["Single token", "Batch mode", "Analysis log"])

with tab_single:
    contract = st.text_input(
        "Contract address",
        placeholder="e.g. 2PENPmfgJfq6CG3k4byj4oWwHf8SerqakmYHMkUupump",
        key="single_contract",
    )
    chain = st.selectbox("Chain", ["auto", "solana", "evm"], key="single_chain")

    x_bio_manual = st.text_area(
        "X bio (optional)",
        help="X usually blocks automatic reading. Paste the project's X bio here.",
        height=70,
        key="single_xbio",
    )

    community_posts_text = st.text_area(
        "Community posts (optional)",
        help="Paste community posts (one per line) from X/Telegram for CSS.",
        placeholder="familiars looking strong\nthis is a scam\nlove the agent leaderboard\nexit liquidity",
        height=120,
        key="single_posts",
    )

    if st.button("Analyze token", key="btn_single"):
        if not contract.strip():
            st.warning("Please enter a contract address.")
        else:
            posts = [line.strip() for line in community_posts_text.splitlines() if line.strip()]
            with st.spinner("Safety gate → market data → narrative extraction..."):
                result = analyze_token(contract, chain, x_bio_manual, posts)

            if "error" in result:
                st.error(result["error"])
            else:
                for w in result.get("warnings", []):
                    st.warning(w)

                token = result["token"]
                safety = result["safety"]
                market = result["market"]
                narrative = result["narrative"]
                css = result["css"]

                st.subheader(f"{token['name'] or 'Unknown'} ({token['symbol'] or '?'})")

                # --- Safety banner ---
                if safety.get("veto"):
                    st.error(
                        "🚨 SAFETY VETO — critical red flags detected. "
                        "Narrative score forced to 0. Do not treat this as investable narrative."
                    )
                    for rf in safety.get("red_flags") or []:
                        st.error(f"• {rf}")
                elif safety.get("red_flags"):
                    st.warning("Soft red flags present:")
                    for rf in safety["red_flags"]:
                        st.warning(f"• {rf}")

                # Metrics
                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    st.metric("Price (USD)", fmt_price_usd(token["price_usd"]))
                with c2:
                    st.metric("Liquidity", fmt_usd_compact(token["liquidity_usd"]))
                with c3:
                    st.metric("24h Volume", fmt_usd_compact(token["volume_24h_usd"]))
                with c4:
                    st.metric("Market Cap", fmt_usd_compact(token["market_cap_usd"]))

                c5, c6, c7, c8 = st.columns(4)
                with c5:
                    age_m = token["age_months"]
                    st.metric("Age", f"{age_m:.1f} mo" if age_m is not None else "N/A")
                with c6:
                    st.metric("Vol/Liq (24h)", interpret_vol_liq(token["vol_liq_24h"]))
                with c7:
                    st.metric("Price change (24h)", fmt_pct_change(token["price_change_24h"]))
                with c8:
                    buys, sells = token["buys_24h"], token["sells_24h"]
                    st.metric(
                        "Buys / Sells (24h)",
                        f"{buys} / {sells}" if buys is not None and sells is not None else "N/A",
                    )

                c9, c10, c11, c12, c13 = st.columns(5)
                with c9:
                    st.metric("FDV", fmt_usd_compact(token["fdv_usd"]))
                with c10:
                    st.metric("FDV / Liq", interpret_fdv_liq(token["fdv_liq_ratio"]))
                with c11:
                    mf = to_float(token["mcap_fdv_ratio"])
                    st.metric("MCap / FDV", f"{mf * 100:.0f}% in circ" if mf is not None else "N/A")
                with c12:
                    st.metric("Vol / MCap", interpret_vol_mcap(token["vol_mcap_24h"]))
                with c13:
                    st.metric("Liq / MCap", interpret_liq_mcap(token["liq_mcap_ratio"]))

                st.caption(
                    f"Chain: {token['chain']} | Detected: {result['chain_detected']} | "
                    f"Pairs: {token['pairs_found']} | "
                    f"[DexScreener]({token['dex_url']})"
                )

                # --- Separate scores ---
                st.subheader("Scores (separate — no arbitrary blend)")
                sc1, sc2, sc3, sc4 = st.columns(4)
                with sc1:
                    st.metric("Safety", f"{safety['score']:.2f}" if safety.get("score") is not None else "N/A")
                    if safety.get("veto"):
                        st.caption("VETOED")
                with sc2:
                    st.metric("Market structure", f"{market['score']:.2f}")
                    st.caption(", ".join(market.get("details") or [])[:80])
                with sc3:
                    st.metric("Narrative", f"{narrative['score']:.2f}")
                    st.caption(", ".join(narrative.get("details") or [])[:80])
                with sc4:
                    css_str = f"{css:.2f}" if css is not None else "N/A"
                    st.metric("Community (CSS)", css_str)
                    st.caption("1.0 = all positive posts")

                # Entry pass/fail
                if result.get("passes_entry"):
                    st.success("✅ Passes entry thresholds (liq / vol / fdv / ratios)")
                else:
                    st.info(
                        "Does not pass entry thresholds: "
                        + "; ".join(result.get("entry_fail_reasons") or [])
                    )

                # Structured narrative
                struct = narrative.get("structured") or {}
                st.subheader("Structured narrative extraction")
                st.write(f"**Product / mission:** {struct.get('product') or '—'}")
                st.write(
                    f"**Live:** {struct.get('is_live')} | **Beta/upcoming:** {struct.get('is_beta')} | "
                    f"**Team doxx signal:** {struct.get('team_doxxed')} | **GitHub signal:** {struct.get('has_github')}"
                )
                if struct.get("verifiable_claims"):
                    st.write("**Verifiable claims:**")
                    for c in struct["verifiable_claims"]:
                        st.write(f"- {c}")
                if struct.get("themes"):
                    st.write(f"**Themes:** {', '.join(struct['themes'])}")
                if struct.get("domain_age_days") is not None:
                    st.write(
                        f"**Domain age (est.):** {struct['domain_age_days']:.0f} days "
                        f"{'(⚠️ young vs token)' if struct.get('domain_vs_token_flag') else ''}"
                    )
                if narrative.get("x_bio"):
                    with st.expander("X bio used"):
                        st.write(narrative["x_bio"])

                if posts:
                    counts = {"positive": 0, "neutral": 0, "negative": 0}
                    for p in posts:
                        counts[classify_post_sentiment(p)] += 1
                    st.caption(
                        f"Community posts: {len(posts)} | "
                        f"+{counts['positive']} / ~{counts['neutral']} / -{counts['negative']}"
                    )

                with st.expander("How to read scores & metrics"):
                    st.markdown(
                        """
- **Safety** is computed first. Critical flags (honeypot, mint/freeze authority, owner-change-balance, selfdestruct) **veto** the whole analysis and force narrative score to 0.
- **Market structure** scores liquidity, volume, FDV, and the classic ratios against your entry thresholds.
- **Narrative** comes from structured extraction (product live?, team doxx signal?, GitHub, verifiable claims, theme alignment) — not a single blended ONS.
- **Community (CSS)** is purely from the posts you paste.
- **Vol/Liq**, **Vol/MCap**, **Liq/MCap**, **FDV/Liq** keep the same interpretation as before.
- Holder concentration and full deployer history still need explorer/Arkham; GoPlus covers the contract-level risks we can automate.
                        """
                    )

                with st.expander("Raw safety flags + JSON"):
                    st.json({"safety_flags": safety.get("flags"), "result": result})

with tab_batch:
    st.markdown("Paste up to ~20 contract addresses (one per line). Ranked table with safety veto + entry pass/fail.")
    batch_text = st.text_area(
        "Contract addresses",
        height=150,
        placeholder="2PENPmfgJfq6CG3k4byj4oWwHf8SerqakmYHMkUupump\n0x...\n...",
        key="batch_addrs",
    )
    batch_chain = st.selectbox("Chain (applied to all)", ["auto", "solana", "evm"], key="batch_chain")

    if st.button("Run batch", key="btn_batch"):
        addrs = [a.strip() for a in batch_text.splitlines() if a.strip()]
        if not addrs:
            st.warning("Paste at least one address.")
        else:
            rows = []
            progress = st.progress(0)
            for i, addr in enumerate(addrs[:25]):
                with st.spinner(f"Analyzing {addr[:12]}..."):
                    r = analyze_token(addr, batch_chain)
                if "error" in r:
                    rows.append({
                        "contract": addr,
                        "symbol": "ERR",
                        "name": r["error"][:40],
                        "safety": None,
                        "market": None,
                        "narrative": None,
                        "css": None,
                        "veto": True,
                        "passes_entry": False,
                        "liq": None,
                        "vol24": None,
                        "fdv": None,
                        "red_flags": r["error"],
                    })
                else:
                    t = r["token"]
                    rows.append({
                        "contract": t.get("address") or addr,
                        "symbol": t.get("symbol"),
                        "name": t.get("name"),
                        "safety": r["safety"].get("score"),
                        "market": r["market"].get("score"),
                        "narrative": r["narrative"].get("score"),
                        "css": r.get("css"),
                        "veto": r["safety"].get("veto"),
                        "passes_entry": r.get("passes_entry"),
                        "liq": t.get("liquidity_usd"),
                        "vol24": t.get("volume_24h_usd"),
                        "fdv": t.get("fdv_usd"),
                        "red_flags": "; ".join(r["safety"].get("red_flags") or [])[:120],
                    })
                progress.progress((i + 1) / min(len(addrs), 25))

            df = pd.DataFrame(rows)
            # Rank: veto last, then by safety * market * narrative (rough priority)
            df["_rank_key"] = (
                (~df["veto"].fillna(True)).astype(int) * 1000
                + df["safety"].fillna(0) * 100
                + df["market"].fillna(0) * 50
                + df["narrative"].fillna(0) * 30
            )
            df = df.sort_values("_rank_key", ascending=False).drop(columns=["_rank_key"])
            st.dataframe(df, use_container_width=True)
            st.caption(
                f"Entry thresholds: liq≥${ENTRY_THRESHOLDS['min_liquidity_usd']:,} | "
                f"vol≥${ENTRY_THRESHOLDS['min_volume_24h_usd']:,} | "
                f"fdv≤${ENTRY_THRESHOLDS['max_fdv_usd']:,} | "
                f"vol/liq≥{ENTRY_THRESHOLDS['min_vol_liq']} | "
                f"fdv/liq≤{ENTRY_THRESHOLDS['max_fdv_liq']} | "
                f"liq/mcap≥{ENTRY_THRESHOLDS['min_liq_mcap']}"
            )

with tab_log:
    st.markdown(
        "Every analysis is logged to SQLite. Later you can join this against 7d/30d returns "
        "to learn which components actually predict anything."
    )
    if st.button("Refresh log", key="btn_log"):
        pass
    try:
        log_df = load_log_df(300)
        if log_df.empty:
            st.info("No analyses logged yet.")
        else:
            st.dataframe(log_df.drop(columns=["full_json"], errors="ignore"), use_container_width=True)
            st.download_button(
                "Download log CSV",
                log_df.to_csv(index=False),
                file_name="analysis_log.csv",
                mime="text/csv",
            )
            st.caption(f"DB path: `{DB_PATH}` — add a 7d/30d price column later and correlate vs scores.")
    except Exception as e:
        st.error(f"Could not load log: {e}")
