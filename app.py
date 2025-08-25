import urllib.parse
import streamlit as st
from duckduckgo_search import DDGS

SPOTIFY_PLAYLIST_HOST = "open.spotify.com"
SPOTIFY_PLAYLIST_PATH = "/playlist/"

def build_query(artist: str, song: str) -> str:
    terms = []
    if artist:
        terms.append(f'"{artist.strip()}"')
    if song:
        terms.append(f'"{song.strip()}"')
    return f'site:{SPOTIFY_PLAYLIST_HOST} inurl:playlist {" ".join(terms)}'.strip()

def only_playlist_results(results):
    filtered = []
    for r in results:
        url = r.get("href")
        title = r.get("title")
        snippet = r.get("body")
        if url and SPOTIFY_PLAYLIST_HOST in url and SPOTIFY_PLAYLIST_PATH in url:
            filtered.append({"title": title, "url": url, "snippet": snippet})
    return filtered

def search_duckduckgo(q: str, max_results: int = 20):
    out = []
    with DDGS() as ddgs:
        for r in ddgs.text(q, max_results=max_results):
            out.append(r)
    return out

# --- Streamlit UI ---
st.set_page_config(page_title="Spotify Playlist Finder", page_icon="🎵", layout="centered")

st.title("🎵 Spotify Playlist Finder")
st.write("Find Spotify playlists containing a given artist and/or song.")

with st.form("inputs"):
    col1, col2 = st.columns(2)
    with col1:
        artist = st.text_input("Artist (optional)", placeholder="e.g., Taylor Swift")
    with col2:
        song = st.text_input("Song (optional)", placeholder="e.g., Cruel Summer")
    submitted = st.form_submit_button("Search")

if submitted:
    if not artist and not song:
        st.warning("Please enter at least an artist or a song.")
    else:
        query = build_query(artist, song)
        st.caption(f"Query: `{query}`")

        try:
            results = search_duckduckgo(query)
            playlists = only_playlist_results(results)
        except Exception as e:
            playlists = []
            st.error(f"Search failed: {e}")

        if playlists:
            st.subheader("Matched playlists")
            for r in playlists:
                st.markdown(f"- [{r['title'] or r['url']}]({r['url']})")
            st.success(f"Found {len(playlists)} playlists.")
        else:
            st.info("No results found. Try opening the query directly on Google:")
            google_url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
            st.link_button("Open on Google", google_url)
