from __future__ import annotations

import asyncio
import json
import os
import shelve
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

fake_yt_dlp = types.ModuleType("yt_dlp")
fake_networking = types.ModuleType("yt_dlp.networking")
fake_impersonate = types.ModuleType("yt_dlp.networking.impersonate")


class _ImpersonateTarget:
    @staticmethod
    def from_str(value):
        return value


fake_impersonate.ImpersonateTarget = _ImpersonateTarget
fake_networking.impersonate = fake_impersonate
fake_yt_dlp.networking = fake_networking
fake_yt_dlp.utils = types.SimpleNamespace(YoutubeDLError=Exception)
sys.modules.setdefault("yt_dlp", fake_yt_dlp)
sys.modules.setdefault("yt_dlp.networking", fake_networking)
sys.modules.setdefault("yt_dlp.networking.impersonate", fake_impersonate)

import subscriptions
from subscriptions import (
    NO_ENTRIES_MSG,
    VIDEO_ONLY_MSG,
    SubscriptionInfo,
    SubscriptionManager,
    _is_subscriber_only_entry,
    browse_playlist,
    coerce_optional_bool,
    extract_flat_playlist,
)


class _Config:
    def __init__(self, state_dir: str):
        self.STATE_DIR = state_dir
        self.SUBSCRIPTION_SCAN_PLAYLIST_END = 50
        self.SUBSCRIPTION_MAX_SEEN_IDS = 50000
        self.DOWNLOAD_DIR = state_dir
        self.TEMP_DIR = state_dir
        self.YTDL_OPTIONS = {}
        self.YTDL_OPTIONS_PRESETS = {}


class _Queue:
    def __init__(self):
        self.entries = []
        self.fail = False

    async def add(self, *args, **kwargs):
        return None

    async def add_entry(self, entry, *args, **kwargs):
        if self.fail:
            return {"status": "error", "msg": "queue failed"}
        self.entries.append((entry, args, kwargs))
        return {"status": "ok"}


class _Notifier:
    async def subscription_added(self, sub):
        return None

    async def subscription_updated(self, sub):
        return None

    async def subscription_removed(self, sub_id):
        return None

    async def subscriptions_all(self, subs):
        return None


def _create_legacy_shelf(path: str, record) -> None:
    with shelve.open(path, "c") as shelf:
        shelf["sub-1"] = record


class SubscriberOnlyHelperTests(unittest.TestCase):
    def test_is_subscriber_only_detects_availability(self):
        self.assertTrue(_is_subscriber_only_entry({"availability": "subscriber_only"}))
        self.assertFalse(_is_subscriber_only_entry({"availability": None}))
        self.assertFalse(_is_subscriber_only_entry({}))

    def test_coerce_optional_bool_defaults_and_fields(self):
        self.assertFalse(coerce_optional_bool(None, default=False))
        self.assertTrue(coerce_optional_bool(True))
        self.assertFalse(coerce_optional_bool(False))
        with self.assertRaises(ValueError):
            coerce_optional_bool("maybe", field_name="skip_subscriber_only")


class SubscriptionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def test_load_imports_legacy_subscription_shelf(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy_path = os.path.join(tmp, "subscriptions")
            json_path = os.path.join(tmp, "subscriptions.json")
            _create_legacy_shelf(
                legacy_path,
                {
                    "id": "sub-1",
                    "name": "Channel",
                    "url": "https://example.com/channel",
                    "timestamp": 1.0,
                },
            )

            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())

            self.assertEqual(len(mgr.list_all()), 1)
            self.assertTrue(os.path.exists(json_path))
            with open(json_path, encoding="utf-8") as f:
                payload = json.load(f)
            self.assertEqual(payload["schema_version"], 2)
            self.assertNotIn("timestamp", payload["items"][0])

    def test_invalid_json_is_quarantined_and_legacy_is_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy_path = os.path.join(tmp, "subscriptions")
            json_path = os.path.join(tmp, "subscriptions.json")
            _create_legacy_shelf(
                legacy_path,
                {
                    "id": "sub-1",
                    "name": "Channel",
                    "url": "https://example.com/channel",
                    "timestamp": 1.0,
                },
            )
            with open(json_path, "w", encoding="utf-8") as f:
                f.write("{not valid json")

            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())

            self.assertEqual(len(mgr.list_all()), 1)
            self.assertTrue(
                any(name.startswith("subscriptions.json.invalid.") for name in os.listdir(tmp))
            )

    def test_load_rewrites_old_json_and_trims_seen_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "subscriptions.json")
            cfg = _Config(tmp)
            cfg.SUBSCRIPTION_MAX_SEEN_IDS = 2
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "schema_version": 1,
                        "kind": "subscriptions",
                        "items": [
                            {
                                "id": "sub-1",
                                "name": "Channel",
                                "url": "https://example.com/channel",
                                "enabled": True,
                                "check_interval_minutes": 60,
                                "download_type": "video",
                                "codec": "auto",
                                "format": "any",
                                "quality": "best",
                                "folder": "",
                                "custom_name_prefix": "",
                                "auto_start": True,
                                "playlist_item_limit": 0,
                                "split_by_chapters": False,
                                "chapter_template": "",
                                "subtitle_language": "en",
                                "subtitle_mode": "prefer_manual",
                                "last_checked": None,
                                "seen_ids": ["a", "b", "a", "c"],
                                "error": None,
                                "timestamp": 123,
                            }
                        ],
                    },
                    f,
                )

            mgr = SubscriptionManager(cfg, _Queue(), _Notifier())
            self.assertEqual(mgr.list_all()[0].seen_ids, ["a", "b"])

            with open(json_path, encoding="utf-8") as f:
                payload = json.load(f)

            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["items"][0]["seen_ids"], ["a", "b"])
            self.assertNotIn("timestamp", payload["items"][0])

    async def test_add_subscription_rolls_back_when_state_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())

            orig_save = __import__("state_store").AtomicJsonStore.save

            def bad_save(store, data):
                if store.path == mgr._path:
                    raise OSError("simulated shelf failure")
                return orig_save(store, data)

            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [{"id": "v1", "webpage_url": "https://example.com/v1"}],
                ),
            ):
                with patch("subscriptions.AtomicJsonStore.save", bad_save):
                    with self.assertRaises(OSError):
                        await mgr.add_subscription(
                            "https://example.com/channel",
                            check_interval_minutes=60,
                            download_type="video",
                            codec="auto",
                            format="any",
                            quality="best",
                            folder="",
                            custom_name_prefix="",
                            auto_start=True,
                            playlist_item_limit=0,
                            split_by_chapters=False,
                            chapter_template="",
                            subtitle_language="en",
                            subtitle_mode="prefer_manual",
                        )

            self.assertEqual(mgr.list_all(), [])
            self.assertNotIn("https://example.com/channel", mgr._url_index)

    async def test_add_subscription_marks_existing_videos_seen_without_queueing(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [
                        {"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"},
                        {"id": "v2", "title": "Two", "webpage_url": "https://example.com/v2"},
                        {"id": "v3", "title": "Three", "webpage_url": "https://example.com/v3"},
                    ],
                ),
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )

            assert result["status"] == "ok"
            sub = mgr.list_all()[0]
            self.assertEqual(sub.seen_ids, ["v1", "v2", "v3"])
            self.assertIsNone(sub.error)
            self.assertEqual(queue.entries, [])

    async def test_add_subscription_skips_collection_tab_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [
                        {
                            "_type": "url",
                            "ie_key": "YoutubeTab",
                            "title": "Channel - Live",
                            "url": "https://example.com/live",
                            "webpage_url": "https://example.com/live",
                        },
                        {
                            "_type": "url",
                            "ie_key": "Youtube",
                            "id": "v1",
                            "title": "One",
                            "duration": 10,
                            "webpage_url": "https://example.com/v1",
                        },
                    ],
                ),
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )

            self.assertEqual(result["status"], "ok")
            sub = mgr.list_all()[0]
            self.assertEqual(sub.seen_ids, ["v1"])
            self.assertEqual(queue.entries, [])

    async def test_check_now_keeps_failed_queue_items_unseen_and_sets_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                side_effect=[
                    (
                        {"_type": "channel", "title": "Channel"},
                        [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                    ),
                    (
                        {"_type": "channel", "title": "Channel"},
                        [{"id": "v2", "title": "Two", "webpage_url": "https://example.com/v2"}],
                    ),
                ],
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )
                queue.fail = True
                await mgr.check_now([result["subscription"]["id"]])

            sub = mgr.list_all()[0]
            self.assertEqual(sub.error, "queue failed")
            self.assertEqual(sub.seen_ids, ["v1"])

    async def test_repeated_queue_errors_are_reported_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                side_effect=[
                    (
                        {"_type": "channel", "title": "Channel"},
                        [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                    ),
                    (
                        {"_type": "channel", "title": "Channel"},
                        [
                            {"id": "v3", "title": "Three", "webpage_url": "https://example.com/v3"},
                            {"id": "v2", "title": "Two", "webpage_url": "https://example.com/v2"},
                            {"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"},
                        ],
                    ),
                ],
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )
                queue.fail = True
                await mgr.check_now([result["subscription"]["id"]])

            sub = mgr.list_all()[0]
            self.assertEqual(sub.error, "queue failed")
            self.assertEqual(sub.seen_ids, ["v1"])

    async def test_check_now_queues_new_video_and_updates_seen_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                side_effect=[
                    (
                        {"_type": "channel", "title": "Channel"},
                        [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                    ),
                    (
                        {"_type": "channel", "title": "Channel"},
                        [
                            {"id": "v2", "title": "Two", "webpage_url": "https://example.com/v2"},
                            {"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"},
                        ],
                    ),
                ],
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )
                await mgr.check_now([result["subscription"]["id"]])

            sub = mgr.list_all()[0]
            self.assertIsNotNone(sub.last_checked)
            self.assertIsNone(sub.error)
            self.assertEqual(sub.seen_ids[:2], ["v2", "v1"])
            self.assertEqual([entry["webpage_url"] for entry, _, _ in queue.entries], ["https://example.com/v2"])

    async def test_check_now_applies_subscription_clip_bounds(self):
        """Issue #1049: clip bounds were the one download option a subscription
        could not carry, so they must reach every entry it queues."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                side_effect=[
                    (
                        {"_type": "channel", "title": "Channel"},
                        [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                    ),
                    (
                        {"_type": "channel", "title": "Channel"},
                        [
                            {"id": "v2", "title": "Two", "webpage_url": "https://example.com/v2"},
                            {"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"},
                        ],
                    ),
                ],
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    clip_start=30.0,
                    clip_end=90.0,
                )
                sub_id = result["subscription"]["id"]
                self.assertEqual(mgr.get(sub_id).clip_start, 30.0)
                self.assertEqual(mgr.get(sub_id).clip_end, 90.0)
                await mgr.check_now([sub_id])

            self.assertEqual(len(queue.entries), 1)
            _entry, args, _kwargs = queue.entries[0]
            # add_entry(entry, download_type, ..., ytdl_options_overrides, clip_start, clip_end)
            self.assertEqual(args[-2], 30.0)
            self.assertEqual(args[-1], 90.0)

    async def test_clip_bounds_survive_reload_and_default_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Config(tmp)
            queue = _Queue()
            mgr = SubscriptionManager(cfg, queue, _Notifier())
            sub_id = await self._add_one_subscription(mgr)
            # Records written before these fields existed simply take the defaults.
            self.assertIsNone(mgr.get(sub_id).clip_start)
            self.assertIsNone(mgr.get(sub_id).clip_end)

            mgr.get(sub_id).clip_start = 12.5
            async with mgr._lock:
                mgr._save_locked()

            reloaded = SubscriptionManager(cfg, _Queue(), _Notifier())
            self.assertEqual(reloaded.get(sub_id).clip_start, 12.5)
            self.assertIsNone(reloaded.get(sub_id).clip_end)

    async def test_check_now_applies_subscription_sponsorblock(self):
        """Subscriptions download unattended, so the sponsor-segment removal has
        to reach every entry the subscription queues, not just manual adds."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                side_effect=[
                    (
                        {"_type": "channel", "title": "Channel"},
                        [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                    ),
                    (
                        {"_type": "channel", "title": "Channel"},
                        [
                            {"id": "v2", "title": "Two", "webpage_url": "https://example.com/v2"},
                            {"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"},
                        ],
                    ),
                ],
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    sponsorblock=True,
                )
                sub_id = result["subscription"]["id"]
                self.assertTrue(mgr.get(sub_id).sponsorblock)
                await mgr.check_now([sub_id])

            self.assertEqual(len(queue.entries), 1)
            _entry, _args, kwargs = queue.entries[0]
            self.assertIs(kwargs["sponsorblock"], True)

    async def test_sponsorblock_survives_reload_and_defaults_to_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Config(tmp)
            mgr = SubscriptionManager(cfg, _Queue(), _Notifier())
            sub_id = await self._add_one_subscription(mgr)
            # Records written before the field existed simply take the default.
            self.assertFalse(mgr.get(sub_id).sponsorblock)

            mgr.get(sub_id).sponsorblock = True
            async with mgr._lock:
                mgr._save_locked()

            reloaded = SubscriptionManager(cfg, _Queue(), _Notifier())
            self.assertTrue(reloaded.get(sub_id).sponsorblock)

    async def test_check_now_applies_subscription_audio_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                side_effect=[
                    (
                        {"_type": "channel", "title": "Channel"},
                        [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                    ),
                    (
                        {"_type": "channel", "title": "Channel"},
                        [
                            {"id": "v2", "title": "Two", "webpage_url": "https://example.com/v2"},
                            {"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"},
                        ],
                    ),
                ],
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="audio",
                    codec="auto",
                    format="auto",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    audio_tags="none",
                )
                sub_id = result["subscription"]["id"]
                await mgr.check_now([sub_id])

            self.assertEqual(len(queue.entries), 1)
            _entry, _args, kwargs = queue.entries[0]
            self.assertEqual(kwargs["audio_tags"], "none")

    async def test_audio_tags_survive_reload_and_default_to_with_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Config(tmp)
            mgr = SubscriptionManager(cfg, _Queue(), _Notifier())
            sub_id = await self._add_one_subscription(mgr)
            # Records written before the field existed simply take the default.
            self.assertEqual(mgr.get(sub_id).audio_tags, "with_cover")

            mgr.get(sub_id).audio_tags = "no_cover"
            async with mgr._lock:
                mgr._save_locked()

            reloaded = SubscriptionManager(cfg, _Queue(), _Notifier())
            self.assertEqual(reloaded.get(sub_id).audio_tags, "no_cover")

    async def test_check_now_queues_subscriber_only_when_skip_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                side_effect=[
                    (
                        {"_type": "channel", "title": "Channel"},
                        [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                    ),
                    (
                        {"_type": "channel", "title": "Channel"},
                        [
                            {
                                "id": "v2",
                                "title": "Members",
                                "webpage_url": "https://example.com/v2",
                                "availability": "subscriber_only",
                            },
                            {"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"},
                        ],
                    ),
                ],
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    skip_subscriber_only=False,
                )
                self.assertFalse(mgr.list_all()[0].skip_subscriber_only)
                await mgr.check_now([result["subscription"]["id"]])

            sub = mgr.list_all()[0]
            self.assertIsNone(sub.error)
            self.assertEqual(sub.seen_ids[:2], ["v2", "v1"])
            self.assertEqual([entry["webpage_url"] for entry, _, _ in queue.entries], ["https://example.com/v2"])

    async def test_check_now_skips_subscriber_only_when_skip_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                side_effect=[
                    (
                        {"_type": "channel", "title": "Channel"},
                        [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                    ),
                    (
                        {"_type": "channel", "title": "Channel"},
                        [
                            {
                                "id": "v2",
                                "title": "Members",
                                "webpage_url": "https://example.com/v2",
                                "availability": "subscriber_only",
                            },
                            {"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"},
                        ],
                    ),
                ],
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    skip_subscriber_only=True,
                )
                self.assertTrue(mgr.list_all()[0].skip_subscriber_only)
                await mgr.check_now([result["subscription"]["id"]])

            sub = mgr.list_all()[0]
            self.assertIsNone(sub.error)
            self.assertEqual(sub.seen_ids[:2], ["v2", "v1"])
            self.assertEqual(queue.entries, [])

    async def test_update_subscription_parses_string_false_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                ),
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )

            sub_id = result["subscription"]["id"]
            update = await mgr.update_subscription(sub_id, {"enabled": "false"})
            self.assertEqual(update["status"], "ok")
            self.assertFalse(mgr.list_all()[0].enabled)

    async def test_update_subscription_rejects_invalid_enabled_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                ),
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )

            sub_id = result["subscription"]["id"]
            update_result = await mgr.update_subscription(sub_id, {"enabled": "maybe"})
            self.assertEqual(update_result["status"], "error")
            stored = mgr.get(sub_id)
            self.assertTrue(stored.enabled)

            update_result = await mgr.update_subscription(
                sub_id, {"check_interval_minutes": "abc"}
            )
            self.assertEqual(update_result["status"], "error")
            self.assertEqual(mgr.get(sub_id).check_interval_minutes, 60)

    async def test_add_subscription_rejects_invalid_title_regex(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                ),
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    title_regex="[",
                )
            self.assertEqual(result["status"], "error")
            self.assertIn("title_regex", result["msg"].lower())
            self.assertEqual(mgr.list_all(), [])

    async def test_add_subscription_stores_and_exposes_title_regex(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())
            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                ),
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    title_regex="EPISODE",
                )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["subscription"]["title_regex"], "EPISODE")
            self.assertEqual(mgr.list_all()[0].title_regex, "EPISODE")

    async def test_check_now_title_regex_queues_only_matches_and_marks_unmatched_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())
            with patch(
                "subscriptions.extract_flat_playlist",
                side_effect=[
                    (
                        {"_type": "channel", "title": "Channel"},
                        [{"id": "v1", "title": "Old", "webpage_url": "https://example.com/v1"}],
                    ),
                    (
                        {"_type": "channel", "title": "Channel"},
                        [
                            {
                                "id": "v2",
                                "title": "Minecraft | EPISODE 1",
                                "webpage_url": "https://example.com/v2",
                            },
                            {
                                "id": "v3",
                                "title": "Unrelated IRL",
                                "webpage_url": "https://example.com/v3",
                            },
                            {
                                "id": "v1",
                                "title": "Old",
                                "webpage_url": "https://example.com/v1",
                            },
                        ],
                    ),
                ],
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    title_regex="EPISODE",
                )
                await mgr.check_now([result["subscription"]["id"]])
            self.assertEqual([e["webpage_url"] for e, _, _ in queue.entries], ["https://example.com/v2"])
            sub = mgr.list_all()[0]
            self.assertEqual(sub.seen_ids[:3], ["v2", "v3", "v1"])

    async def test_check_now_title_regex_queue_failure_keeps_matched_id_unseen(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())
            with patch(
                "subscriptions.extract_flat_playlist",
                side_effect=[
                    (
                        {"_type": "channel", "title": "Channel"},
                        [{"id": "v1", "title": "Old", "webpage_url": "https://example.com/v1"}],
                    ),
                    (
                        {"_type": "channel", "title": "Channel"},
                        [
                            {
                                "id": "v2",
                                "title": "Show | EPISODE 1",
                                "webpage_url": "https://example.com/v2",
                            },
                            {
                                "id": "v3",
                                "title": "Other",
                                "webpage_url": "https://example.com/v3",
                            },
                        ],
                    ),
                ],
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    title_regex="EPISODE",
                )
                queue.fail = True
                await mgr.check_now([result["subscription"]["id"]])
            sub = mgr.list_all()[0]
            self.assertEqual(sub.error, "queue failed")
            self.assertEqual(set(sub.seen_ids), {"v1", "v3"})
            self.assertNotIn("v2", sub.seen_ids)

    async def test_update_subscription_rejects_invalid_title_regex(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())
            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                ),
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )
            sub_id = result["subscription"]["id"]
            upd = await mgr.update_subscription(sub_id, {"title_regex": "("})
            self.assertEqual(upd["status"], "error")
            self.assertEqual(mgr.list_all()[0].title_regex, "")

    async def test_update_subscription_persists_valid_title_regex(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())
            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                ),
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )
            sub_id = result["subscription"]["id"]
            upd = await mgr.update_subscription(sub_id, {"title_regex": "foo|bar"})
            self.assertEqual(upd["status"], "ok")
            self.assertEqual(upd["subscription"]["title_regex"], "foo|bar")
            self.assertEqual(mgr.list_all()[0].title_regex, "foo|bar")

    async def _add_one_subscription(self, mgr):
        with patch(
            "subscriptions.extract_flat_playlist",
            return_value=(
                {"_type": "channel", "title": "Videos"},
                [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
            ),
        ):
            result = await mgr.add_subscription(
                "https://example.com/playlist?list=UULFabc",
                check_interval_minutes=60,
                download_type="video",
                codec="auto",
                format="any",
                quality="best",
                folder="",
                custom_name_prefix="",
                auto_start=True,
                playlist_item_limit=0,
                split_by_chapters=False,
                chapter_template="",
                subtitle_language="en",
                subtitle_mode="prefer_manual",
            )
        return result["subscription"]["id"]

    async def test_update_subscription_renames(self):
        """Issue #1044: UULF-style uploads playlists all come back named 'Videos',
        so the user needs to be able to relabel them."""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            sub_id = await self._add_one_subscription(mgr)
            self.assertEqual(mgr.list_all()[0].name, "Videos")

            upd = await mgr.update_subscription(sub_id, {"name": "  Jane's   uploads \n"})
            self.assertEqual(upd["status"], "ok")
            # Surrounding and interior whitespace is collapsed to keep the name
            # a single-line label.
            self.assertEqual(upd["subscription"]["name"], "Jane's uploads")
            self.assertEqual(mgr.list_all()[0].name, "Jane's uploads")

    async def test_update_subscription_rename_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Config(tmp)
            mgr = SubscriptionManager(cfg, _Queue(), _Notifier())
            sub_id = await self._add_one_subscription(mgr)
            await mgr.update_subscription(sub_id, {"name": "Renamed"})

            reloaded = SubscriptionManager(cfg, _Queue(), _Notifier())
            self.assertEqual(reloaded.get(sub_id).name, "Renamed")

    async def test_update_subscription_rejects_unusable_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            sub_id = await self._add_one_subscription(mgr)

            for bad in ("", "   ", "\n\t", 42, None, ["a"], "x" * 201):
                upd = await mgr.update_subscription(sub_id, {"name": bad})
                self.assertEqual(upd["status"], "error", f"expected {bad!r} to be rejected")
                self.assertEqual(mgr.list_all()[0].name, "Videos")

    async def test_update_subscription_accepts_name_at_length_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            sub_id = await self._add_one_subscription(mgr)

            upd = await mgr.update_subscription(sub_id, {"name": "x" * 200})
            self.assertEqual(upd["status"], "ok")
            self.assertEqual(mgr.list_all()[0].name, "x" * 200)

    async def test_update_subscription_changes_folder(self):
        """Issue #1052: the folder was settable at creation and then frozen,
        because it was never added to the fields the update route accepts."""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            sub_id = await self._add_one_subscription(mgr)
            self.assertEqual(mgr.list_all()[0].folder, "")

            upd = await mgr.update_subscription(sub_id, {"folder": "  channels/jane  "})
            self.assertEqual(upd["status"], "ok")
            self.assertEqual(upd["subscription"]["folder"], "channels/jane")
            self.assertEqual(mgr.list_all()[0].folder, "channels/jane")

    async def test_update_subscription_folder_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Config(tmp)
            mgr = SubscriptionManager(cfg, _Queue(), _Notifier())
            sub_id = await self._add_one_subscription(mgr)
            await mgr.update_subscription(sub_id, {"folder": "archive"})

            reloaded = SubscriptionManager(cfg, _Queue(), _Notifier())
            self.assertEqual(reloaded.get(sub_id).folder, "archive")

    async def test_update_subscription_clears_folder(self):
        # An empty folder is valid and means the base download directory.
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            sub_id = await self._add_one_subscription(mgr)
            await mgr.update_subscription(sub_id, {"folder": "archive"})

            upd = await mgr.update_subscription(sub_id, {"folder": "   "})
            self.assertEqual(upd["status"], "ok")
            self.assertEqual(mgr.list_all()[0].folder, "")

    async def test_update_subscription_rejects_unusable_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            sub_id = await self._add_one_subscription(mgr)
            await mgr.update_subscription(sub_id, {"folder": "keep"})

            bad_values = (
                "/etc",
                "/absolute/path",
                "../escape",
                "nested/../../escape",
                "windows\\..\\escape",
                42,
                ["a"],
            )
            for bad in bad_values:
                upd = await mgr.update_subscription(sub_id, {"folder": bad})
                self.assertEqual(upd["status"], "error", f"expected {bad!r} to be rejected")
                self.assertEqual(mgr.list_all()[0].folder, "keep")

    async def test_update_subscription_folder_leaves_other_fields_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            sub_id = await self._add_one_subscription(mgr)
            before = mgr.get(sub_id)
            name, interval, enabled = before.name, before.check_interval_minutes, before.enabled

            await mgr.update_subscription(sub_id, {"folder": "only/this"})

            after = mgr.get(sub_id)
            self.assertEqual(after.folder, "only/this")
            self.assertEqual(after.name, name)
            self.assertEqual(after.check_interval_minutes, interval)
            self.assertEqual(after.enabled, enabled)

    async def test_update_subscription_skip_subscriber_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())
            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                ),
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )
            sub_id = result["subscription"]["id"]
            self.assertFalse(mgr.list_all()[0].skip_subscriber_only)
            upd = await mgr.update_subscription(sub_id, {"skip_subscriber_only": True})
            self.assertEqual(upd["status"], "ok")
            self.assertTrue(upd["subscription"]["skip_subscriber_only"])
            self.assertTrue(mgr.list_all()[0].skip_subscriber_only)

    async def test_update_subscription_rejects_invalid_skip_subscriber_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())
            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                ),
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )
            sub_id = result["subscription"]["id"]
            upd = await mgr.update_subscription(sub_id, {"skip_subscriber_only": "maybe"})
            self.assertEqual(upd["status"], "error")
            self.assertFalse(mgr.list_all()[0].skip_subscriber_only)

    def test_persistence_includes_title_regex(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "subscriptions.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "schema_version": 2,
                        "kind": "subscriptions",
                        "items": [
                            {
                                "id": "sub-1",
                                "name": "Channel",
                                "url": "https://example.com/channel",
                                "enabled": True,
                                "check_interval_minutes": 60,
                                "download_type": "video",
                                "codec": "auto",
                                "format": "any",
                                "quality": "best",
                                "folder": "",
                                "custom_name_prefix": "",
                                "auto_start": True,
                                "playlist_item_limit": 0,
                                "split_by_chapters": False,
                                "chapter_template": "",
                                "subtitle_language": "en",
                                "subtitle_mode": "prefer_manual",
                                "ytdl_options_presets": [],
                                "ytdl_options_overrides": {},
                                "title_regex": "EPISODE",
                                "last_checked": None,
                                "seen_ids": [],
                                "error": None,
                            }
                        ],
                    },
                    f,
                )
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            self.assertEqual(mgr.list_all()[0].title_regex, "EPISODE")
            self.assertFalse(mgr.list_all()[0].skip_subscriber_only)

    def test_persistence_includes_skip_subscriber_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "subscriptions.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "schema_version": 2,
                        "kind": "subscriptions",
                        "items": [
                            {
                                "id": "sub-1",
                                "name": "Channel",
                                "url": "https://example.com/channel",
                                "enabled": True,
                                "check_interval_minutes": 60,
                                "download_type": "video",
                                "codec": "auto",
                                "format": "any",
                                "quality": "best",
                                "folder": "",
                                "custom_name_prefix": "",
                                "auto_start": True,
                                "playlist_item_limit": 0,
                                "split_by_chapters": False,
                                "chapter_template": "",
                                "subtitle_language": "en",
                                "subtitle_mode": "prefer_manual",
                                "ytdl_options_presets": [],
                                "ytdl_options_overrides": {},
                                "title_regex": "",
                                "skip_subscriber_only": True,
                                "last_checked": None,
                                "seen_ids": [],
                                "error": None,
                            }
                        ],
                    },
                    f,
                )
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            self.assertTrue(mgr.list_all()[0].skip_subscriber_only)


