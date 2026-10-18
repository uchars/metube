"""Stream category search (Twitch, Kick) for the live subscription category picker.

Each platform's requests go through yt-dlp's own extractor helpers, so they use
the same client id, proxy, cookies and impersonation settings as yt-dlp does
when it records from that site. Results are cached in memory for a few
minutes; nothing is persisted.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from functools import partial
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import yt_dlp

log = logging.getLogger("stream_categories")

PLATFORMS = ("twitch", "kick")

_CACHE_TTL = 600
_CACHE_MAX_ENTRIES = 1000
SEARCH_LIMIT = 30
TOP_LIMIT = 60
QUERY_MAX_LENGTH = 100
LOOKUP_MAX_NAMES = 50
# Kick has no lookup-by-name, so each unknown name costs a search request.
_KICK_LOOKUP_MAX_SEARCHES = 10

_cache: dict[tuple, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


class StreamCategoryError(Exception):
    """The platform could not be asked, or answered with an error."""


class _QuietLogger:
    def debug(self, msg):
        log.debug("%s", msg)

    info = warning = error = debug


def platform_for_url(url: str) -> Optional[str]:
    """The category platform a channel URL belongs to, by hostname."""
    host = (urlparse(url or "").hostname or "").lower()
    for platform, domain in (("twitch", "twitch.tv"), ("kick", "kick.com")):
        if host == domain or host.endswith("." + domain):
            return platform
    return None


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


def _safe_image_url(value: Any, domain: str) -> Optional[str]:
    """Only https URLs on the platform's own image hosts reach the browser."""
    if not isinstance(value, str):
        return None
    # Box art URLs can come back as a size template.
    value = value.strip().replace("{width}", "52").replace("{height}", "72")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == domain or host.endswith("." + domain)):
        return None
    return value


def _category(name: Any, display: Any, art: Optional[str], viewers: Any) -> Optional[dict]:
    if not isinstance(name, str) or not name.strip():
        return None
    name = " ".join(name.split())
    return {
        "name": name,
        "display_name": display.strip() if isinstance(display, str) and display.strip() else name,
        "box_art_url": art,
        "viewers": viewers if isinstance(viewers, int) and not isinstance(viewers, bool) else None,
    }


def _ydl(config) -> "yt_dlp.YoutubeDL":
    # Imported here: tests that stub yt_dlp must still be able to import this module.
    from subscriptions import _build_ydl_params

    params = _build_ydl_params(config)
    params["logger"] = _QuietLogger()
    return yt_dlp.YoutubeDL(params=params)


def _call(fn: Callable[[], Any], what: str) -> Any:
    try:
        return fn()
    except yt_dlp.utils.YoutubeDLError as exc:
        raise StreamCategoryError(str(exc)) from exc
    except StreamCategoryError:
        raise
    except Exception as exc:  # an upstream rename of a helper, a bad response
        log.warning("%s failed: %s", what, exc)
        raise StreamCategoryError(f"{what} is unavailable") from exc


# --- Twitch ---------------------------------------------------------------

_TWITCH_FIELDS = "id name displayName boxArtURL(width: 52, height: 72) viewersCount"
_TWITCH_SEARCH = (
    "query MeTubeSearchCategories($query: String!, $first: Int!) {"
    " searchCategories(query: $query, first: $first) { edges { node { %s } } } }" % _TWITCH_FIELDS
)
_TWITCH_TOP = (
    "query MeTubeTopCategories($first: Int!) {"
    " games(first: $first) { edges { node { %s } } } }" % _TWITCH_FIELDS
)


def _twitch_category(node: Any) -> Optional[dict]:
    if not isinstance(node, dict):
        return None
    return _category(
        node.get("name"),
        node.get("displayName"),
        _safe_image_url(node.get("boxArtURL"), "jtvnw.net"),
        node.get("viewersCount"),
    )


