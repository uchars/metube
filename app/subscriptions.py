"""Channel/playlist subscriptions: periodic yt-dlp flat extract + queue new videos.

A subscription whose URL is a live channel (Twitch, Kick, a YouTube ``/live``
page, ...) is a *live* subscription instead: each check asks yt-dlp whether the
channel is live right now and queues a capture when it is and the stream
matches the subscription's category/title filters.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import time
import types
import uuid
from dataclasses import dataclass, field, fields
from functools import partial
from typing import Any, Optional

import yt_dlp
import yt_dlp.networking.impersonate
import bg_tasks
import stream_categories
from dl_formats import merge_ytdl_option_layers
from state_store import AtomicJsonStore, read_legacy_shelf
from url_guard import validate_url, download_proxies

log = logging.getLogger("subscriptions")

# How many subscription feeds to scan at once. Bounded so one slow/hung feed
# doesn't serialize the rest, without bursting a large subscription list at the
# extractor (which risks rate-limiting / bot detection).
_MAX_CONCURRENT_CHECKS = 4

VIDEO_ONLY_MSG = (
    "This URL points to a single video, not a channel or playlist. Use Download instead."
)
NO_ENTRIES_MSG = "No entries found at this URL."
NO_CATEGORY_MSG = (
    "Channel is live, but yt-dlp reported no stream category, so the category filter cannot match."
)

KIND_FEED = "feed"
KIND_LIVE = "live"

# Bounds for the per-subscription live category list; it is persisted and
# broadcast to every connected client.
LIVE_CATEGORIES_MAX = 50
LIVE_CATEGORY_MAX_LENGTH = 100

# A live recording is stopped (and kept) once the stream has stopped matching
# the subscription's filters for this long; 0 never stops it.
DEFAULT_LIVE_STOP_AFTER_MINUTES = 10
# How often a live subscription is checked while one of its recordings runs.
DEFAULT_LIVE_RECHECK_MINUTES = 5

_KEEP = object()

# How often a live channel's profile picture is looked up again.
_AVATAR_REFRESH_SECONDS = 24 * 3600
_FEED_AVATAR_HOSTS = ("ggpht.com", "googleusercontent.com")


def _feed_avatar(info: Any) -> str:
    """The channel avatar yt-dlp lists among a feed's thumbnails (YouTube), if any."""
    thumbs = info.get("thumbnails") if isinstance(info, dict) else None
    candidates = [t for t in thumbs if isinstance(t, dict)] if isinstance(thumbs, list) else []
    candidates.sort(key=lambda t: 0 if t.get("id") == "avatar_uncropped" else 1)
    for thumb in candidates:
        if "avatar" not in str(thumb.get("id") or ""):
            continue
        for host in _FEED_AVATAR_HOSTS:
            url = stream_categories._safe_image_url(thumb.get("url"), host)
            if url:
                return url
    return ""
_MEDIA_HINT_FIELDS = (
    "duration",
    "timestamp",
    "release_timestamp",
    "upload_date",
    "view_count",
    "live_status",
    "availability",
)


def _impersonate_opt(ytdl_options: dict) -> dict:
    opts = dict(ytdl_options)
    if "impersonate" in opts:
        opts["impersonate"] = yt_dlp.networking.impersonate.ImpersonateTarget.from_str(
            opts["impersonate"]
        )
    return opts


def _build_ydl_params(
    config, *, playlistend: Optional[int] = None, extra_opts: Optional[dict[str, Any]] = None
) -> dict:
    params: dict[str, Any] = {
        "quiet": not logging.getLogger().isEnabledFor(logging.DEBUG),
        "verbose": logging.getLogger().isEnabledFor(logging.DEBUG),
        "no_color": True,
        "extract_flat": True,
        "ignore_no_formats_error": True,
        "lazy_playlist": True,
        "paths": {"home": config.DOWNLOAD_DIR, "temp": config.TEMP_DIR},
        **config.YTDL_OPTIONS,
        **(extra_opts or {}),
        # A scan is a poll, not an add: it runs on a timer and queues items
        # through the download queue, which writes the feed metadata itself.
        # yt-dlp emits the playlist-level infojson/description/thumbnail
        # regardless of `download`, so without this a writeinfojson user would
        # get those files rewritten on every check interval. See issue #1040.
        "allow_playlist_files": False,
    }
    params = _impersonate_opt(params)
    if playlistend is not None and playlistend > 0:
        params["playlistend"] = playlistend
    return params


def _is_media_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    etype = str(entry.get("_type") or "")
    if etype in ("playlist", "multi_video", "channel"):
        return False
    if entry.get("entries"):
        return False
    url = _entry_video_url(entry)
    if not url:
        return False
    ie_key = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    if any(token in ie_key for token in ("playlist", "channel", "tab")):
        return any(entry.get(field) is not None for field in _MEDIA_HINT_FIELDS)
    return True


def extract_flat_playlist(
    config, url: str, playlistend: Optional[int], *, extra_opts: Optional[dict[str, Any]] = None, _depth: int = 0
):
    """Return (info_dict, entries_list) for playlist/channel URLs."""
    params = _build_ydl_params(config, playlistend=playlistend, extra_opts=extra_opts)
    with yt_dlp.YoutubeDL(params=params) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return None, []
    etype = info.get("_type") or "video"
    if etype == "video":
        return info, []
    if etype in ("playlist", "channel"):
        entries = info.get("entries") or []
        if isinstance(entries, types.GeneratorType):
            entries = list(entries)
        # Drop None placeholders from incomplete flat playlists
        entries = [e for e in entries if e]
        media_entries = [e for e in entries if _is_media_entry(e)]
        if media_entries:
            return info, media_entries
        if _depth < 1:
            proxies = download_proxies({**config.YTDL_OPTIONS, **(extra_opts or {})})
            for ent in entries[:5]:
                nested_url = _entry_video_url(ent)
                if not nested_url:
                    continue
                # nested_url comes from remote playlist content; guard it too,
                # against the same proxy map this scan is using.
                if validate_url(
                    nested_url,
                    allow_private=getattr(config, "ALLOW_PRIVATE_ADDRESSES", False),
                    proxies=proxies,
                ) is not None:
                    continue
                nested_info, nested_entries = extract_flat_playlist(
                    config,
                    nested_url,
                    playlistend,
                    extra_opts=extra_opts,
                    _depth=_depth + 1,
                )
                if nested_entries:
                    return nested_info, nested_entries
        return info, entries
    if etype.startswith("url") and info.get("url"):
        # Single nested URL without playlist wrapper — treat as non-subscribable
        return info, []
    return info, []


def _entry_video_url(entry: dict) -> Optional[str]:
    return entry.get("webpage_url") or entry.get("url")


def _is_not_live_error(exc: BaseException) -> bool:
    """True when a yt-dlp error means "this channel is not live right now"."""
    not_live = getattr(getattr(yt_dlp, "utils", None), "UserNotLive", None)
    if not isinstance(not_live, type):
        return False
    if isinstance(exc, not_live):
        return True
    # YoutubeDL wraps extractor errors in a DownloadError carrying the original.
    exc_info = getattr(exc, "exc_info", None)
    cause = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) > 1 else None
    return isinstance(cause, not_live)


def _is_live_info(info: Any) -> bool:
    if not isinstance(info, dict):
        return False
    return info.get("live_status") == "is_live" or info.get("is_live") is True


_twitch_category_ie_cls: Any = None


