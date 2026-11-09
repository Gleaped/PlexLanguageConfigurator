"""Small web UI: pick a Home user, a series or movie, and languages."""

from __future__ import annotations

import os
import threading
import time
from importlib import resources

import requests
from flask import Flask, jsonify, request
from plexapi.exceptions import Unauthorized

from . import core, plex_client

CACHE_SECONDS = 300


def _label(item) -> str:
    if getattr(item, "type", None) == "episode":
        return f"S{item.parentIndex:02}E{item.index:02} {item.title}"
    return item.title


def _options(options):
    return [{"code": o.code, "name": o.name, "count": o.count, "total": o.total,
             "label": o.label()} for o in options]


class PlexSession:
    """Holds the admin connection and caches per-user connections and loaded titles."""

    def __init__(self, url: str, token: str, connect=plex_client.connect):
        self.url, self.token, self._connect = url, token, connect
        self._admin = None
        self._users: dict[str, object] = {}
        self._items: dict[tuple[str, str], tuple[float, object, list]] = {}
        self._lock = threading.Lock()

    def admin(self):
        if self._admin is None:
            self._admin = self._connect(self.url, self.token)
        return self._admin

    def user_server(self, name: str):
        with self._lock:
            if name not in self._users:
                user = plex_client.find_user(self.admin(), name)
                self._users[name] = plex_client.as_user(self.admin(), user)
            return self._users[name]

    def items(self, user: str, key: str, refresh: bool = False):
        cache_key = (user, key)
        hit = self._items.get(cache_key)
        if hit and not refresh and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1], hit[2]
        plex = self.user_server(user)
        title = plex.fetchItem(int(key))
        items = plex_client.playable_items(plex, title)
        self._items[cache_key] = (time.time(), title, items)
        return title, items


def create_app(session: PlexSession | None = None) -> Flask:
    app = Flask(__name__)
    if session is None:
        url, token = os.environ.get("PLEX_URL"), os.environ.get("PLEX_TOKEN")
        if not url or not token:
            raise SystemExit("Set PLEX_URL and PLEX_TOKEN.")
        session = PlexSession(url, token)
    app.config["plex"] = session

    @app.errorhandler(LookupError)
    def not_found(e):
        return jsonify(error=str(e)), 404

    @app.errorhandler(requests.exceptions.ConnectionError)
    def unreachable(e):
        return jsonify(error=f"Can't reach Plex at {session.url}. Check PLEX_URL. ({e.__class__.__name__})"), 502

    @app.errorhandler(Unauthorized)
    def unauthorized(e):
        return jsonify(error="Plex rejected the token. Check PLEX_TOKEN is the server owner's token."), 502

    @app.errorhandler(Exception)
    def failed(e):
        code = getattr(e, "code", 500)
        if not isinstance(code, int):
            code = 500
        return jsonify(error=str(e) or e.__class__.__name__), code

    @app.get("/")
    def index():
        return resources.files("plexlang").joinpath("static/index.html").read_text(encoding="utf-8")

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.get("/api/users")
    def users():
        return jsonify([{"title": u.title, "admin": u.is_admin}
                        for u in plex_client.home_users(session.admin())])

    @app.get("/api/search")
    def search():
        user, q = request.args["user"], request.args.get("q", "").strip()
        if not q:
            return jsonify([])
        results = plex_client.search(session.user_server(user), q)
        return jsonify([{"key": str(r.ratingKey), "title": r.title, "year": getattr(r, "year", None),
                         "type": r.type} for r in results])

    @app.get("/api/languages")
    def languages():
        title, items = session.items(request.args["user"], request.args["key"],
                                     refresh=request.args.get("refresh") == "1")
        s = core.summarize(items)
        return jsonify(title=title.title, type=title.type, total=s.total,
                       audio=_options(s.audio), subtitles=_options(s.subtitles))

    @app.post("/api/apply")
    def apply():
        body = request.get_json(force=True)
        title, items = session.items(body["user"], body["key"])
        plan = core.build_plan(items, audio=body.get("audio") or None,
                               subtitles=body.get("subtitles") or None)
        result = {
            "total": plan.total,
            "audio": {"matched": plan.audio_matched,
                      "missing": [_label(i) for i in plan.audio_missing]} if plan.audio else None,
            "subtitles": {"matched": plan.subtitles_matched,
                          "missing": [_label(i) for i in plan.subtitles_missing]} if plan.subtitles else None,
            "files": len(plan.changes),
            "dry_run": bool(body.get("dry_run")),
        }
        if not result["dry_run"]:
            result["updated"] = core.apply_plan(plan)
        return jsonify(result)

    return app


def serve(host: str = "0.0.0.0", port: int = 8080):
    from waitress import serve as waitress_serve

    print(f"Plex Language Configurator on http://{host}:{port}")
    waitress_serve(create_app(), host=host, port=port, threads=8)
