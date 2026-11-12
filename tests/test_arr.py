from unittest.mock import Mock

from plexlang.acquire import Acquirer
from plexlang.arr import ArrError, RadarrClient, analyse_profile, classify_custom_format

ITALIAN = {"id": 5, "name": "Italian"}
LANG_CF = {"id": 1, "name": "Italian", "specifications": [
    {"implementation": "LanguageSpecification", "negate": False, "fields": [{"name": "value", "value": 5}]}]}
MULTI_CF = {"id": 2, "name": "MULTi ITA", "specifications": [
    {"implementation": "ReleaseTitleSpecification", "negate": False,
     "fields": [{"name": "value", "value": r"\b(MULTi|ITA)\b"}]}]}
OTHER_CF = {"id": 3, "name": "x265", "specifications": [
    {"implementation": "ReleaseTitleSpecification", "fields": [{"name": "value", "value": "x265"}]}]}


def profile(pid, name, scores, **kw):
    return {"id": pid, "name": name, "upgradeAllowed": True, "cutoffFormatScore": 10000,
            "minFormatScore": 0, "formatItems": [{"format": f, "score": s} for f, s in scores.items()], **kw}


def test_classifies_language_and_title_formats():
    assert classify_custom_format(LANG_CF, {5}) == "language"
    assert classify_custom_format(MULTI_CF, {5}) == "multi"
    assert classify_custom_format(OTHER_CF, {5}) is None


def test_good_profile_is_ready():
    a = analyse_profile(profile(1, "HD ITA", {1: 100, 2: 200, 3: 10}), [LANG_CF, MULTI_CF, OTHER_CF], {5})
    assert a["ready"] and a["italian_score"] == 200 and a["problems"] == []


def test_profile_problems_are_explained():
    a = analyse_profile(profile(2, "HD", {3: 10}), [LANG_CF, MULTI_CF, OTHER_CF], {5})
    assert not a["ready"] and "No custom format" in a["problems"][0]
    a = analyse_profile(profile(3, "Low cutoff", {1: 100}, cutoffFormatScore=50,
                                language={"name": "Original"}), [LANG_CF], {5})
    text = " ".join(a["problems"])
    assert "cutoff (50)" in text and "Original" in text and "MULTi" in text and not a["ready"]
    a = analyse_profile(profile(4, "No upgrades", {1: 100, 2: 200}, upgradeAllowed=False), [LANG_CF, MULTI_CF], {5})
    assert "Upgrades are off" in a["problems"][0]


class FakeArr:
    def __init__(self, kind, rows):
        self.kind, self.rows, self.calls = kind, rows, []

    def status(self): return {"version": "4.0.0"}
    def languages(self): return [ITALIAN, {"id": 1, "name": "English"}]
    def custom_formats(self): return [LANG_CF, MULTI_CF]
    def quality_profiles(self): return [profile(1, "HD", {}), profile(7, "HD Italian", {1: 100, 2: 200})]
    def root_folders(self): return [{"path": "/tv"}]
    def series(self): return self.rows
    def movies(self): return self.rows
    def episodes(self, sid): return [{"id": 100 + n, "seasonNumber": 1, "episodeNumber": n} for n in (1, 2, 3)]
    def set_profile(self, *a): self.calls.append(("profile", *a))
    def search(self, item_id, episode_ids=None): self.calls.append(("search", item_id, episode_ids))


class FakeIndex:
    def episodes_missing(self, key, language, kind="audio"):
        return [(1, 2), (1, 3)]


def run_job(acq, preview):
    started = acq.start(preview["preview_id"], [r["key"] for r in preview["rows"]])
    acq._executor.shutdown(wait=True)
    return acq.jobs[started["job_id"]]


def test_sonarr_searches_only_episodes_missing_italian_after_profile_switch():
    sonarr = FakeArr("sonarr", [{"id": 9, "title": "Show", "year": 2019, "tvdbId": 555, "qualityProfileId": 1}])
    acq = Acquirer(FakeIndex(), sonarr=sonarr)
    report = acq.config_report()
    assert report["sonarr"]["target_profile"] == {"id": 7, "name": "HD Italian"}
    title = {"key": "s1", "title": "Show", "year": 2019, "type": "show", "ids": {"tvdb": ["555"]}}
    preview = acq.preview([title])
    row = preview["rows"][0]
    assert row["service"] == "sonarr" and row["profile_change"] and row["episodes"] == [[1, 2], [1, 3]]
    job = run_job(acq, preview)
    assert job["succeeded"] == 1
    assert sonarr.calls == [("profile", 9, 7), ("search", 9, [102, 103])]


def test_movie_not_in_radarr_goes_to_seerr():
    seerr = Mock()
    seerr.status.return_value = {"version": "2.0"}
    seerr.servers.side_effect = lambda kind: [{"id": 0, "name": "Radarr ITA", "isDefault": True, "is4k": False,
                                               "profiles": [{"id": 4, "name": "HD"}]}] if kind == "radarr" else []
    radarr = FakeArr("radarr", [])
    acq = Acquirer(FakeIndex(), radarr=radarr, seerr=seerr)
    title = {"key": "m1", "title": "Film", "year": 2020, "type": "movie", "ids": {"tmdb": ["42"]}}
    preview = acq.preview([title])
    assert preview["rows"][0]["service"] == "seerr"
    job = run_job(acq, preview)
    assert job["succeeded"] == 1
    seerr.request_media.assert_called_once_with({"mediaType": "movie", "mediaId": 42, "serverId": 0})


def test_unreachable_service_is_reported_not_raised():
    radarr = RadarrClient("http://radarr:7878", "k", http=Mock(**{"request.side_effect": __import__("requests").ConnectionError()}))
    acq = Acquirer(FakeIndex(), radarr=radarr)
    assert "Could not reach" in acq.config_report()["radarr"]["error"]
    preview = acq.preview([{"key": "m1", "title": "Film", "type": "movie", "ids": {}}])
    assert preview["rows"][0]["service"] is None


def test_radarr_client_paths():
    http = Mock()
    http.request.return_value = Mock(status_code=201, content=b"{}", json=lambda: {})
    RadarrClient("http://radarr:7878/", "k", http=http).search(5)
    args, kwargs = http.request.call_args
    assert args == ("POST", "http://radarr:7878/api/v3/command")
    assert kwargs["json"] == {"name": "MoviesSearch", "movieIds": [5]}
    assert kwargs["headers"]["X-Api-Key"] == "k"


def test_errors_are_arr_errors():
    http = Mock()
    http.request.return_value = Mock(status_code=401, text="Unauthorized", content=b"x")
    try:
        RadarrClient("http://r", "bad", http=http).status()
    except ArrError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError
