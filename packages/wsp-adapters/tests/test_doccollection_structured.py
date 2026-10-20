"""Catalogues published as data, read entry by entry (doccollection.structured): ATT&CK, CWE, CAPEC,
the SP 800-53 OSCAL catalog and the MedlinePlus health topics (owner request 2026-10-18)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from wsp_adapters.doccollection.tei import read_snapshot


def _attack() -> bytes:
    def ref(aid: str, kind: str) -> list[dict[str, str]]:
        return [
            {
                "source_name": "mitre-attack",
                "external_id": aid,
                "url": f"https://attack.mitre.org/{kind}/{aid}",
            }
        ]

    objects = [
        {
            "type": "attack-pattern",
            "id": "attack-pattern--1",
            "name": "Phishing",
            "description": "Adversaries may send phishing messages.(Citation: Report) See [CISA](https://cisa.gov).",
            "kill_chain_phases": [{"kill_chain_name": "mitre-attack", "phase_name": "initial-access"}],
            "x_mitre_platforms": ["Windows", "Linux"],
            "x_mitre_detection": "Monitor for suspicious email attachments.",
            "external_references": ref("T1566", "techniques"),
            "modified": "2026-09-18T00:00:00Z",
        },
        {
            "type": "course-of-action",
            "id": "course-of-action--1",
            "name": "User Training",
            "description": "Train users to spot phishing.",
            "external_references": ref("M1017", "mitigations"),
        },
        {
            "type": "attack-pattern",
            "id": "attack-pattern--old",
            "name": "Gone",
            "revoked": True,
            "external_references": ref("T9999", "techniques"),
        },
        {
            "type": "relationship",
            "relationship_type": "mitigates",
            "source_ref": "course-of-action--1",
            "target_ref": "attack-pattern--1",
        },
    ]
    return json.dumps({"type": "bundle", "objects": objects}).encode()


def test_attack_techniques_carry_tactics_detection_and_mitigations(tmp_path: Path) -> None:
    snap = tmp_path / "attack.zip"
    with zipfile.ZipFile(snap, "w") as z:
        z.writestr("attack-stix-data-master/enterprise-attack/enterprise-attack.json", _attack())
        z.writestr("attack-stix-data-master/enterprise-attack/enterprise-attack-16.0.json", _attack())  # old
    docs = {d.key: d for d in read_snapshot(snap, "mitre")}
    assert sorted(docs) == ["attack/M1017", "attack/T1566"]  # revoked left out, one bundle read
    t = docs["attack/T1566"]
    assert t.title == "T1566: Phishing (ATT&CK technique)"
    assert "Tactics: Initial Access" in t.text
    assert "Platforms: Windows, Linux" in t.text
    assert "Citation" not in t.text
    assert "See CISA." in t.text
    assert "Detection\nMonitor for suspicious email attachments." in t.text
    assert "Mitigations\nM1017 User Training" in t.text
    assert "Techniques it addresses\nT1566 Phishing" in docs["attack/M1017"].text
    assert (t.url, t.date) == ("https://attack.mitre.org/techniques/T1566", "2026-09-18")


CWE = b"""<?xml version="1.0"?><Weakness_Catalog xmlns="http://cwe.mitre.org/cwe-7"><Weaknesses>
<Weakness ID="79" Name="Cross-site Scripting" Abstraction="Base" Status="Stable">
 <Description>The product does not neutralize input.</Description>
 <Common_Consequences><Consequence><Scope>Integrity</Scope>
  <Impact>Execute Unauthorized Code</Impact></Consequence></Common_Consequences>
 <Potential_Mitigations><Mitigation><Phase>Implementation</Phase>
  <Description>Encode all output.</Description></Mitigation></Potential_Mitigations>
