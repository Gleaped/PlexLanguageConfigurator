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
from .bazarr_client import BazarrClient, BazarrError

CACHE_SECONDS = 300
BROWSE_CACHE_SECONDS = 86400


def _label(item) -> str:
    if getattr(item, "type", None) == "episode":
        return f"S{item.parentIndex:02}E{item.index:02} {item.title}"
    return item.title


def _plex_external_ids(title) -> dict[str, list[str]]:
    """Extract stable IMDb/TVDB/TMDB identifiers from Plex GUID metadata."""
    import re

    values = []
    for attr in ("guid", "guids"):
        raw = getattr(title, attr, None)
        if callable(raw):
            try:
                raw = raw()
            except Exception:
                raw = None
        if raw:
            values.extend(raw if isinstance(raw, (list, tuple, set)) else [raw])
    found = {"imdb": [], "tvdb": [], "tmdb": []}
    for value in values:
        text = str(getattr(value, "id", value) or "").strip()
        match = re.search(r"(?:^|[/:.])((?:imdb|tvdb|tmdb))://?([^/?#]+)", text, re.I)
        if not match:
            match = re.search(r"(imdb|tvdb|tmdb)[:/]+([^/?#]+)", text, re.I)
        if match:
            kind, identifier = match.group(1).lower(), match.group(2)
            if identifier not in found[kind]:
                found[kind].append(identifier)
    return found


def _normal_title(value) -> str:
    return " ".join(str(value or "").casefold().split())


def _options(options):
    return [{"code": o.code, "name": o.name, "count": o.count, "total": o.total,
             "label": o.label()} for o in options]


class PlexSession:
    """Holds the admin connection and caches per-user connections and loaded titles."""

    def __init__(self, url: str, token: str, connect=plex_client.connect,
                 bazarr: BazarrClient | None = None):
        self.url, self.token, self._connect = url, token, connect
        self.bazarr = bazarr
        self._admin = None
        self._users: dict[str, object] = {}
        self._items: dict[tuple[str, str], tuple[float, object, list]] = {}
        self._browse: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._browse_lock = threading.Lock()

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

    def browse_index(self, user: str, refresh: bool = False) -> dict:
        """Build/cache a per-user index of languages present in every movie/show."""
        hit = self._browse.get(user)
        if hit and not refresh and time.time() - hit[0] < BROWSE_CACHE_SECONDS:
            return hit[1]

        # Prevent simultaneous first-page requests from scanning the same library.
        with self._browse_lock:
            hit = self._browse.get(user)
            if hit and not refresh and time.time() - hit[0] < BROWSE_CACHE_SECONDS:
                return hit[1]

            plex = self.user_server(user)
            titles = []
            seen = set()
            errors = 0
            for section in plex.library.sections():
                if getattr(section, "type", None) not in ("movie", "show"):
                    continue
                for title in section.all():
                    key = str(getattr(title, "ratingKey", ""))
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    try:
                        playable = plex_client.playable_items(plex, title)
                        summary = core.summarize(playable)
                        titles.append({
                            "key": key,
                            "title": title.title,
                            "year": getattr(title, "year", None),
                            "type": title.type,
                            "ids": _plex_external_ids(title),
                            "total": summary.total,
                            "audio": [
                                {"code": o.code, "name": o.name, "count": o.count,
                                 "total": o.total} for o in summary.audio
                            ],
                            "subtitles": [
                                {"code": o.code, "name": o.name, "count": o.count,
                                 "total": o.total} for o in summary.subtitles
                            ],
                        })
                    except Exception:
                        # One unavailable item must not prevent browsing the rest.
                        errors += 1

            built_at = int(time.time())
            index = {"titles": titles, "scanned": len(seen), "indexed": len(titles),
                     "errors": errors, "built_at": built_at,
                     "expires_at": built_at + BROWSE_CACHE_SECONDS,
                     "cache_seconds": BROWSE_CACHE_SECONDS}
            self._browse[user] = (time.time(), index)
            return index


