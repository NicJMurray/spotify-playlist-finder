import os
import urllib.parse
from typing import List, Dict

import streamlit as st
from duckduckgo_search import DDGS
import httpx
from dotenv import load_dotenv

# Load local .env for dev
load_dotenv()

# Read keys (prefer st.secrets if present, else .env)
def get_secret(key: str) -> str:
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, "")

GOOGLE_API_KEY = get_secret("GOOGLE_API_KEY").strip()
GOOGLE_CSE_ID = get_secret("GOOGLE_CSE_ID").strip()

SPOTIFY_PLAYLIST_HOST = "open.spotify.com"
SPOTIFY_PLAYLIST_PATH = "/playlist/"

def build_query(artist: str, song: str) -> str:
    terms = []
    if artist:
        terms.append(f'"{artist.strip()}"')
    if song:
        terms.append(f'"{song.strip()}"')
    return f'site:{SPOTIFY_PLAYLIST_HOST} inurl:playlist {" ".join(terms)}'.strip()

def only_playlist_results(results: List[Dict]) -> List[Dict]:
    filtered = []
    for r in results:
        url = r.get("url") or r.get("link") or r.get("href")
        title = r.get("title") or r.get("name") or ""
        snippet = r.get("snippet") or r.get("body") or ""
        if url and SPOTIFY_PLAYLIST_HOST in url and SPOTIFY_PLAYLIST_PATH in url:
            filtered.append({"title": title or url, "url": url, "snippet": snippet})
    return filtered

def search_duckduckgo(q: str, max_results: int = 25) -> List[Dict]:
    out = []
    with DDGS() as ddgs:
        for r in ddgs.text(q, max_results=max_results):
            out.append({"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")})
    return out

def search_google_cse(q: str, max_results: int = 25) -> List[Dict]:
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
                out.append({
                    "title": it.get("title"),
                    "url": it.get("link"),
                    "snippet": it.get("snippet")
                })
            if not items:
                break
            start += 10
    return out[:max_results]

def run_search(q: str) -> List[Dict]:
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

# --- UI ---
st.set_page_config(page_title="Spotify Playlist Finder", page_icon="🎵", layout="centered")
st.title("🎵 Spotify Playlist Finder")
st.write("Find Spotify playlists likely containing a given artist and/or song title.")

with st.form("inputs"):
    col1, col2 = st.columns(2)
    with col1:
        artist = st.text_input("Artist (optional)", placeholder="Kanye West")
    with col2:
        song = st.text_input("Song (optional)", placeholder="Runaway")
    submitted = st.form_submit_button("Search")

if submitted:
    if not artist and not song:
        st.warning("Please enter at least an artist or a song.")
    else:
        query = build_query(artist, song)
        st.caption(f"Query: `{query}`")

        results = run_search(query)

        if results:
            st.subheader("Matched playlists")
            for r in results:
                st.markdown(f"- [{r['title']}]({r['url']})")
            st.success(f"Found {len(results)} playlists.")
        else:
            st.info("No results found via configured backends.")
            google_url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
            st.link_button("Open on Google", google_url)

st.divider()
st.caption(
    "Google SERP embedding/scraping is restricted. "
    "This app uses the Google Custom Search JSON API when configured, "
    "with DuckDuckGo as a fallback."
)
