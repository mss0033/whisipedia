"""Human-readable vault documentation (PLAN §7): ``README.txt``, ``CATALOG.html``, ``CATALOG.csv``.

Everything here must be readable decades from now with no Whisipedia software: plain UTF-8 text,
HTML 3.2 with no scripts, styles or external resources, and RFC 4180 CSV.
"""

from __future__ import annotations

import csv
import html
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from wsp_core.model import Generation, GenerationStatus, SourceSpec, SourceStatus
from wsp_vault.atomic import atomic_write_bytes
from wsp_vault.vault import FileVault, VaultError

README_NAME = "README.txt"
CATALOG_HTML = "CATALOG.html"
CATALOG_CSV = "CATALOG.csv"

TOOLS: dict[str, str] = {
    ".zim": "Kiwix (kiwix-desktop, kiwix-serve) or zimdump",
    ".bz2": "bzcat / bzip2 -d, then a text editor or XML reader",
    ".gz": "zcat / gzip -d",
    ".zst": "zstdcat / zstd -d",
    ".xz": "xzcat / xz -d",
    ".7z": "7-Zip (7z x)",
    ".zip": "any unzip tool",
    ".tar": "tar -xf",
    ".pbf": "osmium, or any OpenStreetMap tool",
    ".parquet": "DuckDB, Python (pyarrow/pandas) or any Parquet reader",
    ".epub": "any EPUB reader",
    ".pdf": "any PDF reader",
    ".json": "any text editor; jq",
    ".jsonl": "any text editor; jq",
    ".xml": "any text editor or web browser",
    ".csv": "any spreadsheet or text editor",
    ".txt": "any text editor",
    ".html": "any web browser",
    ".par2": "par2 (par2cmdline) to verify or repair the files beside it",
}

CSV_COLUMNS = (
    "source_id",
    "name",
    "domain",
    "tier",
    "status",
    "license",
    "legal_basis",
    "current_generation",
    "generations",
    "current_bytes",
    "sealed_at",
    "state",
)


@dataclass(frozen=True, slots=True)
class CatalogRow:
    spec: SourceSpec | None
    source_id: str
    domain: str
    current: Generation | None
    generations: tuple[Generation, ...]

    @property
    def state(self) -> str:
        if self.current is None:
            return "not yet archived" if not self.generations else "staged"
        if self.current.status is GenerationStatus.DEGRADED:
            return "DEGRADED"
        return "ok"

    @property
    def current_bytes(self) -> int:
        return sum(a.size for a in self.current.artifacts) if self.current else 0

    def values(self) -> tuple[str, ...]:
        s = self.spec
        sealed = self.current.sealed_at if self.current else None
        sealed_gens = [g for g in self.generations if g.status is not GenerationStatus.STAGING]
        return (
            self.source_id,
            s.name if s else "",
            self.domain,
            str(s.tier) if s else "",
            str(s.status) if s else "",
            s.license if s else "",
            s.legal_basis if s else "",
            self.current.generation_id if self.current else "",
            str(len(sealed_gens)),
            str(self.current_bytes),
            sealed.astimezone(UTC).isoformat(timespec="seconds") if sealed else "",
            self.state,
        )


def catalog_rows(vault: FileVault, specs: Sequence[SourceSpec]) -> list[CatalogRow]:
    by_id = {s.id: s for s in specs if s.status is not SourceStatus.EXCLUDED}
    rows: list[CatalogRow] = []
    for source_id in sorted(set(by_id) | set(vault.sources())):
        try:
            current = vault.current(source_id)
        except VaultError:
            current = None
        gens = tuple(vault.generations(source_id))
        rows.append(CatalogRow(by_id.get(source_id), source_id, vault.domain_of(source_id), current, gens))
    return rows


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    raise AssertionError("unreachable")