</Weakness>
<Weakness ID="1" Name="Old" Abstraction="Base" Status="Deprecated"><Description>x</Description></Weakness>
</Weaknesses></Weakness_Catalog>"""

CAPEC = b"""<?xml version="1.0"?><Attack_Pattern_Catalog xmlns="http://capec.mitre.org/capec-3"><Attack_Patterns>
<Attack_Pattern ID="66" Name="SQL Injection" Abstraction="Standard" Status="Draft">
 <Description>Input becomes part of a query.</Description>
 <Likelihood_Of_Attack>High</Likelihood_Of_Attack>
 <Prerequisites><Prerequisite>Queries built from user input.</Prerequisite></Prerequisites>
 <Mitigations><Mitigation>Use parameterised queries.</Mitigation></Mitigations>
 <Related_Weaknesses><Related_Weakness CWE_ID="89"/></Related_Weaknesses>
</Attack_Pattern></Attack_Patterns></Attack_Pattern_Catalog>"""


def test_cwe_in_its_zip_and_capec_as_one_file(tmp_path: Path) -> None:
    cwe = tmp_path / "cwec_latest.xml.zip"
    with zipfile.ZipFile(cwe, "w") as z:
        z.writestr("cwec_v4.20.xml", CWE)
    (w,) = read_snapshot(cwe, "mitre")  # the deprecated weakness is left out
    assert (w.key, w.title) == ("cwe/CWE-79", "CWE-79: Cross-site Scripting")
    assert "Consequences\n- Integrity: Execute Unauthorized Code" in w.text
    assert "Mitigations\n- Implementation: Encode all output." in w.text
    capec = tmp_path / "capec_latest.xml"
    capec.write_bytes(CAPEC)
    (c,) = read_snapshot(capec, "mitre")
    assert c.key == "capec/CAPEC-66"
    assert c.url == "https://capec.mitre.org/data/definitions/66.html"
    assert "Likelihood: High" in c.text
    assert "- Use parameterised queries." in c.text
    assert "Related weaknesses\nCWE-89" in c.text


def test_oscal_controls_print_their_parameters(tmp_path: Path) -> None:
    catalog = {
        "catalog": {
            "groups": [
                {
                    "id": "ac",
                    "title": "Access Control",
                    "controls": [
                        {
                            "id": "ac-2",
                            "title": "Account Management",
                            "props": [
                                {"name": "label", "value": "AC-2"},
                                {"name": "label", "value": "AC-02", "class": "sp800-53a"},
                            ],
                            "params": [{"id": "ac-2_prm_1", "label": "time period"}],
                            "parts": [
                                {
                                    "name": "statement",
                                    "parts": [
                                        {
                                            "name": "item",
                                            "props": [{"name": "label", "value": "a."}],
                                            "prose": "Review accounts every {{ insert: param, ac-2_prm_1 }};",
                                        }
                                    ],
                                },
                                {"name": "guidance", "prose": "Accounts include individual and shared ones."},
                            ],
                            "links": [{"rel": "related", "href": "#ac-3"}],
                            "controls": [
                                {
                                    "id": "ac-2.1",
                                    "title": "Automated System Account Management",
                                    "props": [{"name": "label", "value": "AC-2(1)"}],
                                    "parts": [{"name": "statement", "prose": "Use automation."}],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    }
    path = tmp_path / "NIST_SP-800-53_rev5_catalog.json"
    path.write_text(json.dumps(catalog))
    docs = {d.key: d for d in read_snapshot(path, "oscal-catalog")}
    assert sorted(docs) == ["800-53r5/ac-2", "800-53r5/ac-2.1"]
    ac2 = docs["800-53r5/ac-2"]
    assert ac2.title == "AC-2 Account Management (NIST SP 800-53 Rev. 5)"
    assert "a. Review accounts every [Assignment: time period];" in ac2.text
    assert "Discussion\nAccounts include individual and shared ones." in ac2.text
    assert "Related controls\nAC-3" in ac2.text
    assert docs["800-53r5/ac-2.1"].title.startswith("AC-2(1) ")


def test_medlineplus_topics_in_both_languages(tmp_path: Path) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<health-topics total="2" date-generated="10/17/2026 02:30:35">
<health-topic title="A1C" url="https://medlineplus.gov/a1c.html" id="6308" language="English">
<also-called>HbA1C</also-called>
<full-summary>&lt;p&gt;A1C is a blood test.&lt;/p&gt;
&lt;ul&gt;&lt;li&gt;Normal is below 5.7%&lt;/li&gt;&lt;/ul&gt;</full-summary>
<group url="https://medlineplus.gov/diagnostictests.html" id="25">Diagnostic Tests</group>
</health-topic>
<health-topic title="Prueba A1C" url="https://medlineplus.gov/spanish/a1c.html" id="6309" language="Spanish">
<full-summary>&lt;p&gt;Es un análisis de sangre.&lt;/p&gt;</full-summary>
</health-topic>
</health-topics>"""
    path = tmp_path / "mplus_topics_2026-10-17.xml"
    path.write_text(xml, encoding="utf-8")
    en, es = read_snapshot(path, "medlineplus-topics")
    assert (en.key, en.lang, en.date) == ("topic/6308", "en", "2026-10-17")
    assert "Also called: HbA1C" in en.text
    assert "Normal is below 5.7%" in en.text
    assert "&lt;" not in en.text
    assert "Groups\nDiagnostic Tests" in en.text
    assert (es.lang, es.title) == ("es", "Prueba A1C")


