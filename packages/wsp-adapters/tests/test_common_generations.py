"""Generation chains and HEAD-identified releases (audit 2026-10-11 C1, H3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from wsp_adapters._common import (
    IncompleteGenerationError,
    earlier_generations,
    files_across_generations,
    generation_order_key,
    head_release,
    head_version,
    plan_by_head,
)
from wsp_core.model import Artifact, Generation, GenerationStatus, ReleaseRef, RemoteHead


def _gen(lib: Path, gen_id: str, sealed: str | None = None, **files: bytes) -> Path:
    up = lib / gen_id / "upstream"
    up.mkdir(parents=True)
    for name, data in files.items():
        (up / name.replace("_", ".")).write_bytes(data)
    if sealed is not None:
        (lib / gen_id / "MANIFEST.json").write_text(json.dumps({"sealed_at": sealed}))
    return lib / gen_id


def test_order_is_date_then_seal_time_never_the_digest(tmp_path: Path) -> None:
    lib = tmp_path / "src"
    # "12ab34cd" must not read as day 12 of the month; the seal time orders same-month releases.
    late = _gen(lib, "2026-10-12ab34cd", "2026-10-20T00:00:00")
    early = _gen(lib, "2026-10-ffff0000", "2026-10-05T00:00:00")
    older = _gen(lib, "2026-09", "2026-09-30T00:00:00")
    newer = _gen(lib, "2026-11-00000000", "2026-11-02T00:00:00")
    order = sorted([late, early, older, newer], key=generation_order_key)
    assert [p.name for p in order] == [older.name, early.name, late.name, newer.name]
    assert earlier_generations(late) == [older, early]
    (lib / "CURRENT.txt").write_text(late.name)  # not a generation
    (lib / "stray").mkdir()  # no upstream/: not a generation
    assert earlier_generations(newer) == [older, early, late]


def test_files_across_generations_prefers_the_newest_copy(tmp_path: Path) -> None:
    lib = tmp_path / "src"
    _gen(lib, "2026-09-01+aaaaaaaa", "2026-09-01T00:00:00", a_txt=b"a1", b_txt=b"b1")
    _gen(lib, "2026-09-15+bbbbbbbb", "2026-09-15T00:00:00", a_txt=b"a2")
    me = _gen(lib, "2026-10-01+cccccccc", "2026-10-01T00:00:00", c_txt=b"c3")
    got = files_across_generations(me, ["a.txt", "b.txt", "c.txt"])
    assert {n: p.read_bytes() for n, p in got.items()} == {"a.txt": b"a2", "b.txt": b"b1", "c.txt": b"c3"}
    with pytest.raises(IncompleteGenerationError, match=r"d\.txt"):
        files_across_generations(me, ["a.txt", "d.txt"])


class _HeadFetcher:
    def __init__(self, heads: dict[str, RemoteHead]) -> None:
        self.heads = heads

    def get_bytes(self, url: str, *, max_bytes: int = 0) -> bytes:
        raise NotImplementedError

    def download(self, remote: object, dest: Path) -> object:
        raise NotImplementedError

    def head(self, url: str) -> RemoteHead:
        return self.heads[url]


def _head(size: int, day: int, etag: str = "") -> RemoteHead:
    return RemoteHead(
        url="u", size=size, etag=etag or None, last_modified=datetime(2026, 10, day, tzinfo=UTC)
    )


def test_head_version_is_stable_for_the_same_bytes_and_changes_with_any_file() -> None:
    urls = [("https://x/a", "a"), ("https://x/b", "b")]
    v1 = head_version(
        *head_release(_HeadFetcher({"https://x/a": _head(5, 1), "https://x/b": _head(7, 3)}), urls)
    )
    v1again = head_version(
        *head_release(_HeadFetcher({"https://x/a": _head(5, 1), "https://x/b": _head(7, 3)}), urls)
    )
    v2 = head_version(
        *head_release(_HeadFetcher({"https://x/a": _head(6, 1), "https://x/b": _head(7, 3)}), urls)
    )
    v3 = head_version(
        *head_release(_HeadFetcher({"https://x/a": _head(5, 1, '"e2"'), "https://x/b": _head(7, 3)}), urls)
    )
    assert v1 == v1again
    assert v1 is not None
    assert v1.startswith("2026-10-03+")
    assert len({v1, v2, v3}) == 3
    assert head_version(*head_release(object(), urls)) is None  # no HEAD: nothing to identify


def test_plan_by_head_skips_only_files_unchanged_since_the_previous_seal() -> None:
    files, meta = head_release(
        _HeadFetcher(
            {
                "https://x/big": _head(11_000, 1),  # same size, modified before the seal: skip
                "https://x/cat": _head(50, 9),  # modified after the seal: fetch
                "https://x/new": _head(10, 1),  # not held: fetch
                "https://x/grew": _head(12, 1),  # size changed: fetch
            }
        ),
        [
            ("https://x/big", "big"),
            ("https://x/cat", "cat"),
            ("https://x/new", "new"),
            ("https://x/grew", "grew"),
        ],
    )
    want = ReleaseRef("s", "v", None, tuple(files), meta=meta)

    def art(name: str, size: int) -> Artifact:
        return Artifact("s", "old", PurePosixPath("upstream") / name, size, "0" * 64, "")

    have = Generation(
        "s",
        "old",
        GenerationStatus.SEALED,
        artifacts=(art("big", 11_000), art("cat", 50), art("grew", 11)),
        sealed_at=datetime(2026, 10, 5, tzinfo=UTC),
    )
    assert [f.name for f in plan_by_head(have, want).files] == ["cat", "new", "grew"]
    assert [f.name for f in plan_by_head(None, want).files] == ["big", "cat", "new", "grew"]
