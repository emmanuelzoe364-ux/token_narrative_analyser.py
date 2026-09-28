import html
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
import streamlit as st

# -------------------------
# Page config
# -------------------------

st.set_page_config(
    page_title="Token Narrative Analyzer",
    page_icon="🧭",
    layout="wide",
)

st.title("Token Narrative Analyzer")
st.caption("Paste a contract address → get narrative + official & community sentiment + on-chain metrics")


# -------------------------
# Helpers: numeric & formatting
# -------------------------

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


# -------------------------
# Interpretable ratio helpers
# -------------------------

def interpret_vol_liq(v: Optional[float]) -> str:
    """Vol/Liq: how many times the pool turns over per day."""
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
    """Vol / MCap: what % of market cap trades per day."""
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
    """Liq / MCap: how much of MCap is backed by liquidity."""
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
    """FDV / Liq: speculation / unlock risk indicator."""
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


# -------------------------
# Helpers: chain detection & address matching
# -------------------------

def detect_chain(contract: str) -> str:
    c = contract.strip()
    if not c.startswith("0x") and 30 <= len(c) <= 48:
        if re.match(r"^[1-9A-HJ-NP-Za-km-z]+$", c):
            return "solana"
    if c.startswith("0x") and len(c) == 42:
        return "evm"
    return "unknown"


def same_address(a: Optional[str], b: Optional[str]) -> bool:
    """EVM addresses are case-insensitive, Solana addresses are not."""
    if not a or not b:
        return False
    a, b = a.strip(), b.strip()
    if a.startswith("0x") or b.startswith("0x"):
        return a.lower() == b.lower()
    return a == b


# -------------------------
# DexScreener fetch
# -------------------------

@st.cache_data(ttl=60, show_spinner=False)
def fetch_token_from_dexscreener(contract: str, chain: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Returns (token_dict, error_message)."""
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

    # Only keep pairs where the contract is the BASE token, otherwise
    # price / name / symbol would describe the other side of the pair.
    base_pairs = [
        p for p in pairs
        if same_address((p.get("baseToken") or {}).get("address"), contract)
    ]
    if not base_pairs:
        return None, "This address only appears as the quote token in its pairs, so its own price can't be read."

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
    age_days = None
    age_months = None
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


# -------------------------
# Website text via jina
# -------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_website_text(url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Returns (text, warning_message)."""
    if not url:
        return None, None
    if not url.startswith("http"):
        url = "https://" + url
    try:
        r = requests.get("https://r.jina.ai/" + url, timeout=25)
        r.raise_for_status()
        return r.text[:40000], None
    except Exception as e:
        return None, f"Website fetch failed: {e}"


# -------------------------
# Text cleaning, sentence splitting, extraction
# -------------------------

def clean_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)      # links -> label
    text = re.sub(r"https?://\S+", "", text)                  # bare URLs
    return text


def split_into_sentences(text: str) -> List[str]:
    """Split line by line first (so headings don't glue onto the next paragraph),
    then on sentence punctuation. Drops fragments that are too short or too long."""
    sentences: List[str] = []
    for line in text.splitlines():
        line = line.lstrip("#*->• ").strip()
        if not line:
            continue
        for part in re.split(r"(?<=[.!?])\s+", line):
            part = part.strip()
            if 25 <= len(part) <= 400:
                sentences.append(part)
    return sentences


def top_sentences(sentences: List[str], rx: "re.Pattern", n: int = 2) -> str:
    scored = [(len(rx.findall(s)), idx, s) for idx, s in enumerate(sentences)]
    scored = [x for x in scored if x[0] > 0]
    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], x[1]))
    return " ".join(s for _, _, s in scored[:n])


# Word-boundary patterns (no more "ai" matching "said", "about" matching every site, etc.)
ROADMAP_RE = re.compile(
    r"\b(?:roadmap|milestones?|phase\s*\d+|q[1-4]\s*(?:20)?2\d|coming soon|upcoming)\b", re.I
)
PRODUCT_RE = re.compile(
    r"\b(?:app|platform|beta|mainnet|testnet|launched|protocol|dashboard|leaderboard|product|sdk|api)\b", re.I
)
LIVE_RE = re.compile(r"\b(?:live|mainnet|launched)\b", re.I)
NARRATIVE_RE = re.compile(
    r"\b(?:our mission|our vision|mission|vision|built by|founded|created by|origin|our story|we are building|we're building)\b",
    re.I,
)
EXCHANGE_RE = re.compile(
    r"\b(?:listed on|binance|kucoin|gate\.io|coinbase|bybit|okx|mexc|raydium|uniswap|jupiter|pancakeswap|buy on|trade on)\b",
    re.I,
)

CATEGORIES = [
    ("roadmap", "Future", ROADMAP_RE),
    ("product", None, PRODUCT_RE),
    ("narrative", "Past", NARRATIVE_RE),
    ("exchange", "Current", EXCHANGE_RE),
]

