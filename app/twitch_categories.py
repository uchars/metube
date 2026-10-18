"""Twitch category (game) search for the live subscription category picker.

Queries go through yt-dlp's own Twitch GraphQL helper, so they use the same
client id, proxy, cookies and impersonation settings as yt-dlp's Twitch
extractor. Results are cached in memory for a few minutes; nothing is persisted.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from functools import partial
from typing import Any, Optional
from urllib.parse import urlparse

import yt_dlp

log = logging.getLogger("twitch_categories")

_CACHE_TTL = 600
_CACHE_MAX_ENTRIES = 500
SEARCH_LIMIT = 30
TOP_LIMIT = 60
QUERY_MAX_LENGTH = 100
LOOKUP_MAX_NAMES = 50

_NODE_FIELDS = "id name displayName boxArtURL(width: 52, height: 72) viewersCount"

_SEARCH_QUERY = (
    "query MeTubeSearchCategories($query: String!, $first: Int!) {"
    " searchCategories(query: $query, first: $first) { edges { node { %s } } } }" % _NODE_FIELDS
)
_TOP_QUERY = (
    "query MeTubeTopCategories($first: Int!) {"
    " games(first: $first) { edges { node { %s } } } }" % _NODE_FIELDS
)

_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


class TwitchCategoryError(Exception):
    """Twitch could not be asked, or answered with an error."""


class _QuietLogger:
    def debug(self, msg):
        log.debug("%s", msg)

    info = warning = error = debug


def _cache_get(key: tuple) -> Any:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        if hit[0] < time.monotonic():
            _cache.pop(key, None)
            return None
        return hit[1]


def _cache_put(key: tuple, value: Any) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            now = time.monotonic()
            for k in [k for k, (exp, _) in _cache.items() if exp < now]:
                _cache.pop(k, None)
            if len(_cache) >= _CACHE_MAX_ENTRIES:
                _cache.clear()
        _cache[key] = (time.monotonic() + _CACHE_TTL, value)


def _safe_box_art_url(value: Any) -> Optional[str]:
    """Only https URLs on Twitch's CDN reach the browser as an <img> source."""
    if not isinstance(value, str):
        return None
    # Box art URLs can come back as a size template.
    value = value.replace("{width}", "52").replace("{height}", "72")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "jtvnw.net" or host.endswith(".jtvnw.net")):
        return None
    return value


def _category_from_node(node: Any) -> Optional[dict]:
    if not isinstance(node, dict):
        return None
    name = node.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    display = node.get("displayName")
    viewers = node.get("viewersCount")
    return {
        "name": name.strip(),
        "display_name": display.strip() if isinstance(display, str) and display.strip() else name.strip(),
        "box_art_url": _safe_box_art_url(node.get("boxArtURL")),
        "viewers": viewers if isinstance(viewers, int) and not isinstance(viewers, bool) else None,
    }


def _edges(data: Any, field: str) -> list[dict]:
    edges = ((data or {}).get(field) or {}).get("edges") if isinstance(data, dict) else None
    out: list[dict] = []
    for edge in edges if isinstance(edges, list) else []:
        cat = _category_from_node(edge.get("node") if isinstance(edge, dict) else None)
        if cat is not None:
            out.append(cat)
    return out


def _gql(config, payload: dict) -> dict:
    """POST one GraphQL request through yt-dlp's Twitch extractor; returns ``data``."""
    # Imported here: subscriptions imports yt_dlp at module level, and tests
    # that stub yt_dlp must still be able to import this module.
    from subscriptions import _build_ydl_params

    params = _build_ydl_params(config)
    params["logger"] = _QuietLogger()
    try:
        with yt_dlp.YoutubeDL(params=params) as ydl:
            ie = ydl.get_info_extractor("TwitchStream")
            response = ie._download_base_gql(
                "categories", payload, "Querying Twitch categories", fatal=True
            )
    except yt_dlp.utils.YoutubeDLError as exc:
        raise TwitchCategoryError(str(exc)) from exc
    except Exception as exc:  # an upstream rename of the helper, a bad response
        log.warning("Twitch category query failed: %s", exc)
        raise TwitchCategoryError("Twitch category lookup is unavailable") from exc
    if not isinstance(response, dict):
        raise TwitchCategoryError("Unexpected response from Twitch")
    errors = response.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        msg = first.get("message") if isinstance(first, dict) else str(first)
        raise TwitchCategoryError(f"Twitch rejected the category query: {msg}")
    data = response.get("data")
    return data if isinstance(data, dict) else {}


def search_categories_sync(config, query: str) -> list[dict]:
    """Categories matching ``query``, or the most-watched ones when it is empty."""
    query = " ".join((query or "").split())[:QUERY_MAX_LENGTH]
    key = ("search", query.casefold()) if query else ("top",)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    if query:
        data = _gql(config, {"query": _SEARCH_QUERY, "variables": {"query": query, "first": SEARCH_LIMIT}})
        result = _edges(data, "searchCategories")
    else:
        data = _gql(config, {"query": _TOP_QUERY, "variables": {"first": TOP_LIMIT}})
        result = _edges(data, "games")
    _cache_put(key, result)
    for cat in result:
        _cache_put(("game", cat["name"].casefold()), cat)
    return result


def lookup_categories_sync(config, names: list[str]) -> list[dict]:
    """Category details (thumbnail etc.) for exact category names; unknown names are omitted."""
    wanted = list(dict.fromkeys(n for n in names if isinstance(n, str) and n.strip()))[:LOOKUP_MAX_NAMES]
    found: dict[str, Optional[dict]] = {}
    missing: list[str] = []
    for name in wanted:
        cached = _cache_get(("game", name.casefold()))
        if cached is not None:
            found[name] = cached or None
        else:
            missing.append(name)
    if missing:
        var_defs = ", ".join(f"$n{i}: String!" for i in range(len(missing)))
        fields = " ".join(f"c{i}: game(name: $n{i}) {{ {_NODE_FIELDS} }}" for i in range(len(missing)))
        data = _gql(config, {
            "query": f"query MeTubeLookupCategories({var_defs}) {{ {fields} }}",
            "variables": {f"n{i}": name for i, name in enumerate(missing)},
        })
        for i, name in enumerate(missing):
            cat = _category_from_node(data.get(f"c{i}"))
            # Cache misses too ({} is falsy but not None) so an unknown custom
            # name isn't looked up again on every render.
            _cache_put(("game", name.casefold()), cat or {})
            found[name] = cat
    return [cat for name in wanted if (cat := found.get(name))]


async def search_categories(config, query: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(search_categories_sync, config, query))


async def lookup_categories(config, names: list[str]) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(lookup_categories_sync, config, names))