class ExtractFlatPlaylistTests(unittest.TestCase):
    def test_descends_one_level_when_root_entries_are_nested_collections(self):
        responses = iter(
            [
                {
                    "_type": "channel",
                    "entries": [
                        {
                            "_type": "url",
                            "ie_key": "YoutubeTab",
                            "title": "Channel - Videos",
                            "url": "https://example.com/videos",
                            "webpage_url": "https://example.com/videos",
                        }
                    ],
                },
                {
                    "_type": "playlist",
                    "entries": [
                        {
                            "_type": "url",
                            "ie_key": "Youtube",
                            "id": "v1",
                            "title": "One",
                            "duration": 10,
                            "webpage_url": "https://example.com/v1",
                        }
                    ],
                },
            ]
        )

        class _FakeYDL:
            def __init__(self, params):
                self.params = params

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                return next(responses)

        cfg = _Config(tempfile.mkdtemp())
        with patch("subscriptions.yt_dlp.YoutubeDL", _FakeYDL, create=True):
            info, entries = extract_flat_playlist(cfg, "https://example.com/channel", 50)

        self.assertEqual(info.get("_type"), "playlist")
        self.assertEqual([entry["webpage_url"] for entry in entries], ["https://example.com/v1"])

    def test_extra_opts_applied_on_top_of_config_options(self):
        captured: dict = {}

        class _FakeYDL:
            def __init__(self, params):
                captured.update(params)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def extract_info(self, url, download=False):
                return {"_type": "video"}

        cfg = _Config(tempfile.mkdtemp())
        with patch("subscriptions.yt_dlp.YoutubeDL", _FakeYDL, create=True):
            extract_flat_playlist(cfg, "https://example.com/v1", 50, extra_opts={"cookiefile": "x"})

        self.assertEqual(captured.get("cookiefile"), "x")


def _make_scan_capturing_fake_ydl(captured_params: list, entries: list[dict]):
    class _FakeYDL:
        def __init__(self, params):
            captured_params.append(params)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=False):
            return {"_type": "channel", "title": "Channel", "entries": entries}

    return _FakeYDL


class SubscriptionScanExtraOptsTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_subscription_scan_applies_presets_and_overrides(self):
        captured_params: list = []
        fake_ydl = _make_scan_capturing_fake_ydl(
            captured_params,
            [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
        )

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Config(tmp)
            cfg.YTDL_OPTIONS_PRESETS = {"mypreset": {"cookiefile": "preset.txt"}}
            mgr = SubscriptionManager(cfg, _Queue(), _Notifier())

            with patch("subscriptions.yt_dlp.YoutubeDL", fake_ydl, create=True):
                await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    ytdl_options_presets=["mypreset"],
                    ytdl_options_overrides={"extra": "override"},
                )

        self.assertTrue(captured_params)
        self.assertEqual(captured_params[0].get("cookiefile"), "preset.txt")
        self.assertEqual(captured_params[0].get("extra"), "override")

    async def test_scan_never_writes_playlist_sidecar_files(self):
        """A subscription scan is a metadata probe. yt-dlp writes the
        playlist-level infojson/description/thumbnail regardless of ``download``,
        so without this a writeinfojson/writethumbnail user would get stray files
        in DOWNLOAD_DIR on every check interval. Issue #1040."""
        captured_params: list = []
        fake_ydl = _make_scan_capturing_fake_ydl(
            captured_params,
            [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
        )

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Config(tmp)
            cfg.YTDL_OPTIONS = {"writeinfojson": True, "writethumbnail": True}
            mgr = SubscriptionManager(cfg, _Queue(), _Notifier())

            with patch("subscriptions.yt_dlp.YoutubeDL", fake_ydl, create=True):
                await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    ytdl_options_overrides={"allow_playlist_files": True},
                )

        self.assertTrue(captured_params)
        self.assertIs(captured_params[0].get("allow_playlist_files"), False)

    async def test_check_now_scan_applies_stored_subscription_presets(self):
        entries = [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}]

        with tempfile.TemporaryDirectory() as tmp:
            cfg = _Config(tmp)
            cfg.YTDL_OPTIONS_PRESETS = {"mypreset": {"cookiefile": "preset.txt"}}
            mgr = SubscriptionManager(cfg, _Queue(), _Notifier())

            add_captured: list = []
            with patch(
                "subscriptions.yt_dlp.YoutubeDL",
                _make_scan_capturing_fake_ydl(add_captured, entries),
                create=True,
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                    ytdl_options_presets=["mypreset"],
                )
            sub_id = result["subscription"]["id"]

            check_captured: list = []
            with patch(
                "subscriptions.yt_dlp.YoutubeDL",
                _make_scan_capturing_fake_ydl(check_captured, entries),
                create=True,
            ):
                await mgr.check_now([sub_id])

        self.assertTrue(check_captured)
        self.assertEqual(check_captured[0].get("cookiefile"), "preset.txt")