def _twitch_category_ie():
    """yt-dlp's Twitch stream extractor, also reporting the stream's category.

    yt-dlp's ``twitch:stream`` extractor already fetches the stream metadata
    that names the game/category, but does not put it in the info dict (and
    puts the stream title in ``description``). This subclass keeps the first
    GraphQL response it downloads and copies the category into ``categories``,
    so no extra request is made. Any failure leaves ``categories`` unset.
    """
    global _twitch_category_ie_cls
    if _twitch_category_ie_cls is not None:
        return _twitch_category_ie_cls or None
    try:
        from yt_dlp.extractor.twitch import TwitchStreamIE
    except Exception:  # stubbed yt_dlp in tests, or an extractor rename upstream
        _twitch_category_ie_cls = False
        return None

    class MeTubeTwitchStreamIE(TwitchStreamIE):
        IE_NAME = TwitchStreamIE.IE_NAME

        def _download_gql(self, *args, **kwargs):
            data = super()._download_gql(*args, **kwargs)
            if getattr(self, "_metube_gql", None) is None:
                self._metube_gql = data
            return data

        def _real_extract(self, url):
            self._metube_gql = None
            info = super()._real_extract(url)
            try:
                gql = self._metube_gql or []
                names = []
                for path in (
                    (0, "data", "user", "stream", "game", "name"),
                    (1, "data", "user", "broadcastSettings", "game", "name"),
                ):
                    node: Any = gql
                    for key in path:
                        if isinstance(key, int):
                            node = node[key] if isinstance(node, list) and len(node) > key else None
                        else:
                            node = node.get(key) if isinstance(node, dict) else None
                    if isinstance(node, str) and node.strip():
                        names.append(node.strip())
                if names and not info.get("categories"):
                    info["categories"] = list(dict.fromkeys(names))
            except Exception as exc:
                log.debug("Could not read Twitch stream category: %s", exc)
            return info

    _twitch_category_ie_cls = MeTubeTwitchStreamIE
    return MeTubeTwitchStreamIE


class _ProbeLogger:
    """yt-dlp logger for live probes: an offline channel is a yt-dlp ERROR, and
    would otherwise print one on every check. Real failures still raise."""

    def debug(self, msg):
        log.debug("%s", msg)

    def info(self, msg):
        log.debug("%s", msg)

    def warning(self, msg):
        log.debug("%s", msg)

    def error(self, msg):
        log.debug("%s", msg)


def probe_live_channel(config, url: str, *, extra_opts: Optional[dict[str, Any]] = None) -> Optional[dict]:
    """Ask yt-dlp whether a live channel is streaming right now.

    Returns None when the channel is offline, otherwise a dict with the stream's
    ``id``, ``title``, ``categories``, ``url`` and ``uploader``. Other extraction
    errors propagate as yt-dlp errors. Nothing is downloaded.
    """
    params = _build_ydl_params(config, extra_opts=extra_opts)
    # Resolve the channel's stream, not a flat placeholder for it.
    params.pop("extract_flat", None)
    params["logger"] = _ProbeLogger()
    proxies = download_proxies({**config.YTDL_OPTIONS, **(extra_opts or {})})
    try:
        with yt_dlp.YoutubeDL(params=params) as ydl:
            ie_key = None
            twitch_ie = _twitch_category_ie()
            if twitch_ie is not None and twitch_ie.suitable(url):
                ydl.add_info_extractor(twitch_ie())
                ie_key = twitch_ie.ie_key()
            info = ydl.extract_info(url, download=False, process=False, ie_key=ie_key)
            # A YouTube /live page resolves to a pointer at the current stream.
            if isinstance(info, dict) and str(info.get("_type") or "").startswith("url") and info.get("url"):
                nested_url = str(info["url"])
                if validate_url(
                    nested_url,
                    allow_private=getattr(config, "ALLOW_PRIVATE_ADDRESSES", False),
                    proxies=proxies,
                ) is not None:
                    return None
                info = ydl.extract_info(
                    nested_url, download=False, process=False, ie_key=info.get("ie_key")
                )
    except yt_dlp.utils.YoutubeDLError as exc:
        if _is_not_live_error(exc):
            return None
        raise
    if not _is_live_info(info):
        return None
    is_twitch = twitch_ie is not None and ie_key == twitch_ie.ie_key()
    # Twitch's own title is "<channel> (live)"; the stream title is the description.
    stream_title = (info.get("description") if is_twitch else None) or info.get("title") or ""
    categories = info.get("categories")
    if not isinstance(categories, list):
        categories = []
    return {
        "id": str(info.get("id") or url),
        "title": " ".join(str(stream_title).split()),
        "categories": [str(c).strip() for c in categories if isinstance(c, str) and c.strip()],
        "url": info.get("webpage_url") or url,
        "uploader": info.get("uploader") or info.get("channel") or "",
    }


def live_stream_matches(
    stream: dict, categories: list[str], title_pattern: Optional[re.Pattern[str]]
) -> tuple[bool, Optional[str]]:
    """Return (matches, error) for a live stream against a live subscription's filters.

    Both filters must pass when both are set. Categories compare
    case-insensitively against any category yt-dlp reports for the stream.
    """
    if categories:
        stream_cats = {c.casefold() for c in stream.get("categories") or []}
        if not stream_cats:
            return False, NO_CATEGORY_MSG
        if not any(c.casefold() in stream_cats for c in categories):
            return False, None
    if title_pattern is not None and not title_pattern.search(stream.get("title") or ""):
        return False, None
    return True, None


async def browse_playlist(
    config,
    url: str,
    playlist_item_limit: int,
    ytdl_options_presets: Optional[list[str]],
    ytdl_options_overrides: Optional[dict[str, Any]],
) -> dict:
    """Flat-probe a playlist/channel URL and return its entries for the Browse dialog.

    Read-only: nothing is downloaded, queued or persisted. A limit <= 0 means
    no limit.
    """
    extra = merge_ytdl_option_layers(
        ytdl_options_presets, ytdl_options_overrides, config.YTDL_OPTIONS_PRESETS
    )
    loop = asyncio.get_running_loop()
    # SSRF guard, same as add_subscription: block non-http(s) schemes and
    # internal/metadata hosts before yt-dlp fetches anything.
    proxies = download_proxies({**config.YTDL_OPTIONS, **extra})
    url_error = await loop.run_in_executor(
        None,
        partial(
            validate_url,
            url,
            allow_private=getattr(config, "ALLOW_PRIVATE_ADDRESSES", False),
            proxies=proxies,
        ),
    )
    if url_error is not None:
        log.warning('Rejected browse URL "%s": %s', url, url_error)
        return {"status": "error", "msg": url_error}
    try:
        info, entries = await loop.run_in_executor(
            None,
            partial(
                extract_flat_playlist,
                config,
                url,
                playlist_item_limit if playlist_item_limit > 0 else None,
                extra_opts=extra,
            ),
        )
    except yt_dlp.utils.YoutubeDLError as exc:
        return {"status": "error", "msg": str(exc)}

    if not info:
        return {"status": "error", "msg": "Could not resolve URL"}
    if (info.get("_type") or "video") not in ("playlist", "channel"):
        return {"status": "error", "msg": VIDEO_ONLY_MSG}

    items: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_url = _entry_video_url(entry)
        if not entry_url:
            continue
        duration = entry.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            duration = None
        items.append({
            "url": entry_url,
            "title": entry.get("title") or entry_url,
            "duration": duration,
        })
    if not items:
        return {"status": "error", "msg": NO_ENTRIES_MSG}
    return {"status": "ok", "title": info.get("title") or url, "entries": items}


def _entry_id(entry: dict) -> Optional[str]:
    eid = entry.get("id")
    if eid is not None:
        return str(eid)
    url = _entry_video_url(entry)
    return url