THEME_PATTERNS = {
    "AI": re.compile(r"\b(?:ai|agents?|gpt|claude|grok|llm)\b", re.I),
    "trading": re.compile(r"\b(?:trade|trading|dex|swap)\b", re.I),
    "education": re.compile(r"\b(?:education|tutorials?|learn|course)\b", re.I),
    "gaming": re.compile(r"\b(?:games?|gaming|quests?|nfts?)\b", re.I),
    "DeFi": re.compile(r"\b(?:defi|yield|staking|farming)\b", re.I),
    "meme": re.compile(r"\b(?:memes?|memecoin|dogs?|doge|cats?|pepe)\b", re.I),
}


def infer_themes(text: str) -> List[str]:
    return [name for name, rx in THEME_PATTERNS.items() if rx.search(text)]


# -------------------------
# Sentiment (shared by official items and community posts)
# -------------------------

POSITIVE_WORDS = {
    "bullish", "moon", "pump", "buy", "undervalued", "gem", "alpha", "love",
    "great", "awesome", "amazing", "strong", "confident", "hold", "hodl",
}
NEGATIVE_WORDS = {
    "bearish", "dump", "scam", "rug", "rugged", "down", "sell", "weak", "trash",
    "hate", "bad", "terrible", "worst", "loss", "rekt", "hack", "hacked",
    "exploit", "exploited", "sorry",
}
NEGATORS = {"not", "no", "never", "isn't", "isnt", "aint", "ain't", "don't", "dont", "without", "hardly"}
NEGATIVE_PHRASES = ["exit scam", "exit liquidity", "rug pull"]


def sentiment_counts(text: str) -> Tuple[int, int]:
    """Returns (positive_hits, negative_hits). Whole words only, with simple negation
    handling ("not a scam" counts as positive). Known negative phrases count once."""
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


# -------------------------
# Website narrative items
# -------------------------

