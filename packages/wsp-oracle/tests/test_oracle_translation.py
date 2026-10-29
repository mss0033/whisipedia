"""Machine translation on the reader's request (ADR-0013): every segment checked, cached, through an
on-demand llama.cpp server. The model is a fake here; the bake-off measured the real one
(docs/research/MT-BAKEOFF-2026-10-24.md)."""

# ruff: noqa: E501, RUF001 - the parametrized cases read better unwrapped, in their own scripts

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from wsp_core.errors import TranslationUnavailableError
from wsp_oracle.config import TranslationConfig
from wsp_oracle.mtcheck import check
from wsp_oracle.translation import LlamaServer, Segment, TranslationCache, Translator, prompt


@pytest.mark.parametrize(
    ("source", "target", "lang", "reason"),
    [
        # The bake-off's failures, each caught.
        ("Elf Länder unterzeichneten 1999.", "Twelve countries signed in 1999.", "de", "numbers differ"),
        ("Elf Länder unterzeichneten 1999.", "Eleven countries signed in 2000.", "de", "numbers differ"),
        ("Die Mauer fiel am 9.11.1989.", "The wall fell on 8 November 1989.", "de", "numbers differ"),
        ("Geöffnet von 10.15 bis 11.45 Uhr.", "Open from 10:15 to 11:30.", "de", "numbers differ"),
        ("東京の人口は約1400万人である。", "Tokyo has a population of about 1.4 million.", "ja", "numbers differ"),
        ("<x1>Berlin</x1> ist die Hauptstadt ⟦1⟧.", "Berlin is the capital ⟦1⟧.", "de", "links or formatting"),
        ("<x1>Berlin</x1> ist groß.", "</x1>Berlin<x1> is big.", "de", "out of order"),
        ("Der Vertrag wurde unterzeichnet. Er trat in Kraft. Er gilt bis heute.", "The treaty was signed.", "de",
         "much shorter"),
        ("La ville est située sur la rive droite du fleuve.", "La ville est située sur la rive droite du fleuve.",
         "fr", "not translated"),
        ("Москва — столица России и крупнейший город страны.", "Москва — столица России, the largest city.", "ru",
         "not translated"),
        ("紡野高中二年A班學生。", "A student in Class 3-A at Tsumugino High School.", "zh", "numbers differ"),
        ("東京是日本的首都，也是日本最大的城市，人口超過一千萬，是世界上最大的都市圈之一。", "Tokyo is the capital.", "zh",
         "much shorter"),
        ("Die Stadt liegt am Rhein.", "Here is the translation: The city lies on the Rhine.", "de", "words of its own"),
        ("東京は日本の首都である。人口は多い。経済の中心である。",
         "Tokyo is the capital of Japan, with a very large population that keeps on growing.", "ja",
         "sentence may be missing"),
    ],
)  # fmt: skip
def test_a_translation_that_fails_a_check_is_not_shown(
    source: str, target: str, lang: str, reason: str
) -> None:
    v = check(source, target, lang)
    assert not v.ok
    assert v.reason is not None
    assert reason in v.reason


