"""Machine translation on the reader's request (ADR-0013): every segment checked, cached, through an
on-demand llama.cpp server. The model is a fake here; the bake-off measured the real one
(docs/research/MT-BAKEOFF-2026-10-24.md)."""

# ruff: noqa: E501 - the parametrized cases read better unwrapped

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from wsp_core.errors import TranslationUnavailableError
from wsp_oracle.config import TranslationConfig
from wsp_oracle.mtcheck import check
from wsp_oracle.translation import LlamaServer, TranslationCache, Translator, prompt


@pytest.mark.parametrize(
    ("source", "target", "lang", "reason"),
    [
        # The bake-off's failures, each caught.
        ("Elf Länder unterzeichneten 1999.", "Twelve countries signed in 1999.", "de", "numbers differ"),
        ("Elf Länder unterzeichneten 1999.", "Eleven countries signed in 2000.", "de", "numbers differ"),
        ("東京の人口は約1400万人である。", "Tokyo has a population of about 1.4 million.", "ja", "numbers differ"),
        ("<x1>Berlin</x1> ist die Hauptstadt ⟦1⟧.", "Berlin is the capital ⟦1⟧.", "de", "links or formatting"),
        ("<x1>Berlin</x1> ist groß.", "</x1>Berlin<x1> is big.", "de", "out of order"),
        ("Der Vertrag wurde unterzeichnet. Er trat in Kraft. Er gilt bis heute.", "The treaty was signed.", "de",
         "much shorter"),
        ("La ville est située sur la rive droite du fleuve.", "La ville est située sur la rive droite du fleuve.",
         "fr", "not translated"),
        ("Москва — столица России и крупнейший город страны.", "Москва — столица России, the largest city.", "ru",
         "not translated"),
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
