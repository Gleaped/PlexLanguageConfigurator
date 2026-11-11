import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from plexlang.profile_mappings import ProfileMappingStore
from plexlang.bazarr_client import BazarrClient


class ProfileMappingStoreTests(unittest.TestCase):
    def test_save_and_load_normalizes_language_and_profile_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileMappingStore(str(Path(tmp) / "map.json"))
            store.save({"ITA": 3, "eng": "12"})
            self.assertEqual(store.load(), {"ita": "3", "eng": "12"})

    def test_rejects_invalid_language_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileMappingStore(str(Path(tmp) / "map.json"))
            with self.assertRaises(ValueError):
                store.save({"italian!": "2"})

    def test_rejects_non_numeric_profile_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileMappingStore(str(Path(tmp) / "map.json"))
            with self.assertRaises(ValueError):
                store.save({"ita": "italian"})


class BazarrProfileApiTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.client = BazarrClient("http://bazarr:6767", "secret", http=self.http)

    def test_get_language_profiles(self):
        response = Mock(status_code=200, content=b"[]")
        response.json.return_value = [{"profileId": 2, "name": "Italian"}]
        self.http.request.return_value = response
        self.assertEqual(self.client.language_profiles(), [{"profileId": 2, "name": "Italian"}])
        self.assertEqual(self.http.request.call_args.args[:2],
                         ("GET", "http://bazarr:6767/api/system/languages/profiles"))

    def test_assign_series_profile(self):
        self.http.request.return_value = Mock(status_code=204, content=b"")
        self.client.assign_profile("show", 42, 3)
        args, kwargs = self.http.request.call_args
        self.assertEqual(args[:2], ("POST", "http://bazarr:6767/api/series"))
        self.assertEqual(kwargs["params"], {"seriesid": 42, "profileid": "3"})

    def test_assign_movie_profile(self):
        self.http.request.return_value = Mock(status_code=204, content=b"")
        self.client.assign_profile("movie", 17, 4)
        args, kwargs = self.http.request.call_args
        self.assertEqual(args[:2], ("POST", "http://bazarr:6767/api/movies"))
        self.assertEqual(kwargs["params"], {"radarrid": 17, "profileid": "4"})


if __name__ == "__main__":
    unittest.main()
