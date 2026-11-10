"""Small Bazarr API client for triggering per-title missing-subtitle searches.

Bazarr's assigned language profile controls which subtitle languages are
searched. This client deliberately does not alter profiles or download a
particular provider result; it asks Bazarr to run its configured
"search-missing" action for a matched movie/series.
"""
from __future__ import annotations

import requests


class BazarrError(RuntimeError):
    pass


class BazarrClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 20, http=None):
        if not base_url or not api_key:
            raise ValueError("BAZARR_URL and BAZARR_API_KEY are required.")
        base_url = base_url.rstrip("/")
        self.api_base = base_url if base_url.endswith("/api") else base_url + "/api"
        self.api_key = api_key
        self.timeout = timeout
        self.http = http or requests.Session()
        self.headers = {"X-API-KEY": api_key, "Accept": "application/json"}

    def _request(self, method: str, path: str, *, params=None):
        url = self.api_base + "/" + path.lstrip("/")
        try:
            response = self.http.request(
                method, url, params=params, headers=self.headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise BazarrError(f"Could not reach Bazarr: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            detail = (response.text or "").strip().replace("\n", " ")[:300]
            raise BazarrError(
                f"Bazarr returned HTTP {response.status_code} for {path}"
                + (f": {detail}" if detail else "")
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise BazarrError(f"Bazarr returned invalid JSON for {path}.") from exc

    def _all(self, path: str) -> list[dict]:
        payload = self._request("GET", path, params={"start": 0, "length": -1})
        if isinstance(payload, dict):
            rows = payload.get("data", [])
        else:
            rows = payload
        if not isinstance(rows, list):
            raise BazarrError(f"Unexpected Bazarr response from /{path}.")
        return [row for row in rows if isinstance(row, dict)]

    def catalog(self):
        return self._all("series"), self._all("movies")

    def search_missing(self, media_type: str, bazarr_id: int):
        if media_type == "show":
            return self._request("PATCH", "series", params={
                "seriesid": int(bazarr_id), "action": "search-missing"
            })
        if media_type == "movie":
            return self._request("PATCH", "movies", params={
                "radarrid": int(bazarr_id), "action": "search-missing"
            })
        raise ValueError("media_type must be 'show' or 'movie'.")
