"""How ``wsp_cli.bootstrap`` wires the API (audit 2026-10-11 #9, #11, #13, #14, #18)."""

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from wsp_api.feed import ReadOnlyChangeFeed
from wsp_cli import bootstrap
from wsp_cli.doctor import Check, Level, exit_code
from wsp_cli.main import main
from wsp_cli.monitor import CachedProposals
from wsp_core.errors import InvalidCursorError
from wsp_core.ids import make_doc_id
from wsp_core.model import ChangeKind, ChangeRecord, DocRecord
from wsp_index import ParquetDocumentStore, SqliteChangeFeed
from wsp_index.refinery import RefineryWriter


def _changes(n: int, start: int = 0) -> list[ChangeRecord]:
    return [
        ChangeRecord(make_doc_id("rfc", str(i)), ChangeKind.ADDED, "g1", None, f"h{i}")
        for i in range(start, start + n)
    ]


def test_read_only_feed_matches_the_writer_and_never_writes(tmp_path: Path) -> None:
    db = tmp_path / "changes.sqlite"
    writer = SqliteChangeFeed(db)
    writer.publish(_changes(30))
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    feed = ReadOnlyChangeFeed(db)
    assert feed.since(None, limit=10) == writer.since(None, limit=10)
    assert feed.head() == writer.head()
    assert feed.tail(5, sources=["rfc"]) == writer.tail(5, sources=["rfc"])
    cursor = feed.since(None, limit=10)[1]
    assert feed.since(cursor, limit=100) == writer.since(cursor, limit=100)  # same cursors
    writer.publish(_changes(5, 30))
    assert len(feed.since(cursor, limit=100)[0]) == 25  # sees the writer's new rows
    with pytest.raises(InvalidCursorError):
        feed.since("deadbeef.0000000000000001")
    with pytest.raises(PermissionError):
        feed.publish(_changes(1))
    with pytest.raises(Exception, match="readonly"):
        feed._db().execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")  # pyright: ignore[reportPrivateUsage]
    feed.close()
    writer.close()
    assert (
        before["changes.sqlite"]
        <= {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}["changes.sqlite"]
    )


def test_read_only_feed_reads_from_many_threads_at_once(tmp_path: Path) -> None:
    writer = SqliteChangeFeed(tmp_path / "changes.sqlite")
    writer.publish(_changes(500))
    feed = ReadOnlyChangeFeed(tmp_path / "changes.sqlite")
    errors: list[BaseException] = []
    sizes: list[int] = []

    def read() -> None:
        try:
            cursor: str | None = None
            total = 0
            while True:
                records, cursor = feed.since(cursor, limit=50)
                if not records:
                    break
                total += len(records)
            sizes.append(total)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sizes == [500] * 16
    feed.close()
    writer.close()


def test_projected_reads_skip_the_text(tmp_path: Path) -> None:
    root = tmp_path / "refinery"
    docs = [
        DocRecord(make_doc_id("src", f"n{i}"), "src", f"n{i}", f"Title {i}", "en", "CC0", "x" * 10_000)
        for i in range(20)
    ]
    with RefineryWriter(root, "src", "g1") as w:
        w.write(docs)
    store = bootstrap.RefineryDocuments(ParquetDocumentStore(root))
    row = store.fields(make_doc_id("src", "n3"), ("title", "license"))
    assert row == {"doc_id": "wsp:src/n3", "title": "Title 3", "license": "CC0"}
    assert store.fields(make_doc_id("src", "nope"), ()) is None
    assert store.fields(make_doc_id("other", "n3"), ()) is None
    reader = store.store.reader("src", "g1")
    assert reader is not None
    cached_columns = {key[2] for key in reader._cache}  # pyright: ignore[reportPrivateUsage]
    assert all("text" not in cols for cols in cached_columns)
    assert store.get(make_doc_id("src", "n3")) is not None


def test_api_services_create_no_signing_key_and_read_the_feed_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    vault = tmp_path / "vault"
    (vault / "library").mkdir(parents=True)
    index = tmp_path / "index"
    index.mkdir()
    SqliteChangeFeed(index / "changes.sqlite").close()
    cfg: dict[str, Any] = {
        "api": {"backend": "real"},
        "vault": {"root": str(vault)},
        "index": {"root": str(index)},
        "oracle": {"enabled": False, "spelling": False},
    }
    lay = bootstrap.layout()
    services = bootstrap.build_services(cfg, lay)
    try:
        assert not (tmp_path / "xdg" / "whisipedia" / bootstrap.SIGNING_KEY_NAME).exists()  # audit #14
        assert isinstance(services.changes, ReadOnlyChangeFeed)  # audit #13
        assert services.assets is None  # the API reads ZIMs through its snapshot and worker pool
        assert hasattr(services.documents, "fields")
    finally:
        services.changes.close()
        services.index.close()
    settings = bootstrap.serve_settings(cfg, lay)
    assert settings.background
    assert settings.zim_workers == 2
    assert settings.cache_dir == tmp_path / "cache" / "api"
    assert not bootstrap.api_settings(cfg, lay).background  # the in-process CLI starts no threads


def test_bad_api_numbers_are_config_errors() -> None:
    with pytest.raises(bootstrap.ConfigError, match="zim_workers"):
        bootstrap.serve_settings({"api": {"zim_workers": "two"}}, bootstrap.layout())


def test_doctor_require_typos_and_warnings_fail() -> None:
    ok = Check("vault", "Vault", Level.OK, "")
    warn = Check("par2", "par2", Level.WARN, "")
    assert exit_code([ok, warn]) == 0  # without --require only failures count
    assert exit_code([ok], ["vault"]) == 0
    assert exit_code([ok, warn], ["vault", "par2"]) == 1  # a required check must be OK
    assert exit_code([ok], ["vualt"]) == 2  # a typo is not silently "all good"


def test_doctor_reports_unknown_check_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    code = main(["--config-dir", str(cfg), "doctor", "--require", "vualt"])
    assert code == 2
    out = capsys.readouterr().out
    assert "no such check" in out
    assert "vault" in out


def test_proposals_are_parsed_once_per_change(tmp_path: Path) -> None:
    root = tmp_path / "proposals"
    root.mkdir()
    for i in range(3):
        (root / f"{i:05d}-x.yaml").write_text(yaml.safe_dump({"id": i, "status": "open"}), encoding="utf-8")

    def files() -> list[Path]:
        return sorted(root.glob("*.yaml"))

    cached = CachedProposals(files)
    assert [d["id"] for d in cached()] == [0, 1, 2]
    assert [d["id"] for d in cached()] == [0, 1, 2]
    assert cached.parsed == 3
    path = root / "00001-x.yaml"
    # a different size: the (mtime, size) stamp changes even within one mtime tick
    path.write_text(yaml.safe_dump({"id": 1, "status": "accepted", "note": "longer"}), encoding="utf-8")
    (root / "00002-x.yaml").unlink()
    assert [(d["id"], d["status"]) for d in cached()] == [(0, "open"), (1, "accepted")]
    assert cached.parsed == 4


def test_cli_commands_do_not_import_the_server_stack() -> None:
    """``wsp search --url ...`` must start fast (audit #11): no FastAPI app, no index, no operator code."""
    code = (
        "import sys, wsp_cli.main as m; m.build_parser(['search', 'x']); "
        "print(','.join(x for x in ('wsp_api.app', 'wsp_index', 'wsp_cli.ops', 'wsp_cli.doccollections', "
        "'fastapi', 'wsp_vault.drill') if x in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
