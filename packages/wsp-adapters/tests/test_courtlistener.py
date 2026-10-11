from __future__ import annotations

import bz2
import csv
import hashlib
import io
from collections.abc import Sequence
from pathlib import Path

import pytest
from adapters_testkit import make_ctx, make_spec

from wsp_adapters import available_adapters
from wsp_adapters.courtlistener import DEFAULT_ENDPOINT, CourtListenerAdapter, csv_rows, parse_s3_listing
from wsp_core.ports import Ctx, SourceAdapter
from wsp_core.testing.adapter_contract import FakeFetcher, SourceAdapterContract

DATE = "2026-09-30"
SPEC = make_spec(
    "courtlistener", {"adapter": "courtlistener", "workers": 1}, license_="LicenseRef-PD-court-opinions"
)
OPINION_HEADER = [
    "id",
    "date_created",
    "date_modified",
    "author_str",
    "per_curiam",
    "joined_by_str",
    "type",
    "sha1",
    "page_count",
    "download_url",
    "local_path",
    "plain_text",
    "html",
    "html_lawbox",
    "html_columbia",
    "html_anon_2020",
    "xml_harvard",
    "xml_scan",
    "html_with_citations",
    "extracted_by_ocr",
    "author_id",
    "cluster_id",
]


def pg_csv(header: Sequence[str], rows: Sequence[dict[str, str]]) -> bytes:
    """CSV the way PostgreSQL COPY ... ESCAPE '\\' writes it (quotes escaped with a backslash)."""
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, header, quoting=csv.QUOTE_ALL, escapechar="\\", doublequote=False, lineterminator="\n"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in header})
    return bz2.compress(buf.getvalue().encode())


OPINIONS = pg_csv(
    OPINION_HEADER,
    [
        {
            "id": "11",
            "date_modified": "2025-06-06 18:30:39.451266+00",
            "author_str": "Marshall",
            "type": "020lead",
            "html_with_citations": '<opinion type="majority">'
            + '<p>The "judicial power" extends to all cases.</p>'
            + "<p>"
            + "x" * 200_000  # longer than the csv module's default field limit
            + "</p></opinion>",
            "cluster_id": "101",
        },
        {
            "id": "12",
            "date_modified": "2025-06-07 00:00:00+00",
            "author_str": "Story",
            "type": "040dissent",
            "plain_text": "I respectfully dissent.\nSecond line.",
            "cluster_id": "101",
        },
        {"id": "13", "type": "010combined", "plain_text": "Per curiam.", "cluster_id": "999"},
    ],
)
CLUSTERS = pg_csv(
    ["id", "case_name", "date_filed", "judges", "slug", "precedential_status", "docket_id"],
    [
        {"id": "101", "case_name": "Marbury v. Madison", "date_filed": "1803-02-24", "judges": "Marshall",
         "slug": "marbury-v-madison", "precedential_status": "Published", "docket_id": "501"},
    ],
)  # fmt: skip
DOCKETS = pg_csv(
    ["id", "court_id", "docket_number"],
    [{"id": "501", "court_id": "scotus", "docket_number": "No. 1"}, {"id": "502", "court_id": "x"}],
)
COURTS = pg_csv(
    ["id", "short_name", "full_name"], [{"id": "scotus", "full_name": "Supreme Court of the United States"}]
)
FILES = {
    f"opinions-{DATE}.csv.bz2": OPINIONS,
    f"opinion-clusters-{DATE}.csv.bz2": CLUSTERS,
    f"dockets-{DATE}.csv.bz2": DOCKETS,
    f"courts-{DATE}.csv.bz2": COURTS,
}


def s3_listing(objects: dict[str, bytes], *, token: str | None = None, multipart: str = "") -> bytes:
    items = "".join(
        f"<Contents><Key>bulk-data/{name}</Key><Size>{len(data)}</Size>"
        f"<ETag>&quot;{'abc-3' if name == multipart else hashlib.md5(data).hexdigest()}&quot;</ETag>"
        "</Contents>"
        for name, data in objects.items()
    )
    more = (
        f"<IsTruncated>true</IsTruncated><NextContinuationToken>{token}</NextContinuationToken>"
        if token
        else ("<IsTruncated>false</IsTruncated>")
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"{more}{items}</ListBucketResult>"
    ).encode()


