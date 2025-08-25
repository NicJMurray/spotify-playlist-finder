import os
import random
from pathlib import Path
from typing import List, Dict
from itertools import combinations
import urllib.parse
from urllib.parse import urlparse, parse_qs, unquote

import streamlit as st
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

GOOGLE_API_KEY = (get_secret("GOOGLE_API_KEY") or "").strip()
GOOGLE_CSE_ID  = (get_secret("GOOGLE_CSE_ID") or "").strip()

SPOTIFY_HOST = "open.spotify.com"
PLAYLIST_TOKEN = "/playlist"  # also matches /user/.../playlist/... and /embed/playlist/...

# ----------------------- URL normalisation & filters -----------------------
def normalize_url(u: str) -> str:
    """Unwrap common redirector links (Google) to the real destination."""
    if not u:
        return ""
    try:
        p = urlparse(u)
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
    """Normalise Spotify playlist URLs for dedupe: strip /embed, querystring, trailing slash."""
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
    out = []
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

        key = url.split("?", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "url": key, "snippet": snippet})
    return out

# ----------------------- Google CSE helpers -----------------------
def build_queries(terms: List[str]) -> List[str]:
    """
    Build multiple query variants to increase recall in CSE.
    Strategy:
      1) site: + inurl:playlist + all quoted terms
      2) site: + intitle:playlist + all quoted terms
      3) site: + all quoted terms + the word "playlist"
      4) For ≥3 terms, pairwise combos with base pattern (helps when a page only surfaces two names)
    """
    t = [s.strip() for s in terms if s and s.strip()]
    quoted_all = " ".join(f'"{s}"' for s in t)
    base_site = f"site:{SPOTIFY_HOST}"

    queries = []
    queries.append(f'{base_site} inurl:playlist {quoted_all}'.strip())
    queries.append(f'{base_site} intitle:playlist {quoted_all}'.strip())
    queries.append(f'{base_site} {quoted_all} "playlist"'.strip())

    if len(t) >= 3:
        # cap number of pair queries to avoid excessive calls
        for a, b in list(combinations(t, 2))[:10]:
            queries.append(f'{base_site} inurl:playlist "{a}" "{b}"')

    # de-dup while preserving order
    seen = set()
    uniq = []
    for q in queries:
        if q not in seen:
            uniq.append(q); seen.add(q)
    return uniq

def google_cse_request(q: str, wanted: int = 40) -> List[Dict]:
    """Fetch up to `wanted` results for a single query using pagination."""
    if not (GOOGLE_API_KEY and GOOGLE_CSE_ID):
        return []

    url = "https://www.googleapis.com/customsearch/v1"
    out: List[Dict] = []
    start = 1
    with httpx.Client(timeout=20) as client:
        while len(out) < wanted and start <= 91:
            params = {
                "key": GOOGLE_API_KEY,
                "cx": GOOGLE_CSE_ID,
                "q": q,
                "num": min(10, wanted - len(out)),
                "start": start,
                # Nudges to align with google.com behaviour
                "safe": "off",
                "hl": "en",
                "gl": "uk",
                "filter": 1,
                # Hard include host (works even if your CSE is "entire web"):
                "siteSearch": SPOTIFY_HOST,
                "siteSearchFilter": "i",
            }
            try:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                st.info(f"CSE error at start={start}: {e}")
                break

            items = data.get("items", []) if isinstance(data, dict) else []
            for it in items:
                out.append({
                    "title": it.get("title"),
                    "url": it.get("link"),
                    "snippet": it.get("snippet"),
                })

            if not items:
                break
            start += 10

    return out[:wanted]

def run_google_only(terms: List[str], max_results: int = 50) -> List[Dict]:
    """Run multiple CSE query variants and merge/dedupe results."""
    queries = build_queries(terms)
    merged: List[Dict] = []
    seen_urls = set()

    for q in queries:
        raw = google_cse_request(q, wanted=50)  # fetch up to 50 per variant
        filt = only_playlist_results(raw)
        for r in filt:
            key = r["url"]
            if key in seen_urls:
                continue
            merged.append(r)
            seen_urls.add(key)
            if len(merged) >= max_results:
                return merged
    return merged[:max_results]

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
        # Show the primary query for transparency
        primary_query_preview = f'site:{SPOTIFY_HOST} inurl:playlist ' + " ".join(f'"{t}"' for t in terms)
        st.caption(f"Primary query: `{primary_query_preview}`")

        results = run_google_only(terms, max_results=60)

        if results:
            st.subheader("Matched playlists")
            btn_html = "".join(
                f'<span class="btn-wrap"><a class="spotify-btn" href="{urllib.parse.quote(r["url"], safe=":/?&=%#")}" target="_blank" rel="noopener">{(r["title"] or "Open playlist")[:80]}</a></span>'
                for r in results
            )
            st.markdown(btn_html, unsafe_allow_html=True)
            st.success(f"Found {len(results)} playlists.")
        else:
            st.info("No results found via Google CSE.")
            google_url = "https://www.google.com/search?q=" + urllib.parse.quote(primary_query_preview)
            st.link_button("Open on Google", google_url)
