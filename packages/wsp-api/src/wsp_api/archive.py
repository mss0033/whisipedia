"""Browsing a stored zip archive without unpacking it (2026-11-05, the gNATSGO archive).

Some sources are one big zip: NRCS's gNATSGO is a 117 GB zip of 149 files (state and national soil
databases by fiscal year, read-me files, ISO 19115 metadata). Its page offered one Download of the
whole. Here the archive is read the way ``unzip -l`` reads it, from the central directory at its end,
and nothing is unpacked to disk:

* :func:`directory_size` reads only the end-of-central-directory records (ZIP64 too) and refuses an
  archive whose directory is too large to hold in memory, before :mod:`zipfile` parses it;
* :class:`Listings` keeps the parsed member list of the last few archives (they are sealed, so a
  listing never goes stale), and :func:`listing` groups it by folder with sizes;
* :func:`read_view` reads one small member: a text file (the start of it, for a big one) or an XML
  metadata file read into sections (:mod:`wsp_api.geometa`);
* :func:`open_member` opens one member for streaming; the route in :mod:`wsp_api.files` sends it in
  chunks as it inflates (and the CRC check at its end aborts a damaged one).
"""

from __future__ import annotations

import struct
import threading
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import IO, NamedTuple

from wsp_api.errors import ApiError
from wsp_api.geometa import read_metadata
from wsp_api.schemas import ArchiveFolder, ArchiveListing, ArchiveMember, ArchiveView

MAX_MEMBERS = 200_000  # more and the central directory is not parsed (zipfile keeps a ZipInfo each)
MAX_DIRECTORY_BYTES = 64 * 1024 * 1024
LISTED_FILES = 2000  # files in one page of a listing; `after=` lists the next page
LISTED_FOLDERS = 2000  # folders in one page (at least 2: a page cut after the top folder alone has no `next`)
VIEW_BYTES = 512 * 1024  # of a text member shown here (the start of a big CSV)
METADATA_BYTES = 16 * 1024 * 1024  # an XML member read whole for its sections (gNATSGO's: 7 MB)
CACHED_LISTINGS = 4
CACHED_MEMBERS = 400_000  # members held by the listing cache in all
TEXT_SUFFIXES = frozenset(
    {
        ".txt", ".text", ".md", ".rst", ".csv", ".tsv", ".tab", ".json", ".geojson", ".xml", ".xsd",
        ".gml", ".kml", ".html", ".htm", ".prj", ".met", ".log", ".yaml", ".yml", ".ini", ".cfg",
        ".sql", ".asc", ".readme", ".1st", ".me", ".lst", ".def", ".qmd", ".sld",
    }
)  # fmt: skip
METADATA_HINTS = ("metadata", "iso19115", "iso19139", "iso_19115", "fgdc", "19115", "19139", "19110", "_meta")

_EOCD = b"PK\x05\x06"
_EOCD64_LOCATOR = b"PK\x06\x07"
_EOCD64 = b"PK\x06\x06"
_EOCD_SIZE = 22
_MAX_COMMENT = 0xFFFF


class Entry(NamedTuple):
    """What a listing keeps of a member: far less than a :class:`zipfile.ZipInfo`."""

    path: str
    size: int
    packed: int
    modified: str | None
    is_dir: bool


def directory_size(fh: IO[bytes]) -> tuple[int, int]:
    """``(members, central directory bytes)`` from the end-of-central-directory record, or from the
    ZIP64 record when the archive has one (gNATSGO's 117 GB does). Reads at most 64 KiB from the end."""
    fh.seek(0, 2)
    end = fh.tell()
    start = max(0, end - _EOCD_SIZE - _MAX_COMMENT)
    fh.seek(start)
    tail = fh.read(end - start)
    # The record is the one whose comment ends the file: a signature inside the comment is not it.
    at = tail.rfind(_EOCD)
    while at >= 0 and (
        len(tail) - at < _EOCD_SIZE
        or at + _EOCD_SIZE + struct.unpack_from("<H", tail, at + 20)[0] != len(tail)
    ):
        at = tail.rfind(_EOCD, 0, at)
    if at < 0:
        raise zipfile.BadZipFile("no end of central directory record")
    count, size = struct.unpack_from("<HI", tail, at + 10)
    # ZIP64: a 20-byte locator just before the record, and the 56-byte ZIP64 record just before
    # that (read where it sits, as zipfile does, not at the locator's offset: a zip with data in
    # front of it has its offsets shifted).
    where = start + at
    if where < 20 + 56:
        return count, size
    fh.seek(where - 20 - 56)
    block = fh.read(76)
    record, locator = block[:56], block[56:]
    if locator[:4] != _EOCD64_LOCATOR:  # not ZIP64: the 16- and 32-bit fields are the counts
        return count, size
    if len(record) < 56 or record[:4] != _EOCD64:
        raise zipfile.BadZipFile("ZIP64 end of central directory record missing")
    count64, size64 = struct.unpack_from("<QQ", record, 32)
    return count64, size64


