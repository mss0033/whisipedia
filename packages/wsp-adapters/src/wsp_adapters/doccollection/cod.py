"""The Crystallography Open Database's full release, one document per structure (``reader: cod-cif``).

COD ships everything as one archive (``cod-cifs-mysql.tgz``, 26.6 GB in the 2026-11-04 generation):
``cif/<d>/<dd>/<dd>/<id>.cif`` for each of its ~520,000 structures, and a MySQL dump of its catalogue.
The owner asked for the structures to be readable one by one (2026-11-05): until then the library held
the archive as a single "data file" document that nothing could look inside.

**Reading.** The archive is read once, front to back, as a gzip stream (:func:`archive_cifs`): no member
is ever looked up, so nothing of it is unpacked to disk, and only the file being read is in memory.
The MySQL dump and anything else that is not a ``.cif`` is passed over.

**A structure's document.** :func:`parse_cif` reads the CIF (a small, forgiving CIF 1.1 reader: quoted
values, text fields, loops; a malformed file still yields what it could read), :func:`summarize` picks
out the chemical name, formula, space group, cell, authors, journal, year and DOI, and the document is
that summary followed by the CIF itself (:data:`CIF_HEADING` separates them; the Navigator shows the
CIF, draws the structure from its coordinates and offers it as a download). Only the summary is
searched (:data:`wsp_core.model.SEARCH_CHARS_META`): a CIF is mostly numbers.

**What is left out of the CIF.** Embedded reflection data and refinement files (``_shelx_hkl_file``,
``_shelx_res_file``, ``_refln_*`` loops ...) are most of a modern CIF's bytes and nothing a reader looks
at: :func:`trim_cif` replaces each with a comment that says so (the archive keeps the whole file).
"""

# ruff: noqa: RUF001, RUF002, RUF003 - CIF's Greek letters (\a is α) and the cell's angles are Greek on purpose

from __future__ import annotations

import contextlib
import errno
import io
import logging
import os
import re
import tarfile
import time
import unicodedata
from collections.abc import Buffer, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from wsp_core import nasretry

log = logging.getLogger(__name__)

CIF_HEADING = "Crystallographic Information File"
"""First words of the line that starts the CIF in a structure's text: ``<CIF_HEADING> 1000000.cif``."""
MAX_CIF_BYTES = 128 << 20
"""A member larger than this is passed over (logged): even with its reflections it would be ten times
the largest CIF a crystallographer writes."""
TEXT_FIELD_MAX = 200_000
"""A text field longer than this is left out of the kept CIF, whatever its tag (an unknown embedded file)."""
CIF_MAX_CHARS = 2_000_000
"""The kept CIF is cut (at a line) after this many characters; the archive keeps the whole file."""
TITLE_NAME_MAX = 120
"""A chemical name longer than this is shortened (at a word) in the title; the summary has it whole."""
ELIDED_TAGS = frozenset(
    {
        "_shelx_hkl_file",
        "_shelx_fab_file",
        "_shelx_res_file",
        "_shelx_fcf_file",
        "_iucr_refine_reflections_details",
        "_iucr_refine_instructions_details",
        "_iucr_refine_fcf_details",
    }
)
"""Embedded reflection and refinement files: megabytes of numbers per structure."""
ELIDED_LOOPS = ("_refln_", "_refln.", "_diffrn_refln_", "_diffrn_refln.")
"""Loops of reflections (one row per measured reflection: tens of thousands of rows)."""
_MEMBER = re.compile(r"(?:^|/)cif/(?:[^/]+/)*(\d{7})\.cif$")
_TOKEN = re.compile(r"""\s*(?:(#.*)|'(.*?)'(?=\s|$)|"(.*?)"(?=\s|$)|(\S+))""")
_WS = re.compile(r"\s+")
_UNCERTAINTY = re.compile(r"\(\d+\)$")
# CIF's accent marks (``\'e`` is é) as Unicode combining characters, and its Greek letters (``\a`` is α).
_ACCENTS = {
    "'": "\N{COMBINING ACUTE ACCENT}",
    "`": "\N{COMBINING GRAVE ACCENT}",
    "^": "\N{COMBINING CIRCUMFLEX ACCENT}",
    '"': "\N{COMBINING DIAERESIS}",
    "~": "\N{COMBINING TILDE}",
    ",": "\N{COMBINING CEDILLA}",
    "%": "\N{COMBINING RING ABOVE}",
    "=": "\N{COMBINING MACRON}",
    ".": "\N{COMBINING DOT ABOVE}",
    ";": "\N{COMBINING OGONEK}",
    "<": "\N{COMBINING CARON}",
    "(": "\N{COMBINING BREVE}",
}
_GREEK = dict(zip("abcdefghiklmnopqrstuwxyz", "αβχδεφγηικλμνοπθρστυωξψζ", strict=True))
_ESCAPE = re.compile(rf"\\([{re.escape(''.join(_ACCENTS))}])([A-Za-z])|\\([A-Za-z])")


