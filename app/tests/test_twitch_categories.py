from __future__ import annotations

import types
import unittest
from unittest.mock import patch

import twitch_categories as tc


def _cfg():
    return types.SimpleNamespace(
        YTDL_OPTIONS={}, DOWNLOAD_DIR="/tmp", TEMP_DIR="/tmp", ALLOW_PRIVATE_ADDRESSES=False
    )


def _node(name, art="https://static-cdn.jtvnw.net/ttv-boxart/1-52x72.jpg", viewers=10):
    return {"id": "1", "name": name, "displayName": name, "boxArtURL": art, "viewersCount": viewers}


class TwitchCategoryTests(unittest.TestCase):
    def setUp(self):
        tc._cache.clear()
        self.addCleanup(tc._cache.clear)

    def test_empty_query_lists_top_categories(self):
        data = {"games": {"edges": [{"node": _node("Just Chatting")}, {"node": _node("Chess")}]}}
        with patch("twitch_categories._gql", return_value=data) as gql:
            result = tc.search_categories_sync(_cfg(), "  ")
        self.assertEqual([c["name"] for c in result], ["Just Chatting", "Chess"])
        payload = gql.call_args.args[1]
        self.assertIn("games(first: $first)", payload["query"])
        self.assertEqual(payload["variables"], {"first": tc.TOP_LIMIT})

    def test_search_passes_the_query_as_a_variable_and_caches(self):
        data = {"searchCategories": {"edges": [{"node": _node("Chess")}]}}
        with patch("twitch_categories._gql", return_value=data) as gql:
            first = tc.search_categories_sync(_cfg(), 'ch"ess')
            second = tc.search_categories_sync(_cfg(), 'CH"ESS')
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
        with patch("twitch_categories._gql", return_value=data):
            result = tc.search_categories_sync(_cfg(), "x")
        self.assertEqual([c["name"] for c in result], ["A", "B", "C", "D"])
        self.assertEqual([c["box_art_url"] for c in result][:3], [None, None, None])
        self.assertTrue(result[3]["box_art_url"].startswith("https://static-cdn.jtvnw.net/"))

    def test_lookup_batches_uncached_names_and_omits_unknown_ones(self):
        tc._cache_put(("game", "chess"), {"name": "Chess", "display_name": "Chess",
                                         "box_art_url": None, "viewers": None})
        data = {"c0": _node("Art"), "c1": None}
        with patch("twitch_categories._gql", return_value=data) as gql:
            result = tc.lookup_categories_sync(_cfg(), ["Chess", "Art", "My Custom"])
            again = tc.lookup_categories_sync(_cfg(), ["Art", "My Custom"])
        self.assertEqual([c["name"] for c in result], ["Chess", "Art"])
        self.assertEqual([c["name"] for c in again], ["Art"])
        gql.assert_called_once()
        payload = gql.call_args.args[1]
        self.assertEqual(payload["variables"], {"n0": "Art", "n1": "My Custom"})
        self.assertIn("c1: game(name: $n1)", payload["query"])


class TwitchGqlTests(unittest.TestCase):
    def setUp(self):
        try:
            from yt_dlp.extractor.twitch import TwitchBaseIE  # noqa: F401
        except Exception:
            self.skipTest("real yt_dlp not importable (stubbed in this run)")

    def test_query_goes_through_yt_dlps_twitch_helper(self):
        from yt_dlp.extractor.twitch import TwitchBaseIE

        seen = {}

        def fake_download_json(self, url, video_id, note, data=None, headers=None, fatal=True):
            seen["url"] = url
            seen["headers"] = headers
            seen["data"] = data
            return {"data": {"games": {"edges": [{"node": _node("Chess")}]}}}

        with patch.object(TwitchBaseIE, "_download_json", fake_download_json):
            data = tc._gql(_cfg(), {"query": "q", "variables": {"first": 1}})
        self.assertEqual(seen["url"], "https://gql.twitch.tv/gql")
        self.assertIn("Client-ID", seen["headers"])
        self.assertIn(b'"variables"', seen["data"])
        self.assertIn("games", data)

    def test_graphql_errors_raise(self):
        from yt_dlp.extractor.twitch import TwitchBaseIE

        with patch.object(TwitchBaseIE, "_download_json", return_value={"errors": [{"message": "bad"}]}):
            with self.assertRaises(tc.TwitchCategoryError) as ctx:
                tc._gql(_cfg(), {"query": "q"})
        self.assertIn("bad", str(ctx.exception))
