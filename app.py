import os
import time
from pathlib import Path
from typing import List, Dict
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
PLAYLIST_TOKEN = "/playlist"  # also matches /embed/playlist

# ----------------------- URL normalisation & filters -----------------------
def normalize_url(u: str) -> str:
    if not u:
        return ""
    try:
        p = urlparse(u)
        # Google redirector
        if p.netloc.endswith("google.com") and p.path.startswith("/url"):
            q = parse_qs(p.query)
            for key in ("url", "q", "u"):
                if key in q and q[key]:
                    return q[key][0]
        return u
    except Exception:
        return u

def canonical_spotify_url(u: str) -> str:
    """Normalise Spotify playlist URLs for dedupe."""
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

# ----------------------- Google CSE (simple + resilient) -----------------------
def build_query(terms: List[str]) -> str:
    quoted = [f'"{t.strip()}"' for t in terms if t and t.strip()]
    # Explicitly constrain via query
    return f'site:{SPOTIFY_HOST} inurl:playlist {" ".join(quoted)}'.strip()

@st.cache_data(ttl=3600, show_spinner=False)
def search_google_cse_cached(q: str, page_start: int, page_size: int) -> Dict:
    """Cached single-page call to CSE to avoid repeat hits on reruns."""
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": q,
        "num": page_size,
        "start": page_start,
        "safe": "off",
        "hl": "en",
    }
    with httpx.Client(timeout=20) as client:
        resp = client.get(url, params=params)
        data = {}
        try:
            if resp.headers.get("content-type", "").startswith("application/json"):
                data = resp.json()
        except Exception:
            data = {}
        return {"status": resp.status_code, "headers": dict(resp.headers), "json": data}

def search_google_cse(q: str, pages: int = 3, page_size: int = 10) -> List[Dict]:
    """Fetch up to `pages` pages (defaults to 3 × 10 = 30 results)."""
    out: List[Dict] = []
    starts = [1 + i * page_size for i in range(max(1, pages))]
    for start in starts:
        res = search_google_cse_cached(q, page_start=start, page_size=page_size)
        status = res["status"]
        data = res["json"] if isinstance(res["json"], dict) else {}

        if status == 200:
            items = data.get("items", []) or []
            for it in items:
                out.append({"title": it.get("title"), "url": it.get("link"), "snippet": it.get("snippet")})
            # be gentle between pages (uncached)
            time.sleep(0.15)
            continue

        if status in (429, 403):
            # Rate limit / quota: show a soft warning and stop
            retry_after = res["headers"].get("Retry-After")
            hint = "rate limit hit" if status == 429 else "quota or access denied"
            msg = f"Google CSE {hint}. "
            if retry_after:
                msg += f"Retry-After: {retry_after}s. "
            msg += "Open the query in Google or try again later."
            st.warning(msg)
            break

        # Other status: stop quietly
        break

    return out

def run_search(q: str, pages: int = 3) -> List[Dict]:
    return only_playlist_results(search_google_cse(q, pages=pages))

# ----------------------- UI -----------------------
st.title("Spotify Playlist Finder")
st.write("Enter up to eight terms (artist or song).")  # trimmed as requested

# Spotify-style button theming
st.markdown(
    """
    <style>
      .spotify-btn {
        display: inline-block;
        padding: 0.55rem 0.95rem;
        background: #1DB954;
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

# Static placeholders (no randomness)
placeholders = ["Kanye", "Jamie XX", "Deki Alem", "Favourite", "CASisDEAD", "Capricorn", "Starburster", "Eusexua"]

# Shared heading for both columns
st.markdown('<div class="grid-label">Artist or song</div>', unsafe_allow_html=True)

with st.form("inputs"):
    c1, c2 = st.columns(2)
    with c1:
        st.text_input("Term 1", placeholder=placeholders[0], key="term_1")
        st.text_input("Term 2", placeholder=placeholders[1], key="term_2")
        st.text_input("Term 3", placeholder=placeholders[2], key="term_3")
        st.text_input("Term 4", placeholder=placeholders[3], key="term_4")
    with c2:
        st.text_input("Term 5", placeholder=placeholders[4], key="term_5")
        st.text_input("Term 6", placeholder=placeholders[5], key="term_6")
        st.text_input("Term 7", placeholder=placeholders[6], key="term_7")
        st.text_input("Term 8", placeholder=placeholders[7], key="term_8")

    submitted = st.form_submit_button("Search")

if submitted:
    # Read from session_state so edits never vanish on rerun
    terms = [st.session_state.get(f"term_{i}", "").strip() for i in range(1, 9)]
    terms = [t for t in terms if t]

    if not terms:
        st.warning("Please enter at least one term.")
    else:
        query = build_query(terms)
        st.caption(f"Query: `{query}`")

        results = run_search(query, pages=3)  # 3 pages (30 results max)

        if results:
            st.subheader("Matched playlists")
            btn_html = "".join(
                f'<span class="btn-wrap"><a class="spotify-btn" href="{urllib.parse.quote(r["url"], safe=":/?&=%#")}" target="_blank" rel="noopener">{(r["title"] or "Open playlist")[:80]}</a></span>'
                for r in results
            )
            st.markdown(btn_html, unsafe_allow_html=True)
            st.success(f"Found {len(results)} playlists.")
        else:
            st.info("No results found right now.")
            google_url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
            st.link_button("Open on Google", google_url)