@pytest.mark.parametrize(
    ("source", "target", "lang"),
    [
        ("Elf Länder unterzeichneten 1999 den Vertrag.", "Eleven countries signed the treaty in 1999.", "de"),
        ("Elf Länder unterzeichneten 1999 den Vertrag.", "11 countries signed the treaty in 1999.", "de"),
        ("Der Fluss ist 1.234,5 km lang.", "The river is 1,234.5 km long.", "de"),
        ("<x1>Berlin</x1> ist die Hauptstadt ⟦1⟧.", "<x1>Berlin</x1> is the capital ⟦1⟧.", "de"),
        ("Un pays, une langue.", "One country, one language.", "fr"),  # "one": every language's article
        (
            "Ab dem Zusammenfluss der beiden Quelläste.",
            "From the confluence of the two source streams.",
            "de",
        ),
        ("東京は日本の首都である。", "Tokyo is the capital of Japan.", "ja"),
        # The same value written another way (2026-10-24: "1400万" is "14 million").
        ("東京の人口は約1400万人である。", "Tokyo has a population of about 14 million.", "ja"),
        ("Berlin hat rund 3,9 Millionen Einwohner.", "Berlin has around 3.9 million inhabitants.", "de"),
        ("Der Fluss ist 1.233 km lang.", "The river is 1,233 km long.", "de"),
        # Numbers written in characters (2026-10-25: 二年A班 is "Class 2-A").
        ("紡野高中二年A班學生。", "A student in Class 2-A at Tsumugino High School.", "zh"),
        ("全書共二十六話。", "The series has twenty-six episodes in all.", "zh"),
        ("Die Serie hat 26 Folgen.", "The series has twenty-six episodes.", "de"),
        ("第二十六話は二〇〇七年に放送された。", "Episode 26 was broadcast in 2007.", "ja"),
        ("人口約三千万人。", "The population is about 30 million.", "zh"),
        ("鈴木一郎は九州で生まれた。", "Ichiro Suzuki was born in Kyushu.", "ja"),  # names, never "missing"
        # Mostly dates and times: the dense script's band is for the Chinese words only.
        (
            "東京電視台 • 2006年10月2日～2007年3月26日 • 星期一 25:30～26:00",
            "Tokyo Television • October 2, 2006 – March 26, 2007 • Mondays 25:30–26:00",
            "zh",
        ),
        # Dotted dates and times, and a year next to a count (2026-10-26 review #27).
        ("Die Mauer fiel am 9.11.1989 in Berlin.", "The Berlin Wall fell on 9 November 1989.", "de"),
        ("Geöffnet von 10.15 bis 11.45 Uhr.", "Open from 10:15 to 11:45.", "de"),
        ("Im Jahr 1990 starben 300 Soldaten.", "In 1990, 300 soldiers died.", "de"),
        ("Es kostet 12 345 678 Euro.", "It costs 12,345,678 euros.", "de"),
        # Words that are numbers only sometimes, and a framing phrase that is a sentence (review #102, #107).
        ("Tu sei il mio migliore amico.", "You are my best friend.", "it"),
        ("Otto von Bismarck fu un politico prussiano.", "Otto von Bismarck was a Prussian politician.", "it"),
        ("Les deux pays ont signé le traité.", "Both countries signed the treaty.", "fr"),
        (
            "Die Übersetzung der Bibel ins Deutsche durch Luther war wichtig.",
            "The translation of the Bible into German by Luther was important.",
            "de",
        ),
    ],
)
def test_a_faithful_translation_passes(source: str, target: str, lang: str) -> None:
    assert check(source, target, lang).ok


def test_the_prompt_names_the_language_and_asks_for_placeholders_only_when_there_are_some() -> None:
    plain = prompt("de", "Die Stadt liegt am Rhein.")
    assert plain == "Translate this from German to English.\nGerman: Die Stadt liegt am Rhein.\nEnglish:"
    tagged = prompt("de", "Die <x1>Stadt</x1> liegt am Rhein.")
    assert "Keep every <xN>" in tagged
    assert prompt("zh", "這是國會").startswith("Translate this from Chinese (Traditional)")


class FakeServer:
    def __init__(self) -> None:
        self.ensured = 0
        self.state = "ready"
        self.error = None
        self.url = "http://127.0.0.1:1"

    def ensure(self) -> None:
        self.ensured += 1

    def touch(self) -> None: ...
    def stop(self) -> None: ...
    def stop_if_idle(self) -> bool:
        return False

    def missing(self) -> str | None:
        return None


def _translator(tmp_path: Path, answers: dict[str, str], calls: list[str]) -> Translator:
    def complete(text: str, limit: int) -> str:
        source = text.split(": ", 1)[1].rsplit("\nEnglish:", 1)[0]
        calls.append(source)
        if source not in answers:
            raise OSError("the server went away")
        return " " + answers[source]

    return Translator(
        TranslationConfig(),
        TranslationCache(tmp_path / "cache.sqlite"),
        FakeServer(),  # type: ignore[arg-type]
        complete=complete,
    )


def test_segments_are_checked_cached_and_a_server_failure_is_not_cached(tmp_path: Path) -> None:
    calls: list[str] = []
    answers = {
        "Elf Länder unterzeichneten 1999.": "Eleven countries signed in 1999.",
        "Zwölf Länder.": "Ten countries.",  # a changed number: not shown
    }
    t = _translator(tmp_path, answers, calls)
    try:
        segments: Sequence[str] = ["Elf Länder unterzeichneten 1999.", "Zwölf Länder.", "   ", "Verloren."]
        first = t.translate("de", segments)
        assert [(s.text, s.ok, s.cached) for s in first[:3]] == [
            ("Eleven countries signed in 1999.", True, False),
            (None, False, False),
            ("", True, False),
        ]
        assert first[1].reason is not None
        assert "numbers differ" in first[1].reason
        assert first[3].reason == "the translator did not answer"
        calls.clear()
        again = t.translate("de", segments)
        assert [s.cached for s in again] == [True, True, False, False]
        assert calls == ["Verloren."]  # only the one the server failed is asked again
        with pytest.raises(ValueError, match="no translation from"):
            t.translate("xx", ["Hallo"])
    finally:
        t.close()


