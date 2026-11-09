from types import SimpleNamespace as NS

import pytest

from plexlang import plex_client, web
from tests.test_core import series


class FakeSession:
    def __init__(self):
        self.items_ = series()
        for n, ep in enumerate(self.items_, 1):
            ep.type, ep.parentIndex, ep.index, ep.title = "episode", 1, n, f"Ep {n}"
        self.title = NS(title="Test Show", type="show")

    def admin(self):
        return "admin"

    def user_server(self, name):
        if name != "Alice":
            raise LookupError(f"No Home user called '{name}'.")
        return "alice-server"

    def items(self, user, key, refresh=False):
        self.user_server(user)
        return self.title, self.items_


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(plex_client, "home_users", lambda plex: [
        plex_client.HomeUser("Admin", "admin", 1, True), plex_client.HomeUser("Alice", None, 2)])
    monkeypatch.setattr(plex_client, "search", lambda plex, q: [
        NS(ratingKey=42, title="Test Show", year=2020, type="show")])
    session = FakeSession()
    app = web.create_app(session)
    c = app.test_client()
    c.session_obj = session
    return c


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Plex Language Configurator" in r.data


def test_healthz_does_not_touch_plex(client):
    assert client.get("/healthz").data == b"ok"


def test_users_and_search(client):
    assert client.get("/api/users").json == [{"title": "Admin", "admin": True},
                                             {"title": "Alice", "admin": False}]
    assert client.get("/api/search?user=Alice&q=test").json[0]["key"] == "42"


def test_languages_shows_counts(client):
    d = client.get("/api/languages?user=Alice&key=42").json
    assert d["total"] == 3
    ita = next(o for o in d["audio"] if o["code"] == "ita")
    assert (ita["count"], ita["total"]) == (2, 3)


def test_dry_run_changes_nothing_then_apply(client):
    body = {"user": "Alice", "key": "42", "audio": "ita", "subtitles": "off", "dry_run": True}
    d = client.post("/api/apply", json=body).json
    assert d["audio"] == {"matched": 2, "missing": ["S01E03 Ep 3"]}
    assert d["subtitles"]["matched"] == 3 and "updated" not in d
    parts = [ep.media[0].parts[0] for ep in client.session_obj.items_]
    assert all(p.calls == [] for p in parts)

    body["dry_run"] = False
    d = client.post("/api/apply", json=body).json
    assert d["updated"] == 3
    assert parts[2].calls == [("subs", 0)]


def test_unknown_user_is_404(client):
    r = client.get("/api/languages?user=Bob&key=42")
    assert r.status_code == 404 and "Bob" in r.json["error"]


def test_plex_unreachable_gives_clear_error(monkeypatch):
    import requests

    session = web.PlexSession("http://nowhere:32400", "t")

    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(session, "_connect", boom)
    r = web.create_app(session).test_client().get("/api/users")
    assert r.status_code == 502 and "http://nowhere:32400" in r.json["error"]
