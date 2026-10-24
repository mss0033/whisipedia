"""TEI texts of git-hosted collections: FRUS documents and Perseus translations (2026-10-17)."""

# ruff: noqa: RUF001, E501 - FRUS titles carry en dashes; TEI fixtures read better unwrapped

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from wsp_adapters.doccollection.adapter import CollectionConfig
from wsp_adapters.doccollection.tei import cts_work, frus_volume, read_snapshot, rst_page
from wsp_core.model import SourceSpec, SourceStatus, Tier

FRUS = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0" xmlns:frus="http://history.state.gov/frus/ns/1.0">
<teiHeader><fileDesc><titleStmt>
<title type="complete">Foreign Relations of the United States, 1969–1976, Volume I, Foundations of
Foreign Policy, 1969–1972</title></titleStmt></fileDesc></teiHeader>
<text><body><div type="chapter" xml:id="ch1"><head>Chapter 1</head>
<div type="document" xml:id="d1" n="1" frus:doc-dateTime-min="1967-07-29T00:00:00-05:00">
<head>1. Editorial Note<note n="1" type="source">Source: Nixon Library.</note></head>
<p>The intellectual assumptions<note n="2">A footnote.</note> of the <persName>Nixon</persName>
administration.</p><p>A second paragraph.</p></div>
<div type="document" xml:id="d2" n="2"><head>2. Address by Richard M. Nixon</head>
<dateline>San Francisco, July 29, 1967.</dateline><p>My fellow Bohemians.</p></div>
</div></body></text></TEI>"""

CTS = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
<teiHeader xml:lang="eng"><fileDesc><titleStmt><title>The Roman History: The Civil Wars</title>
<author>Appian of Alexandria</author><editor role="translator">Horace White</editor></titleStmt>
</fileDesc></teiHeader>
<text><body><div type="translation" n="urn:cts:greekLit:tlg0551.tlg017.perseus-eng2">
<div type="textpart" subtype="book" n="1"><head>BOOK I</head>
<div type="textpart" subtype="chapter" n="intro"><p>THE plebeians and Senate of Rome.</p></div>
<div type="textpart" subtype="chapter" n="9"><div type="textpart" subtype="section" n="1">
<p>a law was once passed<pb n="17"/> that nobody should hold more than 500 jugera.</p></div></div>
</div>
<div type="textpart" subtype="book" n="2"><div type="textpart" subtype="chapter" n="1"><p>Caesar.</p></div></div>
</div></body></text></TEI>"""


def test_a_frus_volume_is_its_documents() -> None:
    docs = list(frus_volume(FRUS.encode(), "frus1969-76v01"))
    assert [d.key for d in docs] == ["frus1969-76v01/d1", "frus1969-76v01/d2"]
    first = docs[0]
    assert first.title == "Editorial Note (FRUS 1969–1976, Volume I, Document 1)"  # no source note
    assert first.date == "1967-07-29"
    assert first.url == "https://history.state.gov/historicaldocuments/frus1969-76v01/d1"
    assert "The intellectual assumptions of the Nixon administration.\n\nA second paragraph." in first.text
    assert "footnote" not in first.text
    assert first.text.startswith("Foreign Relations of the United States, 1969–1976, Volume I")
    assert docs[1].date == ""
    assert "San Francisco, July 29, 1967.\n\nMy fellow Bohemians." in docs[1].text


def test_a_cts_translation_is_its_books_with_chapter_numbers() -> None:
    docs = list(cts_work(CTS.encode(), "tlg0551.tlg017.perseus-eng2"))
    assert [d.key for d in docs] == ["tlg0551.tlg017.perseus-eng2/1", "tlg0551.tlg017.perseus-eng2/2"]
    book = docs[0]
    assert book.title == "Appian of Alexandria, The Roman History: The Civil Wars, Book 1 (tr. Horace White)"
    assert "THE plebeians and Senate of Rome." in book.text  # "intro" is no number
    assert "9. a law was once passed that nobody should hold more than 500 jugera." in book.text
    assert "1. Caesar." in docs[1].text


