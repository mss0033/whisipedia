"""The owner's daily downloads by hand (wsp_cli.manual, owner request 2026-10-24): hosts whose robots.txt
disallows crawlers, or whose bot wall refuses a plain client, are never asked for anything; the owner
saves ten files a day per source and the library takes them in."""

from __future__ import annotations

import argparse
import gzip
import io
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from wsp_cli import manual
from wsp_cli.manual import Item, Queue, SavedPages, render_today, todays_offer
from wsp_core.errors import NotFoundError
from wsp_core.model import SourceSpec, SourceStatus, Tier


def _spec(sid: str, access: dict[str, Any]) -> SourceSpec:
    raw = {"id": sid, "name": sid.title(), "access": access}
    return SourceSpec(
        id=sid,
        name=sid.title(),
        tier=Tier.T1,
        status=SourceStatus.PAUSED,
        adapter=str(access.get("adapter")),
        domain=("reference",),
        license="LicenseRef-US-Gov-PD",
        legal_basis="public-domain",
        raw=raw,
    )


def test_a_rebuilt_list_keeps_each_items_state_and_taken_files_come_back_when_due() -> None:
    q = Queue(
        "s",
        items=[
            Item("a", "u/a", "a.pdf", status="taken", taken="2026-01-01"),
            Item("b", "u/b", "b.pdf", offered=["2026-10-23"]),
        ],
    )
    q.merge([Item("b", "u/b2", "b.pdf"), Item("c", "u/c", "c.pdf")])
    assert [(i.id, i.status, i.offered) for i in q.items] == [
        ("b", "pending", ["2026-10-23"]),
        ("c", "pending", []),
        ("a", "taken", []),  # no longer listed, but taken: kept
    ]
    q.renew(date(2026, 10, 24), 90)
    assert next(i for i in q.items if i.id == "a").status == "pending"  # taken in January: due again


def test_the_days_offer_repeats_what_was_not_received_then_takes_new_items_by_priority() -> None:
    q = Queue("s", items=[Item(f"i{n}", f"u{n}", f"f{n}.pdf", title=f"Title {n}") for n in range(30)])
    q.items[25].title = "UFC 3-520-01 Interior Electrical Systems"
    q.prioritise(["^UFC "])
    first = todays_offer(q, 10, "2026-10-24")
    assert first[0].id == "i25"
    assert len(first) == 10
    for item in first[:4]:
        item.status = "taken"
    second = todays_offer(q, 10, "2026-10-25")
    assert [i.id for i in second[:6]] == [i.id for i in first[4:]]  # offered again first
    assert all(i.offered == ["2026-10-24", "2026-10-25"] for i in second[:6])
    assert len(second) == 10
    q.save_pages = [("https://x/listing", "listing.html")]
    assert len(todays_offer(q, 10, "2026-10-26")) == 9  # a page to save counts as one of the ten


def test_todays_page_names_each_file_and_folder_and_escapes_titles() -> None:
    spec = _spec("wbdg-criteria", {"adapter": "doc-collection"})
    q = Queue(
        "wbdg-criteria",
        items=[
            Item("k", "https://b/x.pdf", "x.pdf", "UFC <3-600-01>", offered=["d1", "d2"], problem="not a PDF")
        ],
    )
    page, text = render_today(
        "2026-10-24", [(spec, q, q.items, r"\\nas\share\_manual\wbdg-criteria", 1)], r"\\nas\share\_manual"
    )
    assert "UFC &lt;3-600-01&gt;" in page
    assert "<code>x.pdf</code>" in page
    assert "offered 2 times" in page
    assert "last try: not a PDF" in page
    assert r"\\nas\share\_manual\wbdg-criteria" in text
    assert "https://b/x.pdf" in text


def test_a_curated_list_becomes_items_with_their_save_names(tmp_path: Path) -> None:
    access = {
        "adapter": "doc-collection",
        "key_prefix": "icrc",
        "issuer": "ICRC",
        "manual": {
            "list": {
                "kind": "list",
                "items": [
                    {"title": "War Surgery, volume 1", "url": "https://library.icrc.org/a/0973.pdf"},
                    {
                        "title": "First Aid in Armed Conflicts",
                        "landing": "https://shop.icrc.org/first-aid",
                        "save_as": "first-aid.pdf",
                    },
                ],
            }
        },
    }
    items, pages, _notes = manual.build_queue(
        _spec("icrc", access), tmp_path, fetcher=None, held=None, user_agent="ua", workdir=tmp_path
    )
    assert pages == []
    assert [(i.id, i.open, i.save_as, i.page) for i in items] == [
        ("icrc:0973", "https://library.icrc.org/a/0973.pdf", "0973.pdf", False),
        ("icrc:first-aid", "https://shop.icrc.org/first-aid", "first-aid.pdf", True),
    ]
    assert items[1].cand["extra"] == {"save_as": "first-aid.pdf"}


