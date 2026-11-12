"""Ask Sonarr, Radarr or Seerr for an Italian (multilingual) version of titles.

Flow: ``config_report`` (read-only check of each service's setup) ->
``preview`` (what would happen per title, nothing changed) -> ``run`` (in a
background job: optionally switch quality profile, then search / request).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .arr import (ArrError, RadarrClient, SeerrClient, SonarrClient, _language_ids,
                  analyse_profile, pick_profile)


def _norm(value) -> str:
    return " ".join(str(value or "").casefold().split())


def clients_from_env():
    def make(cls, prefix):
        url, key = os.environ.get(f"{prefix}_URL"), os.environ.get(f"{prefix}_API_KEY")
        return cls(url, key) if url and key else None
    return make(SonarrClient, "SONARR"), make(RadarrClient, "RADARR"), make(SeerrClient, "SEERR")


class Acquirer:
    CACHE_SECONDS = 300

    def __init__(self, index, sonarr=None, radarr=None, seerr=None, language: str = "ita"):
        self.index = index
        self.sonarr, self.radarr, self.seerr = sonarr, radarr, seerr
        self.language = language
        self.mode = os.environ.get("ARR_REQUEST_VIA", "auto").lower()  # auto | arr | seerr
        self.wanted_profile = {"sonarr": os.environ.get("SONARR_ITALIAN_PROFILE") or None,
                               "radarr": os.environ.get("RADARR_ITALIAN_PROFILE") or None}
        self.seerr_server = {"sonarr": os.environ.get("SEERR_SONARR_SERVER") or None,
                             "radarr": os.environ.get("SEERR_RADARR_SERVER") or None}
        self.seerr_profile = {"sonarr": os.environ.get("SEERR_SONARR_PROFILE") or None,
                              "radarr": os.environ.get("SEERR_RADARR_PROFILE") or None}
        self._cache = (0.0, None)
        self._lock = threading.Lock()
        self.previews: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="arr-request")

    @property
    def configured(self) -> bool:
        return any((self.sonarr, self.radarr, self.seerr))

    # ----- read configuration ------------------------------------------

    def _arr_report(self, client, kind):
        status = client.status()
        version = str(status.get("version", ""))
        languages = client.languages()
        lang_ids = _language_ids(languages, self.language)
        formats = client.custom_formats()
        profiles = [analyse_profile(p, formats, lang_ids, languages) for p in client.quality_profiles()]
        chosen = pick_profile(profiles, self.wanted_profile[kind])
        report = {"configured": True, "version": version, "profiles": profiles,
                  "italian_language_ids": sorted(lang_ids),
                  "custom_formats": [{"id": f["id"], "name": f.get("name")} for f in formats],
                  "root_folders": [r.get("path") for r in client.root_folders()],
                  "configured_profile": self.wanted_profile[kind],
                  "target_profile": chosen and {"id": chosen["id"], "name": chosen["name"]},
                  "warnings": []}
        if kind == "sonarr" and version.startswith("3."):
            report["language_profiles"] = [{"id": p.get("id"), "name": p.get("name")}
                                           for p in client.language_profiles()]
            report["warnings"].append("Sonarr v3 uses language profiles instead of custom-format "
                                      "languages; this check is limited. Sonarr v4 is recommended.")
        if self.wanted_profile[kind] and (not chosen or str(chosen["id"]) != str(self.wanted_profile[kind])
                                          and _norm(chosen["name"]) != _norm(self.wanted_profile[kind])):
            report["warnings"].append(f"Configured profile '{self.wanted_profile[kind]}' was not found.")
        if not chosen:
            report["warnings"].append("No quality profile is set up to prefer Italian releases. Searches "
                                      "will use each title's current profile and may not pick Italian.")
        return report

    def context(self, force=False) -> dict:
        cached_at, cached = self._cache
        if cached and not force and time.time() - cached_at < self.CACHE_SECONDS:
            return cached
        with self._lock:
            cached_at, cached = self._cache
            if cached and not force and time.time() - cached_at < self.CACHE_SECONDS:
                return cached
            ctx = {"sonarr": {"configured": False}, "radarr": {"configured": False},
                   "seerr": {"configured": False}, "mode": self.mode}
            for kind, client in (("sonarr", self.sonarr), ("radarr", self.radarr)):
                if not client:
                    continue
                try:
                    ctx[kind] = self._arr_report(client, kind)
                    rows = client.series() if kind == "sonarr" else client.movies()
                    ctx[kind]["_lookup"] = _arr_lookup(rows, kind)
                    ctx[kind]["managed"] = len(rows)
                except ArrError as exc:
                    ctx[kind] = {"configured": True, "error": str(exc)}
            if self.seerr:
                try:
                    status = self.seerr.status()
                    ctx["seerr"] = {"configured": True, "version": status.get("version"),
                                    "sonarr_servers": self.seerr.servers("sonarr"),
                                    "radarr_servers": self.seerr.servers("radarr")}
                except ArrError as exc:
                    ctx["seerr"] = {"configured": True, "error": str(exc)}
            self._cache = (time.time(), ctx)
            return ctx

    def config_report(self, force=False) -> dict:
        ctx = self.context(force)
        return {k: ({kk: vv for kk, vv in v.items() if not kk.startswith("_")} if isinstance(v, dict) else v)
                for k, v in ctx.items()}

    # ----- plan --------------------------------------------------------

    def _seerr_target(self, ctx, kind):
        servers = ctx["seerr"].get(f"{kind}_servers") or []
        wanted = self.seerr_server[kind]
        server = next((s for s in servers if wanted and (str(s["id"]) == str(wanted)
                                                         or _norm(s["name"]) == _norm(wanted))), None)
        server = server or next((s for s in servers if s.get("isDefault") and not s.get("is4k")), None)
        if not server:
            return None, None
        wanted_p = self.seerr_profile[kind]
        profile = next((p for p in server["profiles"] if wanted_p and (str(p["id"]) == str(wanted_p)
                                                                      or _norm(p["name"]) == _norm(wanted_p))), None)
        return server, profile

    def plan_title(self, title: dict, ctx: dict, kind: str = "audio") -> dict:
        media = "show" if title["type"] == "show" else "movie"
        service = "sonarr" if media == "show" else "radarr"
        row = {"key": title["key"], "title": title["title"], "year": title.get("year"), "type": media}
        arr = ctx[service]
        match = arr.get("_lookup") and _arr_match(arr["_lookup"], title, service)
        use_seerr = self.mode == "seerr" or (self.mode == "auto" and not match)
        if not use_seerr and match:
            target = arr.get("target_profile")
            current = match.get("qualityProfileId")
            profiles = {p["id"]: p for p in arr.get("profiles", [])}
            row.update(service=service, arr_id=match["id"], current_profile=profiles.get(current, {}).get("name"),
                       target_profile_id=target and target["id"],
                       target_profile=target and target["name"],
                       profile_change=bool(target and target["id"] != current),
                       monitored=match.get("monitored", True))
            effective = profiles.get(target["id"] if target else current, {})
            row["problems"] = list(effective.get("problems", []))
            if not match.get("monitored", True):
                row["problems"].append("Not monitored: the search runs, but an RSS grab won't happen later.")
            if media == "show":
                row["episodes"] = [list(se) for se in self.index.episodes_missing(title["key"], self.language, kind)]
                row["action"] = (f"Search {len(row['episodes'])} episode(s) without Italian"
                                 if row["episodes"] else "Search the whole series")
            else:
                row["action"] = "Search for the movie"
            if row["profile_change"]:
                row["action"] = f"Switch profile to {target['name']}, then " + row["action"][0].lower() + row["action"][1:]
            return row
        if use_seerr and ctx["seerr"].get("configured") and not ctx["seerr"].get("error"):
            ids = title.get("ids") or {}
            tmdb = (ids.get("tmdb") or [None])[0]
            server, profile = self._seerr_target(ctx, service)
            row.update(service="seerr", tmdb_id=tmdb, seerr_server=server and server["name"],
                       seerr_server_id=server and server["id"], seerr_profile=profile and profile["name"],
                       seerr_profile_id=profile and profile["id"], problems=[])
            if not tmdb:
                row["problems"].append("Plex has no TMDB ID for this title, so Seerr can't request it.")
            if not server:
                row["problems"].append(f"No {service.title()} server is set up in Seerr.")
            if match:
                row["problems"].append(f"Already in {service.title()}: Seerr will refuse to re-request it unless "
                                       "the request goes to a different (for example Italian) server.")
            row["action"] = f"Request in Seerr via {row['seerr_server'] or '?'}"
            if not tmdb or not server:
                row.update(service=None, action="Can't request")
            return row
        row.update(service=None, problems=[f"Not found in {service.title()}" +
                                           ("" if ctx["seerr"].get("configured") else " and Seerr isn't configured") + "."],
                   action="Nothing to do")
        return row

    def preview(self, titles: list[dict], kind: str = "audio") -> dict:
        ctx = self.context()
        rows = [self.plan_title(t, ctx, kind) for t in titles]
        preview_id = uuid.uuid4().hex
        self.previews[preview_id] = {"rows": rows, "created_at": time.time()}
        if len(self.previews) > 20:
            for key, _ in sorted(self.previews.items(), key=lambda kv: kv[1]["created_at"])[:-20]:
                self.previews.pop(key, None)
        return {"preview_id": preview_id, "rows": rows,
                "actionable": sum(1 for r in rows if r.get("service")),
                "profile_changes": sum(1 for r in rows if r.get("profile_change"))}

    # ----- run ---------------------------------------------------------

    def start(self, preview_id: str, keys: list[str], change_profiles: bool = True) -> dict:
        preview = self.previews.get(preview_id)
        if not preview:
            raise LookupError("Preview not found or expired. Generate a new preview.")
        wanted = {str(k) for k in keys}
        rows = [r for r in preview["rows"] if r["key"] in wanted and r.get("service")]
        if not rows:
            raise ValueError("Select at least one title that can be requested.")
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"job_id": job_id, "status": "queued", "total": len(rows), "completed": 0,
                             "results": [], "created_at": int(time.time())}
        self._executor.submit(self._run, job_id, rows, change_profiles)
        return {"job_id": job_id, "total": len(rows)}

    def _do(self, row, change_profiles):
        if row["service"] == "seerr":
            body = {"mediaType": "tv" if row["type"] == "show" else "movie", "mediaId": int(row["tmdb_id"])}
            if row.get("seerr_server_id") is not None:
                body["serverId"] = row["seerr_server_id"]
            if row.get("seerr_profile_id") is not None:
                body["profileId"] = row["seerr_profile_id"]
            if row["type"] == "show":
                body["seasons"] = "all"
            self.seerr.request_media(body)
            return "Requested in Seerr"
        client = self.sonarr if row["service"] == "sonarr" else self.radarr
        done = []
        if change_profiles and row.get("profile_change"):
            client.set_profile(row["arr_id"], int(row["target_profile_id"]))
            done.append(f"profile → {row['target_profile']}")
        episode_ids = None
        if row["service"] == "sonarr" and row.get("episodes"):
            wanted = {tuple(se) for se in row["episodes"]}
            episode_ids = [e["id"] for e in client.episodes(row["arr_id"])
                           if (e.get("seasonNumber"), e.get("episodeNumber")) in wanted]
        client.search(row["arr_id"], episode_ids)
        done.append(f"searched {len(episode_ids)} episode(s)" if episode_ids else "search started")
        return ", ".join(done)

    def _run(self, job_id, rows, change_profiles):
        job = self.jobs[job_id]
        job["status"] = "running"
        for row in rows:
            try:
                if row.get("service") == "seerr" and not row.get("tmdb_id"):
                    raise ArrError("No TMDB ID.")
                detail = self._do(row, change_profiles)
                job["results"].append({"key": row["key"], "title": row["title"], "status": "success",
                                       "detail": detail})
            except Exception as exc:
                job["results"].append({"key": row["key"], "title": row["title"], "status": "failed",
                                       "error": f"{exc.__class__.__name__}: {exc}"})
            job["completed"] = len(job["results"])
        job.update(status="completed", finished_at=int(time.time()),
                   succeeded=sum(1 for r in job["results"] if r["status"] == "success"),
                   failed=sum(1 for r in job["results"] if r["status"] == "failed"))
        self._cache = (0.0, None)  # profiles may have changed


def _arr_lookup(rows, service):
    ids, titles = {}, {}
    fields = (("tvdbId", "tvdb"), ("imdbId", "imdb"), ("tmdbId", "tmdb"))
    for row in rows:
        for field, kind in fields:
            if row.get(field):
                ids.setdefault((kind, str(row[field]).lower()), row)
        titles.setdefault((_norm(row.get("title")), str(row.get("year") or "")), []).append(row)
    return {"ids": ids, "titles": titles}


def _arr_match(lookup, title, service):
    for kind in (("tvdb", "imdb", "tmdb") if service == "sonarr" else ("tmdb", "imdb")):
        for value in (title.get("ids") or {}).get(kind, []):
            row = lookup["ids"].get((kind, str(value).lower()))
            if row:
                return row
    rows = lookup["titles"].get((_norm(title.get("title")), str(title.get("year") or "")), [])
    return rows[0] if len(rows) == 1 else None