def extract_website_narrative_items(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []
    sentences = split_into_sentences(clean_markdown(text))
    items = []
    for typ, period, rx in CATEGORIES:
        summary = top_sentences(sentences, rx)
        if not summary:
            continue
        if typ == "product":
            period = "Current" if LIVE_RE.search(summary) else "Unclear"
        items.append({
            "source": "website",
            "type": typ,
            "date_period": period,
            "summary": summary,
            "sentiment": classify_post_sentiment(summary),
            "themes": infer_themes(summary),
        })
    return items


# -------------------------
# X profile fetch (best effort, X often blocks this)
# -------------------------

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
    """Returns (profile, warning_message)."""
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


def bio_to_narrative_item(bio: str, source: str) -> Dict[str, Any]:
    return {
        "source": source,
        "type": "narrative",
        "date_period": "Current",
        "summary": bio,
        "sentiment": classify_post_sentiment(bio),
        "themes": infer_themes(bio),
    }


# -------------------------
# ONS (Official Narrative Score)
# -------------------------

def compute_ons(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Score 0..1, or None when there is nothing to score.
    - coverage: how many of narrative/product/roadmap/exchange are present (0-4)
    - alignment: do website and X share a theme? (None if X side is missing)
    - tone: 1 - share of official items with negative sentiment
    Weights are renormalised when alignment is unavailable."""
    if not items:
        return {"score": None, "coverage": 0, "alignment": None, "tone": None}

    types = {i["type"] for i in items}
    coverage = sum(1 for t in ("narrative", "product", "roadmap", "exchange") if t in types)

    web_items = [i for i in items if i["source"] == "website"]
    x_items = [i for i in items if i["source"].startswith("x_")]
    alignment: Optional[int] = None
    if web_items and x_items:
        web_themes = {t for i in web_items for t in i["themes"]}
        x_themes = {t for i in x_items for t in i["themes"]}
        alignment = 1 if (web_themes & x_themes) else 0

    neg_share = sum(1 for i in items if i["sentiment"] == "negative") / len(items)
    tone = 1.0 - neg_share

    parts = [(0.5, coverage / 4.0), (0.25, tone)]
    if alignment is not None:
        parts.append((0.25, float(alignment)))
    score = sum(w * v for w, v in parts) / sum(w for w, _ in parts)

    return {"score": score, "coverage": coverage, "alignment": alignment, "tone": tone}


# -------------------------
# Main analyzer
# -------------------------

def analyze_token(contract: str, chain: Optional[str] = None, x_bio_manual: str = "") -> Dict[str, Any]:
    contract = contract.strip()
    chain_eff = chain if chain and chain != "auto" else detect_chain(contract)

    token, err = fetch_token_from_dexscreener(contract, chain_eff)
    if err or not token:
        return {"error": err or "Token not found", "contract": contract, "chain_detected": chain_eff}

    warnings: List[str] = []

    website_text, w = fetch_website_text(token["website"])
    if w:
        warnings.append(w)
    website_items = extract_website_narrative_items(website_text) if website_text else []

    x_items: List[Dict[str, Any]] = []
    if x_bio_manual.strip():
        x_items.append(bio_to_narrative_item(x_bio_manual.strip(), "x_manual"))
    else:
        handle = extract_twitter_handle(token["twitter"])
        if handle:
            profile, w = fetch_x_profile(handle)
            if w:
                warnings.append(w)
            if profile and profile.get("bio"):
                x_items.append(bio_to_narrative_item(profile["bio"], "x_official"))

    all_items = website_items + x_items
    return {
        "token": token,
        "narrative_items": all_items,
        "ons": compute_ons(all_items),
        "chain_detected": chain_eff,
        "warnings": warnings,
    }


# -------------------------
# Streamlit UI
# -------------------------

contract = st.text_input(
    "Contract address",
    placeholder="e.g. 2PENPmfgJfq6CG3k4byj4oWwHf8SerqakmYHMkUupump",
)
chain = st.selectbox("Chain", ["auto", "solana", "evm"])

x_bio_manual = st.text_area(
    "X bio (optional)",
    help="X usually blocks automatic reading. Paste the project's X bio here to include it in the official score.",
    height=70,
)

community_posts_text = st.text_area(
    "Community posts (optional)",
    help="Paste community posts (one per line) from X/Telegram to compute Community Sentiment Score (CSS).",
    placeholder="""familiars looking strong
this is a scam
love the agent leaderboard
exit liquidity
""",
    height=120,
)

if st.button("Analyze token"):
    if not contract.strip():
        st.warning("Please enter a contract address.")
    else:
        with st.spinner("Fetching market data and analyzing narrative..."):
            result = analyze_token(contract, chain, x_bio_manual)

        community_posts = [
            line.strip() for line in community_posts_text.splitlines() if line.strip()
        ]
        css = compute_css_from_posts(community_posts)

        if "error" in result:
            st.error(result["error"])
        else:
            for w in result.get("warnings", []):
                st.warning(w)

            token = result["token"]
            items = result["narrative_items"]
            ons = result["ons"]

            st.subheader(f"{token['name'] or 'Unknown'} ({token['symbol'] or '?'})")

            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.metric("Price (USD)", fmt_price_usd(token["price_usd"]))
            with c2:
                st.metric("Liquidity", fmt_usd_compact(token["liquidity_usd"]))
            with c3:
                st.metric("24h Volume (main pair)", fmt_usd_compact(token["volume_24h_usd"]))
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
                st.metric("MCap / FDV", f"{mf * 100:.0f}% in circulation" if mf is not None else "N/A")
            with c12:
                st.metric("Vol / MCap (24h)", interpret_vol_mcap(token["vol_mcap_24h"]))
            with c13:
                st.metric("Liq / MCap", interpret_liq_mcap(token["liq_mcap_ratio"]))

            st.caption(
                f"Chain: {token['chain']} | Detected: {result['chain_detected']} | "
                f"Pairs found: {token['pairs_found']} (main pair = highest liquidity) | "
                f"DexScreener: [link]({token['dex_url']})"
            )

            with st.expander("How to read these metrics"):
                st.markdown(
                    """
- **Vol/Liq**: how many times the liquidity pool turns over per day. Higher = more speculative.
- **Vol / MCap**: what percentage of the market cap trades in 24h. Very high often means heavy speculation or low float.
- **Liq / MCap**: what percentage of market cap is backed by liquidity. Very low can mean easy manipulation.
- **FDV / Liq**: fully diluted value vs liquidity. Very high = large unlocked supply could pressure price.
- **Volume and liquidity** are for the main DexScreener pair only. Sites like CoinMarketCap aggregate many pairs and exchanges, so their volume is usually higher.
- **Holder count / top-10 %** are not available from DexScreener; use an explorer (Solscan, Etherscan) or Arkham.
"""
                )

            st.subheader("Sentiment Scores")
            c_oss, c_css = st.columns(2)

            with c_oss:
                oss_str = f"{ons['score']:.2f} / 1.00" if ons["score"] is not None else "N/A"
                st.metric("Official Sentiment Score (OSS)", oss_str)
                if ons["score"] is not None:
                    if ons["alignment"] is None:
                        align_str = "n/a (no X bio)"
                    else:
                        align_str = "yes" if ons["alignment"] else "no"
                    st.caption(
                        f"Coverage {ons['coverage']}/4 | Web-X theme alignment: {align_str} | "
                        f"Tone {ons['tone']:.2f}. Higher = clearer, more consistent official story."
                    )
                else:
                    st.caption("No official content could be read (website / X unavailable).")

            with c_css:
                css_str = f"{css:.2f} / 1.00" if css is not None else "N/A"
                st.metric("Community Sentiment Score (CSS)", css_str)
                st.caption(
                    "Based on pasted community posts. "
                    "1.00 = fully positive, 0.00 = fully negative, 0.50 = neutral."
                )

            if community_posts:
                counts = {"positive": 0, "neutral": 0, "negative": 0}
                for p in community_posts:
                    counts[classify_post_sentiment(p)] += 1
                st.caption(
                    f"Posts analyzed: {len(community_posts)} | "
                    f"Positive: {counts['positive']}, Neutral: {counts['neutral']}, Negative: {counts['negative']}"
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
                        "themes": ", ".join(it["themes"]),
                    })
                try:
                    st.dataframe(rows, width="stretch")
                except TypeError:
                    st.dataframe(rows, use_container_width=True)
            else:
                st.info("No official narrative items extracted (website/X content minimal or unavailable).")

            with st.expander("View raw JSON"):
                st.json(result)