# --- CIF ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class CifBlock:
    """One ``data_`` block: its single items (tags lower-cased, first value kept) and its loops."""

    name: str
    items: dict[str, str] = field(default_factory=dict[str, str])
    loops: list[tuple[tuple[str, ...], list[tuple[str, ...]]]] = field(
        default_factory=list[tuple[tuple[str, ...], list[tuple[str, ...]]]]
    )

    def value(self, *tags: str) -> str:
        """The first of ``tags`` with a known value (CIF's ``?`` and ``.`` are none), whitespace-collapsed."""
        for tag in tags:
            v = self.items.get(tag)
            if v is None:
                v = next(iter(self.column(tag)), None)
            if v is not None and v.strip() not in ("", "?", "."):
                return _WS.sub(" ", v).strip()
        return ""

    def column(self, tag: str) -> list[str]:
        """The values of ``tag`` in the loop that has it (empty when no loop does)."""
        for tags, rows in self.loops:
            if tag in tags:
                i = tags.index(tag)
                return [r[i] for r in rows]
        return []


def _tokens(text: str) -> Iterator[tuple[str, str]]:
    """``(kind, text)`` of every token: ``data``, ``loop``, ``save``, ``tag``, ``value`` (comments dropped).

    A text field (a line starting with ``;`` up to the next such line) is one value. A quote ends only
    before whitespace (CIF 1.1), so ``'O'Neil'`` is one value; an unclosed quote is read as a bare word."""
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].rstrip("\r")
        i += 1
        if line.startswith(";"):
            body = [line[1:]]
            while i < n and not lines[i].startswith(";"):
                body.append(lines[i].rstrip("\r"))
                i += 1
            rest = lines[i][1:] if i < n else ""
            i += 1
            yield "value", "\n".join(body).strip("\n")
            line = rest
        for m in _TOKEN.finditer(line):
            comment, single, double, bare = m.groups()
            if comment is not None:
                break
            if single is not None or double is not None:
                yield "value", single if single is not None else double
                continue
            if bare is None:
                continue
            low = bare.lower()
            if low.startswith("data_"):
                yield "data", bare[5:]
            elif low == "loop_":
                yield "loop", ""
            elif low.startswith("save_"):
                yield "save", bare[5:]
            elif bare.startswith("_"):
                yield "tag", low
            elif low not in ("global_", "stop_"):
                yield "value", bare


def parse_cif(text: str) -> list[CifBlock]:
    """The data blocks of a CIF. Forgiving: values before any block, a tag without a value and a loop's
    incomplete last row are dropped; whatever was read before a malformed part is kept."""
    blocks: list[CifBlock] = []
    block: CifBlock | None = None
    tag: str | None = None
    loop_tags: list[str] | None = None
    loop_values: list[str] = []
    in_header = False

    def close_loop() -> None:
        nonlocal loop_tags, loop_values, in_header
        if block is not None and loop_tags:
            width = len(loop_tags)
            rows = [tuple(loop_values[k : k + width]) for k in range(0, len(loop_values) - width + 1, width)]
            block.loops.append((tuple(loop_tags), rows))
        loop_tags, loop_values, in_header = None, [], False

    for kind, val in _tokens(text):
        if kind == "data":
            close_loop()
            block, tag = CifBlock(val), None
            blocks.append(block)
        elif block is None:
            continue
        elif kind == "loop":
            close_loop()
            loop_tags, in_header, tag = [], True, None
        elif kind == "tag":
            if loop_tags is not None and in_header:
                loop_tags.append(val)
                continue
            close_loop()
            tag = val
        elif kind == "save":
            close_loop()
            tag = None
        elif loop_tags is not None:
            in_header = False
            loop_values.append(val)
        elif tag is not None:
            block.items.setdefault(tag, val)
            tag = None
    close_loop()
    return blocks