def render_catalog_csv(rows: Sequence[CatalogRow]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    writer.writerows(r.values() for r in rows)
    return buf.getvalue()


def render_catalog_html(rows: Sequence[CatalogRow], generated: datetime) -> str:
    e = html.escape
    head = "".join(f"<TH>{e(c)}</TH>" for c in CSV_COLUMNS)
    body: list[str] = []
    for row in rows:
        values = list(row.values())
        values[9] = human_size(row.current_bytes) if row.current else ""
        link = f"library/{row.domain}/{row.source_id}/{README_NAME}"
        cells = [
            f'<TD><A HREF="{e(link)}">{e(values[0])}</A></TD>',
            *(f"<TD>{e(v)}</TD>" for v in values[1:]),
        ]
        body.append("<TR>" + "".join(cells) + "</TR>")
    return "\n".join(
        [
            '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">',
            "<HTML><HEAD><TITLE>Whisipedia catalog</TITLE>",
            '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=utf-8"></HEAD>',
            "<BODY>",
            "<H1>Whisipedia catalog</H1>",
            f"<P>Generated {e(generated.astimezone(UTC).isoformat(timespec='seconds'))}. "
            f"{len(rows)} sources. See README.txt for how to read this library without software.</P>",
            '<TABLE BORDER="1" CELLPADDING="3">',
            f"<TR>{head}</TR>",
            *body,
            "</TABLE>",
            "</BODY></HTML>",
            "",
        ]
    )


def render_root_readme(rows: Sequence[CatalogRow], generated: datetime) -> str:
    archived = [r for r in rows if r.current is not None]
    total = sum(r.current_bytes for r in archived)
    return "\n".join(
        [
            "WHISIPEDIA - a private offline library of open knowledge",
            "=" * 56,
            "",
            f"Generated: {generated.astimezone(UTC).isoformat(timespec='seconds')}",
            f"Sources archived: {len(archived)} of {len(rows)} catalogued ({human_size(total)} current).",
            "",
            "You do not need any Whisipedia software to use this library.",
            "",
            "  CATALOG.html   every source, its license, date and size (open in any web browser)",
            "  CATALOG.csv    the same as a spreadsheet",
            "  library/       the archive: library/<subject>/<source>/<date>/upstream/ holds the",
            "                 original files exactly as published. Each source folder has a",
            "                 README.txt; CURRENT.txt names the newest good dated folder.",
            "  ledger/        an append-only log of everything that happened (JSON lines)",
            "  keys/          public keys that verify the MANIFEST.json.sig signatures",
            "",
            "Checking a file by hand: MANIFEST.json in each dated folder lists every file with its",
            "size and SHA-256. Compare with `sha256sum <file>`. Verify the manifest signature with",
            "  ssh-keygen -Y verify -f allowed_signers -I whisipedia -n whisipedia-manifest \\",
            "      -s MANIFEST.json.sig < MANIFEST.json",
            "where allowed_signers contains: whisipedia <key type> <key> (from keys/).",
            "",
            "A folder containing DEGRADED.txt has damaged files; prefer the one named in CURRENT.txt.",
            "Original works keep their own licenses; see each source's README.txt.",
            "",
        ]
    )


def render_source_readme(row: CatalogRow, generated: datetime) -> str:
    s = row.spec
    lines = [row.source_id if s is None else f"{s.name} ({row.source_id})", "=" * 60, ""]
    if s is not None:
        publisher = s.raw.get("publisher")
        lines += [
            *([f"Publisher:   {publisher}"] if publisher else []),
            f"License:     {s.license}",
            f"Legal basis: {s.legal_basis}",
            f"Tier:        {s.tier}",
            f"Subjects:    {', '.join(s.domain)}",
            f"Update:      {s.cadence or 'library cycle'} ({s.strategy or 'n/a'})",
            "",
        ]
    lines += [f"Current generation: {row.current.generation_id if row.current else 'none'} ({row.state})", ""]
    lines.append("Generations (dated folders, read-only once sealed):")
    for g in row.generations:
        size = human_size(sum(a.size for a in g.artifacts))
        lines.append(f"  {g.generation_id:<16} {g.status!s:<9} {len(g.artifacts):>6} files  {size}")
    if not row.generations:
        lines.append("  (none yet)")
    suffixes = sorted({_suffix(a.relpath) for g in row.generations for a in g.artifacts} - {""})
    lines += ["", "How to open these files:"]
    lines += [f"  {sfx:<9} {TOOLS.get(sfx, 'see the publisher documentation')}" for sfx in suffixes]
    if not suffixes:
        lines.append("  (no files yet)")
    lines += [
        "",
        "Inside each dated folder: upstream/ holds the original files byte for byte;",
        "MANIFEST.json lists size, SHA-256 and BLAKE3 of every file; MANIFEST.json.sig signs it;",
        "parity/ (if present) holds par2 recovery data.",
        "",
        f"Generated: {generated.astimezone(UTC).isoformat(timespec='seconds')}",
        "",
    ]
    return "\n".join(lines)


def _suffix(relpath: PurePosixPath) -> str:
    return relpath.suffix.lower()


def write_library_docs(
    vault: FileVault, specs: Sequence[SourceSpec], *, generated: datetime | None = None
) -> list[Path]:
    """Write the top-level README/CATALOG files and a README.txt in every archived source folder."""
    now = generated or datetime.now(UTC)
    rows = catalog_rows(vault, specs)
    outputs: list[tuple[Path, str]] = [
        (vault.root / README_NAME, render_root_readme(rows, now)),
        (vault.root / CATALOG_HTML, render_catalog_html(rows, now)),
        (vault.root / CATALOG_CSV, render_catalog_csv(rows)),
    ]
    for row in rows:
        src = vault.source_dir(row.source_id)
        if src.is_dir():
            outputs.append((src / README_NAME, render_source_readme(row, now)))
    vault.root.mkdir(parents=True, exist_ok=True)
    for path, text in outputs:
        atomic_write_bytes(path, text.encode("utf-8"), overwrite=True)
    return [p for p, _ in outputs]
