from types import SimpleNamespace as NS

from plexlang import core


class Part:
    def __init__(self, audio=(), subs=()):
        self._audio = list(audio)
        self._subs = list(subs)
        self.calls = []

    def audioStreams(self):
        return self._audio

    def subtitleStreams(self):
        return self._subs

    def setSelectedAudioStream(self, sid):
        self.calls.append(("audio", sid))

    def setSelectedSubtitleStream(self, sid):
        self.calls.append(("subs", sid))

    def resetSelectedSubtitleStream(self):
        self.calls.append(("subs", 0))


_next_id = iter(range(1, 10_000))


def stream(code, name=None, title="", forced=False, default=False):
    return NS(id=next(_next_id), languageCode=code, language=name or code.title(),
              title=title, forced=forced, default=default)


def episode(*parts):
    return NS(type="movie", title="x", media=[NS(parts=list(parts))])


def series():
    return [
        episode(Part(audio=[stream("eng"), stream("ita", "Italiano")], subs=[stream("eng")])),
        episode(Part(audio=[stream("eng"), stream("ita", "Italiano")], subs=[stream("eng"), stream("ita")])),
        episode(Part(audio=[stream("eng")], subs=[])),
    ]


def test_summary_counts_episodes_per_language():
    s = core.summarize(series())
    assert s.total == 3
    audio = {o.code: o.count for o in s.audio}
    subs = {o.code: o.count for o in s.subtitles}
    assert audio == {"eng": 3, "ita": 2}
    assert subs == {"eng": 2, "ita": 1}
    ita = next(o for o in s.audio if o.code == "ita")
    assert ita.label() == "Italiano (ita) - 2 of 3 episodes"
    eng = next(o for o in s.audio if o.code == "eng")
    assert eng.complete and eng.label() == "Eng (eng)"


def test_episode_counted_once_even_with_multiple_versions():
    ep = NS(media=[NS(parts=[Part(audio=[stream("eng")])]), NS(parts=[Part(audio=[stream("eng")])])])
    assert core.summarize([ep]).audio[0].count == 1


def test_plan_and_apply_reports_missing_episodes():
    items = series()
    plan = core.build_plan(items, audio="ITA", subtitles="eng")
    assert plan.audio_matched == 2 and plan.audio_missing == [items[2]]
    assert plan.subtitles_matched == 2 and plan.subtitles_missing == [items[2]]
    assert len(plan.changes) == 2  # third episode has nothing to change
    assert core.apply_plan(plan) == 2
    part = items[0].media[0].parts[0]
    assert part.calls == [("audio", part._audio[1].id), ("subs", part._subs[0].id)]
    assert items[2].media[0].parts[0].calls == []


def test_subtitles_off_applies_everywhere():
    items = series()
    plan = core.build_plan(items, subtitles="off")
    assert plan.subtitles_matched == 3
    core.apply_plan(plan)
    assert all(ep.media[0].parts[0].calls == [("subs", 0)] for ep in items)


def test_pick_stream_prefers_main_audio_and_full_subtitles():
    commentary = stream("eng", title="Director's Commentary", default=False)
    main = stream("eng", title="Surround 5.1")
    assert core.pick_stream([commentary, main], "eng", "audio") is main
    forced = stream("eng", forced=True)
    full = stream("eng")
    assert core.pick_stream([forced, full], "eng", "subtitles") is full
    assert core.pick_stream([forced], "eng", "subtitles") is forced
    assert core.pick_stream([full], "ita", "subtitles") is None


def test_missing_language_code_falls_back():
    s = stream("", name="Klingon")
    s.languageCode = None
    assert core.lang_key(s) == "klingon"
    s.language = None
    assert core.lang_key(s) == core.UNKNOWN