def test_a_catalogue_stored_under_its_document_key_is_read_by_its_published_name(tmp_path: Path) -> None:
    """The vault stores the file as medlineplus__MPLUS_TOPICS-2353a4ac.xml, a name no reader knows:
    MedlinePlus was indexed with no documents (2026-10-19). Its catalog record's URL names it."""
    from adapters_testkit import make_ctx, make_spec

    from wsp_adapters.doccollection.adapter import CATALOG, DocCollectionAdapter
    from wsp_adapters.doccollection.catalog import write_catalog
    from wsp_adapters.doccollection.model import DocumentMeta
    from wsp_core.testing.adapter_contract import FakeFetcher

    gen = tmp_path / "2026-10-19"
    (gen / "upstream").mkdir(parents=True)
    (gen / "upstream" / "medlineplus__MPLUS_TOPICS-2353a4ac.xml").write_text(
        '<health-topics date-generated="10/17/2026 02:30:35">'
        '<health-topic title="A1C" url="https://medlineplus.gov/a1c.html" id="6308" language="English">'
        "<full-summary>A blood test.</full-summary></health-topic></health-topics>",
        encoding="utf-8",
    )
    meta = DocumentMeta(
        doc_key="medlineplus:MPLUS_TOPICS",
        file="upstream/medlineplus__MPLUS_TOPICS-2353a4ac.xml",
        issuer="National Library of Medicine",
        title="MedlinePlus health topics",
        origin_url="https://medlineplus.gov/xml/mplus_topics_2026-10-17.xml",
    )
    write_catalog(gen / CATALOG, [meta])
    spec = make_spec(
        "medlineplus",
        {
            "adapter": "doc-collection",
            "issuer": "National Library of Medicine",
            "key_prefix": "medlineplus",
            "origins": ["medlineplus.gov"],
            "discovery": [
                {"kind": "list", "urls": ["https://medlineplus.gov/xml/mplus_topics_2026-10-17.xml"]}
            ],
            "filter": {"allow_all": True},
            "reader": "medlineplus-topics",
        },
        license_="LicenseRef-US-Gov-PD",
    )
    (doc,) = DocCollectionAdapter(throttle=False).documents(
        make_ctx(spec, FakeFetcher(), tmp_path / "w"), gen
    )
    assert (doc.native_id, doc.title) == ("medlineplus:MPLUS_TOPICS/topic/6308", "A1C")


def test_files_a_reader_does_not_know_are_skipped(tmp_path: Path) -> None:
    other = tmp_path / "README.json"
    other.write_text("{}")
    assert list(read_snapshot(other, "mitre")) == []


SE_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
 <dc:identifier id="uid">https://standardebooks.org/ebooks/jane-austen/pride-and-prejudice</dc:identifier>
 <dc:date>2014-05-25T00:00:00Z</dc:date><dc:title id="title">Pride and Prejudice</dc:title>
 <dc:language>en-GB</dc:language><dc:creator id="author">Jane Austen</dc:creator>
