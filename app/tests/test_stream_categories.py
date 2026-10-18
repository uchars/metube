from __future__ import annotations

import types
import unittest
from unittest.mock import patch

import stream_categories as sc


def _cfg():
    return types.SimpleNamespace(
        YTDL_OPTIONS={}, DOWNLOAD_DIR="/tmp", TEMP_DIR="/tmp", ALLOW_PRIVATE_ADDRESSES=False
    )


def _node(name, art="https://static-cdn.jtvnw.net/ttv-boxart/1-52x72.jpg", viewers=10):
    return {"id": "1", "name": name, "displayName": name, "boxArtURL": art, "viewersCount": viewers}


def _kick(name, art="https://files.kick.com/images/subcategories/1/banner/x.webp", viewers=5):
    return {"id": 1, "name": name, "slug": name.lower(), "viewers": viewers, "banner": {"url": art}}


class _CacheReset(unittest.TestCase):
    def setUp(self):
        sc._cache.clear()
        self.addCleanup(sc._cache.clear)


class PlatformTests(unittest.TestCase):
    def test_platform_comes_from_the_hostname(self):
        self.assertEqual(sc.platform_for_url("https://www.twitch.tv/someone"), "twitch")
        self.assertEqual(sc.platform_for_url("https://m.twitch.tv/someone"), "twitch")
        self.assertEqual(sc.platform_for_url("https://kick.com/someone"), "kick")
        self.assertIsNone(sc.platform_for_url("https://www.youtube.com/@x/live"))
        self.assertIsNone(sc.platform_for_url("https://kick.com.evil.example/x"))
        self.assertIsNone(sc.platform_for_url("https://nottwitch.tv/x"))

    def test_unknown_platform_is_an_error(self):
        with self.assertRaises(sc.StreamCategoryError):
            sc.search_categories_sync(_cfg(), "youtube", "x")


class TwitchCategoryTests(_CacheReset):
    def test_empty_query_lists_top_categories(self):
        data = {"games": {"edges": [{"node": _node("Just Chatting")}, {"node": _node("Chess")}]}}
        with patch("stream_categories._twitch_gql", return_value=data) as gql:
            result = sc.search_categories_sync(_cfg(), "twitch", "  ")
        self.assertEqual([c["name"] for c in result], ["Just Chatting", "Chess"])
        payload = gql.call_args.args[1]
        self.assertIn("games(first: $first)", payload["query"])
        self.assertEqual(payload["variables"], {"first": sc.TOP_LIMIT})

    def test_search_passes_the_query_as_a_variable_and_caches(self):
        data = {"searchCategories": {"edges": [{"node": _node("Chess")}]}}
        with patch("stream_categories._twitch_gql", return_value=data) as gql:
            first = sc.search_categories_sync(_cfg(), "twitch", 'ch"ess')
            second = sc.search_categories_sync(_cfg(), "twitch", 'CH"ESS')
        self.assertEqual(first, second)
        gql.assert_called_once()
        payload = gql.call_args.args[1]
        self.assertEqual(payload["variables"]["query"], 'ch"ess')
        self.assertNotIn('ch"ess', payload["query"])

    def test_untrusted_box_art_urls_are_dropped(self):
        data = {"searchCategories": {"edges": [
            {"node": _node("A", art="http://static-cdn.jtvnw.net/a.jpg")},
            {"node": _node("B", art="https://evil.example/b.jpg")},
            {"node": _node("C", art="https://jtvnw.net.evil.example/c.jpg")},
            {"node": _node("D")},
            {"node": {"name": ""}},
            "junk",
        ]}}
        with patch("stream_categories._twitch_gql", return_value=data):
            result = sc.search_categories_sync(_cfg(), "twitch", "x")
        self.assertEqual([c["name"] for c in result], ["A", "B", "C", "D"])
        self.assertEqual([c["box_art_url"] for c in result][:3], [None, None, None])
        self.assertTrue(result[3]["box_art_url"].startswith("https://static-cdn.jtvnw.net/"))

    def test_box_art_size_template_is_filled_in(self):
        art = "https://static-cdn.jtvnw.net/ttv-boxart/1-{width}x{height}.jpg"
        data = {"searchCategories": {"edges": [{"node": _node("A", art=art)}]}}
        with patch("stream_categories._twitch_gql", return_value=data):
            result = sc.search_categories_sync(_cfg(), "twitch", "a")
        self.assertEqual(result[0]["box_art_url"], "https://static-cdn.jtvnw.net/ttv-boxart/1-52x72.jpg")

    def test_lookup_batches_uncached_names_and_omits_unknown_ones(self):
        sc._cache_put(("twitch", "name", "chess"), {"name": "Chess", "display_name": "Chess",
                                                    "box_art_url": None, "viewers": None})
        data = {"c0": _node("Art"), "c1": None}
        with patch("stream_categories._twitch_gql", return_value=data) as gql:
            result = sc.lookup_categories_sync(_cfg(), "twitch", ["Chess", "Art", "My Custom"])
            again = sc.lookup_categories_sync(_cfg(), "twitch", ["Art", "My Custom"])
        self.assertEqual([c["name"] for c in result], ["Chess", "Art"])
        self.assertEqual([c["name"] for c in again], ["Art"])
        gql.assert_called_once()
        payload = gql.call_args.args[1]
        self.assertEqual(payload["variables"], {"n0": "Art", "n1": "My Custom"})
        self.assertIn("c1: game(name: $n1)", payload["query"])


