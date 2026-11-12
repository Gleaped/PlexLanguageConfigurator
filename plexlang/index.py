"""Shared, persisted library language index that refreshes in the background.

Why this exists: Plex library listings don't include audio/subtitle streams, so
learning which languages a series has means reading every episode's full
metadata. Doing that inside a web request (once per user, per item) made the
first page take minutes. Instead:

* One index is built for the whole server with the admin connection. Stream
  languages are the same for every user; only *visibility* differs per user,
  and that is a cheap listing (see ``visible_keys``).
* Full metadata is read in batches (``/library/metadata/1,2,3``), a few batches
  in parallel, using raw XML so nothing triggers plexapi's lazy reloads.
* Each refresh lists the library (cheap) and only re-reads items whose
  ``updatedAt`` or media parts changed. A periodic full rescan catches edits
  that don't bump ``updatedAt`` (for example a new external subtitle file).
* The index is saved to ``/data`` after every library section, so a container
  restart serves the previous index instantly while a refresh runs.
* Requests never wait on it: they read the current snapshot, which is stale at
  worst, and the status endpoint reports refresh progress.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

INDEX_VERSION = 1
UNKNOWN = "und"
AUDIO, SUBTITLE = "2", "3"  # Plex streamType values

# ISO 639-1 -> 639-2 for languages Plex commonly reports either way, plus the
# names Plex shows. Used to match a requested language against stream codes.
_ALIASES = {
    "ita": {"it", "italian", "italiano"},
    "eng": {"en", "english"},
    "spa": {"es", "spanish", "español", "espanol"},
    "fra": {"fr", "fre", "french", "français", "francais"},
    "deu": {"de", "ger", "german", "deutsch"},
    "por": {"pt", "portuguese", "português", "portugues"},
    "nld": {"nl", "dut", "dutch", "nederlands"},
    "jpn": {"ja", "japanese", "日本語"},
}


def language_aliases(code: str) -> set[str]:
    """Every code or name that means the same language as ``code``."""
    code = (code or "").strip().casefold()
    for main, others in _ALIASES.items():
        if code == main or code in others:
            return {main, *others}
    return {code}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _stream_key(stream) -> tuple[str, str]:
    """(code, display name), keyed the same way as core.lang_key."""
    code = (stream.get("languageCode") or "").strip().lower()
    name = (stream.get("language") or "").strip()
    key = code or name.lower() or UNKNOWN
    return key, name or key


def _external_ids(elem) -> dict[str, list[str]]:
    found = {"imdb": [], "tvdb": [], "tmdb": []}
    for guid in elem.iter("Guid"):
        match = re.match(r"(imdb|tvdb|tmdb)://(.+)", guid.get("id") or "", re.I)
        if match:
            kind, value = match.group(1).lower(), match.group(2)
            if value not in found[kind]:
                found[kind].append(value)
    return found


def _signature(elem) -> str:
    """Changes when Plex re-scans the item or its files change."""
    parts = sorted(p.get("id", "") for p in elem.iter("Part"))
    return f"{elem.get('updatedAt', '0')}|{','.join(parts)}"


def _languages(elem, names: dict) -> tuple[list[str], list[str], bool]:
    """Audio codes, subtitle codes and whether any stream data was present."""
    audio, subs, any_streams = set(), set(), False
    for part in elem.iter("Part"):
        for stream in part.iter("Stream"):
            kind = stream.get("streamType")
            if kind not in (AUDIO, SUBTITLE):
                continue
            any_streams = True
            key, name = _stream_key(stream)
            names.setdefault(key, name)
            (audio if kind == AUDIO else subs).add(key)
    return sorted(audio), sorted(subs), any_streams


def query_all(plex, path: str, page_size: int = 1000) -> list:
    """All child elements of a Plex listing, fetched in large pages."""
    out, start = [], 0
    while True:
        root = plex.query(path, headers={"X-Plex-Container-Start": str(start),
                                         "X-Plex-Container-Size": str(page_size)})
        children = list(root) if root is not None else []
        out.extend(children)
        start += len(children)
        total = int((root.get("totalSize") if root is not None else None) or start)
        if not children or start >= total:
            return out


def _media_sections(plex):
    root = plex.query("/library/sections")
    return [(d.get("key"), d.get("type"), d.get("title"))
            for d in (root if root is not None else []) if d.get("type") in ("movie", "show")]


def visible_keys(plex) -> set[str]:
    """Rating keys of every movie and show this connection can see."""
    keys = set()
    for sid, kind, _ in _media_sections(plex):
        libtype = 1 if kind == "movie" else 2
        for elem in query_all(plex, f"/library/sections/{sid}/all?type={libtype}"):
            if elem.get("ratingKey"):
                keys.add(elem.get("ratingKey"))
    return keys


class LibraryIndex:
    """Server-wide language index with background refresh and disk persistence."""

    def __init__(self, connect, path: str | None = None, *, batch_size: int | None = None,
                 workers: int | None = None, refresh_minutes: int | None = None,
                 full_rescan_hours: int | None = None, after_refresh=None):
        data_dir = os.environ.get("PLEXLANG_DATA_DIR", "/data")
        self.path = Path(path or os.path.join(data_dir, "library-index.json"))
        self._connect = connect  # callable returning the admin PlexServer
        self.batch_size = batch_size or _env_int("PLEX_BATCH_SIZE", 100)
        self.workers = workers or _env_int("PLEX_CONCURRENCY", 4)
        self.refresh_seconds = 60 * (refresh_minutes or _env_int("REFRESH_MINUTES", 30))
        self.full_rescan_seconds = 3600 * (full_rescan_hours or _env_int("FULL_RESCAN_HOURS", 168))
        self.after_refresh = after_refresh
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._wake = threading.Event()
        self._full_requested = False
        self._thread = None
        self._data = {"version": INDEX_VERSION, "built_at": 0, "full_at": 0,
                      "names": {}, "titles": {}, "errors": 0}
        self._snapshot: list[dict] = []
        self.status = {"state": "idle", "phase": "", "done": 0, "total": 0,
                       "last_error": None, "last_duration": None, "mode": None}
        self.load()

    # ----- persistence -------------------------------------------------

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable index %s: %s", self.path, exc)
            return
        if isinstance(data, dict) and data.get("version") == INDEX_VERSION:
            with self._lock:
                self._data = data
                self._snapshot = self._build_snapshot(data)

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self._data, ensure_ascii=False, separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(tmp, self.path)
        except OSError as exc:
            # Still usable from memory; it just won't survive a restart.
            log.warning("Could not save index to %s: %s", self.path, exc)
            self.status["last_error"] = f"Could not save index to {self.path}: {exc}"

    # ----- reading -----------------------------------------------------

    @property
    def ready(self) -> bool:
        return bool(self._data.get("built_at"))

    def titles(self) -> list[dict]:
        """Current snapshot: one dict per show/movie with language counts."""
        return self._snapshot

    def info(self) -> dict:
        data = self._data
        built = int(data.get("built_at") or 0)
        return {"ready": self.ready, "built_at": built, "full_at": int(data.get("full_at") or 0),
                "titles": len(data.get("titles", {})), "errors": int(data.get("errors") or 0),
                "next_refresh_at": built + self.refresh_seconds if built else None,
                "refresh_seconds": self.refresh_seconds, **self.status}

    @staticmethod
    def _build_snapshot(data: dict) -> list[dict]:
        names = data.get("names", {})
        out = []
        for title in data.get("titles", {}).values():
            items = title.get("items", {})
            total = len(items)
            counts = {"audio": {}, "subtitles": {}}
            for item in items.values():
                for code in item.get("a", []):
                    counts["audio"][code] = counts["audio"].get(code, 0) + 1
                for code in item.get("s", []):
                    counts["subtitles"][code] = counts["subtitles"].get(code, 0) + 1
            row = {k: title.get(k) for k in ("key", "title", "year", "type", "ids")}
            row["total"] = total
            for kind in ("audio", "subtitles"):
                options = [{"code": c, "name": names.get(c, c), "count": n, "total": total}
                           for c, n in counts[kind].items()]
                options.sort(key=lambda o: (-o["count"], o["name"].lower()))
                row[kind] = options
            out.append(row)
        out.sort(key=lambda r: (str(r["title"] or "").casefold(), r["year"] or 0))
        return out

    # ----- refreshing --------------------------------------------------

    def _progress(self, phase: str, done: int = 0, total: int = 0):
        self.status.update(phase=phase, done=done, total=total)

    def _fetch_details(self, plex, keys: list[str]) -> tuple[dict, int]:
        """Full metadata for many items: {key: element}, plus an error count."""
        found: dict[str, object] = {}
        errors = 0
        batches = [keys[i:i + self.batch_size] for i in range(0, len(keys), self.batch_size)]
        done = 0

        def fetch(batch):
            try:
                root = plex.query("/library/metadata/" + ",".join(batch))
                return {e.get("ratingKey"): e for e in (root if root is not None else [])}, 0
            except Exception as exc:
                if len(batch) == 1:
                    log.info("Could not read item %s: %s", batch[0], exc)
                    return {}, 1
                # One bad item shouldn't lose the batch: split and retry.
                half = len(batch) // 2
                a, ea = fetch(batch[:half])
                b, eb = fetch(batch[half:])
                return {**a, **b}, ea + eb

        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="plex-index") as pool:
            for result, err in pool.map(fetch, batches):
                found.update(result)
                errors += err
                done += 1
                self.status["done"] = min(len(keys), done * self.batch_size)

        # If a batched response ever arrives without streams, re-read singly.
        retry = [k for k, e in found.items()
                 if e.tag != "Directory" and list(e.iter("Part")) and not _languages(e, {})[2]]
        for key in retry:
            single, err = fetch([key])
            found.update(single)
            errors += err
        return found, errors

    def refresh(self, full: bool = False) -> None:
        """Bring the index up to date. Safe to call from any thread."""
        if not self._refresh_lock.acquire(blocking=False):
            return  # another refresh is already running
        started = time.time()
        try:
            self.status.update(state="running", mode="full" if full else "incremental",
                               last_error=None)
            plex = self._connect()
            with self._lock:
                old_titles = dict(self._data.get("titles", {}))
                names = dict(self._data.get("names", {}))
            new_titles: dict[str, dict] = {}
            errors = 0
            for sid, kind, section_title in _media_sections(plex):
                if kind == "movie":
                    errors += self._refresh_movies(plex, sid, section_title, old_titles,
                                                   new_titles, names, full)
                else:
                    errors += self._refresh_shows(plex, sid, section_title, old_titles,
                                                  new_titles, names, full)
                self._commit(new_titles, names, errors, partial=old_titles, full=False)
            self._commit(new_titles, names, errors, partial=None, full=full)
            self.status["last_duration"] = round(time.time() - started, 1)
        except Exception as exc:
            log.exception("Index refresh failed")
            self.status["last_error"] = f"{exc.__class__.__name__}: {exc}"
        finally:
            self.status.update(state="idle", phase="", done=0, total=0)
            self._refresh_lock.release()
        if self.after_refresh and self.ready:
            try:
                self.after_refresh()
            except Exception:
                log.exception("After-refresh hook failed")

    def _commit(self, new_titles, names, errors, partial, full):
        """Publish progress. While sections remain, keep old entries for them."""
        # Titles in sections not refreshed yet keep their previous data meanwhile.
        titles = dict(partial or {})
        titles.update(new_titles)
        now = int(time.time())
        with self._lock:
            self._data.update(titles=titles, names=names, errors=errors)
            if partial is None:
                self._data["built_at"] = now
                if full or not self._data.get("full_at"):
                    self._data["full_at"] = now
            self._snapshot = self._build_snapshot(self._data)
        self.save()

    def _refresh_movies(self, plex, sid, section_title, old, new, names, full) -> int:
        self._progress(f"Listing {section_title}")
        listing = query_all(plex, f"/library/sections/{sid}/all?type=1")
        changed, sigs = [], {}
        for elem in listing:
            key = elem.get("ratingKey")
            prev = old.get(key)
            sig = sigs[key] = _signature(elem)
            if not full and prev and prev.get("sig") == sig:
                new[key] = {**prev, "title": elem.get("title"), "year": _year(elem)}
            else:
                changed.append(key)
        self._progress(f"Reading {section_title}", 0, len(changed))
        details, errors = self._fetch_details(plex, changed)
        for key in changed:
            elem = details.get(key)
            if elem is None:
                if key in old:
                    new[key] = old[key]
                continue
            audio, subs, _ = _languages(elem, names)
            sig = sigs[key]
            new[key] = {"key": key, "title": elem.get("title"), "year": _year(elem),
                        "type": "movie", "section": sid, "sig": sig,
                        "ids": _external_ids(elem),
                        "items": {key: {"sig": sig, "a": audio, "s": subs}}}
        return errors

    def _refresh_shows(self, plex, sid, section_title, old, new, names, full) -> int:
        self._progress(f"Listing {section_title}")
        shows = query_all(plex, f"/library/sections/{sid}/all?type=2")
        self._progress(f"Listing {section_title} episodes")
        episodes = query_all(plex, f"/library/sections/{sid}/all?type=4")
        by_show: dict[str, list] = {}
        for ep in episodes:
            by_show.setdefault(ep.get("grandparentRatingKey"), []).append(ep)

        changed_eps, new_shows = [], []
        for show in shows:
            key = show.get("ratingKey")
            prev = old.get(key) or {}
            prev_items = prev.get("items", {})
            if full or not prev:
                new_shows.append(key)
            for ep in by_show.get(key, []):
                ek = ep.get("ratingKey")
                if full or prev_items.get(ek, {}).get("sig") != _signature(ep):
                    changed_eps.append(ek)

        self._progress(f"Reading {section_title}", 0, len(changed_eps) + len(new_shows))
        details, errors = self._fetch_details(plex, new_shows + changed_eps)

        for show in shows:
            key = show.get("ratingKey")
            prev = old.get(key) or {}
            prev_items = prev.get("items", {})
            items = {}
            for ep in by_show.get(key, []):
                ek = ep.get("ratingKey")
                full_ep = details.get(ek)
                if full_ep is not None:
                    audio, subs, _ = _languages(full_ep, names)
                    items[ek] = {"sig": _signature(ep), "a": audio, "s": subs}
                elif ek in prev_items:
                    items[ek] = prev_items[ek]
            show_detail = details.get(key)
            new[key] = {
                "key": key, "title": show.get("title"), "year": _year(show),
                "type": "show", "section": sid,
                "ids": _external_ids(show_detail) if show_detail is not None else prev.get("ids", {}),
                "items": items,
            }
        return errors

    # ----- background loop ---------------------------------------------

    def request_refresh(self, full: bool = False) -> None:
        """Ask the background loop to refresh now (returns immediately)."""
        self._full_requested |= full
        self._wake.set()
        if self._thread is None:
            threading.Thread(target=self.refresh, kwargs={"full": full}, daemon=True).start()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="plex-index-refresh", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            full_due = time.time() - (self._data.get("full_at") or 0) >= self.full_rescan_seconds
            full = self._full_requested or full_due
            self._full_requested = False
            self._wake.clear()
            self.refresh(full=full)
            self._wake.wait(self.refresh_seconds)


def _year(elem):
    value = elem.get("year")
    return int(value) if value and value.isdigit() else None


def coverage(title: dict, kind: str, aliases: set[str]) -> int:
    """How many items of a title have a language (any alias) for ``kind``."""
    return max((o["count"] for o in title.get(kind, [])
                if o["code"].casefold() in aliases or o["name"].casefold() in aliases), default=0)
