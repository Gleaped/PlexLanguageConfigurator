"""Small web UI: pick a Home user, a series or movie, and languages."""

from __future__ import annotations

import csv
import io
import os
import re
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib import resources

import requests
from flask import Flask, Response, jsonify, request
from plexapi.exceptions import Unauthorized

from . import core, plex_client
from .bazarr_client import BazarrClient, BazarrError
from .profile_mappings import ProfileMappingStore
from .bazarr_profiles import suggest_profiles
from .index import LibraryIndex, coverage, language_aliases, visible_keys
from .labels import LabelSync, _env_bool

CACHE_SECONDS = 300
VISIBILITY_SECONDS = 1800  # how long a Home user's visible-title list is reused
FOCUS_NAMES = {"ita": "Italian", "eng": "English", "spa": "Spanish", "fra": "French",
               "deu": "German", "por": "Portuguese", "nld": "Dutch", "jpn": "Japanese"}


def _label(item) -> str:
    if getattr(item, "type", None) == "episode":
        return f"S{item.parentIndex:02}E{item.index:02} {item.title}"
    return item.title


def _normal_title(value) -> str:
    return " ".join(str(value or "").casefold().split())


def _record_bazarr_job_event(session, job_id: str, event: str, **details):
    """Append a non-secret job audit event to the persistent data directory."""
    record = {"timestamp": int(time.time()), "job_id": job_id, "event": event, **details}
    try:
        with session._job_log_lock:
            os.makedirs(os.path.dirname(session._job_log_path), exist_ok=True)
            if os.path.exists(session._job_log_path) and os.path.getsize(session._job_log_path) > 2_000_000:
                rotated = session._job_log_path + ".1"
                try:
                    if os.path.exists(rotated):
                        os.remove(rotated)
                    os.replace(session._job_log_path, rotated)
                except OSError:
                    pass
            with open(session._job_log_path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Audit logging must never prevent a subtitle operation from completing.
        pass


def _bazarr_lookup(rows, media):
    """Index Bazarr series/movies by external ID and by (title, year)."""
    id_pairs = (("imdbId", "imdb"), ("tvdbId", "tvdb")) if media == "show" else (("imdbId", "imdb"), ("tmdbId", "tmdb"))
    id_field = "sonarrSeriesId" if media == "show" else "radarrId"
    by_id, by_title = {}, {}
    for row in rows:
        if not row.get(id_field):
            continue
        for field, kind in id_pairs:
            if row.get(field):
                by_id.setdefault((kind, str(row.get(field)).strip().lower()), row)
        by_title.setdefault((_normal_title(row.get("title")), str(row.get("year") or "")), []).append(row)
    return {"ids": by_id, "titles": by_title}


def _wanted_lookup(rows, media):
    """{bazarr id: {language: missing count}} from Bazarr's wanted list."""
    id_keys = (("radarrId", "radarrid", "movieId", "movie_id") if media == "movie"
               else ("sonarrSeriesId", "seriesId", "seriesid", "sonarr_series_id"))
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        lang = str(row.get("language") or row.get("languageName") or row.get("code2") or "Unknown language")
        for row_id in {str(row.get(key)) for key in id_keys if row.get(key) not in (None, "")}:
            counts = out.setdefault(row_id, {})
            counts[lang] = counts.get(lang, 0) + 1
    return out


def _options(options):
    return [{"code": o.code, "name": o.name, "count": o.count, "total": o.total,
             "label": o.label()} for o in options]


class PlexSession:
    """Holds the admin connection and caches per-user connections and loaded titles."""

    def __init__(self, url: str, token: str, connect=plex_client.connect,
                 bazarr: BazarrClient | None = None,
                 profile_mappings: ProfileMappingStore | None = None,
                 index: LibraryIndex | None = None):
        self.url, self.token, self._connect = url, token, connect
        self.focus_language = os.environ.get("FOCUS_LANGUAGE", "ita").strip().lower() or "ita"
        self.auto_label = _env_bool("AUTO_LABEL", False)
        self.index = index or LibraryIndex(self.admin, after_refresh=self._after_index_refresh)
        self.labels = LabelSync(self.admin, self.index, language=self.focus_language)
        self._visible: dict[str, tuple[float, set | None]] = {}
        self._visible_refreshing: set[str] = set()
        self.bazarr = bazarr
        self.profile_mappings = profile_mappings or ProfileMappingStore()
        self._admin = None
        self._users: dict[str, object] = {}
        self._items: dict[tuple[str, str], tuple[float, object, list]] = {}
        self._browse: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._browse_lock = threading.Lock()
        self._bazarr_lock = threading.Lock()
        self._bazarr_status_cache = (0.0, None)
        self._bazarr_previews: dict[str, dict] = {}
        self._bazarr_jobs: dict[str, dict] = {}
        self._bazarr_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bazarr-search")
        self._job_log_lock = threading.Lock()
        self._job_log_path = os.path.join(os.environ.get("PLEXLANG_DATA_DIR", "/data"),
                                          "bazarr-job-history.jsonl")

    def admin(self):
        if self._admin is None:
            self._admin = self._connect(self.url, self.token)
        return self._admin

    def start_background(self):
        """Serve the saved index right away and keep it fresh in the background."""
        self.index.start()

    def _after_index_refresh(self):
        if self.auto_label:
            self.labels.sync()

    def visible(self, user: str) -> set | None:
        """Rating keys this Home user can see (None = everything, for the admin).

        Served from cache; a stale entry is returned at once and refreshed in
        the background, so only a user's very first visit waits on Plex.
        """
        hit = self._visible.get(user)
        if hit and time.time() - hit[0] < VISIBILITY_SECONDS:
            return hit[1]
        if hit:
            if user not in self._visible_refreshing:
                self._visible_refreshing.add(user)
                threading.Thread(target=self._load_visible, args=(user,), daemon=True).start()
            return hit[1]
        return self._load_visible(user)

    def _load_visible(self, user: str):
        try:
            plex = self.user_server(user)
            keys = None if plex is self.admin() else visible_keys(plex)
            self._visible[user] = (time.time(), keys)
            return keys
        finally:
            self._visible_refreshing.discard(user)

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
        """The library index as this user sees it. Never waits for a rescan."""
        if refresh:
            self.index.request_refresh(full=True)
        allowed = self.visible(user)
        titles = [t for t in self.index.titles() if allowed is None or t["key"] in allowed]
        info = self.index.info()
        return {"titles": titles, "scanned": len(titles), "indexed": len(titles),
                "errors": info["errors"], "built_at": info["built_at"],
                "expires_at": info["next_refresh_at"], "cache_seconds": info["refresh_seconds"],
                "ready": info["ready"], "index": info}


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
        session.start_background()
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
                       ready=index.get("ready", True), index=index.get("index"),
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
                       cache_seconds=index["cache_seconds"],
                       ready=index.get("ready", True), index=index.get("index"))

    @app.get("/api/bazarr/profiles")
    def bazarr_profiles():
        if session.bazarr is None:
            return jsonify(error="Bazarr is not configured."), 503
        try:
            profiles = session.bazarr.language_profiles()
            mappings = session.profile_mappings.load()
        except (BazarrError, ValueError) as exc:
            return jsonify(error=str(exc)), 502
        return jsonify(
            profiles=[{"id": str(p.get("profileId")), "name": p.get("name", "Unnamed profile")}
                      for p in profiles if p.get("profileId") is not None],
            mappings=mappings,
            storage_path=str(session.profile_mappings.path),
        )

    @app.put("/api/bazarr/profile-mappings")
    def save_bazarr_profile_mappings():
        if session.bazarr is None:
            return jsonify(error="Bazarr is not configured."), 503
        body = request.get_json(force=True) or {}
        mappings = body.get("mappings")
        if not isinstance(mappings, dict):
            return jsonify(error="mappings must be an object of language-code to profile-ID pairs."), 400
        try:
            profiles = session.bazarr.language_profiles()
            valid_ids = {str(p.get("profileId")) for p in profiles if p.get("profileId") is not None}
            for language, profile_id in mappings.items():
                if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2,3}", language):
                    return jsonify(error=f"Invalid audio language code: {language!r}."), 400
                if str(profile_id) not in valid_ids:
                    return jsonify(error=f"Unknown Bazarr profile ID {profile_id!r} for {language}.",
                                   profiles=[{"id": str(p.get("profileId")), "name": p.get("name")}
                                             for p in profiles]), 400
            session.profile_mappings.save(mappings)
        except (BazarrError, ValueError) as exc:
            return jsonify(error=str(exc)), 502
        return jsonify(saved=True, mappings=session.profile_mappings.load(),
                       storage_path=str(session.profile_mappings.path))

    def _bazarr_context(force=False):
        """Fetch/cache Bazarr catalog, wanted subtitles and profile metadata."""
        if session.bazarr is None:
            raise BazarrError("Bazarr is not configured.")
        cached_at, cached = session._bazarr_status_cache
        if cached is not None and not force and time.time() - cached_at < 300:
            return cached
        with session._bazarr_lock:
            cached_at, cached = session._bazarr_status_cache
            if cached is not None and not force and time.time() - cached_at < 300:
                return cached
            series, movies = session.bazarr.catalog()
            wanted_movies = session.bazarr.wanted_movies()
            wanted_episodes = session.bazarr.wanted_episodes()
            profiles = session.bazarr.language_profiles()
            languages = session.bazarr.languages()
            profile_names = {str(row.get("profileId")): row.get("name", "Unnamed profile")
                             for row in profiles if row.get("profileId") is not None}
            language_names = {}
            language_aliases = {}
            for row in languages:
                code2 = str(row.get("code2") or row.get("code") or "").lower()
                code3 = str(row.get("code3") or "").lower()
                name = str(row.get("name") or row.get("language") or "").strip()
                aliases = {x for x in (code2, code3, name.casefold()) if x}
                if code2 and name:
                    language_names[code2] = name
                if code3 and name:
                    language_names[code3] = name
                for alias in aliases:
                    language_aliases[alias] = aliases
            data = {"series": series, "movies": movies,
                    "wanted_movies": wanted_movies, "wanted_episodes": wanted_episodes,
                    "profiles": profiles, "profile_names": profile_names,
                    "language_names": language_names, "language_aliases": language_aliases,
                    "lookup": {"show": _bazarr_lookup(series, "show"),
                               "movie": _bazarr_lookup(movies, "movie")},
                    "wanted": {"show": _wanted_lookup(wanted_episodes, "show"),
                               "movie": _wanted_lookup(wanted_movies, "movie")}}
            session._bazarr_status_cache = (time.time(), data)
            return data

    def _match_bazarr_title(title, context):
        """Match by external ID first, then by a unique exact title + year."""
        lookup = context["lookup"]["show" if title["type"] == "show" else "movie"]
        ids = title.get("ids") or {}
        for kind in ("imdb", "tvdb", "tmdb"):
            for value in ids.get(kind, []):
                row = lookup["ids"].get((kind, str(value).strip().lower()))
                if row:
                    return row
        rows = lookup["titles"].get((_normal_title(title.get("title")), str(title.get("year") or "")), [])
        return rows[0] if len(rows) == 1 else None

    def _wanted_status(title, context, bazarr_row):
        """Conservative status: missing is explicit; absence is reported as unknown."""
        if not bazarr_row:
            return {"state": "unmatched", "label": "Not in Bazarr", "missing": []}
        media = "movie" if title["type"] == "movie" else "show"
        bazarr_id = str(bazarr_row.get("radarrId" if media == "movie" else "sonarrSeriesId") or "")
        missing_counts = dict(context["wanted"][media].get(bazarr_id, {}))
        if missing_counts:
            missing = sorted(missing_counts)
            return {"state": "missing", "label": "Missing wanted subtitles", "missing": missing,
                    "missing_counts": missing_counts, "missing_items": sum(missing_counts.values())}
        return {"state": "not-listed", "label": "No wanted entries (not verified)", "missing": [],
                "missing_counts": {}, "missing_items": 0,
                "note": "Bazarr wanted-list has no missing entries; this is not a full on-disk verification."}

    def _build_bazarr_preview(user, language, media_type, selection="audio"):
        index = session.browse_index(user)
        if selection == "missing-subtitles":
            # Titles where some or all items lack subtitles in this language.
            aliases = language_aliases(language)
            selected = [title for title in index["titles"]
                        if (media_type == "all" or title["type"] == media_type)
                        and title["total"] and coverage(title, "subtitles", aliases) < title["total"]]
        else:
            selected = [title for title in index["titles"]
                        if (media_type == "all" or title["type"] == media_type)
                        and any(o["code"].lower() == language for o in title["audio"])]
        context = _bazarr_context()
        mappings = session.profile_mappings.load()
        mapped_profile = mappings.get(language)
        profile_names = context["profile_names"]
        if mapped_profile and mapped_profile not in profile_names:
            raise ValueError(f"The saved Bazarr profile mapping for {language} no longer exists.")
        profile_name = profile_names.get(mapped_profile) if mapped_profile else None
        candidates, unmatched = [], []
        for title in selected:
            row = _match_bazarr_title(title, context)
            if not row:
                unmatched.append({"key": title["key"], "title": title["title"], "type": title["type"],
                                  "reason": "No unique Bazarr match by external ID or exact title/year."})
                continue
            bazarr_id = row.get("sonarrSeriesId" if title["type"] == "show" else "radarrId")
            current_profile = str(row.get("profileId") or "")
            changed = bool(mapped_profile and current_profile != mapped_profile)
            status = _wanted_status(title, context, row)
            candidates.append({
                "key": title["key"], "title": title["title"], "year": title["year"],
                "type": title["type"], "bazarr_id": int(bazarr_id),
                "current_profile_id": current_profile,
                "current_profile_name": profile_names.get(current_profile, "Unknown / unset"),
                "target_profile_id": mapped_profile,
                "target_profile_name": profile_name,
                "profile_changed": changed,
                "subtitle_status": status,
            })
        return {"candidates": candidates, "unmatched": unmatched,
                "selected_count": len(selected), "mapped_profile_id": mapped_profile,
                "mapped_profile_name": profile_name}

    @app.get("/api/bazarr/profile-suggestions")
    def bazarr_profile_suggestions():
        if session.bazarr is None:
            return jsonify(error="Bazarr is not configured."), 503
        language = request.args.get("language", "").strip().lower()
        if not language:
            return jsonify(error="language is required"), 400
        try:
            profiles = session.bazarr.language_profiles()
            languages = session.bazarr.languages()
        except BazarrError as exc:
            return jsonify(error=str(exc)), 502
        language_name = language
        aliases = {language}
        for row in languages:
            row_aliases = {str(row.get(key) or "").strip().casefold()
                           for key in ("code2", "code3", "code") if row.get(key)}
            name = str(row.get("name") or row.get("language") or "").strip()
            if language in row_aliases:
                language_name = name or language
                aliases.update(row_aliases)
                if name:
                    aliases.add(name.casefold())
                break
        suggestions = suggest_profiles(profiles, language, language_name, aliases)
        return jsonify(language=language, language_name=language_name,
                       suggestions=suggestions,
                       note="Suggestions inspect Bazarr profile language definitions. Review forced, hearing-impaired, exclude-audio and cutoff rules in Bazarr before assigning a profile.")

    @app.post("/api/browse/bazarr-preview")
    def bazarr_preview():
        if session.bazarr is None:
            return jsonify(error="Bazarr is not configured."), 503
        body = request.get_json(force=True) or {}
        user = str(body.get("user") or "").strip()
        language = str(body.get("language") or "").strip().lower()
        media_type = body.get("type", "all")
        selection = body.get("selection", "audio")
        if not user or not language:
            return jsonify(error="user and audio language are required."), 400
        if media_type not in ("all", "movie", "show"):
            return jsonify(error="type must be all, movie or show."), 400
        if selection not in ("audio", "missing-subtitles"):
            return jsonify(error="selection must be audio or missing-subtitles."), 400
        try:
            plan = _build_bazarr_preview(user, language, media_type, selection)
        except (BazarrError, ValueError) as exc:
            return jsonify(error=str(exc)), 502
        preview_id = uuid.uuid4().hex
        session._bazarr_previews[preview_id] = {**plan, "language": language, "user": user,
                                                 "created_at": time.time()}
        # Bound memory use: retain only the newest 20 previews.
        if len(session._bazarr_previews) > 20:
            oldest = sorted(session._bazarr_previews.items(), key=lambda item: item[1]["created_at"])
            for key, _ in oldest[:-20]:
                session._bazarr_previews.pop(key, None)
        return jsonify(preview_id=preview_id, language=language,
                       selected_count=plan["selected_count"],
                       matched_count=len(plan["candidates"]),
                       unmatched=plan["unmatched"], candidates=plan["candidates"],
                       profile_changes=sum(1 for c in plan["candidates"] if c["profile_changed"]),
                       global_profile_warning=bool(plan["mapped_profile_id"]))

    def _run_bazarr_job(job_id, candidates, language):
        job = session._bazarr_jobs[job_id]
        job.update(status="running", started_at=int(time.time()))
        _record_bazarr_job_event(session, job_id, "started", total=len(candidates), language=language)
        results = []
        for candidate in candidates:
            if job.get("cancel_requested"):
                completed_keys = {row.get("key") for row in results}
                for pending in candidates:
                    if pending["key"] not in completed_keys:
                        results.append({"key": pending["key"], "title": pending["title"],
                                        "type": pending["type"], "status": "cancelled",
                                        "error": "Not started because the job was cancelled."})
                job.update(status="cancelled", finished_at=int(time.time()), results=results,
                           completed=len(results), cancelled=sum(1 for r in results if r["status"] == "cancelled"),
                           failed=sum(1 for r in results if r["status"] == "failed"))
                _record_bazarr_job_event(session, job_id, "cancelled", completed=len(results), total=len(candidates))
                return
            title_label = candidate["title"]
            last_error = None
            for attempt in range(1, 3):
                try:
                    if candidate["profile_changed"]:
                        session.bazarr.assign_profile(candidate["type"], candidate["bazarr_id"],
                                                      int(candidate["target_profile_id"]))
                    session.bazarr.search_missing(candidate["type"], candidate["bazarr_id"])
                    results.append({"key": candidate["key"], "title": title_label,
                                    "type": candidate["type"], "status": "success",
                                    "profile_changed": candidate["profile_changed"],
                                    "attempts": attempt})
                    last_error = None
                    break
                except Exception as exc:
                    last_error = f"{exc.__class__.__name__}: {exc}"
                    if attempt < 2:
                        time.sleep(1)
            if last_error:
                results.append({"key": candidate["key"], "title": title_label,
                                "type": candidate["type"], "status": "failed",
                                "error": last_error, "attempts": 2})
            job.update(completed=len(results), total=len(candidates), results=list(results))
            _record_bazarr_job_event(session, job_id, results[-1]["status"],
                                     title=title_label, media_type=candidate["type"],
                                     attempts=results[-1].get("attempts"), error=results[-1].get("error"))
        job.update(status="completed", finished_at=int(time.time()), results=results,
                   succeeded=sum(1 for r in results if r["status"] == "success"),
                   failed=sum(1 for r in results if r["status"] == "failed"))
        _record_bazarr_job_event(session, job_id, "completed", succeeded=job["succeeded"], failed=job["failed"])

    @app.post("/api/browse/bazarr-jobs")
    def start_bazarr_job():
        if session.bazarr is None:
            return jsonify(error="Bazarr is not configured."), 503
        body = request.get_json(force=True) or {}
        preview_id = str(body.get("preview_id") or "")
        selected_keys = body.get("selected_keys")
        preview = session._bazarr_previews.get(preview_id)
        if not preview:
            return jsonify(error="Preview not found or expired. Generate a new preview."), 404
        if not isinstance(selected_keys, list) or not selected_keys:
            return jsonify(error="Select at least one title to process."), 400
        allowed = {c["key"] for c in preview["candidates"]}
        if any(str(key) not in allowed for key in selected_keys):
            return jsonify(error="Selection contains titles outside this preview."), 400
        chosen = [c for c in preview["candidates"] if c["key"] in {str(k) for k in selected_keys}]
        job_id = uuid.uuid4().hex
        session._bazarr_jobs[job_id] = {"job_id": job_id, "status": "queued", "completed": 0,
                                        "total": len(chosen), "results": [], "created_at": int(time.time()),
                                        "candidates": chosen, "cancel_requested": False,
                                        "language": preview["language"]}
        session._bazarr_executor.submit(_run_bazarr_job, job_id, chosen, preview["language"])
        return jsonify(job_id=job_id, status="queued", total=len(chosen)), 202

    @app.get("/api/bazarr/jobs/<job_id>")
    def bazarr_job_status(job_id):
        job = session._bazarr_jobs.get(job_id)
        if not job:
            return jsonify(error="Job not found."), 404
        public_job = {key: value for key, value in job.items() if key != "candidates"}
        return jsonify(public_job)

    @app.post("/api/bazarr/jobs/<job_id>/cancel")
    def cancel_bazarr_job(job_id):
        job = session._bazarr_jobs.get(job_id)
        if not job:
            return jsonify(error="Job not found."), 404
        if job.get("status") in ("completed", "failed", "cancelled"):
            return jsonify(error=f"Job is already {job.get('status')}"), 409
        job["cancel_requested"] = True
        return jsonify(job_id=job_id, cancel_requested=True)

    @app.post("/api/bazarr/jobs/<job_id>/retry")
    def retry_bazarr_job(job_id):
        previous = session._bazarr_jobs.get(job_id)
        if not previous:
            return jsonify(error="Job not found."), 404
        if previous.get("status") not in ("completed", "cancelled"):
            return jsonify(error="Only completed or cancelled jobs can be retried."), 409
        failed_keys = {row.get("key") for row in previous.get("results", [])
                       if row.get("status") in ("failed", "cancelled")}
        candidates = [row for row in previous.get("candidates", []) if row.get("key") in failed_keys]
        if not candidates:
            return jsonify(error="There are no failed titles to retry."), 400
        new_id = uuid.uuid4().hex
        session._bazarr_jobs[new_id] = {"job_id": new_id, "status": "queued", "completed": 0,
                                        "total": len(candidates), "results": [], "created_at": int(time.time()),
                                        "candidates": candidates, "cancel_requested": False,
                                        "retry_of": job_id, "language": previous.get("language", "")}
        session._bazarr_executor.submit(_run_bazarr_job, new_id, candidates, previous.get("language", ""))
        return jsonify(job_id=new_id, status="queued", total=len(candidates)), 202

    @app.get("/api/bazarr/jobs/history")
    def bazarr_job_history():
        try:
            if not os.path.exists(session._job_log_path):
                return jsonify(events=[], storage_path=session._job_log_path)
            with session._job_log_lock:
                with open(session._job_log_path, "r", encoding="utf-8") as stream:
                    lines = stream.readlines()[-200:]
            events = []
            for line in lines:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return jsonify(events=events, storage_path=session._job_log_path)
        except OSError as exc:
            return jsonify(error=f"Could not read job history: {exc}"), 500

    @app.get("/api/bazarr/status")
    def bazarr_status():
        if session.bazarr is None:
            return jsonify(error="Bazarr is not configured."), 503
        try:
            context = _bazarr_context()
            index = session.browse_index(request.args["user"])
            output = {}
            for title in index["titles"]:
                row = _match_bazarr_title(title, context)
                output[title["key"]] = _wanted_status(title, context, row)
            return jsonify(status=output, cached_seconds=300)
        except BazarrError as exc:
            return jsonify(error=str(exc)), 502

    @app.post("/api/browse/bazarr-search")
    def bazarr_search_legacy():
        return jsonify(error="This endpoint has been replaced by preview + selective background jobs.",
                       replacement="POST /api/browse/bazarr-preview then POST /api/browse/bazarr-jobs"), 410

    @app.get("/api/config")
    def config():
        code = session.focus_language
        return jsonify(focus_language=code, focus_name=FOCUS_NAMES.get(code, code),
                       bazarr_configured=session.bazarr is not None,
                       auto_label=session.auto_label, labels=session.labels.config())

    @app.get("/api/index/status")
    def index_status():
        return jsonify(index=session.index.info(), labels=session.labels.status)

    @app.post("/api/index/refresh")
    def index_refresh():
        full = (request.get_json(silent=True) or {}).get("full", True)
        session.index.request_refresh(full=bool(full))
        return jsonify(queued=True, full=bool(full)), 202

    def _missing(user, language, kind, media_type):
        aliases = language_aliases(language)
        index = session.browse_index(user)
        rows = []
        for title in index["titles"]:
            total = title["total"]
            if not total or (media_type != "all" and title["type"] != media_type):
                continue
            audio = coverage(title, "audio", aliases)
            subs = coverage(title, "subtitles", aliases)
            lacking = {"audio": audio < total, "subtitles": subs < total,
                       "either": audio < total or subs < total,
                       "both": audio < total and subs < total}[kind]
            if not lacking:
                continue
            rows.append({"key": title["key"], "title": title["title"], "year": title["year"],
                         "type": title["type"], "total": total,
                         "audio": audio, "subtitles": subs,
                         "audio_missing": total - audio, "subtitles_missing": total - subs,
                         "ids": title.get("ids") or {}})
        return rows, index

    @app.get("/api/missing")
    def missing():
        """Shows/movies lacking a language entirely or in some episodes."""
        user = request.args["user"]
        language = (request.args.get("language") or session.focus_language).strip().lower()
        kind = request.args.get("kind", "either")
        media_type = request.args.get("type", "all")
        if kind not in ("audio", "subtitles", "either", "both"):
            return jsonify(error="kind must be audio, subtitles, either or both"), 400
        if media_type not in ("all", "movie", "show"):
            return jsonify(error="type must be 'all', 'movie' or 'show'"), 400
        rows, index = _missing(user, language, kind, media_type)
        sort = request.args.get("sort", "missing")
        if sort == "title":
            rows.sort(key=lambda r: (str(r["title"]).casefold(), r["year"] or 0))
        else:
            rows.sort(key=lambda r: (-(r["audio_missing"] + r["subtitles_missing"]),
                                     str(r["title"]).casefold()))
        if request.args.get("format") == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["title", "year", "type", "items", f"{language}_audio", f"{language}_subtitles",
                             "imdb", "tvdb", "tmdb", "plex_key"])
            for r in rows:
                writer.writerow([r["title"], r["year"] or "", r["type"], r["total"], r["audio"],
                                 r["subtitles"], ",".join(r["ids"].get("imdb", [])),
                                 ",".join(r["ids"].get("tvdb", [])), ",".join(r["ids"].get("tmdb", [])),
                                 r["key"]])
            return Response(buf.getvalue(), mimetype="text/csv", headers={
                "Content-Disposition": f'attachment; filename="missing-{language}.csv"'})
        return jsonify(results=rows, language=language, kind=kind,
                       ready=index["ready"], built_at=index["built_at"], index=index["index"])

    @app.get("/api/labels/plan")
    def labels_plan():
        if not session.index.ready:
            return jsonify(error="The library index is still being built."), 409
        return jsonify(session.labels.plan())

    @app.post("/api/labels/sync")
    def labels_sync():
        if not session.index.ready:
            return jsonify(error="The library index is still being built."), 409
        session.labels.sync_in_background()
        return jsonify(queued=True), 202

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
