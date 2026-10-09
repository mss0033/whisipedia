import json
from pathlib import Path

import httpx

from wsp_api.openapi_export import SPEC_RELPATH, main, render_spec, repo_root

ROOT = Path(__file__).resolve().parents[3]


def test_committed_spec_is_current() -> None:
    """Breaking-change guard: API code and the reviewed spec must not drift apart.

    If this fails, run ``uv run python -m wsp_api.openapi_export`` and review the diff of
    docs/api/openapi.json. Removing or renaming anything there is a breaking change (ADR required).
    """
    committed = (ROOT / SPEC_RELPATH).read_text(encoding="utf-8")
    assert committed == render_spec(), "docs/api/openapi.json is stale; run: python -m wsp_api.openapi_export"


def test_spec_is_openapi_31_and_served(client: httpx.Client) -> None:
    spec = json.loads(render_spec())
    assert spec["openapi"].startswith("3.1")
    assert client.get("/v1/openapi.json").json() == spec


def test_every_operation_documents_the_error_envelope() -> None:
    spec = json.loads(render_spec())
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            errors = {k: v for k, v in op["responses"].items() if k[0] in "45"}
            for status, resp in errors.items():
                ref = resp["content"]["application/json"]["schema"]["$ref"]
                assert ref.endswith("/ErrorResponse"), (method, path, status)
    assert "HTTPValidationError" not in spec["components"]["schemas"]


def test_check_mode(tmp_path: Path) -> None:
    out = tmp_path / "openapi.json"
    assert main(["--check", "--out", str(out)]) == 1
    assert main(["--out", str(out)]) == 0
    assert main(["--check", "--out", str(out)]) == 0


def test_repo_root_is_found() -> None:
    assert repo_root() == ROOT
