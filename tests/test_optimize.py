import copy

import pytest

from plexlang.optimize import MULTI_NAME, Optimizer, build_plan

LANGS = [{"id": 1, "name": "English"}, {"id": 5, "name": "Italian"}, {"id": 8, "name": "Japanese"}]


def lang_cf(cf_id, name, language_id):
    return {"id": cf_id, "name": name, "specifications": [
        {"implementation": "LanguageSpecification", "negate": False,
         "fields": [{"name": "value", "value": language_id}]}]}


class FakeArr:
    def __init__(self, formats, profiles):
        self.formats, self.profiles = copy.deepcopy(formats), copy.deepcopy(profiles)
        self.next_id = 100
        self.log = []

    def languages(self): return LANGS
    def custom_formats(self): return copy.deepcopy(self.formats)
    def quality_profiles(self): return copy.deepcopy(self.profiles)

    def create_custom_format(self, body):
        cf = {**body, "id": self.next_id}
        self.next_id += 1
        self.formats.append(cf)
        for p in self.profiles:  # Sonarr/Radarr add new formats to every profile at 0
            p["formatItems"].append({"format": cf["id"], "name": cf["name"], "score": 0})
        self.log.append(("create", cf["name"]))
        return cf

    def delete_custom_format(self, fid):
        self.formats = [f for f in self.formats if f["id"] != fid]
        for p in self.profiles:
            p["formatItems"] = [i for i in p["formatItems"] if i["format"] != fid]
        self.log.append(("delete", fid))

    def update_quality_profile(self, pid, body):
        self.profiles = [copy.deepcopy(body) if p["id"] == pid else p for p in self.profiles]
        self.log.append(("update", pid))


def profile(pid, name, items=(), **kw):
    return {"id": pid, "name": name, "upgradeAllowed": True, "cutoffFormatScore": 0,
            "formatItems": [{"format": f, "name": str(f), "score": s} for f, s in items],
            "language": {"id": -1, "name": "Any"}, **kw}


@pytest.fixture
def arr():
    formats = [lang_cf(1, "My Italian", 5)]
    return FakeArr(formats, [
        profile(10, "HD", [(1, 0)], cutoffFormatScore=0, language={"id": -2, "name": "Original"}),
        profile(11, "Anime", [(1, 500)], upgradeAllowed=False),
    ])


def test_plan_reuses_existing_formats_and_never_lowers_scores(arr):
    plan = build_plan(arr)
    assert [f["name"] for f in plan["new_formats"]] == [
        "PLC Language: English", "PLC Language: Japanese", MULTI_NAME]
    hd = next(p for p in plan["profiles"] if p["id"] == 10)
    italian = next(c for c in hd["changes"] if c.get("format") == "My Italian")
    assert (italian["from"], italian["to"]) == (0, 200)
    assert any(c["type"] == "language" and c["to"] == "Any" for c in hd["changes"])
    assert any(c["type"] == "cutoff" for c in hd["changes"])
    anime = next(p for p in plan["profiles"] if p["id"] == 11)
    # Existing 500 is higher than 200, so it stays; upgrades get switched on.
    assert not any(c.get("format") == "My Italian" for c in anime["changes"])
    assert {"type": "upgrade", "from": False, "to": True} in anime["changes"]


def test_preview_is_read_only(arr, tmp_path):
    Optimizer(sonarr=arr, data_dir=str(tmp_path)).preview("sonarr")
    assert arr.log == [] and arr.profiles[0]["formatItems"] == [{"format": 1, "name": "1", "score": 0}]


def test_apply_requires_confirmation_and_selection(arr, tmp_path):
    opt = Optimizer(sonarr=arr, data_dir=str(tmp_path))
    pid = opt.preview("sonarr")["preview_id"]
    with pytest.raises(ValueError):
        opt.apply(pid, [10], confirmed=False)
    with pytest.raises(ValueError):
        opt.apply(pid, [999], confirmed=True)
    assert arr.log == []