class BrowsePlaylistTests(unittest.IsolatedAsyncioTestCase):
    """browse_playlist is a read-only flat probe backing the Browse dialog."""

    URL = "https://example.com/playlist"

    def _config(self):
        cfg = _Config("unused")
        cfg.ALLOW_PRIVATE_ADDRESSES = False
        return cfg

    async def _browse(self, extract, *, cfg=None, limit=0, presets=None, overrides=None, url_error=None):
        validate = MagicMock(return_value=url_error)
        with patch("subscriptions.validate_url", validate), patch(
            "subscriptions.extract_flat_playlist", extract
        ):
            result = await browse_playlist(
                cfg or self._config(), self.URL, limit, presets, overrides
            )
        return result, validate

    async def test_maps_entries_to_minimal_shape(self):
        info = {"_type": "playlist", "title": "My List", "id": "PL1"}
        entries = [
            {"id": "a", "webpage_url": "https://example.com/a", "url": "https://x/ignored",
             "title": "A", "duration": 61, "thumbnail": "https://example.com/a.jpg"},
            {"id": "b", "url": "https://example.com/b", "duration": 12.5},
            {"id": "c", "url": "https://example.com/c", "title": "C", "duration": "1:02"},
            {"id": "d", "url": "https://example.com/d", "title": "D", "duration": True},
        ]
        extract = MagicMock(return_value=(info, entries))
        result, _ = await self._browse(extract)

        self.assertEqual(
            result,
            {
                "status": "ok",
                "title": "My List",
                "entries": [
                    {"url": "https://example.com/a", "title": "A", "duration": 61},
                    {"url": "https://example.com/b", "title": "https://example.com/b", "duration": 12.5},
                    {"url": "https://example.com/c", "title": "C", "duration": None},
                    {"url": "https://example.com/d", "title": "D", "duration": None},
                ],
            },
        )

    async def test_title_falls_back_to_url(self):
        extract = MagicMock(
            return_value=({"_type": "channel"}, [{"url": "https://example.com/a"}])
        )
        result, _ = await self._browse(extract)
        self.assertEqual(result["title"], self.URL)

    async def test_skips_non_dict_and_urlless_entries(self):
        extract = MagicMock(
            return_value=(
                {"_type": "playlist", "title": "L"},
                ["junk", None, {"title": "no url"}, {"url": "https://example.com/ok", "title": "Ok"}],
            )
        )
        result, _ = await self._browse(extract)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([e["url"] for e in result["entries"]], ["https://example.com/ok"])

    async def test_single_video_is_rejected(self):
        extract = MagicMock(return_value=({"_type": "video", "title": "V"}, []))
        result, _ = await self._browse(extract)
        self.assertEqual(result, {"status": "error", "msg": VIDEO_ONLY_MSG})

    async def test_empty_playlist_is_rejected(self):
        extract = MagicMock(return_value=({"_type": "playlist", "title": "L"}, []))
        result, _ = await self._browse(extract)
        self.assertEqual(result, {"status": "error", "msg": NO_ENTRIES_MSG})
        self.assertNotEqual(NO_ENTRIES_MSG, VIDEO_ONLY_MSG)

    async def test_playlist_with_only_unusable_entries_is_rejected(self):
        extract = MagicMock(
            return_value=({"_type": "channel", "title": "L"}, ["junk", None, {"title": "no url"}])
        )
        result, _ = await self._browse(extract)
        self.assertEqual(result, {"status": "error", "msg": NO_ENTRIES_MSG})

    async def test_blocked_url_never_reaches_the_extractor(self):
        extract = MagicMock()
        result, validate = await self._browse(extract, url_error="Blocked address")
        self.assertEqual(result, {"status": "error", "msg": "Blocked address"})
        extract.assert_not_called()
        validate.assert_called_once()
        self.assertEqual(validate.call_args.args[0], self.URL)
        self.assertFalse(validate.call_args.kwargs["allow_private"])

    async def test_guard_honours_allow_private_and_scan_proxy(self):
        cfg = self._config()
        cfg.ALLOW_PRIVATE_ADDRESSES = True
        cfg.YTDL_OPTIONS_PRESETS = {"px": {"proxy": "http://proxy.local:3128"}}
        extract = MagicMock(return_value=({"_type": "playlist"}, [{"url": "https://example.com/a"}]))
        _, validate = await self._browse(extract, cfg=cfg, presets=["px"])
        self.assertTrue(validate.call_args.kwargs["allow_private"])
        self.assertTrue(validate.call_args.kwargs["proxies"])

    async def test_extractor_error_becomes_error_status(self):
        extract = MagicMock(
            side_effect=subscriptions.yt_dlp.utils.YoutubeDLError("boom")
        )
        result, _ = await self._browse(extract)
        self.assertEqual(result["status"], "error")
        self.assertIn("boom", result["msg"])

    async def test_unresolvable_url(self):
        extract = MagicMock(return_value=(None, []))
        result, _ = await self._browse(extract)
        self.assertEqual(result, {"status": "error", "msg": "Could not resolve URL"})

    async def test_limit_zero_means_no_playlistend(self):
        extract = MagicMock(return_value=({"_type": "playlist"}, [{"url": "https://example.com/a"}]))
        await self._browse(extract, limit=0)
        self.assertIsNone(extract.call_args.args[2])

    async def test_negative_limit_means_no_playlistend(self):
        extract = MagicMock(return_value=({"_type": "playlist"}, [{"url": "https://example.com/a"}]))
        await self._browse(extract, limit=-3)
        self.assertIsNone(extract.call_args.args[2])

    async def test_positive_limit_is_passed_as_playlistend(self):
        extract = MagicMock(return_value=({"_type": "playlist"}, [{"url": "https://example.com/a"}]))
        await self._browse(extract, limit=5)
        self.assertEqual(extract.call_args.args[2], 5)
        self.assertEqual(extract.call_args.args[1], self.URL)

    async def test_presets_and_overrides_are_merged_into_extra_opts(self):
        cfg = self._config()
        cfg.YTDL_OPTIONS_PRESETS = {"mypreset": {"cookiefile": "preset.txt", "a": 1}}
        extract = MagicMock(return_value=({"_type": "playlist"}, [{"url": "https://example.com/a"}]))
        await self._browse(
            extract, cfg=cfg, presets=["mypreset"], overrides={"a": 2, "extra": "override"}
        )
        self.assertEqual(
            extract.call_args.kwargs["extra_opts"],
            {"cookiefile": "preset.txt", "a": 2, "extra": "override"},
        )

    async def test_probe_suppresses_playlist_sidecar_files(self):
        """Through the real extractor: no playlist-level files are written."""
        captured_params: list = []
        fake_ydl = _make_scan_capturing_fake_ydl(
            captured_params, [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}]
        )
        cfg = self._config()
        cfg.YTDL_OPTIONS = {"writeinfojson": True}
        with patch("subscriptions.validate_url", return_value=None), patch(
            "subscriptions.yt_dlp.YoutubeDL", fake_ydl, create=True
        ):
            result = await browse_playlist(cfg, self.URL, 0, None, None)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(captured_params[0]["allow_playlist_files"])
        self.assertNotIn("playlistend", captured_params[0])


class SubscriptionEventLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_now_does_not_block_event_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())

            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=(
                    {"_type": "channel", "title": "Channel"},
                    [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                ),
            ):
                result = await mgr.add_subscription(
                    "https://example.com/channel",
                    check_interval_minutes=60,
                    download_type="video",
                    codec="auto",
                    format="any",
                    quality="best",
                    folder="",
                    custom_name_prefix="",
                    auto_start=True,
                    playlist_item_limit=0,
                    split_by_chapters=False,
                    chapter_template="",
                    subtitle_language="en",
                    subtitle_mode="prefer_manual",
                )
            sub_id = result["subscription"]["id"]

            def _slow_extract(config, url, playlistend, **kwargs):
                time.sleep(0.3)
                return (
                    {"_type": "channel", "title": "Channel"},
                    [{"id": "v1", "title": "One", "webpage_url": "https://example.com/v1"}],
                )

            with patch("subscriptions.extract_flat_playlist", side_effect=_slow_extract):
                check_task = asyncio.ensure_future(mgr.check_now([sub_id]))
                # If check_now() blocked the event loop, this would not complete
                # until after the slow extraction finishes.
                await asyncio.wait_for(asyncio.sleep(0.05), timeout=0.2)
                self.assertFalse(check_task.done())
                await check_task

    async def test_check_many_isolates_a_crashing_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())

            good = SubscriptionInfo(id="good", name="Good", url="https://example.com/good")
            bad = SubscriptionInfo(id="bad", name="Bad", url="https://example.com/bad")
            other = SubscriptionInfo(id="other", name="Other", url="https://example.com/other")

            checked: list[str] = []

            async def fake_check(sub):
                if sub.id == "bad":
                    raise RuntimeError("boom")
                checked.append(sub.id)

            with patch.object(mgr, "_check_one_unlocked", side_effect=fake_check):
                # The crashing subscription must not prevent the others running.
                await mgr._check_many([good, bad, other])

            self.assertIn("good", checked)
            self.assertIn("other", checked)

    async def test_check_many_bounded_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            subs = [
                SubscriptionInfo(id=str(i), name=str(i), url=f"https://example.com/{i}")
                for i in range(10)
            ]

            import subscriptions as subs_mod

            concurrent = 0
            peak = 0

            async def fake_check(sub):
                nonlocal concurrent, peak
                concurrent += 1
                peak = max(peak, concurrent)
                await asyncio.sleep(0.02)
                concurrent -= 1

            with patch.object(mgr, "_check_one_unlocked", side_effect=fake_check):
                await mgr._check_many(subs)

            # Never exceed the configured bound, but do run more than one at once.
            self.assertLessEqual(peak, subs_mod._MAX_CONCURRENT_CHECKS)
            self.assertGreater(peak, 1)


