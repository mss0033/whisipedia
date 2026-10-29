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


def test_what_the_library_holds_is_never_asked_for_and_a_url_without_a_name_gets_one() -> None:
    from wsp_adapters.doccollection.adapter import Survey
    from wsp_adapters.doccollection.model import Candidate

    grid = Candidate(
        key="gebco:GEBCO_2024",
        url="https://www.bodc.ac.uk/data/open_download/gebco/gebco_2024/zip/",
        title="GEBCO 2024",
        issuer="BODC",
        pub_number="GEBCO_2024",
        extra={"suffix": ".zip"},
    )
    survey = Survey("gebco-grid", allowed=[grid])
    (item,) = manual._items_from_survey(survey, [], ".pdf")  # pyright: ignore[reportPrivateUsage]
    assert item.save_as == "GEBCO_2024.zip"
    assert item.cand["extra"]["save_as"] == "GEBCO_2024.zip"  # the import looks for this very name
    q = Queue(
        "geonames",
        items=[
            Item("geo:ALL", "u", "allCountries.zip", offered=["2026-10-24"]),
            Item("geo:NEW", "u2", "new.zip"),
        ],
    )
    assert manual.mark_held(q, {"geo:ALL": "2026-10-23r2"}) == 1
    assert [(i.status, i.taken) for i in q.items] == [("taken", "2026-10-23"), ("pending", "")]


# --- Kiwix checksums (owner decision 2026-10-26: mirrors, checked against Kiwix's own .sha256) -----


def _kiwix(sid: str, selector: dict[str, Any]) -> SourceSpec:
    return _spec(sid, {"adapter": "kiwix-opds", "selector": selector})


def test_kiwix_checksums_held_files_are_recorded_and_only_new_files_are_asked_for(tmp_path: Path) -> None:
    from wsp_adapters import kiwix
    from wsp_core.testing.adapter_contract import FakeFetcher

    kiwix._listings.clear()  # pyright: ignore[reportPrivateUsage]
    vault, seeded, saved = tmp_path / "vault", tmp_path / "state" / "kiwix-checksums", tmp_path / "manual"
    held = "wikipedia_ab_all_maxi_2026-07.zim"
    digest = "ab" * 32
    gen = vault / "library" / "encyclopedia" / "kiwix-ab" / "2026-07"
    gen.mkdir(parents=True)
    (gen / "MANIFEST.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": f"upstream/{held}",
                        "sha256": digest,
                        "upstream": [{"algorithm": "sha256", "hexdigest": digest}],
                    },
                    {"path": "upstream/other.zim", "sha256": "cd" * 32, "upstream": []},  # no Kiwix digest
                ]
            }
        )
    )
    specs = [_kiwix("kiwix-ab", {"name": "wikipedia_ab_all", "flavour": "maxi"})]
    assert manual.seed_kiwix_checksums(vault, specs, seeded) == 1
    assert (seeded / f"{held}.sha256").read_text() == f"{digest}  {held}\n"
    assert manual.seed_kiwix_checksums(vault, specs, seeded) == 0  # once

    base = "https://ftp.fau.de/kiwix/"
    listing = (
        '<a href="wikipedia_ab_all_maxi_2026-07.zim">x</a> <a href="wikipedia_ab_all_maxi_2026-10.zim">y</a>'
    )
    fetcher = FakeFetcher(
        {f"{base}zim/": b'<a href="wikipedia/">w</a>', f"{base}zim/wikipedia/": listing.encode()}
    )
    page = _spec(
        "kiwix-checksums", {"adapter": "kiwix-opds", "manual": {"list": {"kind": "kiwix-checksums"}}}
    )
    items, _, _ = manual.build_queue(
        page, saved, fetcher=fetcher, held=None, user_agent="t", workdir=tmp_path / "w", specs=specs,
        checksum_dirs=[seeded, saved],
    )  # fmt: skip
    (item,) = items
    assert item.id == "wikipedia_ab_all_maxi_2026-10.zim"
    assert item.open == "https://download.kiwix.org/zim/wikipedia/wikipedia_ab_all_maxi_2026-10.zim.sha256"
    assert item.save_as == "wikipedia_ab_all_maxi_2026-10.zim.sha256"

    # What the owner saves is taken only when it is that file's checksum.
    queue = Queue("kiwix-checksums", items=[item])
    saved.mkdir()
    (saved / item.save_as).write_text("<html>blocked</html>")
    manual.import_kiwix_checksums(page, queue, saved, io.StringIO())
    assert queue.items[0].status == "pending"
    assert (saved / f"{item.save_as}.bad").exists()
    (saved / item.save_as).write_text(f"{'ef' * 32}  {item.id}\n")
    manual.import_kiwix_checksums(page, queue, saved, io.StringIO())
    assert queue.items[0].status == "taken"
    items, _, _ = manual.build_queue(
        page, saved, fetcher=fetcher, held=None, user_agent="t", workdir=tmp_path / "w", specs=specs,
        checksum_dirs=[seeded, saved],
    )  # fmt: skip
    assert items == []  # nothing left to ask for