class KickCategoryTests(_CacheReset):
    def test_popular_list_and_banner_url(self):
        response = {"data": [
            _kick("Just Chatting"),
            _kick("Slots & Casino", art="https://evil.example/x.webp"),
            {"name": "Srcset", "banner": {"srcset": "https://files.kick.com/a.webp 1x, https://files.kick.com/b.webp 2x"}},
        ]}
        with patch("stream_categories._kick_api", return_value=response) as api:
            result = sc.search_categories_sync(_cfg(), "kick", "")
        api.assert_called_once()
        self.assertEqual(api.call_args.args[1:], ("v1/subcategories", {"limit": sc.TOP_LIMIT, "page": 1}))
        self.assertEqual([c["name"] for c in result], ["Just Chatting", "Slots & Casino", "Srcset"])
        self.assertTrue(result[0]["box_art_url"].startswith("https://files.kick.com/"))
        self.assertIsNone(result[1]["box_art_url"])
        self.assertEqual(result[2]["box_art_url"], "https://files.kick.com/a.webp")
        self.assertEqual(result[0]["viewers"], 5)

    def test_search_uses_kick_search_endpoint(self):
        with patch("stream_categories._kick_api", return_value={"categories": [_kick("Chess")]}) as api:
            result = sc.search_categories_sync(_cfg(), "kick", "che")
        self.assertEqual(api.call_args.args[1:], ("search", {"searched_word": "che"}))
        self.assertEqual([c["name"] for c in result], ["Chess"])

    def test_search_falls_back_to_filtering_the_popular_list(self):
        def fake_api(config, path, query):
            if path == "search":
                raise sc.StreamCategoryError("blocked")
            return {"data": [_kick("Just Chatting"), _kick("Chess"), _kick("Pools, Hot Tubs")]}

        with patch("stream_categories._kick_api", side_effect=fake_api):
            result = sc.search_categories_sync(_cfg(), "kick", "CHAT")
        self.assertEqual([c["name"] for c in result], ["Just Chatting"])

    def test_lookup_finds_exact_names_through_search(self):
        def fake_api(config, path, query):
            return {"categories": [_kick("Chess"), _kick("Chess Variants")]}

        with patch("stream_categories._kick_api", side_effect=fake_api):
            result = sc.lookup_categories_sync(_cfg(), "kick", ["chess", "Nope"])
        self.assertEqual([c["name"] for c in result], ["Chess"])

    def test_platform_caches_are_separate(self):
        with patch("stream_categories._kick_api", return_value={"data": [_kick("Chess")]}), patch(
            "stream_categories._twitch_gql", return_value={"games": {"edges": []}}
        ):
            self.assertEqual(len(sc.search_categories_sync(_cfg(), "kick", "")), 1)
            self.assertEqual(sc.search_categories_sync(_cfg(), "twitch", ""), [])


