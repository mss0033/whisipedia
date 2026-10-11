"""End to end on synthetic data: ``_incoming`` -> ``wsp ingest`` -> ``wsp index build`` -> API via the SDK.

Everything runs in ``tmp_path`` with the network disabled except loopback: the Kiwix ``.zim.sha256``
is served by an in-process loopback server, and the API runs in-process (no socket).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from e2e_fixtures import build_multistream, build_zim, disable_network

from wsp_api import create_app
from wsp_api.inprocess import InProcessTransport
from wsp_cli import bootstrap
from wsp_cli.main import main
from wsp_core.testing.fetcher_contract import RangeServer
from wsp_sdk import NotFoundError, WspClient
from wsp_vault.ledger import JsonlLedger

REPO = Path(__file__).resolve().parents[2]
ZIM = "wikipedia_en_all_maxi_2026-08.zim"
LONG_AGO = time.time() - 3600

REGISTRY = """\
schema_version: 1
sources:
  - id: wikipedia-en-zim
    name: Wikipedia English (ZIM, test)
    domain: [encyclopedia]
    tier: T1
    status: active
    license: CC-BY-SA-4.0
    legal_basis: open-license
    access: {adapter: kiwix-opds, selector: {name: wikipedia_en_all, flavour: maxi}, workers: 1}
    update: {strategy: snapshot-retention}
  - id: wikipedia-en
    name: Wikipedia English (dump, test)
    domain: [encyclopedia]
    tier: T1
    status: active
    license: CC-BY-SA-4.0
    legal_basis: open-license
    access: {adapter: wikimedia-dumps, wikis: [enwiki], workers: 1}
    update: {strategy: base-plus-incremental}
"""


def _cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), out=out)
    return code, out.getvalue()


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _age(*paths: Path) -> None:
    for p in paths:
        os.utime(p, (LONG_AGO, LONG_AGO))


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    attempts = disable_network(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("WSP_API_URL", raising=False)
    for name in [n for n in os.environ if n.startswith("WSP__")]:
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)

    vault = tmp_path / "vault"
    incoming = vault / "_incoming"
    zim = build_zim(
        incoming / "wikipedia-en-zim" / "2026-08" / ZIM,
        name="wikipedia_en_all",
        articles=[
            (
                "A/Alan_Turing",
                "Alan Turing",
                "<p>Alan Turing, <b>mathematician</b> and codebreaker.</p>",
            ),
            ("A/Enigma", "Enigma machine", "<p>The Enigma cipher machine was broken at Bletchley Park.</p>"),
        ],
        redirects=[("A/Turing", "Turing", "A/Alan_Turing")],
    )
    (zim.parent / (ZIM + ".meta4")).write_text("<metalink/>")  # sits next to downloads; never stored
    dump_dir = incoming / "wikipedia-en" / "20261001"
    xml, idx = build_multistream(
        dump_dir,
        "enwiki",
        "20261001",
        [
            ("Ada Lovelace", "'''Ada Lovelace''' wrote the first [[algorithm]] for a machine.", None),
            ("Lovelace", "", "Ada Lovelace"),
            ("Bletchley Park", "'''Bletchley Park''' was home to the codebreakers of [[Alan Turing]].", None),
        ],
    )
    partial_dir = incoming / "wikipedia-en" / "20261101"
    partial_dir.mkdir(parents=True)
    partial = partial_dir / "enwiki-20261101-pages-articles-multistream.xml.bz2"
    partial.write_bytes(b"half a download")
    (partial_dir / (partial.name + ".aria2")).write_bytes(b"aria2 control")
    _age(zim, xml, idx, partial)

    with RangeServer() as server:
        sha = hashlib.sha256(zim.read_bytes()).hexdigest()
        server.add(f"zim/wikipedia/{ZIM}.sha256", f"{sha}  {ZIM}\n".encode())
        sha1 = {p.name: hashlib.sha1(p.read_bytes()).hexdigest() for p in (xml, idx)}
        targets = [
            {
                "source_id": "wikipedia-en-zim",
                "version": "2026-08",
                "name": ZIM,
                "uris": [server.url(f"zim/wikipedia/{ZIM}.meta4")],
                "lane": "kiwix",
                "checksum": None,
                "size": zim.stat().st_size,
                "meta": {"kind": "metalink"},
            },
            *(
                {
                    "source_id": "wikipedia-en",
                    "version": "20261001",
                    "name": p.name,
                    "uris": [f"https://dumps.example.invalid/{p.name}"],
                    "lane": "wm-mirrors",
                    "checksum": ["sha-1", sha1[p.name]],
                    "size": p.stat().st_size,
                    "meta": {},
                }
                for p in (xml, idx)
            ),
            {
                "source_id": "wikipedia-en",
                "version": "20261101",
                "name": partial.name,
                "uris": [f"https://dumps.example.invalid/{partial.name}"],
                "lane": "wm-mirrors",
                "checksum": ["sha-1", "0" * 40],
                "size": 10_000,
                "meta": {},
            },
        ]
        (incoming / "_plans").mkdir()
        (incoming / "_plans" / "targets.json").write_text(json.dumps(targets))

        config = tmp_path / "config"
        config.mkdir()
        (tmp_path / "registry.yaml").write_text(REGISTRY)
        (config / "default.toml").write_text(
            f"""