def _twitch_edges(data: Any, field: str) -> list[dict]:
    edges = ((data or {}).get(field) or {}).get("edges") if isinstance(data, dict) else None
    out: list[dict] = []
    for edge in edges if isinstance(edges, list) else []:
        cat = _twitch_category(edge.get("node") if isinstance(edge, dict) else None)
        if cat is not None:
            out.append(cat)
    return out


def _twitch_gql(config, payload: dict) -> dict:
    """POST one GraphQL request through yt-dlp's Twitch extractor; returns ``data``."""

    def run():
        with _ydl(config) as ydl:
            ie = ydl.get_info_extractor("TwitchStream")
            return ie._download_base_gql("categories", payload, "Querying Twitch categories", fatal=True)

    response = _call(run, "Twitch category lookup")
    if not isinstance(response, dict):
        raise StreamCategoryError("Unexpected response from Twitch")
    errors = response.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        msg = first.get("message") if isinstance(first, dict) else str(first)
        raise StreamCategoryError(f"Twitch rejected the category query: {msg}")
    data = response.get("data")
    return data if isinstance(data, dict) else {}


def _twitch_search(config, query: str) -> list[dict]:
    if query:
        data = _twitch_gql(config, {"query": _TWITCH_SEARCH, "variables": {"query": query, "first": SEARCH_LIMIT}})
        return _twitch_edges(data, "searchCategories")
    data = _twitch_gql(config, {"query": _TWITCH_TOP, "variables": {"first": TOP_LIMIT}})
    return _twitch_edges(data, "games")


def _twitch_lookup(config, names: list[str]) -> dict[str, Optional[dict]]:
    var_defs = ", ".join(f"$n{i}: String!" for i in range(len(names)))
    fields = " ".join(f"c{i}: game(name: $n{i}) {{ {_TWITCH_FIELDS} }}" for i in range(len(names)))
    data = _twitch_gql(config, {
        "query": f"query MeTubeLookupCategories({var_defs}) {{ {fields} }}",
        "variables": {f"n{i}": name for i, name in enumerate(names)},
    })
    return {name: _twitch_category(data.get(f"c{i}")) for i, name in enumerate(names)}


# --- Kick -----------------------------------------------------------------


def _kick_banner(banner: Any) -> Optional[str]:
    if isinstance(banner, str):
        return _safe_image_url(banner, "kick.com")
    if not isinstance(banner, dict):
        return None
    for field in ("url", "src", "responsive", "srcset"):
        value = banner.get(field)
        if isinstance(value, str) and value.strip():
            # srcset-style values: "https://… 1x, https://… 2x" — take the first URL.
            url = _safe_image_url(value.split(",")[0].split()[0], "kick.com")
            if url:
                return url
    return None


def _kick_category(item: Any) -> Optional[dict]:
    if not isinstance(item, dict):
        return None
    return _category(item.get("name"), item.get("name"), _kick_banner(item.get("banner")), item.get("viewers"))


def _kick_api(config, path: str, query: dict) -> Any:
    def run():
        with _ydl(config) as ydl:
            ie = ydl.get_info_extractor("Kick")
            return ie._call_api(path, "categories", note="Querying Kick categories", query=query)

    return _call(run, "Kick category lookup")


def _kick_items(response: Any, *keys: str) -> list[dict]:
    node = response
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    out = []
    for item in node if isinstance(node, list) else []:
        cat = _kick_category(item)
        if cat is not None:
            out.append(cat)
    return out


def _kick_top(config) -> list[dict]:
    cached = _cache_get(("kick", "top"))
    if cached is not None:
        return cached
    response = _kick_api(config, "v1/subcategories", {"limit": TOP_LIMIT, "page": 1})
    result = _kick_items(response, "data")
    _cache_put(("kick", "top"), result)
    return result