def trim_cif(text: str) -> str:
    """The CIF without its embedded reflection data and refinement files (see the module notes), each
    replaced by a comment line saying what was left out; cut at :data:`CIF_MAX_CHARS`."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith(";"):
            j = i + 1
            while j < n and not lines[j].startswith(";"):
                j += 1
            prev = out[-1].strip() if out else ""
            lone_tag = prev.startswith("_") and len(prev.split()) == 1
            if lone_tag and (
                prev.lower() in ELIDED_TAGS or sum(len(x) + 1 for x in lines[i:j]) > TEXT_FIELD_MAX
            ):
                out[-1] = (
                    f"# {prev} left out here ({j - i - 1:,} lines); the archive's copy of this file has it"
                )
                i = j + 1
                continue
            out.extend(lines[i : j + 1])
            i = j + 1
            continue
        if line.strip().lower() == "loop_":
            k = i + 1
            while k < n and lines[k].strip().startswith("_"):
                k += 1
            first = lines[i + 1].strip().split()[0].lower() if k > i + 1 else ""
            if first.startswith(ELIDED_LOOPS):
                m = k
                while m < n:
                    head = lines[m].lstrip()
                    if head.startswith("_") or head[:5].lower() in ("loop_", "data_", "save_"):
                        break
                    m += 1
                out.append(f"# loop of {first} left out here ({m - k:,} rows); the archive's copy has it")
                i = m
                continue
        out.append(line)
        i += 1
    kept = "\n".join(out).strip("\n") + "\n"
    if len(kept) > CIF_MAX_CHARS:
        cut = kept.rfind("\n", 0, CIF_MAX_CHARS) + 1 or CIF_MAX_CHARS
        kept = kept[:cut] + f"# ... cut here: {len(kept) - cut:,} more characters in the archive's copy\n"
    return kept


def decode_cif(data: bytes) -> str:
    """CIF bytes as text: UTF-8 (CIF 2.0, and ASCII), else Latin-1 (older files with an accented name)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def cif_text(raw: str) -> str:
    """A CIF display string as text: ``K\\"ohler`` is ``Köhler``, ``\\a-quartz`` is ``α-quartz``,
    sub- and superscript marks (``C~6~``, ``Fe^3+^``) dropped."""

    def sub(m: re.Match[str]) -> str:
        accent, letter, greek = m.groups()
        if greek is not None:
            g = _GREEK.get(greek.lower())
            return m.group(0) if g is None else (g.upper() if greek.isupper() else g)
        return unicodedata.normalize("NFC", letter + _ACCENTS[accent])

    text = _ESCAPE.sub(sub, raw.replace("\\ss", "ß"))
    return _WS.sub(" ", text.replace("~", "").replace("^", "")).strip()


# --- the summary -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Structure:
    """What a reader is told about one COD entry (every field may be empty)."""

    cod_id: str
    name: str = ""  # the name the title uses (mineral, common, systematic)
    names: tuple[str, ...] = ()  # every name the CIF gives, for the aliases
    formula: str = ""  # Hill order, as COD writes it: "C6 H12 O6"
    moiety: str = ""
    space_group: str = ""
    space_group_number: str = ""
    cell: tuple[str, ...] = ()  # a, b, c, alpha, beta, gamma (CIF values, uncertainties kept)
    volume: str = ""
    z: str = ""
    temperature: str = ""
    r_factor: str = ""
    authors: tuple[str, ...] = ()
    article: str = ""
    journal: str = ""
    volume_no: str = ""
    issue: str = ""
    pages: str = ""
    year: str = ""
    doi: str = ""

    @property
    def formula_compact(self) -> str:
        """``C6 H12 O6`` as ``C6H12O6`` (titles and aliases)."""
        return self.formula.replace(" ", "")

    @property
    def title(self) -> str:
        label = self.name or self.formula_compact or "Crystal structure"
        if len(label) > TITLE_NAME_MAX:
            label = label[: TITLE_NAME_MAX - 1].rsplit(" ", 1)[0].rstrip(",;-( ") + "…"
        return f"{label} (COD {self.cod_id})"

    @property
    def byline(self) -> str:
        return "; ".join(self.authors)

    def citation(self) -> str:
        """``American Mineralogist 89 (2004), 614-628``."""
        where = " ".join(x for x in (self.journal, self.volume_no) if x)
        if self.issue:
            where += f"({self.issue})"
        if self.year:
            where += f" ({self.year})" if where else self.year
        if self.pages:
            where += f", {self.pages}" if where else self.pages
        return where


