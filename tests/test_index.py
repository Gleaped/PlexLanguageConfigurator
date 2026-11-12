import re
from xml.etree import ElementTree as ET

import pytest

from plexlang import web
from plexlang.index import LibraryIndex, coverage, language_aliases
from plexlang.labels import LabelSync


def _streams(audio=(), subs=()):
    out = "".join(f'<Stream streamType="2" languageCode="{c}" language="{c.title()}"/>' for c in audio)
    out += "".join(f'<Stream streamType="3" languageCode="{c}" language="{c.title()}"/>' for c in subs)
    return out


class FakePlex:
    """Answers the handful of Plex XML endpoints the index uses."""

    def __init__(self):
        self.calls = []
        self.updated = {"m1": "1", "m2": "1", "e1": "1", "e2": "1", "e3": "1"}
        self.labels = {"m2"}  # items already labelled "Italian"
        self.edits = []
        self.streams = {
            "m1": _streams(["ita", "eng"]),
            "m2": _streams(["eng"]),
            "e1": _streams(["ita"], ["ita"]),
            "e2": _streams(["eng"], ["ita"]),
            "e3": _streams(["eng"]),
        }

    def _video(self, key, detail):
        kind = "movie" if key.startswith("m") else "episode"
        extra = f' grandparentRatingKey="s1"' if kind == "episode" else ""
        body = f'<Media><Part id="p{key}">{self.streams[key] if detail else ""}</Part></Media>'
        if detail and kind == "movie":
            body += f'<Guid id="imdb://tt-{key}"/>'
        return (f'<Video ratingKey="{key}" type="{kind}" title="T {key}" year="2020" '
                f'updatedAt="{self.updated[key]}"{extra}>{body}</Video>')

    def query(self, path, headers=None):
        self.calls.append(path)
        if path == "/library/sections":
            xml = '<MediaContainer><Directory key="1" type="movie" title="Movies"/>' \
                  '<Directory key="2" type="show" title="TV"/><Directory key="3" type="artist"/></MediaContainer>'
        elif path.endswith("/label"):
            xml = '<MediaContainer><Directory key="77" title="Italian"/></MediaContainer>'
        elif "label=77" in path:
            keys = [k for k in self.labels if (k.startswith("m")) == path.startswith("/library/sections/1")]
            xml = "<MediaContainer>" + "".join(f'<Video ratingKey="{k}"/>' for k in keys) + "</MediaContainer>"
        elif path == "/library/sections/1/all?type=1":
            xml = f'<MediaContainer totalSize="2">{self._video("m1", False)}{self._video("m2", False)}</MediaContainer>'
        elif path == "/library/sections/2/all?type=2":
            xml = '<MediaContainer totalSize="1"><Directory ratingKey="s1" title="Show" year="2019" type="show"/></MediaContainer>'
        elif path == "/library/sections/2/all?type=4":
            xml = "<MediaContainer>" + "".join(self._video(k, False) for k in ("e1", "e2", "e3")) + "</MediaContainer>"
        elif path.startswith("/library/metadata/"):
            keys = path.rsplit("/", 1)[1].split(",")
            parts = []
            for k in keys:
                if k == "s1":
                    parts.append('<Directory ratingKey="s1" type="show" title="Show"><Guid id="tvdb://555"/></Directory>')
                else:
                    parts.append(self._video(k, True))
            xml = "<MediaContainer>" + "".join(parts) + "</MediaContainer>"
        else:
            raise AssertionError(path)
        return ET.fromstring(xml)

    def fetchItem(self, path):
        key = path.rsplit("/", 1)[1]
        plex = self

        class Item:
            def addLabel(self, label, locked=True):
                plex.edits.append(("add", key, label))

            def removeLabel(self, label, locked=True):
                plex.edits.append(("remove", key, label))
        return Item()


@pytest.fixture
def fake(tmp_path):
    plex = FakePlex()
    index = LibraryIndex(lambda: plex, path=str(tmp_path / "index.json"), batch_size=2, workers=2)
    return plex, index


def _by_key(index):
    return {t["key"]: t for t in index.titles()}


def test_builds_counts_per_series_and_movie(fake):
    plex, index = fake
    index.refresh()
    titles = _by_key(index)
    assert set(titles) == {"m1", "m2", "s1"}
    ita = language_aliases("ita")
    show = titles["s1"]
    assert show["total"] == 3
    assert coverage(show, "audio", ita) == 1 and coverage(show, "subtitles", ita) == 2
    assert show["ids"]["tvdb"] == ["555"] and titles["m1"]["ids"]["imdb"] == ["tt-m1"]
    # Batched: never one metadata request per episode.
    meta = [c for c in plex.calls if c.startswith("/library/metadata/")]
    assert all("," in c for c in meta)