def test_a_snapshot_is_read_by_its_reader(tmp_path: Path) -> None:
    zpath = tmp_path / "frus.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("frus-master/volumes/frus1969-76v01.xml", FRUS)
        z.writestr("frus-master/volumes/broken.xml", "<TEI>")  # a malformed file is skipped
        z.writestr("frus-master/README.md", "public domain")
        z.writestr("canonical/data/tlg0551/tlg017/tlg0551.tlg017.perseus-eng2.xml", CTS)
        z.writestr("canonical/data/tlg0551/tlg017/tlg0551.tlg017.perseus-grc2.xml", CTS)
    assert [d.key for d in read_snapshot(zpath, "tei-frus")] == ["frus1969-76v01/d1", "frus1969-76v01/d2"]
    keys = [d.key for d in read_snapshot(zpath, "tei-cts")]
    assert keys == ["tlg0551.tlg017.perseus-eng2/1", "tlg0551.tlg017.perseus-eng2/2"]  # English only


def test_an_unknown_reader_is_refused() -> None:
    def spec(reader: str) -> SourceSpec:
        raw = {"id": "t", "access": {"adapter": "doc-collection", "reader": reader}}
        return SourceSpec(
            id="t",
            name="T",
            tier=Tier.T1,
            status=SourceStatus.CANDIDATE,
            license="x",
            legal_basis="public-domain",
            domain=("x",),
            adapter="doc-collection",
            raw=raw,
        )

    assert CollectionConfig.from_spec(spec("tei-frus")).reader == "tei-frus"
    with pytest.raises(ValueError, match=r"access\.reader"):
        CollectionConfig.from_spec(spec("tei-unknown"))


RST = """.. SPDX-License-Identifier: GPL-2.0

.. _cgroup-v2:

================
Control Group v2
================

:Date: October, 2015

memory.max
	A read-write single value file which exists on non-root
	cgroups.  The default is "max".

	Memory usage hard limit.
"""


def test_a_kernel_documentation_page_is_titled_by_its_title() -> None:
    (doc,) = rst_page(RST.encode(), "admin-guide/cgroup-v2")
    assert doc.title == "Control Group v2"
    assert doc.url == "https://docs.kernel.org/admin-guide/cgroup-v2.html"
    assert "SPDX" not in doc.text
    assert "=====" not in doc.text
    assert "memory.max\n\tA read-write single value file" in doc.text


def test_kernel_translations_are_left_out(tmp_path: Path) -> None:
    zpath = tmp_path / "linux.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("Documentation/admin-guide/cgroup-v2.rst", RST)
        z.writestr("Documentation/translations/zh_CN/admin-guide/cgroup-v2.rst", RST)
        z.writestr("kernel/cgroup/cgroup.c", "int x;")
    assert [d.key for d in read_snapshot(zpath, "rst-kernel")] == ["admin-guide/cgroup-v2"]


TCP = """<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc><titleStmt>
<title>The brides ornaments viz. fiue meditations</title><author>Aylett, Robert, 1583-1655?</author>
</titleStmt><sourceDesc><biblFull><publicationStmt><pubPlace>London :</pubPlace><date>1625.</date>
</publicationStmt></biblFull></sourceDesc></fileDesc></teiHeader>
<text><front><div><head>TO THE READER</head><p>Gentle reader<note>A marginal note.</note>, behold.</p></div></front>
<body><div><pb n="1"/><head>KNOWLEDGE</head><l>Of all the gifts</l><l>that heauen doth bestow,</l></div></body></text></TEI>"""


def test_a_tcp_book_is_one_document_stored_under_its_key_or_published_name(tmp_path: Path) -> None:
    stored = tmp_path / "tcp__A00002-1a2b3c4d.xml"  # the vault's name for the file
    stored.write_text(TCP)
    for name in (None, "A00002.xml"):
        (doc,) = list(read_snapshot(stored, "tei-tcp", name))
        assert doc.key == "A00002"
        assert doc.title == "The brides ornaments viz. fiue meditations (Aylett, Robert, 1583-1655?, 1625)"
        assert (
            doc.text
            == "TO THE READER\n\nGentle reader, behold.\n\nKNOWLEDGE\n\nOf all the gifts\n\nthat heauen doth bestow,"
        )
        assert doc.url == "https://github.com/textcreationpartnership/A00002"