def summarize(block: CifBlock, cod_id: str) -> Structure:
    v = block.value
    names = tuple(
        dict.fromkeys(
            n
            for n in (
                cif_text(v("_chemical_name_mineral")),
                cif_text(v("_chemical_name_common")),
                cif_text(v("_chemical_name_systematic")),
                cif_text(v("_chemical_name_structure_type")),
            )
            if n
        )
    )
    authors = block.column("_publ_author_name") or ([a] if (a := v("_publ_author_name")) else [])
    first, last = v("_journal_page_first"), v("_journal_page_last")
    return Structure(
        cod_id=cod_id,
        name=names[0] if names else "",
        names=names,
        formula=cif_text(v("_chemical_formula_sum")).strip("'\" "),
        moiety=cif_text(v("_chemical_formula_moiety")),
        space_group=v("_space_group_name_h-m_alt", "_symmetry_space_group_name_h-m"),
        space_group_number=v("_space_group_it_number", "_symmetry_int_tables_number"),
        cell=tuple(
            v(f"_cell_{x}")
            for x in ("length_a", "length_b", "length_c", "angle_alpha", "angle_beta", "angle_gamma")
        ),
        volume=v("_cell_volume"),
        z=v("_cell_formula_units_z"),
        temperature=v("_cell_measurement_temperature", "_diffrn_ambient_temperature"),
        r_factor=v("_refine_ls_r_factor_gt", "_refine_ls_r_factor_all"),
        authors=tuple(a for a in (cif_text(x) for x in authors) if a and a not in ("?", ".")),
        article=cif_text(v("_publ_section_title")),
        journal=cif_text(v("_journal_name_full", "_journal_name_abbrev")),
        volume_no=v("_journal_volume"),
        issue=v("_journal_issue"),
        pages=f"{first}-{last}" if first and last and first != last else first,
        year=v("_journal_year"),
        doi=v("_journal_paper_doi"),
    )


def _plain(number: str) -> str:
    """``6.532(4)`` as ``6.532``: the summary states values, the CIF keeps their uncertainties."""
    return _UNCERTAINTY.sub("", number)


def summary_text(s: Structure, member: str, archive_title: str) -> str:
    """The searchable part of a structure's document: what it is, its crystal data and its source."""
    lines = [s.title, f"Crystal structure {s.cod_id} of the Crystallography Open Database."]
    extra = [x for x in s.names if x != s.name]
    lines += [f"Name: {s.name}"] if s.name else []
    lines += [f"Also named: {'; '.join(extra)}"] if extra else []
    lines += [f"Formula: {s.formula}"] if s.formula else []
    lines += [f"Moiety formula: {s.moiety}"] if s.moiety and s.moiety != s.formula else []
    if s.space_group:
        number = f" (No. {s.space_group_number})" if s.space_group_number else ""
        lines.append(f"Space group: {s.space_group}{number}")
    a, b, c, al, be, ga = (_plain(x) for x in (s.cell or ("",) * 6))
    if a and b and c:
        cell = f"Cell: a = {a} Å, b = {b} Å, c = {c} Å"
        if al and be and ga:
            cell += f"; α = {al}°, β = {be}°, γ = {ga}°"
        if s.volume:
            cell += f"; V = {_plain(s.volume)} Å³"
        if s.z:
            cell += f"; Z = {s.z}"
        lines.append(cell)
    lines += [f"Temperature: {_plain(s.temperature)} K"] if s.temperature else []
    lines += [f"R factor: {_plain(s.r_factor)}"] if s.r_factor else []
    lines += [f"Authors: {s.byline}"] if s.authors else []
    lines += [f"Article: {s.article}"] if s.article else []
    lines += [f"Published in: {cite}"] if (cite := s.citation()) else []
    lines += [f"DOI: {s.doi}"] if s.doi else []
    lines.append(f"From the archive: {archive_title} ({member})")
    return "\n".join(lines)


