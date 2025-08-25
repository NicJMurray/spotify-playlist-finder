import os
from pathlib import Path
from typing import List, Dict, Tuple
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
    Increase recall in CSE with multiple variants:
      1) site: + inurl:playlist + all quoted terms
      2) site: + intitle:playlist + all quoted terms
      3) site: + all quoted terms + "playlist"
      4) For ≥3 terms, a limited set of pairwise combos
    """
    t = [s.strip() for s in terms if s and s.strip()]
    quoted_all = " ".join(f'"{s}"' for s in t)
    base_site = f"site:{SPOTIFY_HOST}"

    queries = [
        f'{base_site} inurl:playlist {quoted_all}'.strip(),
        f'{base_site} intitle:playlist {quoted_all}'.strip(),
        f'{base_site} {quoted_all} "playlist"'.strip(),
    ]
    if len(t) >= 3:
        for a, b in list(combinations(t, 2))[:10]:
            queries.append(f'{base_site} inurl:playlist "{a}" "{b}"')

    # de-dup while preserving order
    seen, uniq = set(), []
    for q in queries:
        if q not in seen:
            uniq.append(q); seen.add(q)
    return uniq

def google_cse_request(q: str, wanted: int = 40, mode: str = "siteSearch", dupe_filter: int = 1) -> Tuple[List[Dict], Dict]:
    """Fetch up to `wanted` results for a single query using pagination."""
    if not (GOOGLE_API_KEY and GOOGLE_CSE_ID):
        return [], {"error": "Missing GOOGLE_API_KEY or GOOGLE_CSE_ID"}

    url = "https://www.googleapis.com/customsearch/v1"
    out: List[Dict] = []
    start = 1
    total = None
    last_err = None

    with httpx.Client(timeout=20) as client:
        while len(out) < wanted and start <= 91:
            params = {
                "key": GOOGLE_API_KEY,
                "cx": GOOGLE_CSE_ID,
                "q": q,
                "num": min(10, wanted - len(out)),
                "start": start,
                "safe": "off",
                "hl": "en",
                "gl": "uk",
                "filter": dupe_filter,
            }
            if mode == "siteSearch":
                params["siteSearch"] = SPOTIFY_HOST
                params["siteSearchFilter"] = "i"
            elif mode == "as_sitesearch":
                params["as_sitesearch"] = SPOTIFY_HOST

            try:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                last_err = str(e)
                break

            si = (data or {}).get("searchInformation", {})
            if total is None and isinstance(si, dict) and "totalResults" in si:
                total = si.get("totalResults")

            items = data.get("items", []) if isinstance(data, dict) else []
            for it in items:
                out.append({"title": it.get("title"), "url": it.get("link"), "snippet": it.get("snippet")})
            if not items:
                break
            start += 10

    meta = {"query": q, "mode": mode, "dupe_filter": dupe_filter, "totalResults": total, "returned": len(out), "error": last_err}
    return out[:wanted], meta

def run_google_only(terms: List[str], max_results: int = 80) -> Tuple[List[Dict], List[Dict]]:
    """Run multiple CSE query variants & param modes; merge/dedupe results."""
    queries = build_queries(terms)
    merged: List[Dict] = []
    seen_urls = set()
    debug_meta: List[Dict] = []
    for q in queries:
        for mode in ("siteSearch", "as_sitesearch", "none"):
            for filt in (1, 0):
                raw, meta = google_cse_request(q, wanted=100, mode=mode, dupe_filter=filt)
                debug_meta.append(meta)
                if meta.get("error"):
                    continue
                for r in only_playlist_results(raw):
                    key = r["url"]
                    if key in seen_urls:
                        continue
                    merged.append(r)
                    seen_urls.add(key)
                    if len(merged) >= max_results:
                        return merged, debug_meta
    return merged[:max_results], debug_meta

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

# --- Static placeholders (no randomness) ---
placeholders = ["Kanye", "Jamie XX", "Deki Alem", "Favourite", "CASisDEAD", "Capricorn", "Starburster", "Eusexua"]

# --- Auto-search plumbing ---
if "do_search" not in st.session_state:
    st.session_state.do_search = False
if "prev_terms" not in st.session_state:
    st.session_state.prev_terms = []

def queue_search():
    st.session_state.do_search = True

# Shared heading for both columns
st.markdown('<div class="grid-label">Artist or song</div>', unsafe_allow_html=True)

# Inputs WITHOUT a form; each edit queues a search immediately
c1, c2 = st.columns(2)
with c1:
    st.text_input("Term 1", key="term_1", placeholder=placeholders[0], on_change=queue_search)
    st.text_input("Term 2", key="term_2", placeholder=placeholders[1], on_change=queue_search)
    st.text_input("Term 3", key="term_3", placeholder=placeholders[2], on_change=queue_search)
    st.text_input("Term 4", key="term_4", placeholder=placeholders[3], on_change=queue_search)
with c2:
    st.text_input("Term 5", key="term_5", placeholder=placeholders[4], on_change=queue_search)
    st.text_input("Term 6", key="term_6", placeholder=placeholders[5], on_change=queue_search)
    st.text_input("Term 7", key="term_7", placeholder=placeholders[6], on_change=queue_search)
    st.text_input("Term 8", key="term_8", placeholder=placeholders[7], on_change=queue_search)

# Manual search button (optional)
st.button("Search", on_click=queue_search)

# ---- Run search when queued (after any edit or button press) ----
if st.session_state.do_search:
    terms = [st.session_state.get(f"term_{i}", "").strip() for i in range(1, 9)]
    terms = [t for t in terms if t]

    # Avoid redundant searches if nothing changed
    if terms != st.session_state.prev_terms:
        if not terms:
            st.warning("Please enter at least one term.")
        else:
            primary_query_preview = f'site:{SPOTIFY_HOST} inurl:playlist ' + " ".join(f'"{t}"' for t in terms)
            st.caption(f"Primary query: `{primary_query_preview}`")

            results, meta = run_google_only(terms, max_results=80)

            if results:
                st.subheader("Matched playlists")
                btn_html = "".join(
                    f'<span class="btn-wrap"><a class="spotify-btn" href="{urllib.parse.quote(r["url"], safe=":/?&=%#")}" target="_blank" rel="noopener">{(r["title"] or "Open playlist")[:80]}</a></span>'
                    for r in results
                )
                st.markdown(btn_html, unsafe_allow_html=True)
                st.success(f"Found {len(results)} unique playlists.")
            else:
                st.info("No results found via Google CSE.")
                google_url = "https://www.google.com/search?q=" + urllib.parse.quote(primary_query_preview)
                st.link_button("Open on Google", google_url)

            with st.expander("Debug: Google CSE variants"):
                lines = []
                for m in meta:
                    tr = m.get("totalResults")
                    tr_s = ("unknown" if tr is None else str(tr))
                    lines.append(
                        f"- mode=`{m['mode']}`, dupe_filter={m['dupe_filter']} → totalResults={tr_s}, returned={m['returned']}  \n"
                        f"  query: `{m['query']}`"
                    )
                st.markdown("\n".join(lines))

        st.session_state.prev_terms = terms

    # reset the trigger regardless
    st.session_state.do_search = False
