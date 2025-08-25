import os
from pathlib import Path
import urllib.parse
from typing import List, Dict

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
GOOGLE_CSE_ID = get_secret("GOOGLE_CSE_ID").strip()

SPOTIFY_PLAYLIST_HOST = "open.spotify.com"
SPOTIFY_PLAYLIST_PATH = "/playlist/"

# ----------------------- Search helpers -----------------------
def build_query(artists: List[str], songs: List[str]) -> str:
    terms: List[str] = []
    for a in artists:
        a = a.strip()
        if a:
            terms.append(f'"{a}"')
    for s in songs:
        s = s.strip()
        if s:
            terms.append(f'"{s}"')
    term_str = " ".join(terms)
    base = f"site:{SPOTIFY_PLAYLIST_HOST} inurl:playlist"
    return f"{base} {term_str}".strip()

def only_playlist_results(results: List[Dict]) -> List[Dict]:
    filtered = []
    for r in results:
        url = r.get("url") or r.get("link") or r.get("href")
        title = r.get("title") or r.get("name") or ""
        snippet = r.get("snippet") or r.get("body") or ""
        if url and SPOTIFY_PLAYLIST_HOST in url and SPOTIFY_PLAYLIST_PATH in url:
            filtered.append({"title": title or url, "url": url, "snippet": snippet})
    return filtered

def search_duckduckgo(q: str, max_results: int = 40) -> List[Dict]:
    out = []
    with DDGS() as ddgs:
        for r in ddgs.text(q, max_results=max_results):
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

def run_search(q: str) -> List[Dict]:
    # Prefer Google CSE if configured, else DuckDuckGo
    try:
        if GOOGLE_API_KEY and GOOGLE_CSE_ID:
            res = search_google_cse(q)
            if res:
                return only_playlist_results(res)
    except Exception as e:
        st.info(f"Google CSE failed: {e}")

    try:
        res = search_duckduckgo(q)
        return only_playlist_results(res)
    except Exception as e:
        st.info(f"DuckDuckGo failed: {e}")
        return []

# ----------------------- UI -----------------------
st.title("Spotify Playlist Finder")
st.write("Enter up to eight artists and eight tracks.")

# Simple Spotify‑style theming for link "buttons"
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
    </style>
    """,
    unsafe_allow_html=True,
)

with st.form("inputs"):
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Artists")
        artists = [
            st.text_input("Artist 1", placeholder="Kanye West"),
            st.text_input("Artist 2", placeholder=""),
            st.text_input("Artist 3", placeholder=""),
            st.text_input("Artist 4", placeholder=""),
            st.text_input("Artist 5", placeholder=""),
            st.text_input("Artist 6", placeholder=""),
            st.text_input("Artist 7", placeholder=""),
            st.text_input("Artist 8", placeholder=""),
        ]
    with c2:
        st.subheader("Songs")
        songs = [
            st.text_input("Song 1", placeholder="Runaway"),
            st.text_input("Song 2", placeholder=""),
            st.text_input("Song 3", placeholder=""),
            st.text_input("Song 4", placeholder=""),
            st.text_input("Song 5", placeholder=""),
            st.text_input("Song 6", placeholder=""),
            st.text_input("Song 7", placeholder=""),
            st.text_input("Song 8", placeholder=""),
        ]
    submitted = st.form_submit_button("Search")

if submitted:
    # Filter out blanks
    artists_filled = [a for a in artists if a.strip()]
    songs_filled   = [s for s in songs if s.strip()]

    if not artists_filled and not songs_filled:
        st.warning("Please enter at least one artist or one song.")
    else:
        query = build_query(artists_filled, songs_filled)
        st.caption(f"Query: `{query}`")

        results = run_search(query)

        if results:
            st.subheader("Matched playlists")
            # Render as Spotify‑style buttons in a fluid wrap
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