if __name__ == "__main__":
    unittest.main()


def _not_live_error_cls():
    """A UserNotLive stand-in that works with both the stub and the real yt_dlp."""

    class _NotLive(subscriptions.yt_dlp.utils.YoutubeDLError):
        pass

    return _NotLive


_ADD_KWARGS = dict(
    check_interval_minutes=2,
    download_type="video",
    codec="auto",
    format="any",
    quality="best",
    folder="",
    custom_name_prefix="",
    auto_start=True,
    playlist_item_limit=0,
    split_by_chapters=False,
    chapter_template="",
    subtitle_language="en",
    subtitle_mode="prefer_manual",
)


class LiveCategoryValidationTests(unittest.TestCase):
    def test_accepts_list_and_comma_string(self):
        self.assertEqual(
            subscriptions.validate_live_categories(["Just Chatting", " chess ", "Chess", ""]),
            ["Just Chatting", "chess"],
        )
        self.assertEqual(
            subscriptions.validate_live_categories("Art, Music ,,"), ["Art", "Music"]
        )
        self.assertEqual(subscriptions.validate_live_categories(None), [])

    def test_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            subscriptions.validate_live_categories([1])
        with self.assertRaises(ValueError):
            subscriptions.validate_live_categories({"a": 1})
        with self.assertRaises(ValueError):
            subscriptions.validate_live_categories(["x" * 101])
        with self.assertRaises(ValueError):
            subscriptions.validate_live_categories([f"c{i}" for i in range(51)])


class LiveStreamMatchTests(unittest.TestCase):
    stream = {"title": "Ranked grind", "categories": ["Chess"]}

    def test_no_filters_match_everything(self):
        self.assertEqual(subscriptions.live_stream_matches(self.stream, [], None), (True, None))

    def test_category_match_is_case_insensitive_and_any_of(self):
        self.assertTrue(subscriptions.live_stream_matches(self.stream, ["Art", "chess"], None)[0])
        self.assertFalse(subscriptions.live_stream_matches(self.stream, ["Art"], None)[0])

    def test_category_and_title_must_both_match(self):
        import re

        self.assertTrue(
            subscriptions.live_stream_matches(self.stream, ["Chess"], re.compile("(?i)ranked"))[0]
        )
        self.assertFalse(
            subscriptions.live_stream_matches(self.stream, ["Chess"], re.compile("casual"))[0]
        )

    def test_category_filter_without_reported_category_explains_why(self):
        matched, error = subscriptions.live_stream_matches({"title": "x", "categories": []}, ["Chess"], None)
        self.assertFalse(matched)
        self.assertEqual(error, subscriptions.NO_CATEGORY_MSG)


class ProbeLiveChannelTests(unittest.TestCase):
    def _fake_ydl(self, result=None, exc=None):
        class _FakeYDL:
            def __init__(self, params=None):
                self.params = params

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def add_info_extractor(self, ie):
                pass

            def extract_info(self, url, download=False, process=False, ie_key=None):
                if exc is not None:
                    raise exc
                return result

        return _FakeYDL

    def test_returns_stream_details_when_live(self):
        info = {
            "id": "stream-1",
            "title": "Speedrun  attempts",
            "is_live": True,
            "categories": ["Celeste"],
            "webpage_url": "https://kick.com/someone",
            "uploader": "Someone",
        }
        with patch("subscriptions.yt_dlp.YoutubeDL", self._fake_ydl(info), create=True), patch(
            "subscriptions._twitch_category_ie", return_value=None
        ):
            stream = subscriptions.probe_live_channel(_Config(tempfile.gettempdir()), "https://kick.com/someone")
        self.assertEqual(stream["id"], "stream-1")
        self.assertEqual(stream["title"], "Speedrun attempts")
        self.assertEqual(stream["categories"], ["Celeste"])
        self.assertEqual(stream["url"], "https://kick.com/someone")

    def test_offline_channel_returns_none(self):
        not_live = _not_live_error_cls()
        wrapped = subscriptions.yt_dlp.utils.YoutubeDLError("offline")
        wrapped.exc_info = (not_live, not_live("offline"), None)
        with patch("subscriptions.yt_dlp.YoutubeDL", self._fake_ydl(exc=wrapped), create=True), patch(
            "subscriptions._twitch_category_ie", return_value=None
        ), patch.object(subscriptions.yt_dlp.utils, "UserNotLive", not_live, create=True):
            self.assertIsNone(
                subscriptions.probe_live_channel(_Config(tempfile.gettempdir()), "https://twitch.tv/x")
            )

    def test_other_errors_propagate(self):
        err = subscriptions.yt_dlp.utils.YoutubeDLError("does not exist")
        with patch("subscriptions.yt_dlp.YoutubeDL", self._fake_ydl(exc=err), create=True), patch(
            "subscriptions._twitch_category_ie", return_value=None
        ):
            with self.assertRaises(subscriptions.yt_dlp.utils.YoutubeDLError):
                subscriptions.probe_live_channel(_Config(tempfile.gettempdir()), "https://twitch.tv/x")

    def test_finished_stream_is_not_live(self):
        info = {"id": "v", "title": "VOD", "live_status": "was_live"}
        with patch("subscriptions.yt_dlp.YoutubeDL", self._fake_ydl(info), create=True), patch(
            "subscriptions._twitch_category_ie", return_value=None
        ):
            self.assertIsNone(
                subscriptions.probe_live_channel(_Config(tempfile.gettempdir()), "https://youtube.com/@x/live")
            )


class LiveSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def _add_offline_live(self, mgr, **extra):
        not_live = _not_live_error_cls()
        with patch("subscriptions.extract_flat_playlist", side_effect=not_live("offline")), patch.object(
            subscriptions.yt_dlp.utils, "UserNotLive", not_live, create=True
        ), patch("subscriptions.validate_url", return_value=None), patch(
            "subscriptions.probe_live_channel", return_value=None
        ):
            result = await mgr.add_subscription("https://www.twitch.tv/someone", **_ADD_KWARGS, **extra)
            # Let the immediate first check run while the probe is still patched.
            await asyncio.sleep(0.05)
        return result

    async def test_offline_live_channel_becomes_live_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            result = await self._add_offline_live(mgr, live_categories="Chess, Art", title_regex="(?i)ranked")
            self.assertEqual(result["status"], "ok")
            row = result["subscription"]
            self.assertEqual(row["kind"], "live")
            self.assertEqual(row["live_categories"], ["Chess", "Art"])
            self.assertEqual(row["name"], "someone")

            # Persisted and reloaded as a live subscription.
            reloaded = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            sub = reloaded.get(row["id"])
            self.assertEqual(sub.kind, "live")
            self.assertEqual(sub.live_categories, ["Chess", "Art"])

    async def test_live_video_result_becomes_live_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            with patch(
                "subscriptions.extract_flat_playlist",
                return_value=({"_type": "video", "is_live": True, "uploader": "Someone"}, []),
            ), patch("subscriptions.validate_url", return_value=None), patch(
                "subscriptions.probe_live_channel", return_value=None
            ):
                result = await mgr.add_subscription("https://kick.com/someone", **_ADD_KWARGS)
                await asyncio.sleep(0.05)
            self.assertEqual(result["subscription"]["kind"], "live")
            self.assertEqual(result["subscription"]["name"], "Someone")

    async def test_plain_video_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            with patch(
                "subscriptions.extract_flat_playlist", return_value=({"_type": "video"}, [])
            ), patch("subscriptions.validate_url", return_value=None):
                result = await mgr.add_subscription("https://example.com/watch", **_ADD_KWARGS)
            self.assertEqual(result, {"status": "error", "msg": VIDEO_ONLY_MSG})

    async def test_invalid_categories_rejected_on_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            result = await mgr.add_subscription(
                "https://www.twitch.tv/someone", **_ADD_KWARGS, live_categories=[5]
            )
            self.assertEqual(result["status"], "error")

    async def test_live_check_captures_matching_stream_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())
            result = await self._add_offline_live(mgr, live_categories=["chess"])
            sub_id = result["subscription"]["id"]
            self.assertEqual(queue.entries, [])
            self.assertFalse(mgr.get(sub_id).live_now)

            stream = {
                "id": "s-100",
                "title": "Ranked",
                "categories": ["Chess"],
                "url": "https://www.twitch.tv/someone",
                "uploader": "Someone",
            }
            with patch("subscriptions.probe_live_channel", return_value=stream):
                await mgr.check_now([sub_id])
                await mgr.check_now([sub_id])

            self.assertEqual(len(queue.entries), 1)
            entry, args, kwargs = queue.entries[0]
            self.assertEqual(entry["webpage_url"], "https://www.twitch.tv/someone")
            self.assertEqual(entry["id"], "s-100")
            self.assertEqual(entry["title"], "Someone - Ranked")
            self.assertEqual(entry["live_status"], "is_live")
            sub = mgr.get(sub_id)
            self.assertTrue(sub.live_now)
            self.assertEqual(sub.live_title, "Ranked")
            self.assertEqual(sub.live_category, "Chess")
            self.assertIn("s-100", sub.seen_ids)
            self.assertIsNone(sub.error)

            # A new stream on the same channel is captured too.
            with patch("subscriptions.probe_live_channel", return_value={**stream, "id": "s-101"}):
                await mgr.check_now([sub_id])
            self.assertEqual(len(queue.entries), 2)

    async def test_live_check_skips_non_matching_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())
            result = await self._add_offline_live(mgr, live_categories=["Chess"])
            sub_id = result["subscription"]["id"]
            stream = {"id": "s1", "title": "x", "categories": ["Art"], "url": "u", "uploader": ""}
            with patch("subscriptions.probe_live_channel", return_value=stream):
                await mgr.check_now([sub_id])
            self.assertEqual(queue.entries, [])
            sub = mgr.get(sub_id)
            self.assertTrue(sub.live_now)
            self.assertEqual(sub.seen_ids, [])

            # Editing the categories lets the next check capture it.
            await mgr.update_subscription(sub_id, {"live_categories": ["art"]})
            with patch("subscriptions.probe_live_channel", return_value=stream):
                await mgr.check_now([sub_id])
            self.assertEqual(len(queue.entries), 1)

    async def test_failed_capture_is_retried_while_still_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _Queue()
            failed: set[tuple[str, str]] = set()
            queue.live_capture_failed = lambda url, eid: (url, eid) in failed
            mgr = SubscriptionManager(_Config(tmp), queue, _Notifier())
            result = await self._add_offline_live(mgr)
            sub_id = result["subscription"]["id"]
            stream = {"id": "s1", "title": "t", "categories": [], "url": "https://kick.com/a", "uploader": ""}
            with patch("subscriptions.probe_live_channel", return_value=stream):
                await mgr.check_now([sub_id])
                await mgr.check_now([sub_id])
                self.assertEqual(len(queue.entries), 1)
                failed.add(("https://kick.com/a", "s1"))
                await mgr.check_now([sub_id])
            self.assertEqual(len(queue.entries), 2)

    async def test_live_check_error_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SubscriptionManager(_Config(tmp), _Queue(), _Notifier())
            result = await self._add_offline_live(mgr)
            sub_id = result["subscription"]["id"]
            err = subscriptions.yt_dlp.utils.YoutubeDLError("someone does not exist")
            with patch("subscriptions.probe_live_channel", side_effect=err):
                await mgr.check_now([sub_id])
            sub = mgr.get(sub_id)
            self.assertEqual(sub.error, "someone does not exist")
            self.assertFalse(sub.live_now)
            self.assertIsNotNone(sub.last_checked)

    async def test_feed_subscription_defaults_to_feed_kind(self):
        sub = SubscriptionInfo(id="a", name="A", url="https://example.com/c")
        self.assertEqual(sub.kind, "feed")
        self.assertEqual(sub.to_public_dict()["live_categories"], [])


class TwitchCategoryExtractorTests(unittest.TestCase):
    def setUp(self):
        try:
            from yt_dlp.extractor.twitch import TwitchStreamIE  # noqa: F401
        except Exception:
            self.skipTest("real yt_dlp not importable (stubbed in this run)")
        subscriptions._twitch_category_ie_cls = None
        self.addCleanup(setattr, subscriptions, "_twitch_category_ie_cls", None)

    def test_category_and_stream_title_come_from_the_existing_gql_response(self):
        from yt_dlp.extractor.twitch import TwitchBaseIE, TwitchStreamIE

        gql = [
            {"data": {"user": {"stream": {"id": "42", "type": "live", "createdAt": None,
                                          "viewers": 1, "game": {"name": "Chess"}}}}},
            {"data": {"user": {"displayName": "Someone",
                               "broadcastSettings": {"title": "Ranked grind"}}}},
            {"data": {"user": {"stream": {"previewImageURL": None}}}},
        ]
        ie_cls = subscriptions._twitch_category_ie()
        self.assertTrue(issubclass(ie_cls, TwitchStreamIE))
        ie = ie_cls()
        with patch.object(TwitchBaseIE, "_download_gql", return_value=gql), patch.object(
            TwitchBaseIE, "_download_access_token", return_value={"value": "v", "signature": "s"}, create=True
        ), patch.object(TwitchBaseIE, "_extract_twitch_m3u8_formats", return_value=[], create=True), patch.object(
            TwitchStreamIE, "get_param", return_value=None
        ):
            info = ie._real_extract("https://www.twitch.tv/someone")
        self.assertEqual(info["categories"], ["Chess"])
        self.assertEqual(info["description"], "Ranked grind")
        self.assertTrue(info["is_live"])
