"""Keep one Plex label (default "Italian") on every show/movie with that language.

Why a single show/movie-level label: Plex stores labels as tags with an indexed
join, so ``label=Italian`` is a fast filter in Plex Web, the apps and smart
collections. Labelling episodes instead would not be filterable from the
library view, and per-language-and-kind labels would split the search.

Labels are server-wide (all users see them). Only differences are written, so
after the first sync a run usually touches nothing.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .index import coverage, language_aliases, query_all

log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class LabelSync:
    def __init__(self, connect, index, *, label: str | None = None, language: str | None = None,
                 criteria: str | None = None, min_coverage: int | None = None,
                 remove_stale: bool | None = None, workers: int = 4):
        self._connect = connect
        self.index = index
        self.label = label or os.environ.get("LANGUAGE_LABEL", "Italian")
        self.language = (language or os.environ.get("FOCUS_LANGUAGE", "ita")).lower()
        self.criteria = criteria or os.environ.get("LABEL_CRITERIA", "any")
        if self.criteria not in ("any", "audio", "subtitles"):
            self.criteria = "any"
        self.min_coverage = min_coverage if min_coverage is not None else int(
            os.environ.get("LABEL_MIN_COVERAGE", "0") or 0)
        self.remove_stale = (remove_stale if remove_stale is not None
                             else _env_bool("LABEL_REMOVE_STALE", False))
        self.workers = workers
        self._lock = threading.Lock()
        self.status = {"state": "idle", "added": 0, "removed": 0, "errors": 0,
                       "done": 0, "total": 0, "finished_at": None, "last_error": None}

    def config(self) -> dict:
        return {"label": self.label, "language": self.language, "criteria": self.criteria,
                "min_coverage": self.min_coverage, "remove_stale": self.remove_stale}

    def qualifies(self, title: dict) -> bool:
        aliases = language_aliases(self.language)
        total = title.get("total") or 0
        if not total:
            return False
        kinds = ("audio", "subtitles") if self.criteria == "any" else (self.criteria,)
        best = max(coverage(title, kind, aliases) for kind in kinds)
        return best > 0 and best * 100 >= self.min_coverage * total

    def _labelled(self, plex) -> dict[str, str]:
        """{ratingKey: section id} of shows/movies already carrying the label."""
        found = {}
        root = plex.query("/library/sections")
        for section in root if root is not None else []:
            kind, sid = section.get("type"), section.get("key")
            if kind not in ("movie", "show"):
                continue
            tags = plex.query(f"/library/sections/{sid}/label")
            match = next((d.get("key") for d in (tags if tags is not None else [])
                          if (d.get("title") or "").casefold() == self.label.casefold()), None)
            if not match:
                continue
            libtype = 1 if kind == "movie" else 2
            for elem in query_all(plex, f"/library/sections/{sid}/all?type={libtype}&label={match}"):
                found[elem.get("ratingKey")] = sid
        return found

    def plan(self) -> dict:
        """What a sync would change. Read-only."""
        plex = self._connect()
        wanted = {t["key"]: t for t in self.index.titles() if self.qualifies(t)}
        have = self._labelled(plex)
        add = sorted(set(wanted) - set(have), key=lambda k: str(wanted[k]["title"]).casefold())
        stale = sorted(set(have) - set(wanted)) if self.index.ready else []
        titles = {t["key"]: t for t in self.index.titles()}
        return {
            **self.config(),
            "qualifying": len(wanted), "labelled": len(have),
            "add": [{"key": k, "title": wanted[k]["title"], "type": wanted[k]["type"]} for k in add],
            "remove": [{"key": k, "title": titles.get(k, {}).get("title", k)} for k in stale],
        }

    def sync(self, plan: dict | None = None) -> dict:
        """Add the label where missing (and remove stale ones if enabled)."""
        if not self._lock.acquire(blocking=False):
            return self.status
        try:
            self.status.update(state="running", added=0, removed=0, errors=0, done=0,
                               last_error=None)
            plan = plan or self.plan()
            plex = self._connect()
            removals = plan["remove"] if self.remove_stale else []
            jobs = [(row["key"], False) for row in plan["add"]] + [(row["key"], True) for row in removals]
            self.status["total"] = len(jobs)

            def run(job):
                key, remove = job
                try:
                    item = plex.fetchItem(f"/library/metadata/{key}")  # full object keeps other labels
                    if remove:
                        item.removeLabel(self.label, locked=True)
                    else:
                        item.addLabel(self.label, locked=True)
                    return remove, None
                except Exception as exc:
                    return remove, f"{key}: {exc.__class__.__name__}: {exc}"

            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="plex-label") as pool:
                for remove, error in pool.map(run, jobs):
                    self.status["done"] += 1
                    if error:
                        self.status["errors"] += 1
                        self.status["last_error"] = error
                    elif remove:
                        self.status["removed"] += 1
                    else:
                        self.status["added"] += 1
        except Exception as exc:
            log.exception("Label sync failed")
            self.status["last_error"] = f"{exc.__class__.__name__}: {exc}"
        finally:
            self.status.update(state="idle", finished_at=int(time.time()))
            self._lock.release()
        return self.status

    def sync_in_background(self) -> None:
        threading.Thread(target=self.sync, name="plex-label-sync", daemon=True).start()
