"""Export the OpenAPI 3.1 spec to ``docs/api/openapi.json``.

    uv run python -m wsp_api.openapi_export            # rewrite the committed spec
    uv run python -m wsp_api.openapi_export --check    # exit 1 if the committed spec is stale

The committed spec is the reviewed contract: a diff to it in a PR is the API change under review, and a
test fails if code and spec drift apart (PLAN §12.4, OpenAPI diff check).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from wsp_api.app import create_app
from wsp_api.testing.fakes import sample_services

SPEC_RELPATH = Path("docs/api/openapi.json")


def render_spec() -> str:
    # The spec depends only on routes and models, never on the injected services.
    spec = create_app(sample_services()).openapi()
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def repo_root(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for parent in (here, *here.parents):
        if (parent / "registry" / "sources.yaml").is_file() and (parent / "packages").is_dir():
            return parent
    raise FileNotFoundError("repository root not found (looked for registry/sources.yaml)")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m wsp_api.openapi_export", description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed spec is stale")
    parser.add_argument("--out", type=Path, default=None, help=f"default: <repo>/{SPEC_RELPATH}")
    args = parser.parse_args(argv)
    out: Path = args.out or repo_root() / SPEC_RELPATH
    rendered = render_spec()
    if args.check:
        current = out.read_text(encoding="utf-8") if out.is_file() else ""
        if current != rendered:
            print(f"{out} is stale; run: uv run python -m wsp_api.openapi_export", file=sys.stderr)
            return 1
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
