from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import pytest
import zstandard
from adapters_testkit import make_ctx, make_spec

from wsp_adapters import available_adapters
from wsp_adapters._streams import Throttle
from wsp_adapters.arxiv import (
    GCS_API,
    GCS_MEDIA,
    OAI_URL,
    ArxivAdapter,
    arxiv_id_of,
    harvest,
    parse_page,
    parse_record,
)
from wsp_core.model import Generation, GenerationStatus
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

SPEC = make_spec("arxiv", {"adapter": "arxiv", "pdf_months": 1}, license_="LicenseRef-per-item")
TODAY = date(2026, 10, 10)


def record(arxiv_id: str, title: str, *, stamp: str = "2026-10-08", deleted: bool = False) -> str:
    if deleted:
        return (
            f'<record><header status="deleted"><identifier>oai:arXiv.org:{arxiv_id}</identifier>'
            f"<datestamp>{stamp}</datestamp></header></record>"
        )
    return f"""<record><header><identifier>oai:arXiv.org:{arxiv_id}</identifier><datestamp>{stamp}</datestamp>
<setSpec>cs</setSpec></header><metadata>
<arXiv xmlns="http://arxiv.org/OAI/arXiv/"><id>{arxiv_id}</id><created>2026-10-01</created>
<authors><author><keyname>Lovelace</keyname><forenames>Ada</forenames></author>
<author><keyname>Babbage</keyname><forenames>Charles</forenames><suffix>Jr</suffix></author></authors>
<title>{title}</title><categories>cs.LG math.ST</categories><comments>12 pages</comments>
<doi>10.1000/test.{arxiv_id.replace("/", "-")}</doi>
<license>http://creativecommons.org/licenses/by/4.0/</license>
<abstract>  We study
  engines.  </abstract></arXiv></metadata></record>"""


def page(*records: str, token: str | None = None) -> bytes:
    tok = f"<resumptionToken>{token}</resumptionToken>" if token is not None else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        f"<ListRecords>{''.join(records)}{tok}</ListRecords></OAI-PMH>"
    ).encode()


PDF = b"%PDF-1.4 fixture"


def routes() -> dict[str, bytes]:
    md5 = base64.b64encode(hashlib.md5(PDF).digest()).decode()
    months = {"prefix": "arxiv/arxiv/pdf/", "delimiter": "/", "fields": "prefixes,nextPageToken"}
    month = {
        "prefix": "arxiv/arxiv/pdf/2609/",
        "maxResults": "1000",
        "fields": "items(name,size,md5Hash),nextPageToken",
    }
    return {
        OAI_URL: page(record("2609.00001", "Analytical Engines")),
        f"{GCS_API}?{urlencode(months)}": json.dumps(
            {"prefixes": ["arxiv/arxiv/pdf/2608/", "arxiv/arxiv/pdf/2609/"]}
        ).encode(),
        f"{GCS_API}?{urlencode(month)}": json.dumps(
            {
                "items": [
                    {"name": "arxiv/arxiv/pdf/2609/2609.00001v1.pdf", "size": str(len(PDF)), "md5Hash": md5},
                    {"name": "arxiv/arxiv/pdf/2609/notes.txt", "size": "1", "md5Hash": md5},
                ]
            }
        ).encode(),
        f"{GCS_MEDIA}arxiv/arxiv/pdf/2609/2609.00001v1.pdf": PDF,
    }


def _harvest_file(path: Path, *pages: bytes) -> None:
    lines = "".join(json.dumps({"url": "u", "xml": p.decode()}) + "\n" for p in pages)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zstandard.ZstdCompressor().compress(lines.encode()))


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """Two metadata generations (full harvest, then an increment) and one PDF part generation."""
    src = tmp_path / "library" / "science" / "arxiv"
    _harvest_file(
        src / "2026-09-10" / "upstream" / "oai-arXiv-all-2026-09-09.jsonl.zst",
        page(record("hep-th/9901001", "Strings"), record("2301.01234", "Old title"), token="x"),
        page(record("2301.09999", "Withdrawn later")),
    )
    _harvest_file(
        src / "2026-10-10" / "upstream" / "oai-arXiv-2026-09-09-2026-10-09.jsonl.zst",
        page(record("2301.01234", "New title", stamp="2026-10-02"), record("2301.09999", "", deleted=True)),
    )
    part = src / "2026-10.pdf.arxiv-2301" / "upstream"
    (part / "manifests").mkdir(parents=True)
    (part / "manifests" / "arxiv-2301.jsonl").write_text(
        "\n".join(
            json.dumps({"path": f"pdf/arxiv/2301/{name}", "size": 1, "md5": "0" * 32})
            for name in ("2301.01234v1.pdf", "2301.01234v3.pdf", "2301.01234v2.pdf")
        )
    )
    old = src / "2026-10.pdf.hep-th-9901" / "upstream" / "manifests"
    old.mkdir(parents=True)
    (old / "hep-th-9901.jsonl").write_text(
        json.dumps({"path": "pdf/hep-th/9901/9901001v1.pdf", "size": 1, "md5": "0"})
    )
    return src


class TestArxivContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return ArxivAdapter(interval=0, today=lambda: TODAY)

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        return make_ctx(SPEC, FakeFetcher(routes()), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, source_dir: Path) -> Path:
        return source_dir / "2026-10-10"


def test_discover_and_incremental_plan(tmp_path: Path) -> None:
    assert "arxiv" in available_adapters()
    adapter = ArxivAdapter(interval=0, today=lambda: TODAY)
    ctx = make_ctx(SPEC, FakeFetcher(routes()), tmp_path)
    (release,) = adapter.discover(ctx)
    assert release.version == "2026-10-10"
    assert [f.name for f in release.files] == ["oai-arXiv-all-2026-10-09.jsonl.zst", "2609.00001v1.pdf"]
    have = Generation("arxiv", "2026-09-10", GenerationStatus.SEALED)
    plan = adapter.plan(ctx, have, release)
    harvest_file = plan.files[0]
    assert harvest_file.name == "oai-arXiv-2026-09-09-2026-10-09.jsonl.zst"
    assert harvest_file.url.endswith("&from=2026-09-09")


def test_harvest_follows_tokens_politely() -> None:
    calls: list[str] = []
    pages = [page(record("1", "a"), token="verb%3DListRecords%26skip%3D1"), page(record("2", "b"))]

    def get(url: str) -> bytes:
        calls.append(url)
        return pages[len(calls) - 1]

    now = [0.0]
    slept: list[float] = []
    throttle = Throttle(3.0, clock=lambda: now[0], sleep=lambda s: slept.append(s))
    out = list(harvest(get, OAI_URL, from_="2026-10-01", until="2026-10-09", throttle=throttle))
    assert len(out) == 2
    assert "from=2026-10-01" in calls[0]
    assert calls[1] == f"{OAI_URL}?verb=ListRecords&resumptionToken=verb%253DListRecords%2526skip%253D1"
    assert slept == [3.0]


def test_documents_union_newest_wins_and_pdf_refs(source_dir: Path, tmp_path: Path) -> None:
    ctx = make_ctx(SPEC, FakeFetcher(), tmp_path / "work")
    docs = {d.native_id: d for d in ArxivAdapter().documents(ctx, source_dir / "2026-10-10")}
    assert sorted(docs) == ["2301.01234", "hep-th/9901001"]  # the deletion hides the older record
    paper = docs["2301.01234"]
    assert paper.title == "New title"
    assert paper.text.splitlines()[:4] == [
        "New title",
        "Ada Lovelace, Charles Babbage Jr",
        "arXiv:2301.01234 [cs.LG math.ST]",
        "We study engines.",
    ]
    assert paper.license == "http://creativecommons.org/licenses/by/4.0/"
    assert paper.html_ref == "arxiv-pdf:2026-10.pdf.arxiv-2301/pdf/arxiv/2301/2301.01234v3.pdf"
    assert paper.links == ("https://arxiv.org/abs/2301.01234", "https://doi.org/10.1000/test.2301.01234")
    assert (
        docs["hep-th/9901001"].html_ref == "arxiv-pdf:2026-10.pdf.hep-th-9901/pdf/hep-th/9901/9901001v1.pdf"
    )
    # the older generation only sees its own harvest
    older = {d.native_id: d for d in ArxivAdapter().documents(ctx, source_dir / "2026-09-10")}
    assert older["2301.01234"].title == "Old title"
    assert "2301.09999" in older


def test_parsing_helpers() -> None:
    assert arxiv_id_of("pdf/arxiv/2301/2301.01234v12.pdf") == ("2301.01234", 12)
    assert arxiv_id_of("pdf/math/0501/0501001v2.pdf") == ("math/0501001", 2)
    assert arxiv_id_of("manifests/x.jsonl") is None
    (rec,) = parse_page(page(record("x/1", "t", deleted=True))).records
    paper = parse_record(rec)
    assert paper is not None
    assert paper.deleted
    error = b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="noRecordsMatch"/></OAI-PMH>'
    assert parse_page(error).records == []
    with pytest.raises(ValueError, match="badArgument"):
        parse_page(
            b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="badArgument"/></OAI-PMH>'
        )