def test_the_server_starts_only_when_installed_and_the_gpu_has_room(tmp_path: Path) -> None:
    model, binary = tmp_path / "m.gguf", tmp_path / "llama-server"
    cfg = TranslationConfig(model=str(model), server=str(binary), min_free_vram_mb=15500)
    ran: list[list[str]] = []
    server = LlamaServer(
        cfg, log_path=tmp_path / "log", run=lambda argv: ran.append(list(argv)) or 0, vram=lambda: 20000
    )
    with pytest.raises(TranslationUnavailableError) as missing:
        server.ensure()
    assert missing.value.retry_after is None  # nothing to wait for: not installed
    assert "not installed" in str(missing.value)
    model.write_bytes(b"gguf")
    binary.write_bytes(b"")
    released: list[int] = []
    busy = LlamaServer(
        cfg,
        log_path=tmp_path / "log",
        run=lambda argv: 0,
        vram=lambda: 6000,
        release=lambda: released.append(1),
    )
    with pytest.raises(TranslationUnavailableError) as gpu:
        busy.ensure()
    assert gpu.value.retry_after == 120
    assert "6000 MB free" in str(gpu.value)
    assert released == [1]  # the API's own cached GPU memory was given back first
    argv = server.argv()
    assert argv[:6] == ["systemd-run", "--user", "--scope", "-q", "--unit=whisipedia-translate", "-p"]
    assert "MemoryMax=4G" in argv
    assert "MemorySwapMax=0" in argv
    assert argv[argv.index("-np") + 1] == "8"
    assert argv[argv.index("-c") + 1] == str(8 * 2048)


def test_an_idle_server_is_stopped(tmp_path: Path) -> None:
    now = [0.0]
    ran: list[list[str]] = []
    cfg = TranslationConfig(idle_seconds=60)
    server = LlamaServer(
        cfg, log_path=tmp_path / "log", run=lambda argv: ran.append(list(argv)) or 0, clock=lambda: now[0]
    )
    server._state = "ready"  # pyright: ignore[reportPrivateUsage]
    server.touch()
    now[0] = 30.0
    assert not server.stop_if_idle()
    now[0] = 100.0
    assert server.stop_if_idle()
    assert server.state == "stopped"
    assert ran == [["systemctl", "--user", "stop", "whisipedia-translate.scope"]]


def test_a_cached_answer_is_judged_again_by_todays_checks(tmp_path: Path) -> None:
    """A segment rejected under an older check (the Rhine's "beiden" as an added 2) passes once the
    check knows better, without asking the model again."""
    cache = TranslationCache(tmp_path / "cache.sqlite")
    source = "Ab dem Zusammenfluss der beiden Quelläste."
    model = TranslationConfig().model_label
    key = TranslationCache.key(model, "de", source)
    english = "From the confluence of the two source streams."
    cache.put(key, Segment(None, False, "numbers differ (added 2)", rejected=english))
    calls: list[str] = []
    t = Translator(TranslationConfig(), cache, FakeServer(), complete=lambda p, n: calls.append(p) or "")  # type: ignore[arg-type]
    try:
        (seg,) = t.translate("de", [source])
        assert (seg.ok, seg.text, seg.cached) == (True, english, True)
        assert calls == []
        old = TranslationCache.key(model, "de", "Alt.")
        cache.put(old, Segment(None, False, "an old failure"))  # no text kept: asked again
        t.translate("de", ["Alt."])
        assert len(calls) == 1
    finally:
        t.close()


def test_an_orphaned_server_is_stopped_and_a_dead_one_is_started_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-26 review #24: after an API crash, the old llama-server held the GPU and every request
    was refused as 'GPU busy'. #26: a server that died while ready was never started again."""
    started: list[int] = []

    def no_start(_self: LlamaServer) -> None:  # never a real server in a test
        started.append(1)

    monkeypatch.setattr(LlamaServer, "_start", no_start)
    model, binary = tmp_path / "m.gguf", tmp_path / "llama-server"
    model.write_bytes(b"gguf")
    binary.write_bytes(b"")
    cfg = TranslationConfig(model=str(model), server=str(binary), min_free_vram_mb=15500)
    free = [1000]
    ran: list[list[str]] = []

    def run(argv: Sequence[str]) -> int:
        ran.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "stop"]:
            free[0] = 23000  # the orphan's memory comes back
        return 0

    server = LlamaServer(cfg, log_path=tmp_path / "log", run=run, vram=lambda: free[0])
    with pytest.raises(TranslationUnavailableError) as starting:
        server.ensure()
    assert "starting" in str(starting.value)  # not "GPU busy"
    assert ran[0] == ["systemctl", "--user", "stop", "whisipedia-translate.scope"]

    class Dead:
        returncode = -11

        def poll(self) -> int:
            return -11

    restarted = LlamaServer(cfg, log_path=tmp_path / "log", run=lambda argv: 0, vram=lambda: 23000)
    restarted._state, restarted._proc = "ready", Dead()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    with pytest.raises(TranslationUnavailableError) as again:
        restarted.ensure()
    assert "starting" in str(again.value)
    assert len(started) == 2
