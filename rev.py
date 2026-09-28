import os
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
# Small formatting / math helpers
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


def fmt_pct(x, decimals: int = 0) -> str:
    v = to_float(x)
    if v is None:
        return "N/A"
    return f"{v * 100:.{decimals}f}%"


def num(x) -> float:
    v = to_float(x)
    return v if v is not None else 0.0


def safe_div(a, b) -> Optional[float]:
    a = to_float(a)
    b = to_float(b)
    if a is None or b is None or b <= 0:
        return None
    return a / b


def positive_or_none(x) -> Optional[float]:
    v = to_float(x)
    if v is None or v <= 0:
        return None
    return v


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
# DexScreener: fetch ALL pools for the token, then aggregate
# -------------------------

DEX_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"
DEX_PAIR_CAP = 30  # DexScreener returns at most ~30 pairs per token lookup


def pair_liq(p: Dict[str, Any]) -> float:
    return num((p.get("liquidity") or {}).get("usd"))


def pair_vol(p: Dict[str, Any], window: str) -> float:
    return num((p.get("volume") or {}).get(window))


def same_address(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    a = a.strip()
    b = b.strip()
    if a.startswith("0x") or b.startswith("0x"):
        return a.lower() == b.lower()
    return a == b


def fetch_pairs(contract: str) -> List[Dict[str, Any]]:
    r = requests.get(DEX_TOKENS_URL + contract.strip(), timeout=20)
    r.raise_for_status()
    pairs = r.json().get("pairs") or []
    seen = set()
    unique = []
    for p in pairs:
        key = (p.get("chainId"), p.get("pairAddress"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def aggregate_token(contract: str, pairs: List[Dict[str, Any]], chain_hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not pairs:
        return None

    # Optional filter by the chain family the user selected
    if chain_hint == "solana":
        filtered = [p for p in pairs if p.get("chainId") == "solana"]
    elif chain_hint == "evm":
        filtered = [p for p in pairs if p.get("chainId") != "solana"]
    else:
        filtered = pairs
    if filtered:
        pairs = filtered

    # Only count pools where this token is the base token (price/mcap/fdv refer to the base token)
    token_pairs = [p for p in pairs if same_address((p.get("baseToken") or {}).get("address"), contract)]
    quote_only = False
    if not token_pairs:
        token_pairs = [p for p in pairs if same_address((p.get("quoteToken") or {}).get("address"), contract)]
        quote_only = bool(token_pairs)
    if not token_pairs:
        token_pairs = pairs

    # The same address can exist on several chains: keep the chain with the deepest liquidity
    by_chain: Dict[str, List[Dict[str, Any]]] = {}
    for p in token_pairs:
        by_chain.setdefault(p.get("chainId") or "unknown", []).append(p)
    chain_id = max(by_chain, key=lambda c: sum(pair_liq(p) for p in by_chain[c]))
    ps = sorted(by_chain[chain_id], key=pair_liq, reverse=True)
    best = ps[0]

    tok = (best.get("quoteToken") if quote_only else best.get("baseToken")) or {}

    # Aggregates across all pools
    liq_total = sum(pair_liq(p) for p in ps)
    vol_h1 = sum(pair_vol(p, "h1") for p in ps)
    vol_h6 = sum(pair_vol(p, "h6") for p in ps)
    vol_h24 = sum(pair_vol(p, "h24") for p in ps)

    buys_24 = 0
    sells_24 = 0
    for p in ps:
        h24 = (p.get("txns") or {}).get("h24") or {}
        buys_24 += int(num(h24.get("buys")))
        sells_24 += int(num(h24.get("sells")))

    # Market cap / FDV are token-level; take the deepest pool's value, else any pool that has one
    mcap = None
    fdv = None
    for p in ps:
        if mcap is None:
            mcap = positive_or_none(p.get("marketCap"))
        if fdv is None:
            fdv = positive_or_none(p.get("fdv"))
    if quote_only:
        mcap = None
        fdv = None

    # Website / socials can be attached to only some pools
    website = None
    twitter = None
    telegram = None
    for p in ps:
        info = p.get("info") or {}
        websites = info.get("websites") or []
        socials = info.get("socials") or []
        if website is None and websites:
            website = websites[0].get("url")
        for s in socials:
            t = (s.get("type") or "").lower()
            u = s.get("url") or ""
            if twitter is None and (t == "twitter" or "x.com" in u or "twitter.com" in u):
                twitter = u
            if telegram is None and t == "telegram":
                telegram = u

    # Age = earliest pool creation time across all pools (closer to the token's real age)
    created_list = [p.get("pairCreatedAt") for p in ps if isinstance(p.get("pairCreatedAt"), (int, float)) and p.get("pairCreatedAt") > 0]
    created_ms = min(created_list) if created_list else None
    age_days = None
    age_months = None
    if created_ms:
        age_days = (time.time() * 1000 - created_ms) / 86400000.0
        age_months = age_days / 30.44

    # Pool table
    pools = []
    for p in ps[:10]:
        v24 = pair_vol(p, "h24")
        pools.append({
            "dex": p.get("dexId"),
            "quote": (p.get("quoteToken") or {}).get("symbol"),
            "liquidity_usd": round(pair_liq(p), 2),
            "volume_24h_usd": round(v24, 2),
            "share_of_volume": round(v24 / vol_h24, 3) if vol_h24 > 0 else None,
            "url": p.get("url"),
        })

    dex_count = len({p.get("dexId") for p in ps if p.get("dexId")})
    top_pool_share = (pair_vol(best, "h24") / vol_h24) if vol_h24 > 0 else None

    return {
        "symbol": tok.get("symbol"),
        "name": tok.get("name"),
        "address": tok.get("address") or contract.strip(),
        "chain": chain_id,
        "price_usd": None if quote_only else best.get("priceUsd"),
        "liquidity_usd": liq_total,
        "volume_24h_usd": vol_h24,
        "volume_6h_usd": vol_h6,
        "volume_1h_usd": vol_h1,
        "market_cap_usd": mcap,
        "fdv_usd": fdv,
        "pair_created_ts": created_ms,
        "website": website,
        "twitter": twitter,
        "telegram": telegram,
        "dex_url": best.get("url"),
        # Age
        "age_days": age_days,
        "age_months": age_months,
        # Vol/Liq on real DexScreener windows (all pools combined)
        "vol_liq_1h": safe_div(vol_h1, liq_total),
        "vol_liq_6h": safe_div(vol_h6, liq_total),
        "vol_liq_24h": safe_div(vol_h24, liq_total),
        # FDV ratios
        "fdv_liq_ratio": safe_div(fdv, liq_total),
        "mcap_fdv_ratio": safe_div(mcap, fdv),
        # Vol/Mcap & Liq/Mcap
        "vol_mcap_24h": safe_div(vol_h24, mcap),
        "liq_mcap_ratio": safe_div(liq_total, mcap),
        # Transparency
        "pool_count": len(ps),
        "dex_count": dex_count,
        "top_pool_volume_share": top_pool_share,
        "top_pool_volume_24h": pair_vol(best, "h24"),
        "top_pool_liquidity": pair_liq(best),
        "buys_24h": buys_24,
        "sells_24h": sells_24,
        "pairs_returned_by_api": len(pairs),
        "possibly_capped": len(pairs) >= DEX_PAIR_CAP,
        "token_is_quote_only": quote_only,
        "pools": pools,
    }


# -------------------------
# CoinGecko cross-check (includes CEX volume, only for listed tokens)
# -------------------------

CG_PLATFORMS = {
    "ethereum": "ethereum",
    "bsc": "binance-smart-chain",
    "base": "base",
    "arbitrum": "arbitrum-one",
    "polygon": "polygon-pos",
    "avalanche": "avalanche",
    "optimism": "optimistic-ethereum",
    "solana": "solana",
    "fantom": "fantom",
    "cronos": "cronos",
    "linea": "linea",
    "blast": "blast",
    "sui": "sui",
    "ton": "the-open-network",
    "tron": "tron",
    "pulsechain": "pulsechain",
    "sonic": "sonic",
    "zksync": "zksync",
}


def get_cg_key() -> Optional[str]:
    key = os.environ.get("COINGECKO_API_KEY")
    if key:
        return key
    try:
        return st.secrets.get("COINGECKO_API_KEY")
    except Exception:
        return None


def fetch_coingecko(chain_id: Optional[str], address: Optional[str]) -> Dict[str, Any]:
    platform = CG_PLATFORMS.get(chain_id or "")
    if not platform or not address:
        return {"status": "unsupported_chain"}
    addr = address.lower() if address.startswith("0x") else address
    url = f"https://api.coingecko.com/api/v3/coins/{platform}/contract/{addr}"
    headers = {"accept": "application/json"}
    key = get_cg_key()
    if key:
        headers["x-cg-demo-api-key"] = key
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 404:
            return {"status": "not_listed"}
        if r.status_code == 429:
            return {"status": "rate_limited"}
        r.raise_for_status()
        j = r.json()
        md = j.get("market_data") or {}
        return {
            "status": "ok",
            "id": j.get("id"),
            "volume_24h": to_float((md.get("total_volume") or {}).get("usd")),
            "market_cap": to_float((md.get("market_cap") or {}).get("usd")),
            "fdv": to_float((md.get("fully_diluted_valuation") or {}).get("usd")),
            "price": to_float((md.get("current_price") or {}).get("usd")),
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


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

    try:
        pairs = fetch_pairs(contract)
    except Exception as e:
        return {
            "error": f"DexScreener error: {e}",
            "contract": contract,
            "chain_detected": chain,
        }

    token = aggregate_token(contract, pairs, chain if chain in ("solana", "evm") else None)
    if not token:
        return {
            "error": "Token not found or no liquidity on DexScreener",
            "contract": contract,
            "chain_detected": chain,
        }

    coingecko = fetch_coingecko(token["chain"], token["address"])

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
        "coingecko": coingecko,
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
            cg = result.get("coingecko") or {}
            items = result["narrative_items"]
            ons = result["ons"]

            st.subheader(f"{token['name']} ({token['symbol']})")
            st.caption(
                f"Market data is combined across {token['pool_count']} pool(s) on "
                f"{token['dex_count']} DEX(es) on {token['chain']}."
            )

            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.metric("Price (USD)", fmt_usd(token["price_usd"], 8))
            with c2:
                st.metric("Liquidity (all pools)", fmt_usd(token["liquidity_usd"]))
            with c3:
                st.metric("24h Volume (all pools)", fmt_usd(token["volume_24h_usd"]))
            with c4:
                st.metric("Market Cap (USD)", fmt_usd(token["market_cap_usd"]))

            # Age & vol/liq on real windows
            c5, c6, c7, c8 = st.columns(4)
            with c5:
                age_m_str = f"{token['age_months']:.1f} months" if token["age_months"] else "N/A"
                st.metric("Age (oldest pool)", age_m_str)
            with c6:
                st.metric("Vol/Liq (1h)", fmt_ratio(token["vol_liq_1h"]))
            with c7:
                st.metric("Vol/Liq (6h)", fmt_ratio(token["vol_liq_6h"]))
            with c8:
                st.metric("Vol/Liq (24h)", fmt_ratio(token["vol_liq_24h"]))

            # FDV & ratios
            c9, c10, c11, c12 = st.columns(4)
            with c9:
                st.metric("FDV (USD)", fmt_usd(token["fdv_usd"]))
            with c10:
                st.metric("FDV / Liq", fmt_ratio(token["fdv_liq_ratio"]))
            with c11:
                st.metric("MCap / FDV", fmt_ratio(token["mcap_fdv_ratio"]))
            with c12:
                st.metric("Pools / DEXs", f"{token['pool_count']} / {token['dex_count']}")

            # Vol/Mcap & Liq/Mcap
            c13, c14, c15, c16 = st.columns(4)
            with c13:
                st.metric("Vol / MCap (24h)", fmt_ratio(token["vol_mcap_24h"]))
            with c14:
                st.metric("Liq / MCap", fmt_ratio(token["liq_mcap_ratio"]))
            with c15:
                st.metric("Holder Count", "N/A")
            with c16:
                st.metric("Top 10 Holders %", "N/A")

            # Trade flow
            c17, c18, c19, c20 = st.columns(4)
            with c17:
                st.metric("Buys (24h)", f"{token['buys_24h']:,}")
            with c18:
                st.metric("Sells (24h)", f"{token['sells_24h']:,}")
            with c19:
                bs = safe_div(token["buys_24h"], token["sells_24h"])
                st.metric("Buy/Sell ratio", fmt_ratio(bs))
            with c20:
                st.metric("Top pool share of volume", fmt_pct(token["top_pool_volume_share"]))

            st.caption(
                f"Chain: {token['chain']} | Detected: {result['chain_detected']} | "
                f"Deepest pool on DexScreener: [link]({token['dex_url']})"
            )

            if token["possibly_capped"]:
                st.warning(
                    "DexScreener returned its maximum number of pairs for this token, "
                    "so totals may still be slightly under-counted."
                )
            if token["token_is_quote_only"]:
                st.warning(
                    "This address only appears as the quote token in the pools found, "
                    "so price, market cap and FDV are not shown."
                )

            # CoinGecko cross-check
            st.subheader("Cross-check: CoinGecko (includes CEX volume)")
            status = cg.get("status")
            if status == "ok":
                g1, g2, g3 = st.columns(3)
                with g1:
                    st.metric("CoinGecko 24h Volume", fmt_usd(cg.get("volume_24h")))
                with g2:
                    st.metric("CoinGecko Market Cap", fmt_usd(cg.get("market_cap")))
                with g3:
                    st.metric("CoinGecko FDV", fmt_usd(cg.get("fdv")))
                cg_vol = to_float(cg.get("volume_24h"))
                dex_vol = to_float(token["volume_24h_usd"])
                if cg_vol and dex_vol is not None and cg_vol > dex_vol * 1.25:
                    st.info(
                        "CoinGecko volume is higher than the on-chain DEX total because it also "
                        "counts trading on centralized exchanges, which DexScreener does not see."
                    )
            elif status == "not_listed":
                st.info("Not listed on CoinGecko (common for new or micro-cap tokens). Only DEX volume is available.")
            elif status == "rate_limited":
                st.warning("CoinGecko rate limit hit. Try again in a minute, or set COINGECKO_API_KEY (free demo key).")
            elif status == "unsupported_chain":
                st.info("CoinGecko cross-check is not available for this chain.")
            else:
                st.warning(f"CoinGecko cross-check failed: {cg.get('detail', 'unknown error')}")

            st.caption(
                "Volume and liquidity are summed across every DexScreener pool for the token. "
                "1h/6h/24h are DexScreener's own windows. Historical 7d/30d/6m averages and holder "
                "metrics need a history or on-chain data source."
            )

            with st.expander("Pools counted (top 10 by liquidity)"):
                st.dataframe(token["pools"], use_container_width=True)

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