def test_apply_backs_up_then_changes_only_selected_profiles(arr, tmp_path):
    original = copy.deepcopy(arr.profiles)
    opt = Optimizer(sonarr=arr, data_dir=str(tmp_path))
    preview = opt.preview("sonarr")
    result = opt.apply(preview["preview_id"], [10], confirmed=True)
    assert result["created_formats"] == ["PLC Language: English", "PLC Language: Japanese", MULTI_NAME]
    hd = next(p for p in arr.profiles if p["id"] == 10)
    scores = {i["name"]: i["score"] for i in hd["formatItems"]}
    assert scores["1"] == 200 and scores["PLC Language: English"] == 100
    assert scores["PLC Language: Japanese"] == 100 and scores[MULTI_NAME] == 50
    assert hd["language"]["name"] == "Any" and hd["cutoffFormatScore"] >= 450
    anime = next(p for p in arr.profiles if p["id"] == 11)
    assert anime["upgradeAllowed"] is False  # not selected, untouched
    assert all(i["score"] == 0 for i in anime["formatItems"] if i["name"].startswith("PLC"))
    backups = opt.backups()
    assert len(backups) == 1 and backups[0]["profiles"] == ["HD"]
    assert backups[0]["created_formats"] == result["created_formats"]
    assert original[0]["formatItems"] == [{"format": 1, "name": "1", "score": 0}]


def test_restore_puts_back_profiles_and_deletes_only_created_formats(arr, tmp_path):
    original = copy.deepcopy(arr.profiles)
    opt = Optimizer(sonarr=arr, data_dir=str(tmp_path))
    opt.apply(opt.preview("sonarr")["preview_id"], [10, 11], confirmed=True)
    backup_id = opt.backups()[0]["id"]
    with pytest.raises(ValueError):
        opt.restore(backup_id, confirmed=False)
    result = opt.restore(backup_id, confirmed=True)
    assert set(result["removed_formats"]) == {"PLC Language: English", "PLC Language: Japanese", MULTI_NAME}
    assert [f["name"] for f in arr.formats] == ["My Italian"]  # user's own format kept
    assert arr.profiles == original
    with pytest.raises(ValueError):
        opt.restore(backup_id, confirmed=True)  # only once


def test_rerun_is_idempotent(arr, tmp_path):
    opt = Optimizer(sonarr=arr, data_dir=str(tmp_path))
    opt.apply(opt.preview("sonarr")["preview_id"], [10, 11], confirmed=True)
    again = opt.preview("sonarr")
    assert again["new_formats"] == [] and not any(p["changed"] for p in again["profiles"])


def test_restore_rejects_bad_backup_ids(arr, tmp_path):
    opt = Optimizer(sonarr=arr, data_dir=str(tmp_path))
    with pytest.raises(LookupError):
        opt.restore("../../etc/passwd", confirmed=True)


def test_endpoints_require_confirmation_and_roundtrip(arr, tmp_path):
    from plexlang import web
    from plexlang.index import LibraryIndex

    index = LibraryIndex(lambda: None, path=str(tmp_path / "i.json"))
    session = web.PlexSession("http://plex", "t", connect=lambda *a: "admin", index=index,
                              arr_clients=(arr, None, None))
    session.optimizer = Optimizer(sonarr=arr, data_dir=str(tmp_path / "data"))
    client = web.create_app(session).test_client()
    preview = client.post("/api/arr/optimize/preview", json={"service": "sonarr"}).json
    assert preview["new_formats"] and "_targets" not in preview
    assert client.post("/api/arr/optimize/preview", json={"service": "radarr"}).status_code == 503
    denied = client.post("/api/arr/optimize/apply", json={"preview_id": preview["preview_id"], "profile_ids": [10]})
    assert denied.status_code == 400 and arr.log == []
    ok = client.post("/api/arr/optimize/apply", json={"preview_id": preview["preview_id"],
                                                      "profile_ids": [10], "confirm": True})
    assert ok.status_code == 200
    backup_id = client.get("/api/arr/optimize/backups").json["backups"][0]["id"]
    assert client.post("/api/arr/optimize/restore", json={"backup_id": backup_id}).status_code == 400
    assert client.post("/api/arr/optimize/restore", json={"backup_id": backup_id, "confirm": True}).status_code == 200
