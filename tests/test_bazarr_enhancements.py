import unittest
from unittest.mock import Mock

from plexlang.bazarr_client import BazarrClient
from plexlang.bazarr_profiles import profile_language_values, suggest_profiles


class BazarrReadApiTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.client = BazarrClient("http://bazarr:6767", "secret", http=self.http)

    def test_get_language_catalogue(self):
        response = Mock(status_code=200, content=b"[]")
        response.json.return_value = [{"code2": "it", "code3": "ita", "name": "Italian"}]
        self.http.request.return_value = response
        self.assertEqual(self.client.languages()[0]["name"], "Italian")
        self.assertEqual(self.http.request.call_args.args[:2],
                         ("GET", "http://bazarr:6767/api/system/languages"))

    def test_get_wanted_movie_subtitles(self):
        response = Mock(status_code=200, content=b"[]")
        response.json.return_value = {"data": [{"radarrId": 7, "language": "Italian"}]}
        self.http.request.return_value = response
        self.assertEqual(self.client.wanted_movies()[0]["radarrId"], 7)
        self.assertEqual(self.http.request.call_args.args[1],
                         "http://bazarr:6767/api/movies/wanted")

    def test_get_wanted_episode_subtitles(self):
        response = Mock(status_code=200, content=b"[]")
        response.json.return_value = [{"seriesId": 42, "language": "Italian"}]
        self.http.request.return_value = response
        self.assertEqual(self.client.wanted_episodes()[0]["seriesId"], 42)
        self.assertEqual(self.http.request.call_args.args[1],
                         "http://bazarr:6767/api/episodes/wanted")


class BazarrProfileSuggestionTests(unittest.TestCase):
    def test_extracts_language_aliases_from_nested_profile(self):
        profile = {"profileId": 2, "languages": [
            {"language": {"name": "Italian", "code2": "it", "code3": "ita"}}
        ]}
        values = profile_language_values(profile)
        self.assertIn("italian", values)
        self.assertIn("it", values)
        self.assertIn("ita", values)

    def test_suggests_profile_containing_selected_language_first(self):
        profiles = [
            {"profileId": 1, "name": "English", "languages": [{"code2": "en"}]},
            {"profileId": 2, "name": "Italian + English", "languages": [
                {"name": "Italian", "code2": "it", "code3": "ita"},
                {"name": "English", "code2": "en"},
            ]},
        ]
        suggestions = suggest_profiles(profiles, "ita", "Italian", {"it", "ita", "italian"})
        self.assertEqual(suggestions[0]["id"], "2")
        self.assertTrue(suggestions[0]["contains_language"])
        self.assertFalse(suggestions[1]["contains_language"])

    def test_profile_name_alone_is_not_a_language_match(self):
        profiles = [{"profileId": 9, "name": "Italian", "languages": []}]
        suggestions = suggest_profiles(profiles, "it", "Italian", {"it", "italian"})
        self.assertFalse(suggestions[0]["contains_language"])


if __name__ == "__main__":
    unittest.main()