</metadata>
<manifest>
 <item href="text/titlepage.xhtml" id="titlepage.xhtml" media-type="application/xhtml+xml"/>
 <item href="text/chapter-1.xhtml" id="chapter-1.xhtml" media-type="application/xhtml+xml"/>
 <item href="text/chapter-2.xhtml" id="chapter-2.xhtml" media-type="application/xhtml+xml"/>
 <item href="text/colophon.xhtml" id="colophon.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine><itemref idref="titlepage.xhtml"/><itemref idref="chapter-2.xhtml"/><itemref idref="chapter-1.xhtml"/>
<itemref idref="colophon.xhtml"/></spine></package>"""


def _se_page(title: str, paragraphs: list[str], kind: str = "chapter") -> str:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        f'<head><title>{title}</title></head><body epub:type="bodymatter">'
        f'<section epub:type="{kind}"><h2>{title}</h2>{body}</section></body></html>'
    )


def test_a_standard_ebooks_repository_is_one_book_in_reading_order(tmp_path: Path) -> None:
    snap = tmp_path / "se.zip"
    root = "jane-austen_pride-and-prejudice-ed94a32/src/epub"
    with zipfile.ZipFile(snap, "w") as z:
        z.writestr(f"{root}/content.opf", SE_OPF)
        z.writestr(
            f"{root}/text/titlepage.xhtml", _se_page("Titlepage", ["Pride and Prejudice"], "titlepage")
        )
        z.writestr(
            f"{root}/text/chapter-1.xhtml",
            _se_page("I", ["It is a truth universally acknowledged.", "However little known."]),
        )
        z.writestr(f"{root}/text/chapter-2.xhtml", _se_page("II", ["Mr. Bennet was among the earliest."]))
        z.writestr(
            f"{root}/text/colophon.xhtml", _se_page("Colophon", ["Typeset by Standard Ebooks."], "colophon")
        )
    (book,) = read_snapshot(snap, "standard-ebooks")
    assert (book.key, book.title, book.lang, book.date) == ("book", "Pride and Prejudice", "en", "2014-05-25")
    assert book.url == "https://standardebooks.org/ebooks/jane-austen/pride-and-prejudice"
    assert book.text.startswith(
        "Pride and Prejudice\n\nby Jane Austen\n\nChapter II\n\nMr. Bennet"
    )  # spine order
    assert "Chapter I\n\nIt is a truth universally acknowledged.\n\nHowever little known." in book.text
    assert "Titlepage" not in book.text
    assert "Typeset by" not in book.text  # colophon left out


def test_a_repositorys_markdown_docs_are_its_pages() -> None:
    """Meshtastic's site is Docusaurus Markdown (owner request 2026-10-18): a page each, its title from
    the front matter or first heading, JSX and imports dropped, translations and the blog left out."""
    import io
    import zipfile

    from wsp_adapters.doccollection.structured import markdown_docs

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "meshtastic-master/README.md", "# Meshtastic docs\n\nThe documentation website of the project.\n"
        )
        z.writestr(
            "meshtastic-master/docs/configuration/radio/lora.mdx",
            "---\ntitle: LoRa Configuration\nsidebar_label: LoRa\n---\nimport Tabs from '@theme/Tabs';\n\n"
            "<Tabs>\nThe LoRa config sets the region and modem preset.\n:::note\nSet the region first.\n:::\n"
            "</Tabs>\n```bash\nmeshtastic --set lora.region US\n```\n",
        )
        z.writestr(
            "meshtastic-master/i18n/de/docs/lora.mdx",
            "# LoRa\n\nDeutsch: die LoRa-Konfiguration ist wichtig.",
        )
        z.writestr(
            "meshtastic-master/blog/2024-news.md", "# News\n\nA blog post about a release, long enough."
        )
    with zipfile.ZipFile(buf) as z:
        docs = {d.key: d for d in markdown_docs(z)}
    assert sorted(docs) == ["README", "docs/configuration/radio/lora"]
    lora = docs["docs/configuration/radio/lora"]
    assert lora.title == "LoRa Configuration"
    assert "import Tabs" not in lora.text
    assert "<Tabs>" not in lora.text
    assert "Note:" in lora.text
    assert "meshtastic --set lora.region US" in lora.text
    assert docs["README"].title == "Meshtastic docs"
