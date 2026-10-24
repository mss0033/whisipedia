"""``/v1/translate``: English machine translation on the reader's request (ADR-0013)."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Mapping, Sequence
from types import SimpleNamespace

import httpx
import pytest

from wsp_api import ApiSettings, Services, create_app
from wsp_api.testing import local_client
from wsp_core.errors import TranslationUnavailableError


class FakeTranslator:
    def __init__(self) -> None:
        self.unavailable: TranslationUnavailableError | None = None
        self.closed = False

    def status(self) -> Mapping[str, object]:
        return {
            "available": True,
            "model": "MiLMMT-46-12B v1.0 (Q8_0)",
            "state": "stopped",
            "languages": ["de", "fr"],
            "prompt_version": "milmmt-tags-1",
            "unknown_key": "ignored",
        }

    def translate(self, lang: str, segments: Sequence[str]) -> Sequence[object]:
        if self.unavailable is not None:
            raise self.unavailable
        if lang not in ("de", "fr"):
            raise ValueError(f"no translation from {lang!r}")
        return [
            SimpleNamespace(text=None, ok=False, reason="numbers differ (missing 11)", cached=False)
            if "11" in s
            else SimpleNamespace(text=f"EN {s}", ok=True, reason=None, cached=True)
            for s in segments
        ]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake() -> FakeTranslator:
    return FakeTranslator()


@pytest.fixture
def tclient(services: Services, fake: FakeTranslator) -> Iterator[httpx.Client]:
    app = create_app(dataclasses.replace(services, translator=fake), ApiSettings())
    with local_client(app) as c:
        yield c


def test_status_names_the_model_and_languages(tclient: httpx.Client) -> None:
    body = tclient.get("/v1/translate").json()
    assert body["available"] is True
    assert body["model"] == "MiLMMT-46-12B v1.0 (Q8_0)"
    assert body["languages"] == ["de", "fr"]
    assert "unknown_key" not in body


def test_segments_come_back_translated_or_with_the_reason_they_are_not(tclient: httpx.Client) -> None:
    r = tclient.post(
        "/v1/translate", json={"lang": "de", "segments": ["Berlin ist groß.", "Elf: 11 Länder."]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "MiLMMT-46-12B v1.0 (Q8_0)"
    assert body["prompt_version"] == "milmmt-tags-1"
    assert body["segments"] == [
        {"text": "EN Berlin ist groß.", "ok": True, "reason": None, "cached": True},
        {"text": None, "ok": False, "reason": "numbers differ (missing 11)", "cached": False},
    ]


def test_busy_and_starting_answer_503_with_when_to_ask_again(
    tclient: httpx.Client, fake: FakeTranslator
) -> None:
    fake.unavailable = TranslationUnavailableError("the translator is starting", retry_after=10)
    r = tclient.post("/v1/translate", json={"lang": "de", "segments": ["Hallo."]})
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "10"
    assert r.json()["error"]["code"] == "translation_unavailable"
    fake.unavailable = TranslationUnavailableError("the translation model is not installed")
    r = tclient.post("/v1/translate", json={"lang": "de", "segments": ["Hallo."]})
    assert r.status_code == 503
    assert "Retry-After" not in r.headers


def test_bad_requests_are_refused(tclient: httpx.Client) -> None:
    assert tclient.post("/v1/translate", json={"lang": "xx", "segments": ["Hallo."]}).status_code == 400
    assert tclient.post("/v1/translate", json={"lang": "de", "segments": ["x" * 4001]}).status_code == 400
    too_many = {"lang": "de", "segments": ["a"] * 33}
    assert tclient.post("/v1/translate", json=too_many).status_code == 422


def test_without_a_translator_it_says_so(client: httpx.Client) -> None:
    body = client.get("/v1/translate").json()
    assert body["available"] is False
    assert body["reason"]
    assert client.post("/v1/translate", json={"lang": "de", "segments": ["Hallo."]}).status_code == 501


def test_the_translator_is_closed_with_the_api(services: Services, fake: FakeTranslator) -> None:
    app = create_app(dataclasses.replace(services, translator=fake), ApiSettings())
    app.state.library.close()  # what the app's lifespan does when the server stops
    assert fake.closed