def _kick_search(config, query: str) -> list[dict]:
    if not query:
        return _kick_top(config)
    try:
        result = _kick_items(_kick_api(config, "search", {"searched_word": query}), "categories")
    except StreamCategoryError as exc:
        log.debug("Kick category search failed, filtering the popular list instead: %s", exc)
        result = None
    if not result:
        # Kick's search endpoint is undocumented; fall back to the popular list.
        needle = query.casefold()
        result = [cat for cat in _kick_top(config) if needle in cat["name"].casefold()]
    return result[:SEARCH_LIMIT]


def _kick_lookup(config, names: list[str]) -> dict[str, Optional[dict]]:
    found: dict[str, Optional[dict]] = {}
    for name in names[:_KICK_LOOKUP_MAX_SEARCHES]:
        wanted = name.casefold()
        found[name] = next((c for c in _kick_search(config, name) if c["name"].casefold() == wanted), None)
    return found


# --- Channel avatars -----------------------------------------------------

_TWITCH_LOGIN = re.compile(r"^[A-Za-z0-9_]{1,25}$")
_KICK_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TWITCH_AVATAR = (
    "query MeTubeChannelAvatar($login: String!) {"
    " user(login: $login) { profileImageURL(width: 70) } }"
)


def _channel_slug(url: str) -> Optional[str]:
    path = (urlparse(url or "").path or "").strip("/")
    return path.split("/", 1)[0] if path else None


def channel_avatar_sync(config, url: str) -> Optional[str]:
    """Profile picture of a Twitch or Kick channel URL; None for other sites or none set."""
    platform = platform_for_url(url)
    slug = _channel_slug(url)
    if platform == "twitch" and slug and _TWITCH_LOGIN.match(slug):
        data = _twitch_gql(config, {"query": _TWITCH_AVATAR, "variables": {"login": slug.lower()}})
        user = data.get("user") if isinstance(data, dict) else None
        return _safe_image_url((user or {}).get("profileImageURL"), "jtvnw.net")
    if platform == "kick" and slug and _KICK_SLUG.match(slug):
        response = _kick_api(config, f"v2/channels/{slug.lower()}", {})
        user = response.get("user") if isinstance(response, dict) else None
        return _safe_image_url((user or {}).get("profile_pic"), "kick.com")
    return None


_BACKENDS = {
    "twitch": (_twitch_search, _twitch_lookup),
    "kick": (_kick_search, _kick_lookup),
}


# --- Public API -----------------------------------------------------------


def _backend(platform: str):
    try:
        return _BACKENDS[platform]
    except KeyError:
        raise StreamCategoryError(f"Unsupported category platform: {platform}") from None


def search_categories_sync(config, platform: str, query: str) -> list[dict]:
    """Categories matching ``query``, or the most-watched ones when it is empty."""
    search, _ = _backend(platform)
    query = " ".join((query or "").split())[:QUERY_MAX_LENGTH]
    key = (platform, "search", query.casefold()) if query else (platform, "search-top")
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = search(config, query)
    _cache_put(key, result)
    for cat in result:
        _cache_put((platform, "name", cat["name"].casefold()), cat)
    return result


def lookup_categories_sync(config, platform: str, names: list[str]) -> list[dict]:
    """Category details (thumbnail etc.) for exact category names; unknown names are omitted."""
    _, lookup = _backend(platform)
    wanted = list(dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip()))[:LOOKUP_MAX_NAMES]
    found: dict[str, Optional[dict]] = {}
    missing: list[str] = []
    for name in wanted:
        cached = _cache_get((platform, "name", name.casefold()))
        if cached is not None:
            found[name] = cached or None
        else:
            missing.append(name)
    if missing:
        for name, cat in lookup(config, missing).items():
            # Cache misses too ({} is falsy but not None) so an unknown custom
            # name isn't looked up again on every render.
            _cache_put((platform, "name", name.casefold()), cat or {})
            found[name] = cat
    return [cat for name in wanted if (cat := found.get(name))]


async def search_categories(config, platform: str, query: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(search_categories_sync, config, platform, query))


async def lookup_categories(config, platform: str, names: list[str]) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(lookup_categories_sync, config, platform, names))
