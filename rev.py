import re
import json
import time
import requests
from typing import Optional, Dict, Any, List

import streamlit as st

# NOTE: this file deliberately contains no backslash characters.
# Earlier copies broke because copy/paste turned newline escapes into real line breaks
# and dropped the backslash from whitespace and digit patterns.

# -------------------------
# Page config
# -------------------------

st.set_page_config(
    page_title="Token Narrative Analyzer",
    page_icon="🧭",
    layout="wide"
)

st.title("Token Narrative Analyzer")
st.caption("Paste a contract address → get official narrative + ONS score + on-chain metrics")


# -------------------------
# Small formatting helpers
# -------------------------

def to_float(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_usd(x, decimals: int = 0) -> str:
    v = to_float(x)
    if v is None:
        return "N/A"
    return f"${v:,.{decimals}f}"


def fmt_ratio(x, decimals: int = 2) -> str:
    v = to_float(x)
    if v is None:
        return "N/A"
    return f"{v:.{decimals}f}x"


# -------------------------
# Helpers: chain detection
# -------------------------

def detect_chain(contract: str) -> str:
    c = contract.strip()
    if not c.startswith("0x") and 30 <= len(c) <= 48:
        if re.match("^[1-9A-HJ-NP-Za-km-z]+$", c):
            return "solana"
    if c.startswith("0x") and len(c) == 42:
        return "evm"
    return "unknown"


# -------------------------
# DexScreener fetch
# -------------------------

def fetch_token_from_dexscreener(contract: str) -> Optional[Dict[str, Any]]:
    url = "https://api.dexscreener.com/latest/dex/search/"
    params = {"q": contract}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None

        pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
        base = pair.get("baseToken") or {}
        info = pair.get("info") or {}
        websites = info.get("websites") or []
        socials = info.get("socials") or []

        website = websites[0]["url"] if websites else None
        twitter = None
        telegram = None
        for s in socials:
            t = s.get("type", "").lower()
            u = s.get("url", "")
            if t == "twitter" or "x.com" in u or "twitter.com" in u:
                twitter = u
            if t == "telegram":
                telegram = u

        # Age (of the highest-liquidity pair)
        created_ms = pair.get("pairCreatedAt")
        age_days = None
        age_months = None
        if created_ms and isinstance(created_ms, (int, float)) and created_ms > 0:
            now_ms = time.time() * 1000
            age_days = (now_ms - created_ms) / 86400000.0
            age_months = age_days / 30.44

        # Volume / liquidity / market cap
        liq_usd = (pair.get("liquidity") or {}).get("usd") or 0.0
        vol_h24 = (pair.get("volume") or {}).get("h24") or 0.0
        mcap = pair.get("marketCap") or 0.0
        fdv = pair.get("fdv") or 0.0

        vol_liq_24h = (vol_h24 / liq_usd) if liq_usd > 0 else None

        # Proxies for 7d and 30d using 24h volume as a rough base
        vol_7d_proxy = vol_h24 * 7
        vol_30d_proxy = vol_h24 * 30
        vol_liq_7d_proxy = (vol_7d_proxy / liq_usd) if liq_usd > 0 else None
        vol_liq_30d_proxy = (vol_30d_proxy / liq_usd) if liq_usd > 0 else None

        # FDV / liquidity
        fdv_liq_ratio = (fdv / liq_usd) if liq_usd > 0 else None
        mcap_fdv_ratio = (mcap / fdv) if fdv > 0 else None

        # Volume / market cap and liquidity / market cap
        vol_mcap_24h = (vol_h24 / mcap) if mcap > 0 else None
        liq_mcap_ratio = (liq_usd / mcap) if mcap > 0 else None

        return {
            "symbol": base.get("symbol"),
            "name": base.get("name"),
            "address": base.get("address"),
            "chain": pair.get("chainId"),
            "price_usd": pair.get("priceUsd"),
            "liquidity_usd": liq_usd,
            "volume_24h_usd": vol_h24,
            "market_cap_usd": mcap,
            "fdv_usd": fdv,
            "pair_created_ts": created_ms,
            "website": website,
            "twitter": twitter,
            "telegram": telegram,
            "dex_url": pair.get("url"),
            # Age
            "age_days": age_days,
            "age_months": age_months,
            # Vol/Liq
            "vol_liq_24h": vol_liq_24h,
            "vol_liq_7d_proxy": vol_liq_7d_proxy,
            "vol_liq_30d_proxy": vol_liq_30d_proxy,
            # FDV ratios
            "fdv_liq_ratio": fdv_liq_ratio,
            "mcap_fdv_ratio": mcap_fdv_ratio,
            # Vol/Mcap & Liq/Mcap
            "vol_mcap_24h": vol_mcap_24h,
            "liq_mcap_ratio": liq_mcap_ratio,
        }
    except Exception as e:
        st.error(f"DexScreener error: {e}")
        return None


# -------------------------
# Website text via jina
# -------------------------

def fetch_website_text(url: str) -> Optional[str]:
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    jina_url = "https://r.jina.ai/http://" + url.replace("https://", "").replace("http://", "")
    try:
        r = requests.get(jina_url, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception as e:
        st.warning(f"Website fetch warning: {e}")
        return None


# -------------------------
# Sentence splitting and extraction
# -------------------------

def flatten(text: str) -> str:
    # Turn all line breaks into single spaces (no backslashes needed)
    return " ".join(text.splitlines())


def split_into_sentences(text: str) -> List[str]:
    # Split on spaces that follow . ! or ?
    parts = re.split("(?<=[.!?]) +", flatten(text))
    return [p.strip() for p in parts if p.strip()]


def extract_summary_sentences(text: str, keywords: List[str]) -> str:
    text = flatten(text)
    sentences = split_into_sentences(text)
    scored = []
    kw_lower = [k.lower() for k in keywords]
    for s in sentences:
        sl = s.lower()
        score = sum(1 for k in kw_lower if k in sl)
        if score > 0:
            scored.append((score, s))
    if not scored:
        for s in sentences:
            if s:
                return s
        return ""
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [x[1] for x in scored[:2]]
    return " ".join(top)


def infer_themes(text: str) -> List[str]:
    t = text.lower()
    themes = []
    if any(k in t for k in ["ai", "agent", "gpt", "claude", "grok", "model"]):
        themes.append("AI")
    if any(k in t for k in ["trade", "trading", "dex", "swap", "liquidity"]):
        themes.append("trading")
    if any(k in t for k in ["education", "tutorial", "learn", "course"]):
        themes.append("education")
    if any(k in t for k in ["game", "gaming", "quest", "nft"]):
        themes.append("gaming")
    if any(k in t for k in ["defi", "yield", "staking", "farm"]):
        themes.append("DeFi")
    if any(k in t for k in ["meme", "dog", "cat", "pepe"]):
        themes.append("meme")
    return themes


def extract_website_narrative_items(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []
    items = []
    text_lower = text.lower()

    if any(k in text_lower for k in ["roadmap", "phase", "q1", "q2", "q3", "q4", "2026", "2027"]):
        summary = extract_summary_sentences(
            text, ["roadmap", "phase", "q1", "q2", "q3", "q4", "2026", "2027"]
        )
        items.append({
            "source": "website",
            "type": "roadmap",
            "date_period": "Future",
            "summary": summary,
            "sentiment": "positive",
            "themes": infer_themes(summary),
        })

    if any(k in text_lower for k in ["app", "platform", "live", "beta", "mainnet", "testnet", "product"]):
        is_live = "live" in text_lower or "mainnet" in text_lower
        summary = extract_summary_sentences(
            text, ["app", "platform", "live", "beta", "mainnet", "testnet", "product"]
        )
        items.append({
            "source": "website",
            "type": "product",
            "date_period": "Current" if is_live else "Past",
            "summary": summary,
            "sentiment": "positive",
            "themes": infer_themes(summary),
        })

    if any(k in text_lower for k in ["about", "mission", "vision", "built by", "created", "origin", "story"]):
        summary = extract_summary_sentences(
            text, ["about", "mission", "vision", "built", "created", "origin", "story"]
        )
        items.append({
            "source": "website",
            "type": "narrative",
            "date_period": "Past",
            "summary": summary,
            "sentiment": "positive",
            "themes": infer_themes(summary),
        })

    if any(k in text_lower for k in ["listed on", "exchange", "binance", "kucoin", "gate", "dex", "pool"]):
        summary = extract_summary_sentences(text, ["listed", "exchange", "dex", "pool"])
        items.append({
            "source": "website",
            "type": "exchange",
            "date_period": "Current",
            "summary": summary,
            "sentiment": "positive",
            "themes": infer_themes(summary),
        })

    return items


# -------------------------
# X profile fetch
# -------------------------

def extract_twitter_handle(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search("(?:x[.]com|twitter[.]com)/([A-Za-z0-9_]+)", url)
    return m.group(1) if m else None


def fetch_x_profile(handle: str) -> Optional[Dict[str, Any]]:
    url = f"https://x.com/{handle}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        html = r.text

        bio_match = re.search('<meta name="twitter:description" content="([^"]+)"', html)
        bio = bio_match.group(1) if bio_match else None

        joined_match = re.search('<meta name="twitter:data2" content="([^"]+)"', html)
        joined = joined_match.group(1) if joined_match else None

        posts = None
        posts_match = re.search('<meta name="twitter:data1" content="([^"]+)"', html)
        if posts_match:
            digits = re.sub("[^0-9]", "", posts_match.group(1))
            posts = int(digits) if digits else None

        return {
            "bio": bio,
            "joined": joined,
            "posts": posts,
        }
    except Exception as e:
        st.warning(f"X profile warning: {e}")
        return None


def x_profile_to_narrative_item(profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    bio = profile.get("bio")
    if not bio:
        return None
    return {
        "source": "x_official",
        "type": "narrative",
        "date_period": "Current",
        "summary": bio,
        "sentiment": "positive",
        "themes": infer_themes(bio),
    }


# -------------------------
# ONS computation
# -------------------------

def compute_ons(items: List[Dict[str, Any]]) -> float:
    if not items:
        return 0.0

    has_origin = any(
        i["type"] == "narrative"
        and any(k in i["summary"].lower() for k in ["built", "created", "origin", "story", "mission"])
        for i in items
    )
    has_product = any(i["type"] == "product" for i in items)
    has_roadmap = any(i["type"] == "roadmap" for i in items)
    has_exchange = any(i["type"] == "exchange" for i in items)

    coverage = sum([has_origin, has_product, has_roadmap, has_exchange])

    web_items = [i for i in items if i["source"] == "website"]
    x_items = [i for i in items if i["source"] == "x_official"]

    alignment = 0
    if web_items and x_items:
        web_text = " ".join(i["summary"] for i in web_items).lower()
        x_text = " ".join(i["summary"] for i in x_items).lower()
        key_terms = ["ai", "agent", "trade", "trading", "fomo", "education", "tutorial", "defi", "gaming"]
        web_terms = {t for t in key_terms if t in web_text}
        x_terms = {t for t in key_terms if t in x_text}
        if web_terms & x_terms:
            alignment = 1

    roadmap_repeated = sum(1 for i in items if i["type"] == "roadmap") >= 2
    consistency = alignment + roadmap_repeated

    negative_words = ["scam", "rug", "sorry", "issue", "problem", "hack", "exploit"]
    pos_count = 0
    neg_count = 0
    for i in items:
        s = i["summary"].lower()
        if any(w in s for w in negative_words):
            neg_count += 1
        else:
            pos_count += 1

    tone = 1.0 if neg_count == 0 else (0.5 if pos_count > neg_count else 0.0)

    ons = (coverage + consistency + tone) / 7.0
    return ons


# -------------------------
# Main analyzer
# -------------------------

def analyze_token(contract: str, chain: Optional[str] = None) -> Dict[str, Any]:
    if not chain or chain == "auto":
        chain = detect_chain(contract)

    token = fetch_token_from_dexscreener(contract)
    if not token:
        return {
            "error": "Token not found or no liquidity on DexScreener",
            "contract": contract,
            "chain_detected": chain,
        }

    website_text = fetch_website_text(token["website"])
    website_items = extract_website_narrative_items(website_text) if website_text else []

    x_items = []
    handle = extract_twitter_handle(token["twitter"])
    if handle:
        profile = fetch_x_profile(handle)
        if profile:
            x_item = x_profile_to_narrative_item(profile)
            if x_item:
                x_items.append(x_item)

    all_items = website_items + x_items
    ons = compute_ons(all_items)

    return {
        "token": token,
        "narrative_items": all_items,
        "ons": ons,
        "chain_detected": chain,
    }


# -------------------------
# Streamlit UI
# -------------------------

contract = st.text_input(
    "Contract address",
    placeholder="e.g. 2PENPmfgJfq6CG3k4byj4oWwHf8SerqakmYHMkUupump",
)
chain_options = ["auto", "solana", "evm"]
chain = st.selectbox("Chain", chain_options)

if st.button("Analyze token"):
    if not contract.strip():
        st.warning("Please enter a contract address.")
    else:
        with st.spinner("Fetching market data and analyzing narrative..."):
            result = analyze_token(contract, chain if chain != "auto" else None)

        if "error" in result:
            st.error(result["error"])
        else:
            token = result["token"]
            items = result["narrative_items"]
            ons = result["ons"]

            st.subheader(f"{token['name']} ({token['symbol']})")
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.metric("Price (USD)", fmt_usd(token["price_usd"], 8))
            with c2:
                st.metric("Liquidity (USD)", fmt_usd(token["liquidity_usd"]))
            with c3:
                st.metric("24h Volume (USD)", fmt_usd(token["volume_24h_usd"]))
            with c4:
                st.metric("Market Cap (USD)", fmt_usd(token["market_cap_usd"]))

            # Age & vol/liq
            c5, c6, c7, c8 = st.columns(4)
            with c5:
                age_m_str = f"{token['age_months']:.1f} months" if token["age_months"] else "N/A"
                st.metric("Age", age_m_str)
            with c6:
                st.metric("Vol/Liq (24h)", fmt_ratio(token["vol_liq_24h"]))
            with c7:
                st.metric("Vol/Liq (7d proxy)", fmt_ratio(token["vol_liq_7d_proxy"]))
            with c8:
                st.metric("Vol/Liq (30d proxy)", fmt_ratio(token["vol_liq_30d_proxy"]))

            # FDV & ratios
            c9, c10, c11, c12 = st.columns(4)
            with c9:
                st.metric("FDV (USD)", fmt_usd(token["fdv_usd"]))
            with c10:
                st.metric("FDV / Liq", fmt_ratio(token["fdv_liq_ratio"]))
            with c11:
                st.metric("MCap / FDV", fmt_ratio(token["mcap_fdv_ratio"]))
            with c12:
                st.metric("6m Avg Vol/Liq", "N/A (needs history)")

            # Vol/Mcap & Liq/Mcap
            c13, c14, c15, c16 = st.columns(4)
            with c13:
                st.metric("Vol / MCap (24h)", fmt_ratio(token["vol_mcap_24h"]))
            with c14:
                st.metric("Liq / MCap", fmt_ratio(token["liq_mcap_ratio"]))
            with c15:
                # Reserved for future metrics
                st.metric("Holder Count", "N/A")
            with c16:
                st.metric("Top 10 Holders %", "N/A")

            st.caption(
                f"Chain: {token['chain']} | Detected: {result['chain_detected']} | "
                f"DexScreener: [link]({token['dex_url']})"
            )

            st.caption(
                "Vol/Liq 7d/30d are rough proxies based on 24h volume. "
                "True 6m averages and holder metrics require historical/on-chain data."
            )

            st.subheader("Official Narrative Score (ONS)")
            st.write(f"ONS = **{ons:.2f} / 1.00**")
            st.write(
                "Higher ONS means a clearer, more consistent official story "
                "(origin, product, roadmap, exchange info, and aligned messaging)."
            )

            if items:
                st.subheader("Official Narrative Timeline")
                rows = []
                for i, it in enumerate(items):
                    rows.append({
                        "#": i + 1,
                        "date_period": it["date_period"],
                        "source": it["source"],
                        "type": it["type"],
                        "summary": it["summary"],
                        "sentiment": it["sentiment"],
                        "themes": ", ".join(it["themes"]) if it["themes"] else "",
                    })
                st.dataframe(rows, use_container_width=True)
            else:
                st.info("No official narrative items extracted (website/X content minimal or unavailable).")

            with st.expander("View raw JSON"):
                st.json(result)
