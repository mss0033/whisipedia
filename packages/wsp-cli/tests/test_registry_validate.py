"""`wsp registry validate`: the schema, and that every source an entry names is defined."""

from __future__ import annotations

import argparse
import io

from wsp_cli.main import _source_refs, cmd_registry_validate  # pyright: ignore[reportPrivateUsage]


def test_the_committed_registry_validates() -> None:
    out = io.StringIO()
    assert cmd_registry_validate(argparse.Namespace(config_dir=None, registry=None, schema=None), out) == 0
    assert out.getvalue().startswith("ok: ")


def test_source_references_are_found_wherever_the_access_block_names_them() -> None:
    """2026-10-26 review #137: wikisource-en named kiwix-wikisource-en-all, defined only in the
    git-ignored overlay; nothing checked such references."""
    access = {
        "adapter": "wikimedia-dumps",
        "rendition": [{"source": "kiwix-wikisource-en-all"}],
        "copies": [{"source": "x", "match": "native"}],
        "discovery": [{"kind": "api"}],
    }
    assert sorted(_source_refs(access)) == ["kiwix-wikisource-en-all", "x"]
    assert _source_refs(None) == []