def _is_subscriber_only_entry(entry: dict) -> bool:
    """True when yt-dlp marks the entry as channel member-only (subscriber_only availability)."""
    return str(entry.get("availability") or "") == "subscriber_only"


def coerce_optional_bool(value: Any, *, default: bool = False, field_name: str = "value") -> bool:
    """Parse optional JSON booleans for subscription settings."""
    if value is None:
        return default
    try:
        return _coerce_bool(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a boolean") from exc


@dataclass
class SubscriptionInfo:
    id: str
    name: str
    url: str
    enabled: bool = True
    check_interval_minutes: int = 60
    download_type: str = "video"
    codec: str = "auto"
    format: str = "any"
    quality: str = "best"
    folder: str = ""
    custom_name_prefix: str = ""
    auto_start: bool = True
    playlist_item_limit: int = 0
    split_by_chapters: bool = False
    sponsorblock: bool = False
    audio_tags: str = "with_cover"
    chapter_template: str = ""
    subtitle_language: str = "en"
    subtitle_mode: str = "prefer_manual"
    ytdl_options_presets: list[str] = field(default_factory=list)
    ytdl_options_overrides: dict[str, Any] = field(default_factory=dict)
    title_regex: str = ""
    skip_subscriber_only: bool = False
    # A fixed range applied to every video the subscription downloads. Only
    # sensible for channels with a consistent format (a standing intro, a fixed
    # sponsor read); left unset, videos download whole. Old stored records
    # predate these fields and take the defaults — _from_stored filters by
    # field name, so nothing needs migrating.
    clip_start: Optional[float] = None
    clip_end: Optional[float] = None
    # "feed" polls a channel/playlist for new videos; "live" polls a live
    # channel and captures each stream that matches live_categories/title_regex.
    # seen_ids then holds the stream ids already captured.
    kind: str = KIND_FEED
    live_categories: list[str] = field(default_factory=list)
    # What the last live check saw, for the UI.
    live_now: bool = False
    live_title: str = ""
    live_category: str = ""
    live_stop_after_minutes: int = DEFAULT_LIVE_STOP_AFTER_MINUTES
    live_recheck_minutes: int = DEFAULT_LIVE_RECHECK_MINUTES
    # When the running recording's stream was first seen not matching.
    live_mismatch_since: Optional[float] = None
    # The URL of this subscription's current recording in the download queue.
    live_capture_url: str = ""
    live_recording: bool = False
    # The channel's profile picture, for the table; "" when unknown.
    avatar_url: str = ""
    avatar_checked: Optional[float] = None
    last_checked: Optional[float] = None
    seen_ids: list[str] = field(default_factory=list)
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    def seen_set(self) -> set[str]:
        return set(self.seen_ids)

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "enabled": self.enabled,
            "check_interval_minutes": self.check_interval_minutes,
            "download_type": self.download_type,
            "codec": self.codec,
            "format": self.format,
            "quality": self.quality,
            "folder": self.folder,
            "title_regex": self.title_regex,
            "skip_subscriber_only": self.skip_subscriber_only,
            "clip_start": self.clip_start,
            "clip_end": self.clip_end,
            "kind": self.kind,
            "live_categories": list(self.live_categories),
            "live_now": self.live_now,
            "live_title": self.live_title,
            "live_category": self.live_category,
            "live_stop_after_minutes": self.live_stop_after_minutes,
            "live_recheck_minutes": self.live_recheck_minutes,
            "live_mismatch_since": self.live_mismatch_since,
            "live_recording": self.live_recording,
            "avatar_url": self.avatar_url,
            "last_checked": self.last_checked,
            "seen_count": len(self.seen_ids),
            "error": self.error,
        }


def _subscription_to_record(sub: SubscriptionInfo) -> dict[str, Any]:
    return {
        "id": sub.id,
        "name": sub.name,
        "url": sub.url,
        "enabled": sub.enabled,
        "check_interval_minutes": sub.check_interval_minutes,
        "download_type": sub.download_type,
        "codec": sub.codec,
        "format": sub.format,
        "quality": sub.quality,
        "folder": sub.folder,
        "custom_name_prefix": sub.custom_name_prefix,
        "auto_start": sub.auto_start,
        "playlist_item_limit": sub.playlist_item_limit,
        "split_by_chapters": sub.split_by_chapters,
        "sponsorblock": sub.sponsorblock,
        "audio_tags": sub.audio_tags,
        "chapter_template": sub.chapter_template,
        "subtitle_language": sub.subtitle_language,
        "subtitle_mode": sub.subtitle_mode,
        "ytdl_options_presets": list(sub.ytdl_options_presets),
        "ytdl_options_overrides": sub.ytdl_options_overrides,
        "title_regex": sub.title_regex,
        "skip_subscriber_only": sub.skip_subscriber_only,
        "clip_start": sub.clip_start,
        "clip_end": sub.clip_end,
        "kind": sub.kind,
        "live_categories": list(sub.live_categories),
        "live_now": sub.live_now,
        "live_title": sub.live_title,
        "live_category": sub.live_category,
        "live_stop_after_minutes": sub.live_stop_after_minutes,
        "live_recheck_minutes": sub.live_recheck_minutes,
        "live_mismatch_since": sub.live_mismatch_since,
        "live_capture_url": sub.live_capture_url,
        "live_recording": sub.live_recording,
        "avatar_url": sub.avatar_url,
        "avatar_checked": sub.avatar_checked,
        "last_checked": sub.last_checked,
        "seen_ids": list(sub.seen_ids),
        "error": sub.error,
    }


def _normalize_subscription_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy ytdl_options_preset (str) to ytdl_options_presets (list)."""
    out = dict(rec)
    if "ytdl_options_presets" not in out:
        old = out.pop("ytdl_options_preset", None)
        if old is None:
            out["ytdl_options_presets"] = []
        elif isinstance(old, list):
            out["ytdl_options_presets"] = [str(x).strip() for x in old if str(x).strip()]
        elif isinstance(old, str):
            out["ytdl_options_presets"] = [old.strip()] if old.strip() else []
        else:
            out["ytdl_options_presets"] = []
    else:
        out.pop("ytdl_options_preset", None)
    return out


def _subscription_from_record(record: Any) -> Optional[SubscriptionInfo]:
    field_names = {f.name for f in fields(SubscriptionInfo)}
    if isinstance(record, SubscriptionInfo):
        return record
    if isinstance(record, dict):
        try:
            normalized = _normalize_subscription_record(dict(record))
            return SubscriptionInfo(**{k: v for k, v in normalized.items() if k in field_names})
        except TypeError:
            return None
    return None


def _normalize_title_regex_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def validate_title_regex(value: Any) -> str:
    """Return stored title regex string; non-empty values must compile (re.error on failure)."""
    s = _normalize_title_regex_value(value)
    if s:
        re.compile(s)
    return s


def validate_live_categories(value: Any) -> list[str]:
    """Return a stored live category list, or raise ValueError if unusable.

    Accepts a list of strings or one comma-separated string. Entries are
    trimmed and de-duplicated case-insensitively; an empty list means any
    category.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError("live_categories must be a list of strings")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise ValueError("live_categories must be a list of strings")
        name = " ".join(item.split())
        if not name:
            continue
        if len(name) > LIVE_CATEGORY_MAX_LENGTH:
            raise ValueError(
                f"each live category must be at most {LIVE_CATEGORY_MAX_LENGTH} characters"
            )
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    if len(out) > LIVE_CATEGORIES_MAX:
        raise ValueError(f"at most {LIVE_CATEGORIES_MAX} live categories are allowed")
    return out


