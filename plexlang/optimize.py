"""Tune Sonarr/Radarr quality profiles to favour Italian + English + Japanese releases.

Safe by construction:

* ``preview`` is read-only and lists every change (custom formats to create,
  score changes per profile, cutoff/upgrade/language fixes).
* ``apply`` first writes a full backup of the profiles it will touch to
  ``/data/backups`` and only runs after an explicit confirmation from the UI.
* ``restore`` puts the backed-up profiles back and deletes the custom formats
  this tool created (never ones you already had).
* Existing custom formats are reused, and a score you already set higher than
  the target is left alone. Only the profiles you tick are changed.

Scoring (defaults, editable in the UI): each wanted language adds to a
release's score, so a release carrying Italian + English + Japanese beats one
with a single language, and Italian is worth the most because it is the one
being hunted. MULTi is a small bonus on top.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .arr import ArrError, _language_ids, classify_custom_format

LANGS = {"ita": "Italian", "eng": "English", "jpn": "Japanese"}
DEFAULT_SCORES = {"ita": 200, "eng": 100, "jpn": 100, "multi": 50}
PREFIX = "PLC "  # marks custom formats created by this app
MULTI_NAME = PREFIX + "MULTi / Dual audio"
MULTI_REGEX = r"\b(MULTi|DUAL([ ._-]?AUDIO)?)\b"


def _lang_cf_name(code: str) -> str:
    return f"{PREFIX}Language: {LANGS[code]}"


def _spec_language(language_id: int, name: str) -> dict:
    return {"name": name, "implementation": "LanguageSpecification", "negate": False,
            "required": False, "fields": [{"name": "value", "value": language_id}]}


def _spec_title(regex: str, name: str) -> dict:
    return {"name": name, "implementation": "ReleaseTitleSpecification", "negate": False,
            "required": False, "fields": [{"name": "value", "value": regex}]}


def _single_language_cf(cf: dict, language_id: int) -> bool:
    """A custom format that is exactly 'has language X' (safe to reuse for scoring)."""
    specs = cf.get("specifications", [])
    if len(specs) != 1:
        return False
    spec = specs[0]
    fields = {f.get("name"): f.get("value") for f in spec.get("fields", [])}
    return (spec.get("implementation") == "LanguageSpecification" and not spec.get("negate")
            and fields.get("value") == language_id)


def build_plan(client, scores: dict | None = None) -> dict:
    """Everything that would change, computed from the service's current state."""
    scores = {**DEFAULT_SCORES, **{k: int(v) for k, v in (scores or {}).items() if k in DEFAULT_SCORES}}
    languages = client.languages()
    formats = client.custom_formats()
    profiles = client.quality_profiles()
    warnings, new_formats, targets = [], [], {}  # targets: key -> (cf id or None, name, score)

    for code, name in LANGS.items():
        ids = _language_ids(languages, name)
        if not ids:
            warnings.append(f"{name} isn't in this service's language list, so it was skipped.")
            continue
        language_id = next(iter(ids))
        existing = next((cf for cf in formats if _single_language_cf(cf, language_id)), None)
        if existing:
            targets[code] = (existing["id"], existing["name"], scores[code])
        else:
            cf_name = _lang_cf_name(code)
            clash = next((cf for cf in formats if cf.get("name") == cf_name), None)
            if clash:
                targets[code] = (clash["id"], clash["name"], scores[code])
            else:
                new_formats.append({"key": code, "name": cf_name,
                                    "body": {"name": cf_name, "includeCustomFormatWhenRenaming": False,
                                             "specifications": [_spec_language(language_id, name)]}})
                targets[code] = (None, cf_name, scores[code])

    multi = next((cf for cf in formats if cf.get("name") == MULTI_NAME), None)
    if multi:
        targets["multi"] = (multi["id"], multi["name"], scores["multi"])
    else:
        new_formats.append({"key": "multi", "name": MULTI_NAME,
                            "body": {"name": MULTI_NAME, "includeCustomFormatWhenRenaming": False,
                                     "specifications": [_spec_title(MULTI_REGEX, "MULTi or Dual Audio")]}})
        targets["multi"] = (None, MULTI_NAME, scores["multi"])

    positive_total = sum(score for _, _, score in targets.values() if score > 0)
    plan_profiles = []
    for profile in profiles:
        items = {i.get("format"): i for i in profile.get("formatItems", [])}
        changes, final_total = [], 0
        for key, (cf_id, cf_name, target) in targets.items():
            current = items.get(cf_id, {}).get("score", 0) if cf_id is not None else 0
            if current >= target:  # never lower a score the user chose
                final_total += current if current > 0 else 0
                continue
            final_total += target
            changes.append({"type": "score", "format": cf_name, "key": key, "from": current, "to": target,
                            "new_format": cf_id is None})
        cutoff = profile.get("cutoffFormatScore")
        # The cutoff must reach the best achievable score, or upgrades stop early.
        best_other = max((i.get("score", 0) for fid, i in items.items()
                          if fid not in {t[0] for t in targets.values()}), default=0)
        need = max(positive_total, final_total) + max(best_other, 0)
        if cutoff is not None and cutoff < need:
            changes.append({"type": "cutoff", "from": cutoff, "to": need})
        if profile.get("upgradeAllowed") is False:
            changes.append({"type": "upgrade", "from": False, "to": True})
        language = (profile.get("language") or {}).get("name")
        if language and language.casefold() not in ("any",):
            changes.append({"type": "language", "from": language, "to": "Any"})
        plan_profiles.append({"id": profile["id"], "name": profile.get("name"), "changes": changes,
                              "changed": bool(changes)})
    return {"scores": scores, "new_formats": [{"key": f["key"], "name": f["name"]} for f in new_formats],
            "profiles": plan_profiles, "warnings": warnings,
            "_new_formats": new_formats, "_targets": targets}


class Optimizer:
    def __init__(self, sonarr=None, radarr=None, data_dir: str | None = None):
        self.clients = {"sonarr": sonarr, "radarr": radarr}
        self.dir = Path(data_dir or os.environ.get("PLEXLANG_DATA_DIR", "/data")) / "backups"
        self.previews: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _client(self, service):
        client = self.clients.get(service)
        if client is None:
            raise LookupError(f"{service.title()} is not configured.")
        return client

    # ----- preview -----------------------------------------------------

    def preview(self, service: str, scores: dict | None = None) -> dict:
        plan = build_plan(self._client(service), scores)
        preview_id = uuid.uuid4().hex
        self.previews[preview_id] = {"service": service, "plan": plan, "created_at": time.time()}
        for key, _ in sorted(self.previews.items(), key=lambda kv: kv[1]["created_at"])[:-10]:
            self.previews.pop(key, None)
        public = {k: v for k, v in plan.items() if not k.startswith("_")}
        return {**public, "preview_id": preview_id, "service": service}

    # ----- apply -------------------------------------------------------

    def apply(self, preview_id: str, profile_ids: list, confirmed: bool) -> dict:
        if confirmed is not True:
            raise ValueError("Explicit confirmation is required.")
        preview = self.previews.get(preview_id)
        if not preview:
            raise LookupError("Preview not found or expired. Generate a new preview.")
        service, plan = preview["service"], preview["plan"]
        client = self._client(service)
        wanted = {int(i) for i in profile_ids}
        chosen = [p for p in plan["profiles"] if p["id"] in wanted and p["changed"]]
        if not chosen:
            raise ValueError("Select at least one profile that has changes.")
        with self._lock:
            # Re-read live state so we never overwrite edits made since the preview.
            current = {p["id"]: p for p in client.quality_profiles()}
            backup_id = time.strftime("%Y%m%d-%H%M%S") + f"-{service}"
            backup = {"id": backup_id, "service": service, "created_at": int(time.time()),
                      "profiles": [copy.deepcopy(current[p["id"]]) for p in chosen if p["id"] in current],
                      "created_formats": [], "scores": plan["scores"]}
            self._write_backup(backup)  # durable before the first change

            targets = {k: list(v) for k, v in plan["_targets"].items()}
            for new in plan["_new_formats"]:
                created = client.create_custom_format(new["body"])
                backup["created_formats"].append({"id": created["id"], "name": new["name"]})
                targets[new["key"]][0] = created["id"]
                self._write_backup(backup)

            results = []
            for p in chosen:
                profile = copy.deepcopy(current[p["id"]])
                self._apply_to_profile(profile, targets, p["changes"])
                client.update_quality_profile(profile["id"], profile)
                results.append({"id": profile["id"], "name": profile.get("name"), "changes": len(p["changes"])})
            backup["applied_at"] = int(time.time())
            self._write_backup(backup)
        return {"backup_id": backup_id, "profiles": results,
                "created_formats": [f["name"] for f in backup["created_formats"]]}

    @staticmethod
    def _apply_to_profile(profile, targets, changes):
        items = {i.get("format"): i for i in profile.setdefault("formatItems", [])}
        for change in changes:
            if change["type"] == "score":
                cf_id, cf_name, score = targets[change["key"]]
                item = items.get(cf_id)
                if item is None:
                    item = {"format": cf_id, "name": cf_name, "score": 0}
                    profile["formatItems"].append(item)
                    items[cf_id] = item
                item["score"] = max(item.get("score", 0), score)
            elif change["type"] == "cutoff":
                profile["cutoffFormatScore"] = change["to"]
            elif change["type"] == "upgrade":
                profile["upgradeAllowed"] = True
            elif change["type"] == "language":
                profile["language"] = {"id": -1, "name": "Any"}

    # ----- backups / restore -------------------------------------------

    def _write_backup(self, backup: dict):
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"arr-{backup['id']}.json"
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(self.dir))
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(backup, stream, indent=2)
        os.replace(tmp, path)

    def backups(self) -> list[dict]:
        out = []
        for path in sorted(self.dir.glob("arr-*.json"), reverse=True) if self.dir.exists() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"id": data["id"], "service": data["service"], "created_at": data.get("created_at"),
                        "applied": bool(data.get("applied_at")), "restored_at": data.get("restored_at"),
                        "profiles": [p.get("name") for p in data.get("profiles", [])],
                        "created_formats": [f["name"] for f in data.get("created_formats", [])]})
        return out

    def restore(self, backup_id: str, confirmed: bool) -> dict:
        if confirmed is not True:
            raise ValueError("Explicit confirmation is required.")
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-(sonarr|radarr)", backup_id or ""):
            raise LookupError("Unknown backup.")
        path = self.dir / f"arr-{backup_id}.json"
        try:
            backup = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise LookupError("Backup not found.")
        if backup.get("restored_at"):
            raise ValueError("This backup was already restored.")
        client = self._client(backup["service"])
        with self._lock:
            live_formats = {cf["id"] for cf in client.custom_formats()}
            restored = []
            for profile in backup["profiles"]:
                # Drop entries for formats that no longer exist, then put the profile back.
                profile = copy.deepcopy(profile)
                profile["formatItems"] = [i for i in profile.get("formatItems", [])
                                          if i.get("format") in live_formats]
                client.update_quality_profile(profile["id"], profile)
                restored.append(profile.get("name"))
            removed = []
            for cf in backup.get("created_formats", []):
                try:
                    client.delete_custom_format(cf["id"])
                    removed.append(cf["name"])
                except ArrError:
                    pass  # already gone
            backup["restored_at"] = int(time.time())
            self._write_backup(backup)
        return {"restored_profiles": restored, "removed_formats": removed}
