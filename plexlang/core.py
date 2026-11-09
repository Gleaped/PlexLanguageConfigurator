"""Language summary and assignment logic.

Everything here works on plexapi-style objects (duck-typed), so it can be
tested without a Plex server:

    item.media -> [Media]           (an episode or a movie)
    media.parts -> [MediaPart]
    part.audioStreams() / part.subtitleStreams() -> [Stream]
    stream.id, stream.languageCode, stream.language, stream.title, stream.forced

A "series" is just a list of playable items (all its episodes). A movie is a
list with one item. Users pick one language per series; we then apply it to
every part of every episode that has that language.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

UNKNOWN = "und"
OFF = "off"  # subtitle choice meaning "no subtitles"


def lang_key(stream) -> str:
    """Stable key for a stream's language (ISO 639-2 code where available)."""
    code = (getattr(stream, "languageCode", None) or "").strip().lower()
    if code:
        return code
    name = (getattr(stream, "language", None) or "").strip().lower()
    return name or UNKNOWN


def lang_name(stream) -> str:
    return getattr(stream, "language", None) or lang_key(stream)


def _parts(item):
    for media in getattr(item, "media", None) or []:
        for part in getattr(media, "parts", None) or []:
            yield part


def _streams(part, kind: str):
    getter = getattr(part, "audioStreams" if kind == "audio" else "subtitleStreams")
    return list(getter() or [])


@dataclass
class LanguageOption:
    code: str
    name: str
    count: int  # number of items (episodes) that have this language
    total: int  # total items in the series

    @property
    def complete(self) -> bool:
        return self.count == self.total

    def label(self) -> str:
        if self.complete:
            return f"{self.name} ({self.code})"
        return f"{self.name} ({self.code}) - {self.count} of {self.total} episodes"


@dataclass
class LanguageSummary:
    total: int
    audio: list[LanguageOption]
    subtitles: list[LanguageOption]


def summarize(items: Iterable) -> LanguageSummary:
    """Count, per language, how many items have at least one matching stream."""
    items = list(items)
    result = {}
    for kind in ("audio", "subtitles"):
        counts: dict[str, int] = {}
        names: dict[str, str] = {}
        for item in items:
            seen = set()
            for part in _parts(item):
                for s in _streams(part, kind):
                    key = lang_key(s)
                    seen.add(key)
                    names.setdefault(key, lang_name(s))
            for key in seen:
                counts[key] = counts.get(key, 0) + 1
        options = [
            LanguageOption(code=k, name=names[k], count=c, total=len(items))
            for k, c in counts.items()
        ]
        options.sort(key=lambda o: (-o.count, o.name.lower()))
        result[kind] = options
    return LanguageSummary(total=len(items), audio=result["audio"], subtitles=result["subtitles"])


def pick_stream(streams, code: str, kind: str):
    """Choose the best stream of a language from one part.

    Audio: prefer streams that are not commentary tracks.
    Subtitles: prefer full (non-forced) subtitles.
    Ties keep Plex's own order, with the file's default stream first.
    """
    matches = [s for s in streams if lang_key(s) == code]
    if not matches:
        return None

    def score(s):
        title = (getattr(s, "title", None) or getattr(s, "displayTitle", None) or "").lower()
        penalty = 0
        if kind == "audio" and "commentary" in title:
            penalty += 2
        if kind == "subtitles" and getattr(s, "forced", False):
            penalty += 2
        if not getattr(s, "default", False):
            penalty += 1
        return penalty

    return min(matches, key=score)


@dataclass
class PartChange:
    item: object
    part: object
    audio_stream: object | None = None  # stream to select, or None to leave alone
    subtitle_stream: object | None = None
    subtitles_off: bool = False


@dataclass
class Plan:
    total: int
    audio: str | None
    subtitles: str | None
    changes: list[PartChange] = field(default_factory=list)
    audio_matched: int = 0  # items where the audio language was found
    subtitles_matched: int = 0
    audio_missing: list = field(default_factory=list)  # items without the audio language
    subtitles_missing: list = field(default_factory=list)


def build_plan(items: Iterable, audio: str | None = None, subtitles: str | None = None) -> Plan:
    """Work out which stream to select on every part.

    audio: language code, or None to leave audio unchanged.
    subtitles: language code, OFF to disable subtitles, or None to leave unchanged.
    """
    items = list(items)
    audio = audio.lower() if audio else None
    subtitles = subtitles.lower() if subtitles else None
    plan = Plan(total=len(items), audio=audio, subtitles=subtitles)

    for item in items:
        got_audio = got_subs = False
        for part in _parts(item):
            change = PartChange(item=item, part=part)
            if audio:
                change.audio_stream = pick_stream(_streams(part, "audio"), audio, "audio")
                got_audio |= change.audio_stream is not None
            if subtitles == OFF:
                change.subtitles_off = True
                got_subs = True
            elif subtitles:
                change.subtitle_stream = pick_stream(_streams(part, "subtitles"), subtitles, "subtitles")
                got_subs |= change.subtitle_stream is not None
            if change.audio_stream is not None or change.subtitle_stream is not None or change.subtitles_off:
                plan.changes.append(change)
        if audio:
            if got_audio:
                plan.audio_matched += 1
            else:
                plan.audio_missing.append(item)
        if subtitles:
            if got_subs:
                plan.subtitles_matched += 1
            else:
                plan.subtitles_missing.append(item)
    return plan


def apply_plan(plan: Plan) -> int:
    """Send the selections to Plex. Returns the number of parts updated."""
    updated = 0
    for change in plan.changes:
        part = change.part
        if change.audio_stream is not None:
            part.setSelectedAudioStream(change.audio_stream.id)
        if change.subtitles_off:
            part.resetSelectedSubtitleStream()
        elif change.subtitle_stream is not None:
            part.setSelectedSubtitleStream(change.subtitle_stream.id)
        updated += 1
    return updated