def open_archive(path: Path) -> zipfile.ZipFile:
    """``path`` as a :class:`zipfile.ZipFile`, refused (``413``) when its directory is too big to
    parse and (``422``) when it is not a zip."""
    try:
        with path.open("rb") as fh:
            members, size = directory_size(fh)
        if members > MAX_MEMBERS or size > MAX_DIRECTORY_BYTES:
            raise ApiError(
                413,
                "archive_too_large",
                f"the archive holds {members:,} files, more than can be listed here; download it whole",
            )
        return zipfile.ZipFile(path)  # by path: a ZipFile given a file object never closes it
    except zipfile.BadZipFile as exc:
        raise ApiError(
            422, "not_an_archive", f"the stored file is not a readable zip archive: {exc}"
        ) from exc
    except OSError as exc:
        raise ApiError(404, "no_original", f"the stored file cannot be read: {exc.strerror}") from exc


def _modified(info: zipfile.ZipInfo) -> str | None:
    y, mo, d, hh, mm, ss = info.date_time
    if not (1980 <= y <= 2107 and 1 <= mo <= 12 and 1 <= d <= 31 and hh < 24 and mm < 60 and ss < 62):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}T{hh:02d}:{mm:02d}:{min(ss, 59):02d}"


def entries_of(zf: zipfile.ZipFile) -> tuple[Entry, ...]:
    return tuple(
        Entry(i.filename, i.file_size, i.compress_size, _modified(i), i.is_dir()) for i in zf.infolist()
    )


class Listings:
    """The member lists of the last few archives listed, by ``(path, size, mtime)``. Sealed files do
    not change, and a folder opened after the whole listing needs no second read of the directory."""

    def __init__(self, size: int = CACHED_LISTINGS, members: int = CACHED_MEMBERS) -> None:
        self._size = size
        self._members = members
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple[str, int, int], tuple[Entry, ...]] = OrderedDict()

    def entries(self, path: Path) -> tuple[Entry, ...]:
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
        with self._lock:
            hit = self._items.get(key)
            if hit is not None:
                self._items.move_to_end(key)
                return hit
        with open_archive(path) as zf:
            found = entries_of(zf)
        with self._lock:
            self._items[key] = found
            self._items.move_to_end(key)
            while len(self._items) > 1 and (
                len(self._items) > self._size or sum(len(v) for v in self._items.values()) > self._members
            ):
                self._items.popitem(last=False)
        return found


def view_kind(path: str) -> str | None:
    """``"metadata"``, ``"text"`` or ``None`` for a member path, by its name."""
    name = path.rsplit("/", 1)[-1].lower()
    suffix = name[name.rfind(".") :] if "." in name else ""
    if suffix == ".xml" and any(h in name for h in METADATA_HINTS):
        return "metadata"
    if suffix in TEXT_SUFFIXES or name.lstrip("_0123456789 -").startswith(("readme", "read me", "license")):
        return "text"
    return None


def _folder_of(path: str) -> str:
    return path[: path.rfind("/") + 1]


def _sort_key(path: str) -> tuple[str, str]:
    return (path.casefold(), path)