def test_incremental_refresh_rereads_only_changed_items(fake):
    plex, index = fake
    index.refresh()
    plex.calls.clear()
    plex.updated["e3"] = "2"
    plex.streams["e3"] = _streams(["ita"])
    index.refresh()
    meta = [c for c in plex.calls if c.startswith("/library/metadata/")]
    assert meta == ["/library/metadata/e3"]
    assert coverage(_by_key(index)["s1"], "audio", language_aliases("ita")) == 2


def test_index_persists_and_loads_without_plex(fake, tmp_path):
    plex, index = fake
    index.refresh()
    reloaded = LibraryIndex(lambda: (_ for _ in ()).throw(RuntimeError("offline")),
                            path=str(tmp_path / "index.json"))
    assert reloaded.ready and _by_key(reloaded)["s1"]["total"] == 3


def test_removed_titles_drop_out(fake):
    plex, index = fake
    index.refresh()
    original = plex.query

    def without_m2(path, headers=None):
        root = original(path, headers)
        for child in list(root):
            if child.get("ratingKey") == "m2":
                root.remove(child)
        return root
    plex.query = without_m2
    index.refresh()
    assert "m2" not in _by_key(index)


def test_label_plan_and_sync_only_touch_differences(fake):
    plex, index = fake
    index.refresh()
    sync = LabelSync(lambda: plex, index, label="Italian", language="ita", remove_stale=True, workers=1)
    plan = sync.plan()
    # m1 (ita audio) and s1 (ita in some episodes) need it; m2 (english only) has a stale label.
    assert sorted(r["key"] for r in plan["add"]) == ["m1", "s1"]
    assert [r["key"] for r in plan["remove"]] == ["m2"]
    sync.sync(plan)
    assert sorted(plex.edits) == [("add", "m1", "Italian"), ("add", "s1", "Italian"),
                                  ("remove", "m2", "Italian")]


def test_label_min_coverage(fake):
    plex, index = fake
    index.refresh()
    sync = LabelSync(lambda: plex, index, language="ita", criteria="audio", min_coverage=50)
    assert not sync.qualifies(_by_key(index)["s1"])  # 1 of 3 episodes
    assert sync.qualifies(_by_key(index)["m1"])


class IndexedSession(web.PlexSession):
    def __init__(self, index):
        super().__init__("http://plex", "t", connect=lambda *a: "admin", index=index)

    def user_server(self, name):
        return self.admin()


def test_missing_endpoint_and_csv(fake):
    plex, index = fake
    index.refresh()
    client = web.create_app(IndexedSession(index)).test_client()
    d = client.get("/api/missing?user=Admin&kind=audio").json
    keys = {r["key"]: r for r in d["results"]}
    assert set(keys) == {"m2", "s1"}
    assert (keys["s1"]["audio"], keys["s1"]["total"]) == (1, 3)
    d = client.get("/api/missing?user=Admin&kind=both").json
    assert sorted(r["key"] for r in d["results"]) == ["m2", "s1"]
    csv = client.get("/api/missing?user=Admin&kind=either&format=csv")
    assert csv.mimetype == "text/csv" and "T m2" in csv.get_data(as_text=True)


def test_browse_never_waits_for_an_empty_index(tmp_path):
    index = LibraryIndex(lambda: (_ for _ in ()).throw(RuntimeError("slow")),
                         path=str(tmp_path / "none.json"))
    client = web.create_app(IndexedSession(index)).test_client()
    d = client.get("/api/browse/languages?user=Admin&kind=audio").json
    assert d["languages"] == [] and d["ready"] is False


def test_playable_items_reads_episodes_in_batches():
    from types import SimpleNamespace as NS
    from plexlang import plex_client

    class Plex:
        def __init__(self):
            self.batches = []

        def fetchItems(self, keys):
            self.batches.append(len(keys))
            return [NS(ratingKey=k) for k in keys]

        def fetchItem(self, key):
            raise AssertionError("should not fall back to single reads")

    plex = Plex()
    title = NS(type="show", episodes=lambda: [NS(ratingKey=str(i)) for i in range(250)])
    items = plex_client.playable_items(plex, title)
    assert [int(i.ratingKey) for i in items] == list(range(250))
    assert sorted(plex.batches) == [50, 100, 100]
