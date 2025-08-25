import os
import re
import random
from pathlib import Path
from typing import List, Dict
import urllib.parse
from urllib.parse import urlparse, parse_qs, unquote

import streamlit as st
from duckduckgo_search import DDGS
import httpx
from dotenv import load_dotenv

# ── Streamlit must be first call ────────────────────────────────────────────────
st.set_page_config(page_title="Spotify Playlist Finder", layout="centered")
# ───────────────────────────────────────────────────────────────────────────────

# Load local .env for dev
load_dotenv()

# Avoid noisy secrets warning: only touch st.secrets if a file exists
def _secrets_available() -> bool:
    home = Path.home() / ".streamlit" / "secrets.toml"
    local = Path(".streamlit/secrets.toml")
    return home.exists() or local.exists()

def get_secret(key: str) -> str:
    if _secrets_available():
        try:
            return st.secrets.get(key, "")
        except Exception:
            pass
    return os.getenv(key, "")

GOOGLE_API_KEY = get_secret("GOOGLE_API_KEY").strip()
GOOGLE_CSE_ID  = get_secret("GOOGLE_CSE_ID").strip()

SPOTIFY_HOST = "open.spotify.com"
PLAYLIST_TOKEN = "/playlist"  # also matches /embed/playlist

# ----------------------- URL normalisation & filters -----------------------
def normalize_url(u: str) -> str:
    """Unwrap common redirector links (DuckDuckGo, Google) to the real destination."""
    if not u:
        return ""
    try:
        p = urlparse(u)

        # DuckDuckGo redirector: https://duckduckgo.com/l/?uddg=<encoded>
        if p.netloc.endswith("duckduckgo.com") and p.path.startswith("/l/"):
            q = parse_qs(p.query)
            if "uddg" in q and q["uddg"]:
                return unquote(q["uddg"][0])

        # Google redirector: https://www.google.com/url?url=... (or q/u)
        if p.netloc.endswith("google.com") and p.path.startswith("/url"):
            q = parse_qs(p.query)
            for key in ("url", "q", "u"):
                if key in q and q[key]:
                    return q[key][0]

        return u
    except Exception:
        return u

def canonical_spotify_url(u: str) -> str:
    """Normalise Spotify playlist URLs for dedupe: strip /embed, strip query, strip trailing slash."""
    try:
        u = normalize_url(u)
        p = urlparse(u)
        if SPOTIFY_HOST not in p.netloc:
            return u
        path = p.path
        if path.startswith("/embed/"):
            path = path.replace("/embed", "", 1)  # /embed/playlist/... -> /playlist/...
        path = path.rstrip("/")
        return f"https://{SPOTIFY_HOST}{path}"
    except Exception:
        return u

def only_playlist_results(results: List[Dict]) -> List[Dict]:
    seen = set()
    filtered = []
    for r in results:
        raw_url = r.get("url") or r.get("link") or r.get("href") or ""
        url = canonical_spotify_url(raw_url)
        title = (r.get("title") or r.get("name") or "") or url
        snippet = r.get("snippet") or r.get("body") or ""

        if not url:
            continue
        p = urlparse(url)
        if SPOTIFY_HOST not in p.netloc or PLAYLIST_TOKEN not in p.path:
            continue

        key = url.split("?", 1)[0]  # drop tracking params
        if key in seen:
            continue
        seen.add(key)
        filtered.append({"title": title, "url": key, "snippet": snippet})
    return filtered

# ----------------------- Search helpers -----------------------
def build_query(terms: List[str]) -> str:
    quoted = [f'"{t.strip()}"' for t in terms if t and t.strip()]
    base = f"site:{SPOTIFY_HOST} inurl:playlist"
    return f"{base} {' '.join(quoted)}".strip()

def search_duckduckgo(q: str, max_results: int = 40) -> List[Dict]:
    out = []
    with DDGS() as ddgs:
        # Be explicit about region/safesearch to maximise recall
        for r in ddgs.text(q, region="uk-en", safesearch="off", max_results=max_results):
            out.append({"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")})
    return out

def search_google_cse(q: str, max_results: int = 40) -> List[Dict]:
    if not (GOOGLE_API_KEY and GOOGLE_CSE_ID):
        return []
    url = "https://www.googleapis.com/customsearch/v1"
    out = []
    start = 1
    with httpx.Client(timeout=20) as client:
        while len(out) < max_results and start <= 91:
            params = {
                "key": GOOGLE_API_KEY,
                "cx": GOOGLE_CSE_ID,
                "q": q,
                "num": min(10, max_results - len(out)),
                "start": start,
                # Nudge CSE closer to what you see on google.com:
                "safe": "off",
                "hl": "en",
                "gl": "uk",
                "filter": 1,  # remove near duplicates
                "siteSearch": SPOTIFY_HOST,       # hard include
                "siteSearchFilter": "i",
            }
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", []) if isinstance(data, dict) else []
            for it in items:
                out.append({"title": it.get("title"), "url": it.get("link"), "snippet": it.get("snippet")})
            if not items:
                break
            start += 10
    return out[:max_results]