def _doc_item(key: str) -> Item:
    from wsp_adapters.doccollection.model import Candidate

    cand = Candidate(key=key, url=f"https://x.example/{key}.pdf", title=key.upper(), issuer="X")
    return Item(key, cand.url, f"{key.split(':')[1]}.pdf", cand=cand.to_json())


def test_held_and_denied_documents_leave_the_offer_and_a_held_one_returns_once_decided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-26 review: a document held for review was marked taken and never came back after the
    owner allowed it; a denied one headed the daily page forever."""
    from wsp_adapters.doccollection.model import Candidate
    from wsp_adapters.doccollection.policy import Rules
    from wsp_cli import doccollections

    incoming = tmp_path / "vault" / "_incoming"
    folder = incoming / "_manual" / "s"
    quarantine = incoming / "_quarantine" / "s"
    folder.mkdir(parents=True)
    quarantine.mkdir(parents=True)
    q = Queue("s", items=[_doc_item(k) for k in ("s:a", "s:b", "s:c")])
    for item in q.items:
        (folder / item.save_as).write_bytes(b"%PDF-1.4\n%%EOF\n")
    rules = Rules()
    listed: list[list[str]] = []

    def fetch(args: argparse.Namespace, out: Any) -> int:
        cands = [Candidate.from_json(c) for c in args.discovery[0]["items"]]
        listed.append([c.key for c in cands])
        report = doccollections.FetchReport("s", "2026-10-29")
        for c in cands:
            (folder / f"{c.key[2:]}.pdf").unlink(missing_ok=True)
            if c.key == "s:a" and not rules.allow_documents:  # the screen holds it until allowed
                (quarantine / c.file_name).write_bytes(b"%PDF")
                report.held.append({"key": c.key, "rule": "review:safety=x"})
            elif c.key == "s:b":
                report.rejected.append((c.key, "deny:marking=distribution-C"))
        args.report_sink.append(report)
        return doccollections.EXIT_COMPLETE

    class FakeAdapter:
        def config(self, _spec: SourceSpec) -> Any:
            return type("Cfg", (), {"rules": rules})()

    class FakeOps:
        @staticmethod
        def cmd_ingest(args: argparse.Namespace, out: Any) -> int:
            return 0

    monkeypatch.setattr(doccollections, "cmd_fetch", fetch)
    monkeypatch.setattr(manual, "DocCollectionAdapter", FakeAdapter)
    import wsp_cli

    monkeypatch.setattr(wsp_cli, "ops", FakeOps, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "wsp_cli.ops", FakeOps)
    spec = _spec("s", {"adapter": "doc-collection"})
    args = argparse.Namespace(config_dir=None)

    assert manual.import_collection(args, spec, q, folder, io.StringIO()) == 0
    assert {i.id: i.status for i in q.items} == {"s:a": "held", "s:b": "denied", "s:c": "taken"}
    assert q.pending() == []  # neither is offered again
    q.merge([_doc_item(k) for k in ("s:a", "s:b", "s:c")])
    assert {i.id: i.status for i in q.items} == {"s:a": "held", "s:b": "denied", "s:c": "taken"}

    assert manual.import_collection(args, spec, q, folder, io.StringIO()) == 0
    assert len(listed) == 1  # undecided: left alone

    rules = Rules(allow_documents=frozenset({"S:A"}))  # `wsp proposals accept` (keys normalized)
    assert manual.import_collection(args, spec, q, folder, io.StringIO()) == 0
    assert listed[-1] == ["s:a"]  # taken from the quarantine and screened again
    assert next(i for i in q.items if i.id == "s:a").status == "taken"

    # A copy the library already holds byte for byte: "nothing new" (8) is not a failure.
    q.items.append(_doc_item("s:d"))
    (folder / "d.pdf").write_bytes(b"%PDF")

    def same(args: argparse.Namespace, out: Any) -> int:
        (folder / "d.pdf").unlink()
        args.report_sink.append(doccollections.FetchReport("s", "2026-10-29"))
        return doccollections.EXIT_NOTHING_NEW

    monkeypatch.setattr(doccollections, "cmd_fetch", same)
    assert manual.import_collection(args, spec, q, folder, io.StringIO()) == 0
    assert q.items[-1].status == "taken"


def test_an_unfinished_ingest_is_retried_before_new_files_are_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-26 review: after a busy ingest the plan sat unsealed, every later fetch was refused
    (exit 5) and nothing retried the ingest; on the same day a second import rewrote its plan."""
    from wsp_adapters.doccollection.policy import Rules
    from wsp_cli import doccollections

    vault = tmp_path / "vault"
    incoming = vault / "_incoming"
    folder = incoming / "_manual" / "s"
    folder.mkdir(parents=True)
    plans = incoming / "_plans" / "targets.d"
    plans.mkdir(parents=True)
    (plans / "s-2026-10-28.json").write_text(json.dumps([{"source_id": "s", "version": "2026-10-28"}]))
    q = Queue("s", items=[_doc_item("s:new")])
    (folder / "new.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    spec = _spec("s", {"adapter": "doc-collection"})
    codes = [3]
    fetched: list[int] = []

    class FakeOps:
        @staticmethod
        def cmd_ingest(args: argparse.Namespace, out: Any) -> int:
            code = codes.pop(0) if codes else 0
            if code == 0:
                gen = vault / "library" / spec.vault_domain / "s" / "2026-10-28"
                gen.mkdir(parents=True, exist_ok=True)
                (gen / "MANIFEST.json").write_text("{}")
            return code

    def fetch(args: argparse.Namespace, out: Any) -> int:
        fetched.append(1)
        args.report_sink.append(doccollections.FetchReport("s", "2026-10-29"))
        (folder / "new.pdf").unlink()
        return doccollections.EXIT_COMPLETE

    class FakeAdapter:
        def config(self, _spec: SourceSpec) -> Any:
            return type("Cfg", (), {"rules": Rules()})()

    monkeypatch.setattr(doccollections, "cmd_fetch", fetch)
    monkeypatch.setattr(manual, "DocCollectionAdapter", FakeAdapter)
    import wsp_cli

    monkeypatch.setattr(wsp_cli, "ops", FakeOps, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "wsp_cli.ops", FakeOps)
    args = argparse.Namespace(config_dir=None)

    assert manual.import_collection(args, spec, q, folder, io.StringIO()) == 3  # still busy
    assert fetched == []
    assert (folder / "new.pdf").is_file()  # waits for the next run
    assert manual.import_collection(args, spec, q, folder, io.StringIO()) == 0
    assert fetched == [1]
    assert q.items[0].status == "taken"


def test_permission_requests_are_written_and_an_answer_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner request 2026-10-28: letters, drafts and a page for each permission request, in the manual
    folder; an answer saved there raises an alert."""
    from wsp_cli import bootstrap, notify

    raised: list[str] = []

    class Alerter:
        def raise_(self, alert: Any) -> bool:
            raised.append(alert.key)
            return True

        def resolve(self, key: str) -> None: ...

    class Store:
        def close(self) -> None: ...

    def alerter(_cfg: object) -> tuple[Alerter, Store]:
        return Alerter(), Store()

    monkeypatch.setattr(notify, "_alerter", alerter)
    lay = bootstrap.layout()
    root = tmp_path / "_manual"
    assert manual.sync_permissions(lay, {}, root, io.StringIO()) == 0
    perm = root / "_permissions"
    assert (perm / "PERMISSIONS.html").is_file()
    draft = (perm / "sphere-handbook" / "draft.eml").read_text()
    assert "To: info@spherestandards.org" in draft
    assert "X-Unsent: 1" in draft
    assert "@gmail" not in (perm / "PERMISSIONS.html").read_text()  # never the owner's address
    (perm / "sphere-handbook" / "reply.txt").write_text("Granted, with attribution.")
    assert manual.sync_permissions(lay, {}, root, io.StringIO()) == 0
    assert raised == ["permission:sphere-handbook"]
    status = json.loads((perm / "status.json").read_text())
    assert status["sphere-handbook"]["state"] == "answered"
    assert status["kiwix"]["state"] == "to send"