def validate_minutes(value: Any, *, field_name: str, minimum: int, default: int) -> int:
    """Parse a whole number of minutes, or raise ValueError."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer") from None
    if minutes < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return minutes


# The name is a display label the user picks; it is persisted and broadcast to
# every connected client, so keep it a bounded single-line string.
SUBSCRIPTION_NAME_MAX_LENGTH = 200


def validate_subscription_name(value: Any) -> str:
    """Return a stored subscription name, or raise ValueError if unusable."""
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    # Collapse newlines/tabs so a pasted title can't break the table layout.
    name = " ".join(value.split())
    if not name:
        raise ValueError("name must not be empty")
    if len(name) > SUBSCRIPTION_NAME_MAX_LENGTH:
        raise ValueError(f"name must be at most {SUBSCRIPTION_NAME_MAX_LENGTH} characters")
    return name


def validate_subscription_folder(value: Any) -> str:
    """Return a stored subscription folder, or raise ValueError if unusable.

    The folder is relative to the configured download directory, and the
    authoritative check still happens at download time in ``DownloadQueue`` —
    that is where ``CUSTOM_DIRS``, ``CREATE_CUSTOM_DIRS`` and the
    resolves-inside-the-base-directory rule live, and where the directory is
    created. This rejects only values that could never be valid, so an edit is
    refused while the user is looking at it rather than silently failing every
    check from then on. An empty folder is valid and means the base directory.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("folder must be a string")
    folder = value.strip()
    if not folder:
        return ""
    if os.path.isabs(folder):
        raise ValueError("folder must be relative to the download directory")
    # Check both separators: the value is stored as typed, and a Windows-style
    # path would otherwise carry an unexamined '..' past this point.
    if any(part == ".." for part in folder.replace("\\", "/").split("/")):
        raise ValueError('folder must not contain ".."')
    return folder