class ChannelAvatarTests(_CacheReset):
    def test_twitch_avatar(self):
        art = "https://static-cdn.jtvnw.net/jtv_user_pictures/x-profile_image-70x70.png"
        with patch("stream_categories._twitch_gql", return_value={"user": {"profileImageURL": art}}) as gql:
            self.assertEqual(sc.channel_avatar_sync(_cfg(), "https://www.twitch.tv/SomeOne"), art)
        self.assertEqual(gql.call_args.args[1]["variables"], {"login": "someone"})

    def test_kick_avatar(self):
        art = "https://files.kick.com/images/user/1/profile_image/a.webp"
        with patch("stream_categories._kick_api", return_value={"user": {"profile_pic": art}}) as api:
            self.assertEqual(sc.channel_avatar_sync(_cfg(), "https://kick.com/someone"), art)
        self.assertEqual(api.call_args.args[1], "v2/channels/someone")

    def test_untrusted_or_unsupported(self):
        with patch("stream_categories._kick_api", return_value={"user": {"profile_pic": "https://evil.example/a"}}):
            self.assertIsNone(sc.channel_avatar_sync(_cfg(), "https://kick.com/someone"))
        with patch("stream_categories._twitch_gql") as gql:
            self.assertIsNone(sc.channel_avatar_sync(_cfg(), "https://www.twitch.tv/../x"))
            self.assertIsNone(sc.channel_avatar_sync(_cfg(), "https://www.youtube.com/@x/live"))
        gql.assert_not_called()


class RealYtDlpTests(unittest.TestCase):
    def setUp(self):
        try:
            from yt_dlp.extractor.kick import KickBaseIE  # noqa: F401
            from yt_dlp.extractor.twitch import TwitchBaseIE  # noqa: F401
        except Exception:
            self.skipTest("real yt_dlp not importable (stubbed in this run)")

    def test_twitch_query_goes_through_yt_dlps_twitch_helper(self):
        from yt_dlp.extractor.twitch import TwitchBaseIE

        seen = {}

        def fake_download_json(self, url, video_id, note, data=None, headers=None, fatal=True):
            seen.update(url=url, headers=headers, data=data)
            return {"data": {"games": {"edges": [{"node": _node("Chess")}]}}}

        with patch.object(TwitchBaseIE, "_download_json", fake_download_json):
            data = sc._twitch_gql(_cfg(), {"query": "q", "variables": {"first": 1}})
        self.assertEqual(seen["url"], "https://gql.twitch.tv/gql")
        self.assertIn("Client-ID", seen["headers"])
        self.assertIn(b'"variables"', seen["data"])
        self.assertIn("games", data)

    def test_twitch_graphql_errors_raise(self):
        from yt_dlp.extractor.twitch import TwitchBaseIE

        with patch.object(TwitchBaseIE, "_download_json", return_value={"errors": [{"message": "bad"}]}):
            with self.assertRaises(sc.StreamCategoryError) as ctx:
                sc._twitch_gql(_cfg(), {"query": "q"})
        self.assertIn("bad", str(ctx.exception))

    def test_kick_query_goes_through_yt_dlps_kick_helper(self):
        from yt_dlp.extractor.kick import KickBaseIE

        seen = {}

        def fake_download_json(self, url, video_id, note=None, headers=None, impersonate=None, **kwargs):
            seen.update(url=url, impersonate=impersonate, query=kwargs.get("query"))
            return {"data": []}

        with patch.object(KickBaseIE, "_download_json", fake_download_json):
            sc._kick_api(_cfg(), "v1/subcategories", {"limit": 5, "page": 1})
        self.assertEqual(seen["url"], "https://kick.com/api/v1/subcategories")
        self.assertTrue(seen["impersonate"])
        self.assertEqual(seen["query"], {"limit": 5, "page": 1})
