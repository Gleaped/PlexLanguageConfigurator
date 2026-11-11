"""Persistent mapping from Plex audio language codes to Bazarr profile IDs."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


class ProfileMappingStore:
    def __init__(self, path: str | None = None):
        data_dir = os.environ.get("PLEXLANG_DATA_DIR", "/data")
        self.path = Path(path or os.environ.get(
            "BAZARR_PROFILE_MAPPINGS_FILE",
            str(Path(data_dir) / "bazarr-language-profile-mappings.json"),
        ))
        self._lock = threading.Lock()

    def load(self) -> dict[str, str]:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Could not read Bazarr profile mappings: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("Bazarr profile mapping file must contain a JSON object.")
            return {str(k).lower(): str(v) for k, v in payload.items()
                    if isinstance(k, str) and isinstance(v, (str, int)) and str(v)}

    def save(self, mappings: dict[str, str]) -> None:
        clean = {}
        for language, profile_id in mappings.items():
            code = str(language).strip().lower()
            value = str(profile_id).strip()
            if not code or not value:
                continue
            if not code.isalpha() or len(code) not in (2, 3):
                raise ValueError(f"Invalid language code: {code!r}.")
            if not value.isdigit():
                raise ValueError(f"Invalid Bazarr profile ID for {code}.")
            clean[code] = value

        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(
                    prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
                )
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(clean, stream, indent=2, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.path)
            except OSError as exc:
                try:
                    if 'temp_name' in locals() and os.path.exists(temp_name):
                        os.unlink(temp_name)
                except OSError:
                    pass
                raise ValueError(f"Could not save Bazarr profile mappings to {self.path}: {exc}") from exc