def structure_text(summary: str, cod_id: str, cif: str) -> str:
    """The document's text: the summary, then the CIF under its heading line."""
    return f"{summary}\n\n{CIF_HEADING} {cod_id}.cif\n{cif}"


def aliases(s: Structure) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            a
            for a in (*s.names, s.formula_compact, f"COD {s.cod_id}")
            if a and len(a) <= 200 and a != s.title
        )
    )


def page_url(cod_id: str) -> str:
    """The entry's page on crystallography.net (informational: the library never fetches it)."""
    return f"https://www.crystallography.net/cod/{cod_id}.html"


def member_id(name: str) -> str | None:
    """The COD id of an archive member (``cif/9/00/34/9003435.cif`` is ``9003435``), else ``None``."""
    m = _MEMBER.search(name)
    return m.group(1) if m else None


@dataclass(frozen=True, slots=True)
class CodEntry:
    """One structure read from the archive, ready to be a document."""

    cod_id: str
    member: str
    structure: Structure
    summary: str
    text: str


def read_entry(member: str, data: bytes, archive_title: str) -> CodEntry | None:
    """A structure's entry from its CIF, or ``None`` when the file holds no data block."""
    cod_id = member_id(member)
    if cod_id is None:
        return None
    raw = decode_cif(data)
    kept = trim_cif(raw)
    blocks = parse_cif(kept)
    if not blocks:
        log.warning("COD %s (%s): no data block; left out", cod_id, member)
        return None
    block = next((b for b in blocks if b.column("_atom_site_label") or b.items), blocks[0])
    s = summarize(block, cod_id)
    summary = summary_text(s, member, archive_title)
    return CodEntry(cod_id, member, s, summary, structure_text(summary, cod_id, kept))


# --- the archive -------------------------------------------------------------------------------------

READ_TRIES = 8
READ_WAITS = (2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 120.0)
"""Seconds before each new try of a refused read: about four minutes in all, long enough for the
CIFS client to reconnect after a NAS hiccup (2026-11-06 review)."""
DROP_EVERY = 64 << 20
TRANSIENT_ERRNOS = frozenset(
    {
        errno.EIO,  # a ``soft`` CIFS mount turns a server timeout or reconnect into EIO
        errno.EAGAIN,
        errno.ETIMEDOUT,
        errno.ESTALE,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.ENETRESET,
    }
)
"""Errors of a read on the share that a moment later may succeed (as ``wsp_vault.atomic``'s)."""


def transient(exc: OSError) -> bool:
    """Whether a refused read is worth making again: :data:`wsp_core.nasretry.TRANSIENT` or an errno
    of :data:`TRANSIENT_ERRNOS` (EIO above all: the 2026-11-06 review found one EIO 20 GB into the
    26.6 GB archive would have ended the refine and thrown away hours of reading)."""
    return isinstance(exc, nasretry.TRANSIENT) or exc.errno in TRANSIENT_ERRNOS