def _coerce_bool(value: Any) -> bool:
    """Accept JSON booleans and common string forms used by API clients."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "on"}:
            return True
        if lowered in {"false", "0", "off"}:
            return False
    raise ValueError("enabled must be a boolean")


class SubscriptionNotifier:
    """Hook for Socket.IO / UI updates."""

    async def subscription_added(self, sub: SubscriptionInfo) -> None:
        raise NotImplementedError

    async def subscription_updated(self, sub: SubscriptionInfo) -> None:
        raise NotImplementedError

    async def subscription_removed(self, sub_id: str) -> None:
        raise NotImplementedError

    async def subscriptions_all(self, subs: list[SubscriptionInfo]) -> None:
        raise NotImplementedError


class SubscriptionManager:
    def __init__(self, config, download_queue, notifier: SubscriptionNotifier):
        self.config = config
        self.dqueue = download_queue
        self.notifier = notifier
        pdir = config.STATE_DIR
        if not os.path.isdir(pdir):
            os.makedirs(pdir, exist_ok=True)
        self._legacy_path = os.path.join(pdir, "subscriptions")
        self._path = os.path.join(pdir, "subscriptions.json")
        self._store = AtomicJsonStore(self._path, kind="subscriptions")
        self._subs: dict[str, SubscriptionInfo] = {}
        self._url_index: dict[str, str] = {}  # normalized url -> id
        self._pending_urls: set[str] = set()
        self._checks_in_flight: set[str] = set()  # subscription ids being checked right now
        self._lock = asyncio.Lock()
        self._loop_task: Optional[asyncio.Task] = None
        self._load_all()

    def close(self) -> None:
        # No persistent shelf handle to close.
        return

    def _normalize_url(self, url: str) -> str:
        return (url or "").strip()

    def _normalize_seen_ids(self, seen_ids: list[str]) -> list[str]:
        max_seen = int(getattr(self.config, "SUBSCRIPTION_MAX_SEEN_IDS", 50000))
        normalized = [str(sid) for sid in dict.fromkeys(seen_ids)]
        if len(normalized) > max_seen:
            normalized = normalized[:max_seen]
        return normalized

    def _load_all(self) -> None:
        payload = self._store.load()
        loaded_from_legacy = False
        if payload is not None:
            records = payload.get("items") or []
        else:
            legacy_items = read_legacy_shelf(self._legacy_path)
            records = [raw for _key, raw in legacy_items] if legacy_items else []
            if records:
                loaded_from_legacy = True

        loaded_subs = self._iter_valid_subs(records)
        compact_records = []
        for sub in loaded_subs:
            sub.seen_ids = self._normalize_seen_ids(sub.seen_ids)
            self._subs[sub.id] = sub
            self._url_index[self._normalize_url(sub.url)] = sub.id
            compact_records.append(_subscription_to_record(sub))

        if loaded_from_legacy or (
            payload is not None
            and (
                payload.get("schema_version") != self._store.schema_version
                or compact_records != records
            )
        ):
            self._store.save({"items": compact_records})

    def _iter_valid_subs(self, records: list[Any]) -> list[SubscriptionInfo]:
        subs: list[SubscriptionInfo] = []
        for record in records:
            sub = _subscription_from_record(record)
            if sub is not None:
                subs.append(sub)
        return subs

    def _save_locked(self) -> None:
        self._store.save({"items": [_subscription_to_record(sub) for sub in self._subs.values()]})

    def _scan_extra_opts(
        self,
        ytdl_options_presets: Optional[list[str]],
        ytdl_options_overrides: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """Merge configured presets (in order) with per-subscription overrides.

        Applied on top of the global YTDL_OPTIONS when scanning a
        subscription's feed, so cookies/impersonation/etc. configured via a
        preset or override also take effect during the flat-playlist scan,
        not just the eventual per-video download. (The global YTDL_OPTIONS base
        is already spread into the scan params by ``_build_ydl_params``.)
        """
        return merge_ytdl_option_layers(
            ytdl_options_presets, ytdl_options_overrides, self.config.YTDL_OPTIONS_PRESETS
        )

    async def _queue_subscription_entries(
        self,
        entries: list[dict],
        *,
        download_type: str,
        codec: str,
        format: str,
        quality: str,
        folder: str,
        custom_name_prefix: str,
        playlist_item_limit: int,
        auto_start: bool,
        split_by_chapters: bool,
        chapter_template: str,
        subtitle_language: str,
        subtitle_mode: str,
        ytdl_options_presets: Optional[list[str]] = None,
        ytdl_options_overrides: Optional[dict[str, Any]] = None,
        clip_start: Optional[float] = None,
        clip_end: Optional[float] = None,
        sponsorblock: bool = False,
        audio_tags: str = "with_cover",
    ) -> tuple[list[str], list[str]]:
        queued_ids: list[str] = []
        queue_errors: list[str] = []
        presets = list(ytdl_options_presets or [])
        for ent in entries:
            eid = _entry_id(ent)
            vurl = _entry_video_url(ent)
            if not eid or not vurl:
                continue
            queue_entry = dict(ent)
            if "id" not in queue_entry:
                queue_entry["id"] = eid
            queue_entry["_type"] = "video"
            queue_entry["webpage_url"] = vurl
            result = await self.dqueue.add_entry(
                queue_entry,
                download_type,
                codec,
                format,
                quality,
                folder or None,
                custom_name_prefix,
                playlist_item_limit,
                auto_start,
                split_by_chapters,
                chapter_template or None,
                subtitle_language,
                subtitle_mode,
                presets,
                ytdl_options_overrides,
                clip_start,
                clip_end,
                sponsorblock=sponsorblock,
                audio_tags=audio_tags,
            )
            if isinstance(result, dict) and result.get("status") == "error":
                msg = str(result.get("msg") or f"Queueing failed for {vurl}")
                queue_errors.append(msg)
                log.warning("Subscription queueing failed for %s: %s", vurl, msg)
                continue
            queued_ids.append(eid)
        return queued_ids, queue_errors

    def list_all(self) -> list[SubscriptionInfo]:
        return list(self._subs.values())

    def get(self, sub_id: str) -> Optional[SubscriptionInfo]:
        return self._subs.get(sub_id)

    def start_background_loop(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        # bg_tasks.create_task already logs unexpected task failures with the name.
        self._loop_task = bg_tasks.create_task(self._periodic_loop(), name="subscription_loop")

    async def _periodic_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.run_due_checks()
            except Exception as e:
                log.exception("Subscription periodic check error: %s", e)

    async def run_due_checks(self) -> None:
        now = time.time()
        due: list[SubscriptionInfo] = []
        async with self._lock:
            for sub in list(self._subs.values()):
                if not sub.enabled:
                    continue
                interval_min = int(sub.check_interval_minutes)
                if sub.kind == KIND_LIVE and sub.live_recording:
                    # Recording: check often enough to notice the stream
                    # leaving the subscribed categories.
                    interval_min = min(interval_min, int(sub.live_recheck_minutes))
                interval_sec = max(60, interval_min * 60)
                if sub.last_checked is None:
                    due.append(sub)
                    continue
                if now - sub.last_checked < interval_sec:
                    continue
                due.append(sub)
        await self._check_many(due)

    async def _check_many(self, subs: list[SubscriptionInfo]) -> None:
        """Check subscriptions with bounded concurrency so one slow feed does
        not serialize the rest. Failures are isolated per subscription."""
        if not subs:
            return
        sem = asyncio.Semaphore(_MAX_CONCURRENT_CHECKS)

        async def _guarded(sub: SubscriptionInfo) -> None:
            async with sem:
                await self._check_one_unlocked(sub)

        results = await asyncio.gather(
            *(_guarded(sub) for sub in subs), return_exceptions=True
        )
        for sub, result in zip(subs, results):
            if isinstance(result, Exception):
                log.error(
                    "Subscription check crashed for %s: %s",
                    sub.name,
                    result,
                    exc_info=result,
                )

    async def add_subscription(
        self,
        url: str,
        *,
        check_interval_minutes: int,
        download_type: str,
        codec: str,
        format: str,
        quality: str,
        folder: str,
        custom_name_prefix: str,
        auto_start: bool,
        playlist_item_limit: int,
        split_by_chapters: bool,
        chapter_template: str,
        subtitle_language: str,
        subtitle_mode: str,
        ytdl_options_presets: Optional[list[str]] = None,
        ytdl_options_overrides: Optional[dict[str, Any]] = None,
        sponsorblock: bool = False,
        audio_tags: str = "with_cover",
        title_regex: Any = None,
        skip_subscriber_only: Any = None,
        clip_start: Optional[float] = None,
        clip_end: Optional[float] = None,
        live_categories: Any = None,
        live_stop_after_minutes: Any = None,
        live_recheck_minutes: Any = None,
    ) -> dict:
        url = self._normalize_url(url)
        if not url:
            return {"status": "error", "msg": "Missing URL"}
        try:
            live_categories_stored = validate_live_categories(live_categories)
            stop_after = validate_minutes(
                live_stop_after_minutes, field_name="live_stop_after_minutes",
                minimum=0, default=DEFAULT_LIVE_STOP_AFTER_MINUTES,
            )
            recheck = validate_minutes(
                live_recheck_minutes, field_name="live_recheck_minutes",
                minimum=1, default=DEFAULT_LIVE_RECHECK_MINUTES,
            )
        except ValueError as exc:
            return {"status": "error", "msg": str(exc)}
        # SSRF guard: block non-http(s) schemes and internal/metadata hosts
        # before yt-dlp fetches the feed. May do a DNS lookup, so run off-loop.
        # The scan's own options pick the proxy, so a feed fetched through one
        # is not resolved here — see validate_url.
        proxies = download_proxies({
            **self.config.YTDL_OPTIONS,
            **self._scan_extra_opts(ytdl_options_presets, ytdl_options_overrides),
        })
        url_error = await asyncio.get_running_loop().run_in_executor(
            None, partial(validate_url, url,
                          allow_private=getattr(self.config, "ALLOW_PRIVATE_ADDRESSES", False),
                          proxies=proxies))
        if url_error is not None:
            log.warning('Rejected subscription URL "%s": %s', url, url_error)
            return {"status": "error", "msg": url_error}
        try:
            title_regex_stored = validate_title_regex(title_regex)
        except re.error as exc:
            return {"status": "error", "msg": f"Invalid title_regex: {exc}"}
        try:
            skip_so = coerce_optional_bool(
                skip_subscriber_only,
                default=False,
                field_name="skip_subscriber_only",
            )
        except ValueError as exc:
            return {"status": "error", "msg": str(exc)}

        async with self._lock:
            if url in self._url_index or url in self._pending_urls:
                return {"status": "error", "msg": "This URL is already subscribed"}
            self._pending_urls.add(url)

        try:
            scan_first = max(int(getattr(self.config, "SUBSCRIPTION_SCAN_PLAYLIST_END", 50)), 1)
            scan_extra_opts = self._scan_extra_opts(ytdl_options_presets, ytdl_options_overrides)
            loop = asyncio.get_running_loop()
            kind = KIND_FEED
            live_stream: Optional[dict] = None
            try:
                info, entries = await loop.run_in_executor(
                    None,
                    partial(extract_flat_playlist, self.config, url, scan_first, extra_opts=scan_extra_opts),
                )
            except yt_dlp.utils.YoutubeDLError as exc:
                # An offline live channel (Twitch, Kick, YouTube /live, ...).
                if not _is_not_live_error(exc):
                    return {"status": "error", "msg": str(exc)}
                kind = KIND_LIVE
                info, entries = None, []

            if kind == KIND_FEED:
                if not info:
                    return {"status": "error", "msg": "Could not resolve URL"}
                etype = info.get("_type") or "video"
                if etype not in ("playlist", "channel"):
                    if _is_live_info(info):
                        kind = KIND_LIVE
                    elif etype.startswith("url"):
                        # A /live page pointing at the channel's current stream.
                        try:
                            live_stream = await loop.run_in_executor(
                                None,
                                partial(probe_live_channel, self.config, url, extra_opts=scan_extra_opts),
                            )
                        except yt_dlp.utils.YoutubeDLError as exc:
                            return {"status": "error", "msg": str(exc)}
                        if live_stream is None:
                            return {"status": "error", "msg": VIDEO_ONLY_MSG}
                        kind = KIND_LIVE
                    else:
                        return {"status": "error", "msg": VIDEO_ONLY_MSG}

            all_ids: list[str] = []
            if kind == KIND_LIVE:
                info = info or {}
                # The display name is the channel, not the current stream title.
                name = (
                    info.get("uploader")
                    or info.get("channel")
                    or (live_stream or {}).get("uploader")
                    or url.rstrip("/").rsplit("/", 1)[-1]
                    or url
                )
                # No seen ids: a stream already live when subscribing is
                # captured on the first check, if it matches the filters.
                last_checked: Optional[float] = None
            else:
                name = (
                    info.get("title")
                    or info.get("channel")
                    or info.get("playlist_title")
                    or info.get("uploader")
                    or url
                )
                last_checked = time.time()

                seen_entries = [ent for ent in entries if _is_media_entry(ent)]
                for ent in seen_entries:
                    if ent.get("live_status") == "is_upcoming":
                        continue  # Don't mark scheduled streams as seen; queue them when they go live
                    eid = _entry_id(ent)
                    if eid:
                        all_ids.append(eid)

            sub = SubscriptionInfo(
                id=str(uuid.uuid4()),
                name=str(name),
                url=url,
                enabled=True,
                check_interval_minutes=max(1, int(check_interval_minutes)),
                download_type=download_type,
                codec=codec,
                format=format,
                quality=quality,
                folder=folder or "",
                custom_name_prefix=custom_name_prefix or "",
                auto_start=bool(auto_start),
                playlist_item_limit=int(playlist_item_limit),
                split_by_chapters=bool(split_by_chapters),
                sponsorblock=bool(sponsorblock),
                audio_tags=audio_tags,
                chapter_template=chapter_template or "",
                subtitle_language=subtitle_language,
                subtitle_mode=subtitle_mode,
                ytdl_options_presets=list(ytdl_options_presets or []),
                ytdl_options_overrides=dict(ytdl_options_overrides or {}),
                title_regex=title_regex_stored,
                skip_subscriber_only=skip_so,
                clip_start=clip_start,
                clip_end=clip_end,
                kind=kind,
                live_categories=live_categories_stored,
                live_stop_after_minutes=stop_after,
                live_recheck_minutes=recheck,
                avatar_url=_feed_avatar(info) if kind == KIND_FEED else "",
                last_checked=last_checked,
                seen_ids=list(dict.fromkeys(all_ids)),
                error=None,
            )

            async with self._lock:
                if url in self._url_index:
                    return {"status": "error", "msg": "This URL is already subscribed"}
                self._subs[sub.id] = sub
                self._url_index[url] = sub.id
                try:
                    self._save_locked()
                except Exception:
                    self._subs.pop(sub.id, None)
                    self._url_index.pop(url, None)
                    raise

            await self.notifier.subscription_added(sub)
            if kind == KIND_LIVE:
                log.info("Added live subscription %s (%s)", sub.name, url)
                # Check straight away rather than waiting for the next minute tick.
                bg_tasks.create_task(self._check_many([sub]), name="live_subscription_first_check")
            return {"status": "ok", "subscription": sub.to_public_dict()}
        finally:
            async with self._lock:
                self._pending_urls.discard(url)

    async def delete_subscriptions(self, ids: list[str]) -> dict:
        removed: list[str] = []
        async with self._lock:
            previous_subs = self._subs.copy()
            previous_index = self._url_index.copy()
            for sid in ids:
                sub = self._subs.pop(sid, None)
                if sub:
                    normalized_url = self._normalize_url(sub.url)
                    self._url_index.pop(normalized_url, None)
                    removed.append(sid)
            if removed:
                try:
                    self._save_locked()
                except Exception:
                    self._subs = previous_subs
                    self._url_index = previous_index
                    raise
        for sid in removed:
            await self.notifier.subscription_removed(sid)
        return {"status": "ok"}

    async def update_subscription(self, sub_id: str, changes: dict) -> dict:
        validated_name: Optional[str] = None
        if "name" in changes:
            try:
                validated_name = validate_subscription_name(changes["name"])
            except ValueError as exc:
                return {"status": "error", "msg": str(exc)}

        validated_folder: Optional[str] = None
        if "folder" in changes:
            try:
                validated_folder = validate_subscription_folder(changes["folder"])
            except ValueError as exc:
                return {"status": "error", "msg": str(exc)}

        validated_tr: Optional[str] = None
        if "title_regex" in changes:
            try:
                validated_tr = validate_title_regex(changes["title_regex"])
            except re.error as exc:
                return {"status": "error", "msg": f"Invalid title_regex: {exc}"}

        skip_so_set = False
        validated_skip_so = False
        if "skip_subscriber_only" in changes:
            try:
                validated_skip_so = coerce_optional_bool(
                    changes["skip_subscriber_only"],
                    field_name="skip_subscriber_only",
                )
                skip_so_set = True
            except ValueError as exc:
                return {"status": "error", "msg": str(exc)}

        validated_categories: Optional[list[str]] = None
        if "live_categories" in changes:
            try:
                validated_categories = validate_live_categories(changes["live_categories"])
            except ValueError as exc:
                return {"status": "error", "msg": str(exc)}

        validated_minutes: dict[str, int] = {}
        for name, minimum, default in (
            ("live_stop_after_minutes", 0, DEFAULT_LIVE_STOP_AFTER_MINUTES),
            ("live_recheck_minutes", 1, DEFAULT_LIVE_RECHECK_MINUTES),
        ):
            if name in changes:
                try:
                    validated_minutes[name] = validate_minutes(
                        changes[name], field_name=name, minimum=minimum, default=default
                    )
                except ValueError as exc:
                    return {"status": "error", "msg": str(exc)}

        enabled_set = False
        validated_enabled = False
        if "enabled" in changes:
            try:
                validated_enabled = _coerce_bool(changes["enabled"])
                enabled_set = True
            except ValueError as exc:
                return {"status": "error", "msg": str(exc)}

        interval_set = False
        validated_interval = 0
        if "check_interval_minutes" in changes:
            try:
                validated_interval = max(1, int(changes["check_interval_minutes"]))
            except (TypeError, ValueError):
                return {"status": "error", "msg": "check_interval_minutes must be an integer"}
            interval_set = True

        async with self._lock:
            sub = self._subs.get(sub_id)
            if not sub:
                return {"status": "error", "msg": "Subscription not found"}
            previous = copy.deepcopy(sub)
            old_enabled = sub.enabled

            if enabled_set:
                sub.enabled = validated_enabled
            if interval_set:
                sub.check_interval_minutes = validated_interval
            if validated_name is not None:
                sub.name = validated_name
            if validated_folder is not None:
                # Applies to future downloads only; files already downloaded
                # stay where they are.
                sub.folder = validated_folder
            if validated_tr is not None:
                sub.title_regex = validated_tr
            if skip_so_set:
                sub.skip_subscriber_only = validated_skip_so
            if validated_categories is not None:
                sub.live_categories = validated_categories
            for name, minutes in validated_minutes.items():
                setattr(sub, name, minutes)

            try:
                self._save_locked()
            except Exception:
                self._subs[sub_id] = previous
                raise
            updated = sub
        if "enabled" in changes and updated.enabled != old_enabled:
            log.info(
                "Subscription %s %s",
                updated.name,
                "resumed" if updated.enabled else "paused",
            )
        await self.notifier.subscription_updated(updated)
        return {"status": "ok", "subscription": updated.to_public_dict()}

    async def check_now(self, ids: Optional[list[str]] = None) -> dict:
        async with self._lock:
            targets = (
                [self._subs[i] for i in ids if i in self._subs]
                if ids
                else [s for s in self._subs.values() if s.enabled]
            )
        log.info(
            "Manual subscription check requested for %d subscription(s)",
            len(targets),
        )
        await self._check_many(targets)
        return {"status": "ok"}

    async def _check_one_unlocked(self, sub: SubscriptionInfo) -> None:
        sid = sub.id
        # Prevent overlapping checks for the same subscription (e.g. the periodic
        # loop and a manual check-now firing together), which could double-queue
        # entries and drop seen_ids via a read-modify-write race.
        async with self._lock:
            if sid in self._checks_in_flight:
                log.info("Subscription check already in progress for %s, skipping", sub.name)
                return
            self._checks_in_flight.add(sid)
        try:
            if sub.kind == KIND_LIVE:
                await self._refresh_live_avatar(sub)
                await self._check_live_inner(sub)
            else:
                await self._check_one_inner(sub)
        finally:
            async with self._lock:
                self._checks_in_flight.discard(sid)

    async def _check_one_inner(self, sub: SubscriptionInfo) -> None:
        sid = sub.id
        scan = int(getattr(self.config, "SUBSCRIPTION_SCAN_PLAYLIST_END", 50))
        # ytdl_options_presets/overrides are set at subscription creation and
        # never mutated afterwards (update_subscription doesn't allow it), so
        # reading them off `sub` here without holding the lock is safe.
        scan_extra_opts = self._scan_extra_opts(sub.ytdl_options_presets, sub.ytdl_options_overrides)
        log.info("Checking subscription: %s", sub.name)
        try:
            info, entries = await asyncio.get_running_loop().run_in_executor(
                None,
                partial(extract_flat_playlist, self.config, sub.url, scan, extra_opts=scan_extra_opts),
            )
        except yt_dlp.utils.YoutubeDLError as exc:
            async with self._lock:
                cur = self._subs.get(sid)
                if cur:
                    previous = copy.deepcopy(cur)
                    cur.error = str(exc)
                    cur.last_checked = time.time()
                    try:
                        self._save_locked()
                    except Exception:
                        self._subs[sid] = previous
                        raise
                    sub = cur
            log.warning("Subscription check failed for %s: %s", sub.name, exc)
            await self.notifier.subscription_updated(sub)
            return
        entries = [ent for ent in entries if _is_media_entry(ent)]

        etype = (info or {}).get("_type") or "video"
        if etype == "video":
            async with self._lock:
                cur = self._subs.get(sid)
                if cur:
                    previous = copy.deepcopy(cur)
                    cur.error = VIDEO_ONLY_MSG
                    cur.last_checked = time.time()
                    try:
                        self._save_locked()
                    except Exception:
                        self._subs[sid] = previous
                        raise
                    sub = cur
            log.warning("Subscription %s no longer resolves to a subscribable feed", sub.name)
            await self.notifier.subscription_updated(sub)
            return
        if not entries:
            async with self._lock:
                cur = self._subs.get(sid)
                if cur:
                    previous = copy.deepcopy(cur)
                    cur.last_checked = time.time()
                    cur.error = None
                    try:
                        self._save_locked()
                    except Exception:
                        self._subs[sid] = previous
                        raise
                    sub = cur
            log.warning("Subscription check finished for %s: No entries found", sub.name)
            await self.notifier.subscription_updated(sub)
            return

        async with self._lock:
            cur = self._subs.get(sid)
            if not cur:
                return
            seen = cur.seen_set()
            seen_ids_snapshot = list(cur.seen_ids)
            dl_type = cur.download_type
            dl_codec = cur.codec
            dl_format = cur.format
            dl_quality = cur.quality
            dl_folder = cur.folder
            dl_prefix = cur.custom_name_prefix
            dl_plimit = cur.playlist_item_limit
            dl_autostart = cur.auto_start
            dl_split = cur.split_by_chapters
            dl_sponsorblock = cur.sponsorblock
            dl_audio_tags = cur.audio_tags
            dl_chapter = cur.chapter_template
            dl_sublang = cur.subtitle_language
            dl_submode = cur.subtitle_mode
            dl_ytdl_presets = list(cur.ytdl_options_presets)
            dl_ytdl_overrides = dict(cur.ytdl_options_overrides)
            dl_title_regex = cur.title_regex or ""
            dl_skip_subscriber_only = bool(cur.skip_subscriber_only)
            dl_clip_start = cur.clip_start
            dl_clip_end = cur.clip_end

        new_entries: list[dict] = []
        for ent in entries:
            eid = _entry_id(ent)
            if not eid:
                continue
            # Seen entries that are currently live are deliberately re-queued:
            # a stream first seen as 'upcoming' must still be captured once it
            # goes live. The download queue dedups by URL while a capture is
            # in flight, so this can't double-queue an active capture.
            if eid in seen and ent.get("live_status") != "is_live":
                continue
            new_entries.append(ent)

        pattern_re: Optional[re.Pattern[str]] = None
        if dl_title_regex:
            try:
                pattern_re = re.compile(dl_title_regex)
            except re.error:
                log.warning(
                    "Invalid stored title_regex on subscription %s, ignoring filter",
                    sub.name,
                )

        queue_entries: list[dict] = []
        filtered_ids: list[str] = []
        for ent in new_entries:
            eid = _entry_id(ent)
            if pattern_re is not None:
                title = str(ent.get("title") or "")
                if not pattern_re.search(title):
                    if eid:
                        filtered_ids.append(eid)
                    continue
            queue_entries.append(ent)

        subscriber_filtered_ids: list[str] = []
        if dl_skip_subscriber_only:
            kept_entries: list[dict] = []
            for ent in queue_entries:
                eid = _entry_id(ent)
                if _is_subscriber_only_entry(ent):
                    if eid:
                        subscriber_filtered_ids.append(eid)
                    continue
                kept_entries.append(ent)
            queue_entries = kept_entries

        queued_ids, queue_errors = await self._queue_subscription_entries(
            queue_entries,
            download_type=dl_type,
            codec=dl_codec,
            format=dl_format,
            quality=dl_quality,
            folder=dl_folder,
            custom_name_prefix=dl_prefix,
            playlist_item_limit=dl_plimit,
            auto_start=dl_autostart,
            split_by_chapters=dl_split,
            sponsorblock=dl_sponsorblock,
            audio_tags=dl_audio_tags,
            chapter_template=dl_chapter or "",
            subtitle_language=dl_sublang,
            subtitle_mode=dl_submode,
            ytdl_options_presets=dl_ytdl_presets,
            ytdl_options_overrides=dl_ytdl_overrides,
            clip_start=dl_clip_start,
            clip_end=dl_clip_end,
        )
        log.info(
            "Subscription check finished for %s: %d new, %d filtered, %d subscriber_skipped, %d queued, %d failed",
            sub.name,
            len(new_entries),
            len(filtered_ids),
            len(subscriber_filtered_ids),
            len(queued_ids),
            len(queue_errors),
        )

        merged = list(
            dict.fromkeys(
                queued_ids + filtered_ids + subscriber_filtered_ids + seen_ids_snapshot
            )
        )
        max_seen = int(getattr(self.config, "SUBSCRIPTION_MAX_SEEN_IDS", 50000))
        if len(merged) > max_seen:
            merged = merged[:max_seen]

        async with self._lock:
            cur = self._subs.get(sid)
            if not cur:
                return
            previous = copy.deepcopy(cur)
            cur.seen_ids = merged
            cur.avatar_url = _feed_avatar(info) or cur.avatar_url
            cur.last_checked = time.time()
            cur.error = "; ".join(list(dict.fromkeys(queue_errors))[:3]) if queue_errors else None
            try:
                self._save_locked()
            except Exception:
                self._subs[sid] = previous
                raise
            sub = cur
        await self.notifier.subscription_updated(sub)

    async def _refresh_live_avatar(self, sub: SubscriptionInfo) -> None:
        """Look up a live channel's profile picture, at most once a day.

        Purely cosmetic, so any failure is logged and the check goes on.
        """
        now = time.time()
        if sub.avatar_checked is not None and now - sub.avatar_checked < _AVATAR_REFRESH_SECONDS:
            return
        try:
            avatar = await asyncio.get_running_loop().run_in_executor(
                None, partial(stream_categories.channel_avatar_sync, self.config, sub.url)
            )
        except Exception as exc:
            log.info("Could not look up the profile picture for %s: %s", sub.name, exc)
            avatar = None
        async with self._lock:
            cur = self._subs.get(sub.id)
            if not cur:
                return
            previous = copy.deepcopy(cur)
            cur.avatar_checked = now
            if avatar:
                cur.avatar_url = avatar
            try:
                self._save_locked()
            except Exception:
                self._subs[sub.id] = previous
                raise

    def _live_capture_state(self, url: str) -> str:
        state_fn = getattr(self.dqueue, "live_capture_state", None)
        if not url or not callable(state_fn):
            return "none"
        return state_fn(url)

    async def _check_live_inner(self, sub: SubscriptionInfo) -> None:
        """Check a live subscription: record the stream while it matches.

        Each stream is recorded once (its id goes into seen_ids). It is
        recorded again when its recording failed while the channel is still
        live — a dropped connection — but a cancelled recording stays
        cancelled. While a recording runs, a stream that stops matching the
        filters for live_stop_after_minutes gets its recording stopped (the
        file is kept) and its id forgotten, so it is recorded afresh if it
        matches again.
        """
        sid = sub.id
        scan_extra_opts = self._scan_extra_opts(sub.ytdl_options_presets, sub.ytdl_options_overrides)
        log.info("Checking live subscription: %s", sub.name)
        try:
            stream = await asyncio.get_running_loop().run_in_executor(
                None,
                partial(probe_live_channel, self.config, sub.url, extra_opts=scan_extra_opts),
            )
        except yt_dlp.utils.YoutubeDLError as exc:
            log.warning("Live subscription check failed for %s: %s", sub.name, exc)
            # A failed probe says nothing about the stream: keep the mismatch
            # timer and the recording state as they were.
            await self._finish_live_check(
                sid, live_now=False, error=str(exc), mismatch_since=_KEEP,
                capture_state=self._live_capture_state(sub.live_capture_url),
            )
            return

        async with self._lock:
            cur = self._subs.get(sid)
            if not cur:
                return
            categories = list(cur.live_categories)
            title_regex = cur.title_regex or ""
            seen = cur.seen_set()
            mismatch_since = cur.live_mismatch_since
            stop_after_sec = max(0, int(cur.live_stop_after_minutes)) * 60
            capture_url = cur.live_capture_url
            opts = copy.deepcopy(cur)
        capture_state = self._live_capture_state(capture_url)

        if stream is None:
            log.info("Live subscription check finished for %s: offline", sub.name)
            await self._finish_live_check(sid, live_now=False, capture_state=capture_state)
            return

        pattern_re: Optional[re.Pattern[str]] = None
        if title_regex:
            try:
                pattern_re = re.compile(title_regex)
            except re.error:
                log.warning("Invalid stored title_regex on subscription %s, ignoring filter", sub.name)

        live_category = ", ".join(stream["categories"])
        stream_id = stream["id"]
        stream_url = stream["url"] or sub.url
        matches, match_error = live_stream_matches(stream, categories, pattern_re)
        if not matches:
            now = time.time()
            forget_id = None
            if capture_state != "active" or not stop_after_sec:
                mismatch_since = None
            else:
                mismatch_since = mismatch_since or now
                if now - mismatch_since >= stop_after_sec:
                    stop_fn = getattr(self.dqueue, "stop_live_capture", None)
                    if callable(stop_fn) and stop_fn(capture_url):
                        log.info(
                            "Stopping recording for %s: the stream has not matched the filters for %d min",
                            sub.name, stop_after_sec // 60,
                        )
                        capture_state = "stopping"
                        forget_id = stream_id
                    mismatch_since = None
            log.info(
                "Live subscription check finished for %s: live, filters do not match (title=%r, categories=%r)",
                sub.name, stream["title"], stream["categories"],
            )
            await self._finish_live_check(
                sid, live_now=True, title=stream["title"], category=live_category, error=match_error,
                mismatch_since=mismatch_since, capture_state=capture_state, forget_id=forget_id,
            )
            return

        if stream_id in seen:
            capture_failed = getattr(self.dqueue, "live_capture_failed", None)
            if not (callable(capture_failed) and capture_failed(stream_url, stream_id)):
                log.info("Live subscription check finished for %s: stream already recorded", sub.name)
                await self._finish_live_check(
                    sid, live_now=True, title=stream["title"], category=live_category,
                    mismatch_since=None, capture_state=capture_state,
                )
                return
            log.info("Live recording for %s failed while still live; recording again", sub.name)

        if self._live_capture_state(stream_url) == "stopping":
            # The previous recording is still finalizing; queueing now would
            # be deduplicated against it. Start the new one on the next check.
            log.info("Live subscription check finished for %s: waiting for the stopped recording to finish", sub.name)
            await self._finish_live_check(
                sid, live_now=True, title=stream["title"], category=live_category,
                mismatch_since=None, capture_state="stopping",
            )
            return

        title_parts = [p for p in (stream.get("uploader"), stream["title"]) if p]
        entry = {
            "id": stream_id,
            "title": " - ".join(dict.fromkeys(title_parts)) or stream_url,
            "webpage_url": stream_url,
            "live_status": "is_live",
        }
        queued_ids, queue_errors = await self._queue_subscription_entries(
            [entry],
            download_type=opts.download_type,
            codec=opts.codec,
            format=opts.format,
            quality=opts.quality,
            folder=opts.folder,
            custom_name_prefix=opts.custom_name_prefix,
            playlist_item_limit=opts.playlist_item_limit,
            auto_start=opts.auto_start,
            split_by_chapters=opts.split_by_chapters,
            sponsorblock=opts.sponsorblock,
            audio_tags=opts.audio_tags,
            chapter_template=opts.chapter_template or "",
            subtitle_language=opts.subtitle_language,
            subtitle_mode=opts.subtitle_mode,
            ytdl_options_presets=list(opts.ytdl_options_presets),
            ytdl_options_overrides=dict(opts.ytdl_options_overrides),
            clip_start=opts.clip_start,
            clip_end=opts.clip_end,
        )
        log.info(
            "Live subscription check finished for %s: live, %s",
            sub.name,
            "recording queued" if queued_ids else "queueing failed",
        )
        await self._finish_live_check(
            sid,
            live_now=True,
            title=stream["title"],
            category=live_category,
            error="; ".join(queue_errors[:3]) if queue_errors else None,
            captured_id=queued_ids[0] if queued_ids else None,
            capture_url=stream_url if queued_ids else _KEEP,
            capture_state="active" if queued_ids else capture_state,
            mismatch_since=None,
        )

    async def _finish_live_check(
        self,
        sid: str,
        *,
        live_now: bool,
        title: str = "",
        category: str = "",
        error: Optional[str] = None,
        captured_id: Optional[str] = None,
        forget_id: Optional[str] = None,
        mismatch_since: Any = None,
        capture_url: Any = _KEEP,
        capture_state: str = "none",
    ) -> None:
        async with self._lock:
            cur = self._subs.get(sid)
            if not cur:
                return
            previous = copy.deepcopy(cur)
            cur.live_now = live_now
            cur.live_title = title
            cur.live_category = category
            cur.error = error
            cur.last_checked = time.time()
            if mismatch_since is not _KEEP:
                cur.live_mismatch_since = mismatch_since
            if capture_url is not _KEEP:
                cur.live_capture_url = capture_url
            cur.live_recording = capture_state != "none"
            seen_ids = list(cur.seen_ids)
            if forget_id is not None:
                seen_ids = [i for i in seen_ids if i != forget_id]
            if captured_id is not None:
                seen_ids = [captured_id] + seen_ids
            if forget_id is not None or captured_id is not None:
                cur.seen_ids = self._normalize_seen_ids(seen_ids)
            try:
                self._save_locked()
            except Exception:
                self._subs[sid] = previous
                raise
            sub = cur
        await self.notifier.subscription_updated(sub)

    async def emit_all(self) -> None:
        await self.notifier.subscriptions_all(self.list_all())
