"""Sonarr, Radarr and Seerr (Overseerr/Jellyseerr) clients and strategy checks.

How a "get me a multilingual version" request works:

* Sonarr/Radarr pick releases by quality profile. In Sonarr v4 and Radarr
  v4/v5, language preference is expressed with **custom formats** (a Language
  condition, or a release-title regex such as ``\\bMULTi\\b`` / ``\\bITA\\b``) that
  the profile scores. A search only *replaces* an existing file when the new
  release scores higher and the profile allows upgrades up to a cutoff that
  the current file hasn't reached.
* So the app never edits profiles or custom formats. It reads them, reports
  whether a search would actually prefer an Italian/MULTi release (``analyse_profile``),
  optionally moves the title to a profile you choose, then triggers a search:
  only the episodes Plex says lack Italian for Sonarr, the movie for Radarr.
* Seerr can't re-request media it already considers available, so it's used
  for titles Sonarr/Radarr don't manage, or to send a request to a separate
  Sonarr/Radarr server you've configured in Seerr for Italian.
"""

from __future__ import annotations

import re

import requests

from .index import language_aliases


class ArrError(RuntimeError):
    pass


class _Http:
    def __init__(self, base_url: str, api_key: str, api_prefix: str, timeout: int = 20, http=None):
        if not base_url or not api_key:
            raise ValueError("URL and API key are required.")
        base = base_url.rstrip("/")
        self.base = base if base.endswith(api_prefix) else base + api_prefix
        self.headers = {"X-Api-Key": api_key, "Accept": "application/json"}
        self.timeout = timeout
        self.http = http or requests.Session()
        self.name = type(self).__name__.replace("Client", "")

    def request(self, method: str, path: str, *, params=None, json=None):
        url = self.base + "/" + path.lstrip("/")
        try:
            response = self.http.request(method, url, params=params, json=json,
                                         headers=self.headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ArrError(f"Could not reach {self.name}: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            detail = (response.text or "").strip().replace("\n", " ")[:300]
            raise ArrError(f"{self.name} returned HTTP {response.status_code} for {path}"
                           + (f": {detail}" if detail else ""))
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ArrError(f"{self.name} returned invalid JSON for {path}.") from exc


class _ArrClient(_Http):
    """Shared Sonarr/Radarr v3 API."""

    def __init__(self, base_url, api_key, **kw):
        super().__init__(base_url, api_key, "/api/v3", **kw)

    def status(self) -> dict:
        return self.request("GET", "system/status") or {}

    def quality_profiles(self) -> list[dict]:
        return self.request("GET", "qualityprofile") or []

    def custom_formats(self) -> list[dict]:
        return self.request("GET", "customformat") or []

    def languages(self) -> list[dict]:
        try:
            return self.request("GET", "language") or []
        except ArrError:
            return []

    def root_folders(self) -> list[dict]:
        return self.request("GET", "rootfolder") or []

    def language_profiles(self) -> list[dict]:
        """Sonarr v3 only; v4 removed language profiles in favour of custom formats."""
        try:
            return self.request("GET", "languageprofile") or []
        except ArrError:
            return []

    def command(self, body: dict):
        return self.request("POST", "command", json=body)


class SonarrClient(_ArrClient):
    def series(self) -> list[dict]:
        return self.request("GET", "series") or []

    def episodes(self, series_id: int) -> list[dict]:
        return self.request("GET", "episode", params={"seriesId": series_id}) or []

    def set_profile(self, series_id: int, profile_id: int):
        return self.request("PUT", "series/editor",
                            json={"seriesIds": [series_id], "qualityProfileId": profile_id})

    def search(self, series_id: int, episode_ids: list[int] | None = None):
        if episode_ids:
            return self.command({"name": "EpisodeSearch", "episodeIds": episode_ids})
        return self.command({"name": "SeriesSearch", "seriesId": series_id})


class RadarrClient(_ArrClient):
    def movies(self) -> list[dict]:
        return self.request("GET", "movie") or []

    def set_profile(self, movie_id: int, profile_id: int):
        return self.request("PUT", "movie/editor",
                            json={"movieIds": [movie_id], "qualityProfileId": profile_id})

    def search(self, movie_id: int, episode_ids=None):
        return self.command({"name": "MoviesSearch", "movieIds": [movie_id]})


class SeerrClient(_Http):
    """Overseerr / Jellyseerr."""

    def __init__(self, base_url, api_key, **kw):
        super().__init__(base_url, api_key, "/api/v1", **kw)
        self.name = "Seerr"

    def status(self) -> dict:
        return self.request("GET", "status") or {}

    def servers(self, kind: str) -> list[dict]:
        """Configured Sonarr ('sonarr') or Radarr ('radarr') servers, with profiles."""
        rows = self.request("GET", f"settings/{kind}") or []
        out = []
        for row in rows:
            try:
                detail = self.request("GET", f"service/{kind}/{row.get('id')}") or {}
            except ArrError:
                detail = {}
            out.append({"id": row.get("id"), "name": row.get("name"), "is4k": row.get("is4k", False),
                        "isDefault": row.get("isDefault", False),
                        "activeProfileId": row.get("activeProfileId"),
                        "activeDirectory": row.get("activeDirectory"),
                        "profiles": [{"id": p.get("id"), "name": p.get("name")}
                                     for p in detail.get("profiles", [])],
                        "rootFolders": [r.get("path") for r in detail.get("rootFolders", [])]})
        return out

    def media(self, media_type: str, tmdb_id: int) -> dict:
        return self.request("GET", f"{media_type}/{tmdb_id}") or {}

    def request_media(self, body: dict):
        return self.request("POST", "request", json=body)


# ----- strategy analysis -------------------------------------------------

# "ITA" as a word inside a regex (e.g. \bITA\b, [. ]ITA[. ]) or MULTi.
_TITLE_HINT = re.compile(r"ita(?![a-z])|multi", re.I)


def _language_ids(languages: list[dict], language: str) -> set[int]:
    aliases = language_aliases(language)
    return {int(l["id"]) for l in languages
            if str(l.get("name", "")).casefold() in aliases and l.get("id") is not None}


def classify_custom_format(cf: dict, language_ids: set[int]) -> str | None:
    """'language' if it requires the language, 'multi' if it matches MULTi/ITA titles."""
    kind = None
    for spec in cf.get("specifications", []):
        impl = spec.get("implementation", "")
        fields = {f.get("name"): f.get("value") for f in spec.get("fields", [])}
        if impl == "LanguageSpecification" and not spec.get("negate"):
            if fields.get("value") in language_ids:
                return "language"
        if impl == "ReleaseTitleSpecification" and not spec.get("negate"):
            if _TITLE_HINT.search(str(fields.get("value") or "")):
                kind = "multi"
    return kind


def analyse_profile(profile: dict, custom_formats: list[dict], language_ids: set[int],
                    languages: list[dict] | None = None) -> dict:
    """Would a search with this profile prefer (and upgrade to) an Italian/MULTi release?"""
    kinds = {cf["id"]: classify_custom_format(cf, language_ids) for cf in custom_formats}
    names = {cf["id"]: cf.get("name") for cf in custom_formats}
    scored = []
    for item in profile.get("formatItems", []):
        kind = kinds.get(item.get("format"))
        if kind:
            scored.append({"name": names.get(item.get("format")), "kind": kind,
                           "score": item.get("score", 0)})
    best = max((s["score"] for s in scored), default=0)
    problems = []
    profile_language = (profile.get("language") or {}).get("name")
    if profile_language and profile_language.casefold() not in ("any", "original") \
            and profile_language.casefold() not in language_aliases("ita"):
        problems.append(f"Profile language is '{profile_language}', so Italian releases are rejected.")
    if profile_language and profile_language.casefold() == "original":
        problems.append("Profile language is 'Original': Italian audio is only accepted for Italian-original titles, unless a custom format overrides it.")
    if not scored:
        problems.append("No custom format scores Italian or MULTi releases, so a search won't prefer them.")
    elif best <= 0:
        problems.append("Italian/MULTi custom formats score 0 or less, so they're not preferred.")
    if not profile.get("upgradeAllowed", True):
        problems.append("Upgrades are off, so an existing file won't be replaced.")
    cutoff_score = profile.get("cutoffFormatScore")
    if cutoff_score is not None and best > 0 and cutoff_score < best:
        problems.append(f"Custom format cutoff ({cutoff_score}) is below the Italian score ({best}); "
                        "files already at the cutoff won't be upgraded.")
    min_score = profile.get("minFormatScore") or 0
    if best and min_score > best:
        problems.append(f"Minimum custom format score ({min_score}) is above the Italian score ({best}).")
    positive = {s["kind"] for s in scored if s["score"] > 0}
    if positive and "multi" not in positive:
        problems.append("Only a Language format is scored: an Italian-only release could replace the "
                        "original-language audio. Score a MULTi format higher to keep both.")
    if positive and "language" not in positive:
        problems.append("Only a MULTi/title format is scored: MULTi also matches non-Italian releases "
                        "(for example French + English). Add a Language: Italian condition.")
    return {"id": profile.get("id"), "name": profile.get("name"), "italian_formats": scored,
            "italian_score": best, "upgrade_allowed": profile.get("upgradeAllowed", True),
            "cutoff_format_score": cutoff_score, "min_format_score": min_score,
            "language": profile_language, "problems": problems,
            "ready": not [p for p in problems if not p.startswith("Only a ")]}


def pick_profile(analysis: list[dict], wanted: str | None):
    """The configured Italian profile (by id or name), else the best-scoring one."""
    if wanted:
        for p in analysis:
            if str(p["id"]) == str(wanted) or str(p["name"]).casefold() == str(wanted).casefold():
                return p
    ready = [p for p in analysis if p["ready"]]
    return max(ready, key=lambda p: p["italian_score"]) if ready else None