class ShareReader(io.RawIOBase):
    """A file on the NAS read front to back, as a raw stream for :mod:`tarfile` and :mod:`gzip`.

    * A read the share refuses for a moment (:func:`transient`: EAGAIN, EIO, a dropped connection) is
      made again from the last byte read, after :data:`READ_WAITS` (``pread`` at the offset reached:
      nothing is skipped or read twice, so the gzip stream above goes on as if nothing happened). The
      file is opened again first: after a reconnect the old handle may be stale. The
      :data:`READ_TRIES`-th refusal in a row ends the read, as for every other NAS read.
    * ``pace`` is told every byte read: the library's shared budget for NAS reads and writes
      (``wsp_cli.bootstrap.ingest_pace``), so 26.6 GB read at the link's full speed does not starve the
      API's reads and the ingests running at once (2026-11-06 review).
    * The page cache behind the read is released every :data:`DROP_EVERY` bytes: 26.6 GB read once
      would otherwise sit in the cache charged to the refine's memory budget, which is what pushed the
      nightly job against its limit (2026-10-14 follow-ups).
    """

    def __init__(
        self,
        path: Path,
        *,
        sleep: Sequence[float] = READ_WAITS,
        pace: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__()
        self._path = path
        self._fd = os.open(path, os.O_RDONLY)
        self._pos = 0
        self._dropped = 0
        self._waits = tuple(sleep)
        self._pace = pace
        self._sequential()

    def _sequential(self) -> None:
        if hasattr(os, "posix_fadvise"):
            with contextlib.suppress(OSError):
                os.posix_fadvise(self._fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        data = self._pread(len(memoryview(buffer)))
        n = len(data)
        memoryview(buffer).cast("B")[:n] = data
        self._pos += n
        if self._pace is not None and n:
            self._pace(n)
        if self._pos - self._dropped >= DROP_EVERY:
            self._drop()
        return n

    def _pread(self, want: int) -> bytes:
        for attempt in range(1, READ_TRIES + 1):
            try:
                return os.pread(self._fd, want, self._pos)
            except OSError as exc:
                if not transient(exc) or attempt == READ_TRIES:
                    raise
                wait = self._waits[min(attempt, len(self._waits)) - 1] if self._waits else 0.0
                log.warning("read at byte %d refused (%s); reading again in %.0f s", self._pos, exc, wait)
                time.sleep(wait)
                self._reopen()
        raise AssertionError("unreachable")

    def _reopen(self) -> None:
        """A fresh handle on the file (best effort: the old one is kept when the share still refuses)."""
        try:
            fd = os.open(self._path, os.O_RDONLY)
        except OSError:
            return
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = fd
        self._sequential()

    def tell(self) -> int:
        return self._pos

    def _drop(self) -> None:
        if hasattr(os, "posix_fadvise"):
            with contextlib.suppress(OSError):
                os.posix_fadvise(self._fd, 0, self._pos, os.POSIX_FADV_DONTNEED)
        self._dropped = self._pos

    def close(self) -> None:
        if not self.closed:
            self._drop()
            os.close(self._fd)
        super().close()


def archive_cifs(path: Path, *, pace: Callable[[int], None] | None = None) -> Iterator[tuple[str, bytes]]:
    """``(member name, bytes)`` of every structure's CIF in a COD archive, in archive order; its
    reads paced by ``pace`` (:class:`ShareReader`).

    Stream mode keeps every member header it has read (``TarFile.members``): 520,000 of them would be
    hundreds of MB by the end, so the list is emptied as the read goes (``next`` reads on from its own
    offset, not from the list)."""
    with (
        ShareReader(path, pace=pace) as raw,
        tarfile.open(fileobj=io.BufferedReader(raw, 1 << 20), mode="r|gz") as tar,
    ):
        read: list[tarfile.TarInfo] = vars(tar)["members"]  # the list ``next`` appends to (untyped)
        while (member := tar.next()) is not None:
            read.clear()
            if not member.isfile() or member_id(member.name) is None:
                continue
            if member.size > MAX_CIF_BYTES:
                log.warning("%s: %s is %d bytes; left out", path.name, member.name, member.size)
                continue
            fh = tar.extractfile(member)
            if fh is not None:
                yield member.name, fh.read()


def archive_entries(
    path: Path, archive_title: str, *, pace: Callable[[int], None] | None = None
) -> Iterator[CodEntry]:
    """Every structure of the archive as an entry, each COD id once (a later copy is logged and left
    out); ``pace`` as :func:`archive_cifs`."""
    seen: set[int] = set()
    for name, data in archive_cifs(path, pace=pace):
        try:
            entry = read_entry(name, data, archive_title)
        except (ValueError, IndexError) as exc:  # a parser bug must not end a 520,000-file read
            log.warning("%s: %s not read: %s: %s", path.name, name, type(exc).__name__, exc)
            continue
        if entry is None:
            continue
        key = int(entry.cod_id)
        if key in seen:
            log.warning(
                "%s: COD %s appears again (%s); the first copy is kept", path.name, entry.cod_id, name
            )
            continue
        seen.add(key)
        yield entry


def is_archive(path: Path | PurePosixPath) -> bool:
    name = path.name.lower()
    return name.endswith((".tgz", ".tar.gz"))


TAR_READERS: Mapping[str, str] = {"cod-cif": "Crystallography Open Database CIF archive"}
"""Readers of a gzipped tar archive, streamed (:meth:`DocCollectionAdapter._archive_documents`)."""