[registry]
path = "{tmp_path / "registry.yaml"}"
schema = "{REPO / "registry" / "schema" / "source.schema.json"}"
[vault]
root = "{vault}"
[acquirer]
user_agent = "Whisipedia-e2e/0"
max_attempts = 2
backoff_base = 0.01
[index]
root = "{tmp_path / "index"}"
languages = ["en"]
[api]
backend = "real"
[orchestrator]
state_db = "{tmp_path / "state" / "orchestrator.sqlite3"}"
workdir = "{tmp_path / "work"}"
"""
        )
        yield {"tmp": tmp_path, "vault": vault, "config": config, "attempts": attempts, "zim": zim}


def test_ingest_index_and_serve(library: dict[str, Any]) -> None:
    vault: Path = library["vault"]
    cfg_dir = str(library["config"])

    # 1. A dry run changes nothing anywhere under the vault.
    before = _snapshot(vault)
    code, text = _cli("--config-dir", cfg_dir, "ingest", "--dry-run")
    assert code == 4, text  # 2026-11-01 is still downloading: "incomplete" is not success (EXIT_INCOMPLETE)
    assert "DRY RUN" in text
    assert _snapshot(vault) == before
    assert not (library["tmp"] / "xdg" / "whisipedia").exists(), "dry run must not create a signing key"

    # 2. Ingest adopts the complete versions and skips the one still downloading.
    code, raw = _cli("--config-dir", cfg_dir, "ingest", "--json")
    assert code == 4, raw  # one version still downloading
    report = {(v["source_id"], v["generation_id"]): v for v in json.loads(raw)["versions"]}
    assert report[("wikipedia-en-zim", "2026-08")]["status"] == "ingested"
    assert report[("wikipedia-en-zim", "2026-08")]["verifiable"] == 1  # Kiwix .sha256 via the adapter
    assert report[("wikipedia-en", "2026-10-01")]["status"] == "ingested"
    assert report[("wikipedia-en", "2026-11-01")]["status"] == "incomplete"

    lib = vault / "library" / "encyclopedia"
    manifest = json.loads((lib / "wikipedia-en-zim" / "2026-08" / "MANIFEST.json").read_text())
    (entry,) = manifest["files"]
    assert entry["path"] == f"upstream/{ZIM}"
    assert entry["upstream"] == [{"algorithm": "sha256", "hexdigest": entry["sha256"]}]
    assert (lib / "wikipedia-en-zim" / "CURRENT.txt").read_text() == "2026-08\n"
    assert (lib / "wikipedia-en" / "CURRENT.txt").read_text() == "2026-10-01\n"
    assert not library["zim"].exists(), "adopted by rename"
    assert (library["zim"].parent / (ZIM + ".meta4")).exists(), "unplanned files are left alone"
    partial = vault / "_incoming/wikipedia-en/20261101/enwiki-20261101-pages-articles-multistream.xml.bz2"
    assert partial.read_bytes() == b"half a download"
    assert (vault / "README.txt").is_file()
    assert "wikipedia-en-zim" in (vault / "CATALOG.csv").read_text()
    assert list((library["tmp"] / "xdg" / "whisipedia").glob("*.pem")), "key generated outside the repo"

    lay = bootstrap.layout(Path(cfg_dir))
    cfg = bootstrap.load(lay)
    ledger = JsonlLedger(vault / "ledger", bootstrap.load_signer(cfg, lay))
    assert ledger.verify()
    kinds = [e.kind for e in ledger.events()]
    assert kinds.count("vault.adopt") == 3
    assert kinds.count("ingest.adopted") == 2

    # 3. Idempotent: a second run changes nothing.
    snap = _snapshot(vault)
    code, raw = _cli("--config-dir", cfg_dir, "ingest", "--json")
    assert code == 4  # 2026-11-01 is still incomplete
    statuses = {v["generation_id"]: v["status"] for v in json.loads(raw)["versions"]}
    assert statuses == {
        "2026-08": "already-sealed",
        "2026-10-01": "already-sealed",
        "2026-11-01": "incomplete",
    }
    assert _snapshot(vault) == snap

    # 4. Refine + index on local disk; a rerun is a no-op.
    code, text = _cli("--config-dir", cfg_dir, "index", "build")
    assert code == 0, text
    assert "wikipedia-en-zim: refined 2026-08 (2 docs)" in text
    assert "wikipedia-en: refined 2026-10-01 (2 docs)" in text
    assert "rank: " in text  # PageRank and the spelling vocabulary are the last steps of a build
    assert "spell: " in text
    lexical = library["tmp"] / "index" / "lexical"
    assert (lexical / "static_rank.bin").is_file()
    assert (lexical / "spelling.bin").is_file()
    code, text = _cli("--config-dir", cfg_dir, "index", "build")
    assert "nothing new" in text
    code, text = _cli("--config-dir", cfg_dir, "index", "rank")  # re-runnable on its own
    assert code == 0, text
    assert "nodes" in text
    code, text = _cli("--config-dir", cfg_dir, "index", "spell")
    assert code == 0, text

    # 5. The API (in-process) serves search, suggest, documents and the system view from real backends.
    services = bootstrap.build_services(cfg, lay)
    app = create_app(services, bootstrap.api_settings(cfg, lay))
    http = httpx.Client(transport=InProcessTransport(app), base_url="http://127.0.0.1")
    with WspClient(http=http) as wsp:
        hits = wsp.search("codebreaker").hits
        assert {h.doc_id for h in hits} >= {
            "wsp:wikipedia-en-zim/A/Alan_Turing",
            "wsp:wikipedia-en/Bletchley_Park",
        }
        assert wsp.search("codebreeker").did_you_mean == "codebreaker"
        doc = wsp.document("wsp:wikipedia-en/Ada_Lovelace")
        assert "first algorithm" in doc.text
        assert doc.license == "CC-BY-SA-4.0"
        assert [v.generation_id for v in wsp.iter_history("wsp:wikipedia-en-zim/A/Enigma")] == ["2026-08"]
        # Redirects become aliases: the prefix matches the alias, the suggestion is the article title.
        assert "Alan Turing" in wsp.suggest("tur").suggestions  # ZIM redirect "Turing"
        assert "Ada Lovelace" in wsp.suggest("love").suggestions  # dump redirect "Lovelace"
        turing = wsp.document("wsp:wikipedia-en-zim/A/Alan_Turing")
        assert turing.format == "html"  # original HTML straight from the sealed ZIM (ZimAssetStore)
        assert turing.html is not None
        assert "codebreaker" in turing.html
        wsp.report_gap("https://en.wikipedia.org/wiki/Colossus", from_doc_id=turing.doc_id)
        gaps = (library["tmp"] / "index" / "gaps.jsonl").read_text().splitlines()
        assert [json.loads(g)["href"] for g in gaps] == ["https://en.wikipedia.org/wiki/Colossus"]
        with pytest.raises(NotFoundError):
            wsp.document("wsp:wikipedia-en/Nope")
        assert {c.doc_id for c in wsp.changes().items} >= {"wsp:wikipedia-en/Ada_Lovelace"}
        system = wsp.system()
        assert system.ledger_ok
        assert {f.source_id: f.current_generation for f in system.freshness} == {
            "wikipedia-en-zim": "2026-08",
            "wikipedia-en": "2026-10-01",
        }
    services.index.close()
    services.changes.close()

    # 6. Status sees both sources; nothing reached beyond loopback.
    code, text = _cli("--config-dir", cfg_dir, "status")
    assert code == 0, text
    assert "wikipedia-en-zim" in text
    assert "degraded: none" in text
    assert library["attempts"] == []