def routes() -> dict[str, bytes]:
    bucket = DEFAULT_ENDPOINT.rstrip("/").rsplit("/", 1)[0]
    first = dict(list(FILES.items())[:2])
    second = dict(list(FILES.items())[2:])
    second["opinions-2026-06-30.csv.bz2"] = OPINIONS
    second["people-db-schools-2026-06-30.csv.bz2"] = b"x"
    out = {
        f"{bucket}/?list-type=2&prefix=bulk-data/": s3_listing(first, token="t/1="),
        f"{bucket}/?list-type=2&prefix=bulk-data/&continuation-token=t%2F1%3D": s3_listing(
            second, multipart="people-db-schools-2026-06-30.csv.bz2"
        ),
    }
    for name, data in {**first, **second}.items():
        out[f"{bucket}/bulk-data/{name}"] = data
    return out


@pytest.fixture
def generation_dir(tmp_path: Path) -> Path:
    up = tmp_path / "gen" / "upstream"
    up.mkdir(parents=True)
    for name, data in FILES.items():
        (up / name).write_bytes(data)
    return tmp_path / "gen"


class TestCourtListenerContract(SourceAdapterContract):
    @pytest.fixture
    def adapter(self) -> SourceAdapter:
        return CourtListenerAdapter()

    @pytest.fixture
    def ctx(self, tmp_path: Path) -> Ctx:
        spec = make_spec(
            "courtlistener",
            {"adapter": "courtlistener", "workers": 1, "max_releases": 2},
            license_="LicenseRef-PD-court-opinions",
        )
        return make_ctx(spec, FakeFetcher(routes()), tmp_path / "work")

    @pytest.fixture
    def generation_dir(self, generation_dir: Path) -> Path:
        return generation_dir


def test_discover(tmp_path: Path) -> None:
    assert "courtlistener" in available_adapters()
    releases = list(CourtListenerAdapter().discover(make_ctx(SPEC, FakeFetcher(routes()), tmp_path)))
    assert [r.version for r in releases] == [DATE]
    (release,) = releases
    assert sorted(f.name for f in release.files) == sorted(FILES)
    assert all(f.checksums and f.checksums[0].algorithm == "md5" for f in release.files)


def test_multipart_etag_is_not_a_checksum() -> None:
    objects, token = parse_s3_listing(
        s3_listing({"a-2026-01-01.csv.bz2": b"x"}, multipart="a-2026-01-01.csv.bz2")
    )
    assert token is None
    assert objects[0].etag == "abc-3"


def test_documents_join_cluster_docket_and_court(generation_dir: Path, tmp_path: Path) -> None:
    ctx = make_ctx(SPEC, FakeFetcher(), tmp_path / "work")
    docs = {d.native_id: d for d in CourtListenerAdapter().documents(ctx, generation_dir)}
    assert sorted(docs) == ["11", "12", "13"]
    lead = docs["11"]
    assert lead.title == "Marbury v. Madison"
    assert lead.text.startswith(
        "Marbury v. Madison\nSupreme Court of the United States. Filed 1803-02-24. Docket No. 1\n"
        "Judges: Marshall\nOpinion by Marshall\n"
    )
    assert 'The "judicial power" extends to all cases.' in lead.text
    assert "<p>" not in lead.text
    assert lead.url_native == "https://www.courtlistener.com/opinion/101/marbury-v-madison/"
    assert lead.timestamp is not None
    assert lead.timestamp.year == 2025
    assert docs["12"].title == "Marbury v. Madison (dissent, Story)"
    assert docs["12"].text.endswith("I respectfully dissent.\nSecond line.")
    assert docs["13"].title == "Opinion 13"  # cluster not in the clusters file
    assert (tmp_path / "work" / "courtlistener-lookup.sqlite").is_file()
    # the lookup is reused on the next pass
    again = [d.content_hash for d in CourtListenerAdapter().documents(ctx, generation_dir)]
    assert again == [docs[k].content_hash for k in ("11", "12", "13")]


def test_csv_dialect_handles_backslash_escapes() -> None:
    data = bz2.decompress(OPINIONS).decode()
    rows = list(csv_rows(io.StringIO(data, newline="")))
    assert rows[0]["html_with_citations"].startswith('<opinion type="majority">')
