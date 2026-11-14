"""Standalone check: TMDb id (+ title, year) -> Plex Discover ratingKey.

Not imported by the bot. Needs PLEX_TOKEN in the environment. Never prints it.
Run:  python scripts/check_plex_watchlist_lookup.py

Approach under test: Discover search by title, then fetch each candidate's
metadata and match its tmdb:// guid against the wanted TMDb id.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

DISCOVER = "https://discover.provider.plex.tv"
METADATA = "https://metadata.provider.plex.tv"

# (kind, title, year, tmdb_id). The ids are from memory: a "none" result with
# candidates printed means the id here may be wrong, not that the lookup is.
CASES = [
    ("movie", "The Thing", 1982, 1091),
    ("movie", "The Thing", 2011, 60935),
    ("movie", "Dune", 1984, 841),
    ("movie", "Dune", 2021, 438631),
    ("show", "Breaking Bad", 2008, 1396),
    ("show", "Severance", 2022, 95396),
    ("show", "Battlestar Galactica", 2004, 71365),
]


def get(token, url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={
        "Accept": "application/json",
        "X-Plex-Token": token,
        "X-Plex-Client-Identifier": "usher-lookup-check",
        "X-Plex-Product": "usher-lookup-check",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def search(token, title, kind):
    data = get(token, DISCOVER + "/library/search", {
        "query": title, "limit": 30, "includeMetadata": 1,
        "searchTypes": "movies" if kind == "movie" else "tv",
        "searchProviders": "discover",
    })
    out = []
    for group in data.get("MediaContainer", {}).get("SearchResults", []):
        for res in group.get("SearchResult", []):
            md = res.get("Metadata")
            if md and md.get("type") == kind:
                out.append(md)
    return out


def tmdb_guids(token, rating_key):
    data = get(token, METADATA + "/library/metadata/" + rating_key, {})
    items = data.get("MediaContainer", {}).get("Metadata", [])
    return [g["id"] for it in items for g in it.get("Guid", [])
            if g.get("id", "").startswith("tmdb://")]


def main():
    token = os.environ.get("PLEX_TOKEN")
    if not token:
        sys.exit("Set PLEX_TOKEN first.")
    for kind, title, year, tmdb_id in CASES:
        print(f"== {kind} {title} ({year}) tmdb={tmdb_id}")
        try:
            cands = search(token, title, kind)
        except Exception as exc:
            print(f"   search failed: {type(exc).__name__}: {exc}".replace(token, "***"))
            continue
        matched = []
        for md in cands:
            rk = (md.get("ratingKey") or md.get("guid", "").rsplit("/", 1)[-1])
            try:
                guids = tmdb_guids(token, rk)
            except Exception as exc:
                guids = [f"error {type(exc).__name__}"]
            hit = f"tmdb://{tmdb_id}" in guids
            if hit:
                matched.append(rk)
            print(f"   cand {rk} {md.get('title')!r} {md.get('year')} {guids} {'<-- MATCH' if hit else ''}")
        print(f"   result: {matched if matched else 'none'}")


if __name__ == "__main__":
    main()
