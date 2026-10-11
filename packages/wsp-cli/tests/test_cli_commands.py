import io
import json
from pathlib import Path
from typing import Any

import pytest

from wsp_cli import bootstrap
from wsp_cli.main import main

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _in_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("WSP_API_URL", raising=False)
    monkeypatch.delenv("WSP_CONFIG_DIR", raising=False)


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


def test_search_human_and_json() -> None:
    code, text = run("search", "turing", "computer")
    assert code == 0
    assert "Alan Turing" in text
    assert "wsp://wikipedia-en-zim/A/Alan_Turing" in text
    code, raw = run("search", "http", "--source", "rfc", "--json")
    assert code == 0
    assert {h["source_id"] for h in json.loads(raw)["hits"]} == {"rfc"}


def test_search_no_results() -> None:
    assert "No results" in run("search", "zzzzqqq")[1]


def test_doc_and_at(capsys: pytest.CaptureFixture[str]) -> None:
    code, text = run("doc", "wsp:rfc/9110")
    assert code == 0
    assert text.startswith("RFC 9110: HTTP Semantics\n")
    code, raw = run("doc", "wsp:wikipedia-en-zim/A/Alan_Turing", "--at", "2026-09-01", "--json")
    assert "father" not in json.loads(raw)["text"]
    assert run("doc", "wsp:rfc/0")[0] == 1
    assert "document_not_found" in capsys.readouterr().err


def test_sources_uses_registry() -> None:
    code, text = run("sources")
    assert code == 0
    assert "wikidata" in text
    code, raw = run("sources", "--json")
    assert any(s["id"] == "rfc" for s in json.loads(raw))


def test_changes_and_cursor(capsys: pytest.CaptureFixture[str]) -> None:
    code, text = run("changes", "--limit", "2")
    assert code == 0
    assert text.strip().endswith("cursor: 2")
    code, raw = run("changes", "--since", "4", "--json")
    assert [c["kind"] for c in json.loads(raw)["items"]] == ["modified"]
    assert run("changes", "--follow")[0] == 2
    assert "--url" in capsys.readouterr().err


def test_registry_validate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, text = run("registry", "validate")
    assert code == 0
    assert text.startswith("ok: ")
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nsources: [{id: x}]\n")
    assert run("registry", "validate", "--registry", str(bad))[0] == 1
    assert "invalid" in capsys.readouterr().err


def test_config_show_redacts_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "default.toml").write_text(
        '[vault]\nroot = "/srv/lib"\nsigning_key = "k"\n[alerts]\nntfy_token = "t"\napi_key = "a"\n'
    )
    monkeypatch.setenv("WSP__ALERTS__PASSWORD", "p")
    code, raw = run("--config-dir", str(tmp_path), "config", "show")
    cfg: dict[str, Any] = json.loads(raw)
    assert code == 0
    assert cfg["vault"] == {"root": "/srv/lib", "signing_key": "***"}
    assert cfg["alerts"] == {"ntfy_token": "***", "api_key": "***", "password": "***"}


def test_redact_keeps_non_secrets() -> None:
    assert bootstrap.redact({"index": {"root": "x", "monkey": 1}}) == {"index": {"root": "x", "monkey": 1}}


def test_serve_refuses_non_loopback(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("serve", "--host", "0.0.0.0")[0] == 2
    assert "--allow-remote" in capsys.readouterr().err


def test_serve_runs_uvicorn_on_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    code, text = run("serve", "--port", "9999", "--static-dir", str(tmp_path))
    assert code == 0
    assert calls == [
        {
            "host": "127.0.0.1",
            "port": 9999,
            "server_header": False,
            "proxy_headers": False,
            "log_level": "info",
            "access_log": False,  # failures and slow requests only (wsp_api.serving.SlowRequestLog)
        }
    ]
    assert str(tmp_path) in text


def test_unreachable_url_is_reported(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def refuse(*_: object, **__: object) -> None:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.Client, "request", refuse)
    assert run("--url", "http://127.0.0.1:1", "sources")[0] == 1
    assert "wsp serve" in capsys.readouterr().err


def test_layout_outside_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    lay = bootstrap.layout()
    assert lay.root is None
    services = bootstrap.build_services({}, lay)
    assert {s.id for s in services.sources} == {"wikipedia-en-zim", "rfc", "gutenberg"}
