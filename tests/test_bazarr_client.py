import unittest
from unittest.mock import Mock

from plexlang.bazarr_client import BazarrClient


class BazarrClientTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.client = BazarrClient("http://bazarr:6767", "secret", http=self.http)

    def test_series_search_missing_uses_series_id(self):
        response = Mock(status_code=204, content=b"")
        self.http.request.return_value = response
        self.client.search_missing("show", 42)
        args, kwargs = self.http.request.call_args
        self.assertEqual(args[:2], ("PATCH", "http://bazarr:6767/api/series"))
        self.assertEqual(kwargs["params"], {"seriesid": 42, "action": "search-missing"})
        self.assertEqual(kwargs["headers"]["X-API-KEY"], "secret")

    def test_movie_search_missing_uses_radarr_id(self):
        response = Mock(status_code=204, content=b"")
        self.http.request.return_value = response
        self.client.search_missing("movie", 17)
        args, kwargs = self.http.request.call_args
        self.assertEqual(args[:2], ("PATCH", "http://bazarr:6767/api/movies"))
        self.assertEqual(kwargs["params"], {"radarrid": 17, "action": "search-missing"})

    def test_url_with_api_suffix_is_not_duplicated(self):
        client = BazarrClient("http://bazarr:6767/api/", "secret", http=self.http)
        self.http.request.return_value = Mock(status_code=204, content=b"")
        client.search_missing("movie", 7)
        self.assertEqual(self.http.request.call_args.args[1], "http://bazarr:6767/api/movies")


if __name__ == "__main__":
    unittest.main()