def merge_unique(primary: List[Dict], secondary: List[Dict], limit: int) -> List[Dict]:
    """Merge two result lists, de-duplicating by canonical Spotify URL."""
    seen = {r["url"].split("?", 1)[0] for r in primary}
    for r in secondary:
        key = r["url"].split("?", 1)[0]
        if key not in seen:
            primary.append(r)
            seen.add(key)
        if len(primary) >= limit:
            break
    return primary[:limit]

def run_search(q: str, max_results: int = 40) -> List[Dict]:
    results: List[Dict] = []
    # Try Google CSE first
    try:
        if GOOGLE_API_KEY and GOOGLE_CSE_ID:
            results = only_playlist_results(search_google_cse(q, max_results))
    except Exception as e:
        st.info(f"Google CSE failed: {e}")

    # Top up with DuckDuckGo if CSE is sparse
    if len(results) < max_results:
        try:
            ddg = only_playlist_results(search_duckduckgo(q, max_results))
            results = merge_unique(results, ddg, max_results)
        except Exception as e:
            st.info(f"DuckDuckGo failed: {e}")

    return results[:max_results]

# ----------------------- UI -----------------------
st.title("Spotify Playlist Finder")
st.write("Enter up to eight terms (artist or song). We’ll look for Spotify playlists likely containing them.")

# Spotify-style button theming
st.markdown(
    """
    <style>
      .spotify-btn {
        display: inline-block;
        padding: 0.55rem 0.95rem;
        background: #1DB954; /* Spotify green */
        color: #000;
        border-radius: 9999px;
        text-decoration: none !important;
        font-weight: 600;
        line-height: 1;
        border: 0;
        transition: background 0.15s ease-in-out, transform 0.05s ease-in-out;
        box-shadow: 0 2px 8px rgba(0,0,0,0.15);
      }
      .spotify-btn:hover { background: #1ed760; }
      .spotify-btn:active { transform: scale(0.98); }
      .btn-wrap { margin: 0.25rem 0.35rem 0.25rem 0; display: inline-block; }
      .grid-label { font-weight: 600; margin: 0.5rem 0 0.25rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Randomised placeholders for the 8 boxes (stable across reruns)
examples = ["Kanye", "Jamie XX", "Deki Alem", "Favourite", "CASisDEAD", "Capricorn", "Starburster", "Eusexua"]
if "placeholders" not in st.session_state:
    st.session_state.placeholders = random.sample(examples, k=len(examples))
placeholders = st.session_state.placeholders

# Shared heading for both columns
st.markdown('<div class="grid-label">Artist or song</div>', unsafe_allow_html=True)

with st.form("inputs"):
    c1, c2 = st.columns(2)
    with c1:
        term1 = st.text_input("Term 1", placeholder=placeholders[0], key="term_1")
        term2 = st.text_input("Term 2", placeholder=placeholders[1], key="term_2")
        term3 = st.text_input("Term 3", placeholder=placeholders[2], key="term_3")
        term4 = st.text_input("Term 4", placeholder=placeholders[3], key="term_4")
    with c2:
        term5 = st.text_input("Term 5", placeholder=placeholders[4], key="term_5")
        term6 = st.text_input("Term 6", placeholder=placeholders[5], key="term_6")
        term7 = st.text_input("Term 7", placeholder=placeholders[6], key="term_7")
        term8 = st.text_input("Term 8", placeholder=placeholders[7], key="term_8")

    submitted = st.form_submit_button("Search")

if submitted:
    # Read from session_state to avoid transient empty reads on rerun
    terms = [st.session_state.get(f"term_{i}", "").strip() for i in range(1, 9)]
    terms = [t for t in terms if t]

    if not terms:
        st.warning("Please enter at least one term.")
    else:
        query = build_query(terms)
        st.caption(f"Query: `{query}`")

        results = run_search(query, max_results=40)

        if results:
            st.subheader("Matched playlists")
            btn_html = "".join(
                f'<span class="btn-wrap"><a class="spotify-btn" href="{urllib.parse.quote(r["url"], safe=":/?&=%#")}" target="_blank" rel="noopener">{(r["title"] or "Open playlist")[:80]}</a></span>'
                for r in results
            )
            st.markdown(btn_html, unsafe_allow_html=True)
            st.success(f"Found {len(results)} playlists.")
        else:
            st.info("No results found via configured backends.")
            google_url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
            st.link_button("Open on Google", google_url)