def test_a_saved_listing_is_read_from_the_owners_file_and_a_missing_page_is_asked_for(tmp_path: Path) -> None:
    access = {
        "adapter": "doc-collection",
        "key_prefix": "pubchem",
        "issuer": "NCBI",
        "origins": ["ftp.ncbi.nlm.nih.gov"],
        "partitioned": True,
        "filter": {"allow_all": True, "data_files": True},
        "discovery": [
            {
                "kind": "index-page",
                "partition": "extras",
                "url": "https://ftp.ncbi.nlm.nih.gov/x/Extras/",
                "documents": r"/Extras/CID-[^/]+(?<!\.md5)$",
            },
            {
                "kind": "index-page",
                "partition": "sdf",
                "url": "https://ftp.ncbi.nlm.nih.gov/x/SDF/",
                "documents": r"\.sdf\.gz$",
            },
        ],
        "manual": {
            "list": {
                "kind": "saved",
                "pages": [
                    {"url": "https://ftp.ncbi.nlm.nih.gov/x/Extras/", "save_as": "extras.html"},
                    {"url": "https://ftp.ncbi.nlm.nih.gov/x/SDF/", "save_as": "sdf.html"},
                ],
            }
        },
    }
    listing = tmp_path / manual.LISTING_DIR
    listing.mkdir()
    (listing / "extras.html").write_text(
        '<a href="CID-LCSS.xml.gz">CID-LCSS.xml.gz</a> <a href="CID-LCSS.xml.gz.md5">m</a>'
    )
    items, pages, _ = manual.build_queue(
        _spec("pubchem", access),
        tmp_path,
        fetcher=None,
        held=None,
        user_agent="Whisipedia/0.1",
        workdir=tmp_path / "w",
    )
    assert [(i.save_as, i.cand["partition"]) for i in items] == [("CID-LCSS.xml.gz", "extras")]
    assert pages == [("https://ftp.ncbi.nlm.nih.gov/x/SDF/", "sdf.html")]
    with pytest.raises(NotFoundError):
        SavedPages(listing, {}).get_bytes("https://ftp.ncbi.nlm.nih.gov/robots.txt")


def test_pubmed_offers_the_numbers_after_the_newest_sealed_generation(tmp_path: Path) -> None:
    items = manual._pubmed_items("pubmed26-u1682", 3)  # pyright: ignore[reportPrivateUsage]
    assert [i.save_as for i in items] == [
        "pubmed26n1683.xml.gz",
        "pubmed26n1684.xml.gz",
        "pubmed26n1685.xml.gz",
    ]
    assert items[0].open == "https://ftp.ncbi.nlm.nih.gov/pubmed/updatefiles/pubmed26n1683.xml.gz"
    assert manual._pubmed_items("pubmed26", 3) == []  # pyright: ignore[reportPrivateUsage]


def _gz(text: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as z:
        z.write(text)
    return buf.getvalue()


def test_a_file_is_taken_only_when_it_reads_whole(tmp_path: Path) -> None:
    good = tmp_path / "a.xml.gz"
    good.write_bytes(_gz(b"<PubmedArticleSet/>"))
    cut = tmp_path / "b.xml.gz"
    cut.write_bytes(_gz(b"x" * 100000)[:-30])
    page = tmp_path / "c.pdf"
    page.write_bytes(b"<!DOCTYPE html><html>Access denied</html>")
    fake = tmp_path / "d.zip"
    fake.write_bytes(b"<html>login</html>")
    check = manual._check  # pyright: ignore[reportPrivateUsage]
    assert check(good) is None
    assert "does not read whole" in str(check(cut))
    assert "not a PDF" in str(check(page))
    assert check(fake) is not None


def test_pubmed_update_files_become_a_generation_of_the_unbroken_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    folder = tmp_path / "manual"
    folder.mkdir()
    for n in (1683, 1684, 1686):  # 1685 missing: 1686 waits for it
        (folder / f"pubmed26n{n}.xml.gz").write_bytes(_gz(b"<PubmedArticleSet/>"))
    calls: list[tuple[str, str | None]] = []

    class FakeOps:
        @staticmethod
        def cmd_ingest(args: argparse.Namespace, out: Any) -> int:
            calls.append((args.source, args.version))
            assert args.wait == manual.INGEST_WAIT
            return 0

    import wsp_cli

    monkeypatch.setattr(wsp_cli, "ops", FakeOps, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "wsp_cli.ops", FakeOps)
    q = Queue("pubmed", items=manual._pubmed_items("pubmed26-u1682", 4))  # pyright: ignore[reportPrivateUsage]
    out = io.StringIO()
    code = manual.import_pubmed(
        argparse.Namespace(config_dir=None),
        _spec("pubmed", {"adapter": "pubmed"}),
        q,
        folder,
        "pubmed26-u1682",
        vault,
        out,
    )
    assert code == 0
    assert calls == [("pubmed", "pubmed26-u1684")]
    dest = vault / "_incoming" / "pubmed" / "pubmed26-u1684"
    assert sorted(p.name for p in dest.iterdir()) == ["pubmed26n1683.xml.gz", "pubmed26n1684.xml.gz"]
    plan = json.loads(
        (vault / "_incoming" / "_plans" / "targets.d" / "pubmed-pubmed26-u1684.json").read_text()
    )
    assert [t["name"] for t in plan] == ["pubmed26n1683.xml.gz", "pubmed26n1684.xml.gz"]
    assert all(t["checksum"][0] == "sha-256" and t["meta"]["manual"] for t in plan)
    assert (folder / "pubmed26n1686.xml.gz").is_file()  # after the gap: left for a later run
    assert [i.status for i in q.items] == ["taken", "taken", "pending", "pending"]


def test_the_newest_sealed_pubmed_generation_is_found_current_or_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "library" / "medicine" / "pubmed"
    for gen in ("pubmed26", "pubmed26-u1671", "pubmed26-u1682", "pubmed26-u1690"):
        (lib / gen).mkdir(parents=True)
        if gen != "pubmed26-u1690":  # not sealed
            (lib / gen / "MANIFEST.json").write_text("{}")

    class Paths:
        @staticmethod
        def require_vault() -> Path:
            return tmp_path

    from wsp_cli import bootstrap

    def paths(cfg: object) -> type[Paths]:
        return Paths

    monkeypatch.setattr(bootstrap, "paths", paths)
    spec = _spec("pubmed", {"adapter": "pubmed"})
    monkeypatch.setattr(SourceSpec, "vault_domain", property(lambda self: "medicine"))
    assert manual.held_generation({}, spec) == "pubmed26-u1682"