def listing(
    name: str,
    entries: tuple[Entry, ...],
    folder: str,
    *,
    member_url: str,
    view_url: str,
    after: str | None = None,
) -> ArchiveListing:
    """The files under ``folder`` ("" for all) grouped by folder, folders in path order, files by name.
    At most :data:`LISTED_FILES` files and :data:`LISTED_FOLDERS` folders a page; ``truncated`` says so
    and ``next`` is where the next page starts (``after``). Once the files run out, the folders that
    follow are still listed (without their files) so the reader sees the whole tree.

    ``after`` is the last file listed, or the last folder listed in full: a page resumes in that file's
    folder, after it. Without it a folder of more than 2,000 files, the top of an archive too, could
    never be listed past its 2,000th (2026-11-05 review: "Show all" on it fetched the same page)."""
    if folder and not folder.endswith("/"):
        folder += "/"
    if after is not None and (not after or not after.startswith(folder)):
        raise ApiError(400, "invalid_cursor", f"after={after!r} is not in the folder {folder!r}")
    files = [e for e in entries if not e.is_dir and e.path.startswith(folder)]
    dirs = {
        e.path if e.path.endswith("/") else e.path + "/"
        for e in entries
        if e.is_dir and e.path.startswith(folder)
    }
    if folder and not files and folder not in dirs:
        raise ApiError(404, "member_not_found", f"the archive has no folder {folder!r}")
    direct: dict[str, list[Entry]] = {}
    totals: dict[str, list[int]] = {}  # folder -> [files, bytes] in it and under it
    for d in dirs | {folder}:
        direct.setdefault(d, [])
    for e in files:
        parent = _folder_of(e.path)
        direct.setdefault(parent, []).append(e)
    for f in list(direct):  # every ancestor down from `folder` is a folder too
        at = f
        while len(at) > len(folder):
            at = _folder_of(at[:-1])
            direct.setdefault(at, [])
    for f, members in direct.items():
        n, b = len(members), sum(m.size for m in members)
        at = f
        while True:
            t = totals.setdefault(at, [0, 0])
            t[0] += n
            t[1] += b
            if len(at) <= len(folder):
                break
            at = _folder_of(at[:-1])
    order = sorted(direct, key=_sort_key)
    resume_in: str | None = None  # the folder `after` is a file of: listed from after that file
    if after is not None and after.endswith("/"):  # a folder listed in full, and every one before it
        order = [f for f in order if _sort_key(f) > _sort_key(after)]
    elif after is not None:
        resume_in = _folder_of(after)
        order = [f for f in order if _sort_key(f) >= _sort_key(resume_in)]
    out: list[ArchiveFolder] = []
    shown = 0
    truncated = False
    last_file: str | None = None
    cut: str | None = None  # the last file listed before the first one left out
    for f in order:
        if len(out) >= LISTED_FOLDERS:
            truncated = True
            break
        members = sorted(direct[f], key=lambda e: _sort_key(e.path))
        rest = members
        if f == resume_in and after is not None:
            past = _sort_key(after)
            rest = [m for m in members if _sort_key(m.path) > past]
        room = max(0, LISTED_FILES - shown)
        listed = rest[:room]
        if listed:
            last_file = listed[-1].path
        if len(rest) > room:
            truncated = True
            if cut is None:
                cut = last_file
        shown += len(listed)
        out.append(
            ArchiveFolder(
                path=f,
                count=len(members),
                bytes=sum(m.size for m in members),
                total_count=totals[f][0],
                total_bytes=totals[f][1],
                files=[_member(m) for m in listed],
            )
        )
    # Cut among the files: after the last one listed. Cut only among the folders (every file of the
    # folders listed is listed): after the last folder. Either way a page lists at least one file or
    # one folder past `after`, so paging ends.
    following = (cut or out[-1].path) if truncated and out else None
    return ArchiveListing(
        name=name,
        folder=folder,
        members=len(files),
        bytes=sum(e.size for e in files),
        packed=sum(e.packed for e in files),
        folders=out,
        truncated=truncated,
        next=following,
        member_url=member_url,
        view_url=view_url,
    )


def _member(e: Entry) -> ArchiveMember:
    kind = view_kind(e.path)
    return ArchiveMember(
        path=e.path,
        name=e.path.rsplit("/", 1)[-1],
        size=e.size,
        packed=e.packed,
        modified=e.modified,
        view="metadata" if kind == "metadata" else "text" if kind else None,
    )


def member_info(zf: zipfile.ZipFile, path: str) -> zipfile.ZipInfo:
    """The member named ``path``: a file, not encrypted. ``404``/``409`` otherwise."""
    try:
        info = zf.getinfo(path)
    except KeyError:
        raise ApiError(404, "member_not_found", f"the archive has no file {path!r}") from None
    if info.is_dir():
        raise ApiError(404, "member_not_found", f"{path!r} is a folder")
    if info.flag_bits & 0x1:
        raise ApiError(409, "encrypted", f"{path!r} is encrypted")
    return info


def open_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> IO[bytes]:
    """``info`` opened for reading as it inflates (``415`` for a compression zipfile cannot undo)."""
    try:
        return zf.open(info)
    except NotImplementedError as exc:  # Deflate64, PPMd, ...
        raise ApiError(415, "unsupported_compression", f"{info.filename!r}: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise ApiError(422, "damaged_member", f"{info.filename!r}: {exc}") from exc


def read_view(zf: zipfile.ZipFile, path: str) -> ArchiveView:
    """A text member's start (:data:`VIEW_BYTES`), or an XML metadata member read into sections."""
    info = member_info(zf, path)
    name = path.rsplit("/", 1)[-1]
    kind = view_kind(path)
    xml = name.lower().endswith(".xml")
    if kind is None:
        raise ApiError(415, "not_viewable", f"{name!r} is not a text file; download it")
    limit = METADATA_BYTES if xml and info.file_size <= METADATA_BYTES else VIEW_BYTES
    try:
        with open_member(zf, info) as fh:
            data = fh.read(limit + 1)  # bounded: a lying header cannot make it read more
    except zipfile.BadZipFile as exc:  # a CRC mismatch on a member read to its end
        raise ApiError(422, "damaged_member", f"{name!r}: {exc}") from exc
    whole = len(data) <= limit
    if xml and whole:
        meta = read_metadata(data)
        if meta is not None:
            return ArchiveView(
                path=path, name=name, size=info.file_size, text=None, truncated=False, metadata=meta
            )
    shown = data[:VIEW_BYTES]
    cut = len(data) > VIEW_BYTES
    return ArchiveView(path=path, name=name, size=info.file_size, text=decode(shown, cut=cut), truncated=cut)


def decode(data: bytes, *, cut: bool) -> str:
    """UTF-8 (a BOM dropped; a character split by the cut dropped), else Windows-1252 as federal
    read-me files often are."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for trim in range(4 if cut else 1):
        try:
            return data[: len(data) - trim].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return data.decode("cp1252", "replace")