def create_app(session: PlexSession | None = None) -> Flask:
    app = Flask(__name__)
    if session is None:
        url, token = os.environ.get("PLEX_URL"), os.environ.get("PLEX_TOKEN")
        if not url or not token:
            raise SystemExit("Set PLEX_URL and PLEX_TOKEN.")
        bazarr_url = os.environ.get("BAZARR_URL")
        bazarr_key = os.environ.get("BAZARR_API_KEY")
        bazarr = BazarrClient(bazarr_url, bazarr_key) if bazarr_url and bazarr_key else None
        session = PlexSession(url, token, bazarr=bazarr)
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

    @app.get("/api/browse/languages")
    def browse_languages():
        user = request.args["user"]
        kind = request.args.get("kind", "audio")
        if kind not in ("audio", "subtitles"):
            return jsonify(error="kind must be 'audio' or 'subtitles'"), 400
        index = session.browse_index(user, refresh=request.args.get("refresh") == "1")

        aggregate = {}
        for title in index["titles"]:
            for option in title[kind]:
                entry = aggregate.setdefault(option["code"], {
                    "code": option["code"], "name": option["name"],
                    "titles": 0, "items": 0,
                })
                entry["titles"] += 1
                entry["items"] += option["count"]
        languages = sorted(aggregate.values(), key=lambda x: (-x["titles"], x["name"].lower()))
        return jsonify(languages=languages, scanned=index["scanned"],
                       indexed=index["indexed"], errors=index["errors"],
                       built_at=index["built_at"], expires_at=index["expires_at"],
                       cache_seconds=index["cache_seconds"],
                       bazarr_configured=session.bazarr is not None)

    @app.get("/api/browse")
    def browse():
        user = request.args["user"]
        kind = request.args.get("kind", "audio")
        language = request.args.get("language", "").strip().lower()
        media_type = request.args.get("type", "all")
        if kind not in ("audio", "subtitles"):
            return jsonify(error="kind must be 'audio' or 'subtitles'"), 400
        if media_type not in ("all", "movie", "show"):
            return jsonify(error="type must be 'all', 'movie' or 'show'"), 400
        if not language:
            return jsonify(error="language is required"), 400

        index = session.browse_index(user, refresh=request.args.get("refresh") == "1")
        results = []
        for title in index["titles"]:
            if media_type != "all" and title["type"] != media_type:
                continue
            option = next((o for o in title[kind] if o["code"].lower() == language), None)
            if option:
                results.append({
                    "key": title["key"], "title": title["title"],
                    "year": title["year"], "type": title["type"],
                    "count": option["count"], "total": option["total"],
                    "complete": option["count"] == option["total"],
                })
        for result in results:
            result["percentage"] = round((result["count"] / result["total"] * 100), 2) if result["total"] else 0
        sort_by = request.args.get("sort", "title")
        sort_order = request.args.get("order", "asc")
        allowed_sorts = {"title", "episodes", "percentage"}
        if sort_by not in allowed_sorts or sort_order not in ("asc", "desc"):
            return jsonify(error="Invalid sort or order."), 400
        if sort_by in ("episodes", "percentage") and media_type == "movie":
            return jsonify(error="Episode-count and coverage sorting apply to series."), 400
        reverse = sort_order == "desc"
        if sort_by == "episodes":
            results.sort(key=lambda x: (x["total"], x["title"].casefold()), reverse=reverse)
        elif sort_by == "percentage":
            results.sort(key=lambda x: (x["percentage"], x["total"], x["title"].casefold()), reverse=reverse)
        else:
            results.sort(key=lambda x: (x["title"].casefold(), x["year"] or 0), reverse=reverse)
        return jsonify(results=results, scanned=index["scanned"],
                       indexed=index["indexed"], errors=index["errors"],
                       built_at=index["built_at"], expires_at=index["expires_at"],
                       cache_seconds=index["cache_seconds"])

    @app.post("/api/browse/bazarr-search")
    def bazarr_search():
        if session.bazarr is None:
            return jsonify(error="Bazarr is not configured. Set BAZARR_URL and BAZARR_API_KEY."), 503
        body = request.get_json(force=True)
        user = str(body.get("user") or "").strip()
        language = str(body.get("language") or "").strip().lower()
        kind = body.get("kind", "audio")
        media_type = body.get("type", "all")
        if kind != "audio":
            return jsonify(error="Bazarr bulk search must be scoped by an audio language."), 400
        if not user or not language:
            return jsonify(error="user and audio language are required."), 400
        if media_type not in ("all", "movie", "show"):
            return jsonify(error="type must be all, movie or show."), 400

        index = session.browse_index(user)
        selected = []
        for title in index["titles"]:
            if media_type != "all" and title["type"] != media_type:
                continue
            if any(o["code"].lower() == language for o in title["audio"]):
                selected.append(title)
        if not selected:
            return jsonify(queued=0, matched=0, unmatched=[], failures=[],
                           message="No titles in the cached Plex index have this audio language.")

        try:
            bazarr_series, bazarr_movies = session.bazarr.catalog()
        except BazarrError as exc:
            return jsonify(error=str(exc)), 502

        def id_set(row, *keys):
            return {str(row.get(k)).strip().lower() for k in keys
                    if row.get(k) not in (None, "", 0)}

        by_external = {"show": {}, "movie": {}}
        by_title = {"show": {}, "movie": {}}
        for media, rows, id_fields, id_field in (
            ("show", bazarr_series, ("imdbId", "tvdbId"), "sonarrSeriesId"),
            ("movie", bazarr_movies, ("imdbId",), "radarrId"),
        ):
            for row in rows:
                for identifier in id_set(row, *id_fields):
                    by_external[media].setdefault(identifier, row.get(id_field))
                title_key = (_normal_title(row.get("title")), str(row.get("year") or ""))
                if title_key[0]:
                    by_title[media].setdefault(title_key, []).append(row.get(id_field))

        queued, unmatched, failures = [], [], []
        for title in selected:
            media = title["type"]
            ids = title.get("ids") or {}
            candidates = [str(x).lower() for key in (("imdb", "tvdb") if media == "show" else ("imdb",))
                          for x in ids.get(key, [])]
            bazarr_id = next((by_external[media][x] for x in candidates
                              if x in by_external[media] and by_external[media][x]), None)
            if not bazarr_id:
                title_key = (_normal_title(title.get("title")), str(title.get("year") or ""))
                exact_matches = [x for x in by_title[media].get(title_key, []) if x]
                if len(exact_matches) == 1:
                    bazarr_id = exact_matches[0]
            if not bazarr_id:
                unmatched.append({"title": title["title"], "type": media,
                                  "reason": "No unique Bazarr match by external ID or exact title/year."})
                continue
            try:
                session.bazarr.search_missing(media, int(bazarr_id))
                queued.append({"title": title["title"], "type": media})
            except (BazarrError, ValueError) as exc:
                failures.append({"title": title["title"], "error": str(exc)})

        return jsonify(queued=len(queued), matched=len(selected), results=queued,
                       unmatched=unmatched, failures=failures,
                       audio_language=language,
                       note="Bazarr searches using the language profile already assigned to each title.")

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
