"""The owner's daily downloads by hand from hosts the library may not crawl (owner request 2026-10-24).

A host whose robots.txt disallows crawlers, or that answers a plain client with a bot wall, is never
fetched by the library. The owner downloads its files with a browser instead, about ten a day per
source, into ``<vault>/_incoming/_manual/<source>/`` (``Z:\\whisipedia\\_incoming\\_manual\\<source>``).
The library makes no request of those hosts at any step.

A source takes part through ``access.manual`` in the registry:

* ``daily`` (default 10): links offered a day;
* ``list``: how its list of files is made, without asking the host that refuses us:

  - ``{kind: discovery}``: the source's own discovery, which reads only hosts that allow us (WBDG's
    criteria are listed by wbdg.org; their files sit on a bucket whose robots.txt disallows us);
  - ``{kind: saved, pages: [{url, save_as}]}``: the same discovery, reading listing pages the owner
    saved once into ``_manual/<source>/_listing/`` (PubChem's FTP folders, the Army's product maps);
    a page not saved yet is offered first;
  - ``{kind: pubmed}``: PubMed's numbered update files after the newest the library holds;
  - ``{kind: list, items: [{title, url, landing, save_as, pub_number}]}``: a curated list;
  - ``{kind: kiwix-checksums}``: Kiwix's own ``<file>.sha256`` of every newer ZIM the mirrors list
    for a Kiwix source (owner decision 2026-10-26: mirrors only with Kiwix's checksums). Saved files
    stay in the folder, where the Kiwix adapter reads them; nothing is ingested. The checksums Kiwix
    published for the files the library already holds are copied from the vault manifests into
    ``<state>/kiwix-checksums`` first, so only new files are asked for.

* ``browse``: ``[pattern, replacement]`` turning a listed URL into one a browser opens
  (``ftp://`` into NCBI's ``https://`` names of the same files);
* ``first``: patterns (on the title or file name) of the items offered before the rest, in that order
  (PubChem's chemical safety summaries, WBDG's Unified Facilities Criteria);
* ``again_days``: a file taken this many days ago is offered again (GeoNames, refreshed quarterly).

``wsp collections manual --sync`` (hourly, ``whisipedia-manual.timer``) does both halves:

1. once a day, ``_manual/TODAY.html`` (and ``TODAY.txt``): per source, up to ``daily`` links, those
   offered before and not yet received first, each with the exact name to save it under;
2. every run, the import: each source folder's files that match an item are checked (an archive must
   read whole: zip, gzip and bzip2 CRCs; a PDF must be one), screened like any download, planned and
   ingested (``wsp ingest --wait``). A document collection's files go through ``wsp collections
   fetch`` with the received files standing for the listing (``kind: candidates``, a sample: nothing
   held is inferred withdrawn); PubMed's become a ``pubmedYY-uNNNN`` generation of the new update files.

The queue of each source is kept in ``<state>/manual/<source>.json`` and rebuilt weekly; an item's
state is ``pending`` until its file is taken, with the days it was offered and the last problem found
(a file that fails its check is left in the folder, renamed ``*.bad``, and offered again).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from wsp_adapters.doccollection import DocCollectionAdapter
from wsp_adapters.doccollection.adapter import Survey
from wsp_adapters.doccollection.model import Candidate, key_part
from wsp_adapters.doccollection.policy import Verdict
from wsp_cli import bootstrap
from wsp_core.errors import FetchError, NotFoundError
from wsp_core.model import FetchResult, RemoteFile, SourceSpec, SourceStatus
from wsp_core.ports import Ctx, Fetcher

DEFAULT_DAILY = 10
REBUILD_DAYS = 7
LISTING_DIR = "_listing"  # <folder>/_listing/: listing pages the owner saved once
PUBMED_FILE = re.compile(r"^pubmed(?P<yy>\d\d)n(?P<n>\d{4})\.xml\.gz$")
PUBMED_GEN = re.compile(r"^pubmed(?P<yy>\d\d)(?:-u(?P<upto>\d{4}))?$")
PUBMED_UPDATES = "https://ftp.ncbi.nlm.nih.gov/pubmed/updatefiles/"


@dataclass
class Item:
    id: str  # the document key (a collection) or the file name (PubMed)
    open: str  # what the owner opens in the browser
    save_as: str  # the exact file name to save it under
    title: str = ""
    page: bool = False  # ``open`` is a landing page: save the document it offers
    cand: dict[str, Any] = field(default_factory=dict[str, Any])  # Candidate.to_json (collections)
    status: str = "pending"  # pending | taken | denied (the content screen refused it: never offered again)
    offered: list[str] = field(default_factory=list[str])  # days it was on the daily page
    taken: str = ""
    problem: str = ""


@dataclass
class Queue:
    source: str
    built: str = ""
    items: list[Item] = field(default_factory=list[Item])
    save_pages: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])  # (url, save_as)
    notes: list[str] = field(default_factory=list[str])

    @staticmethod
    def load(path: Path, source: str) -> Queue:
        try:
            raw = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return Queue(source)
        return Queue(
            source,
            str(raw.get("built") or ""),
            [Item(**i) for i in cast(list[dict[str, Any]], raw.get("items") or [])],
            [(str(u), str(n)) for u, n in cast(list[tuple[object, object]], raw.get("save_pages") or [])],
            [str(n) for n in cast(list[object], raw.get("notes") or [])],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")
        tmp.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")
        tmp.replace(path)

    def merge(self, fresh: Sequence[Item]) -> None:
        """Take a rebuilt list: known items keep their state; new ones are appended in the builder's
        order (its priority); items no longer listed stay only if they were taken."""
        known = {i.id: i for i in self.items}
        merged: list[Item] = []
        for item in fresh:
            old = known.pop(item.id, None)
            if old is not None:
                item.status, item.offered, item.taken, item.problem = (
                    old.status,
                    old.offered,
                    old.taken,
                    old.problem,
                )
            merged.append(item)
        merged += [i for i in known.values() if i.status in ("taken", "denied")]
        self.items = merged

    def pending(self) -> list[Item]:
        return [i for i in self.items if i.status == "pending"]

    def renew(self, today: date, again_days: int) -> None:
        """Items taken ``again_days`` ago or more are wanted again (an issuer's periodic refresh)."""
        for item in self.items:
            if (
                item.status == "taken"
                and item.taken
                and (today - date.fromisoformat(item.taken)).days >= again_days
            ):
                item.status, item.offered, item.problem = "pending", [], ""

    def prioritise(self, first: Sequence[str]) -> None:
        """Items matching ``first`` (in that order) ahead of the rest; otherwise the builder's order."""
        rxs = [re.compile(p, re.IGNORECASE) for p in first]

        def rank(item: Item) -> int:
            return next(
                (n for n, rx in enumerate(rxs) if rx.search(item.title) or rx.search(item.save_as)), len(rxs)
            )

        self.items.sort(key=rank)  # stable: the builder's order within a rank


def manual_config(spec: SourceSpec) -> Mapping[str, Any] | None:
    access = cast(Mapping[str, Any], spec.raw.get("access") or {})
    block = access.get("manual")
    return cast(Mapping[str, Any], block) if isinstance(block, Mapping) else None


def manual_sources(specs: Sequence[SourceSpec]) -> list[SourceSpec]:
    return [s for s in specs if manual_config(s) is not None and s.status is not SourceStatus.EXCLUDED]


# --- listings ------------------------------------------------------------------------------------


class SavedPages:
    """A fetcher that reads only the listing pages the owner saved (``pages``: URL -> file name in
    ``folder``); any other URL is not found. No request leaves the machine."""

    def __init__(self, folder: Path, pages: Mapping[str, str]) -> None:
        self.folder, self.pages = folder, dict(pages)
        self.missing: list[str] = []

    def get_bytes(self, url: str, *, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        name = self.pages.get(url)
        path = self.folder / name if name else None
        if path is None or not path.is_file():
            if name and url not in self.missing:
                self.missing.append(url)
            raise NotFoundError(url, "not saved by the owner")
        return path.read_bytes()[:max_bytes]

    def download(self, remote: RemoteFile, dest: Path) -> FetchResult:
        raise FetchError(f"{remote.url}: a saved listing downloads nothing")


def _browse(url: str, rule: Sequence[str]) -> str:
    return re.sub(rule[0], rule[1], url) if len(rule) == 2 else url


def _items_from_survey(survey: Survey, rule: Sequence[str], page_ext: str) -> list[Item]:
    out: list[Item] = []
    unresolved = set(survey.unresolved_keys)
    for cand in survey.allowed:
        name = cand.extra.get("save_as") or _url_name(cand.url)
        if "." not in name:  # GEBCO's ".../gebco_2024/zip/": the number and the stored extension
            stem = key_part(cand.pub_number or cand.key.partition(":")[2])
            name = stem + (cand.extra.get("suffix") or Path(cand.file_name).suffix)
        # The name travels with the candidate: the import takes exactly the file the page asked for.
        doc = cand.with_url(cand.url, extra={**cand.extra, "save_as": name})
        page = cand.extra.get("page") == "1"  # a landing page: the owner saves the file it offers
        out.append(Item(cand.key, _browse(cand.url, rule), name, cand.title, page=page, cand=doc.to_json()))
    for cand, decision in survey.decisions:
        if decision.verdict is not Verdict.ALLOW or cand.key not in unresolved:
            continue
        # A landing page the library may not open (its detail hop is on the refusing host): the owner
        # opens it and saves the document it offers under a name the import recognises.
        name = f"{key_part(cand.pub_number or cand.key.partition(':')[2])}{page_ext}"
        doc = cand.with_url(cand.url, extra={**cand.extra, "save_as": name})
        out.append(Item(cand.key, _browse(cand.url, rule), name, cand.title, page=True, cand=doc.to_json()))
    return out


def _url_name(url: str) -> str:
    from urllib.parse import unquote, urlsplit

    return unquote(urlsplit(url).path.rstrip("/").rpartition("/")[2])


def build_queue(
    spec: SourceSpec,
    folder: Path,
    *,
    fetcher: Fetcher | None,
    held: str | None,
    user_agent: str,
    workdir: Path,
    specs: Sequence[SourceSpec] = (),
    checksum_dirs: Sequence[Path] = (),
) -> tuple[list[Item], list[tuple[str, str]], list[str]]:
    """The source's list: ``(items, pages the owner should save first, notes)``."""
    mc = manual_config(spec) or {}
    how = cast(Mapping[str, Any], mc.get("list") or {})
    kind = str(how.get("kind") or "")
    rule = [str(x) for x in cast(list[object], mc.get("browse") or [])]
    page_ext = str(mc.get("page_ext") or ".pdf")
    if kind == "kiwix-checksums":
        if fetcher is None:
            raise bootstrap.ConfigError(
                f"{spec.id}: the Kiwix checksum list needs a fetcher (the mirror listing)"
            )
        return _kiwix_checksum_items(specs, fetcher, user_agent, workdir, checksum_dirs)
    if kind == "pubmed":
        return _pubmed_items(held, int(mc.get("daily") or DEFAULT_DAILY)), [], []
    if kind == "list":
        items: list[Item] = []
        prefix = str(cast(Mapping[str, Any], spec.raw.get("access") or {}).get("key_prefix") or spec.id)
        for raw in cast(list[Mapping[str, Any]], how.get("items") or []):
            url = str(raw.get("url") or "")
            landing = str(raw.get("landing") or "")
            name = str(raw.get("save_as") or _url_name(url) or "")
            if not name or not (url or landing):
                continue
            pub = str(raw.get("pub_number") or Path(name).stem)
            cand = Candidate(
                key=f"{key_part(prefix)}:{key_part(pub)}",
                url=url or landing,
                title=str(raw.get("title") or pub),
                issuer=str(cast(Mapping[str, Any], spec.raw.get("access") or {}).get("issuer") or spec.name),
                pub_number=pub,
                landing_url=landing,
                discovered_by="manual-list",
                extra={"save_as": name},
            )
            items.append(Item(cand.key, landing or url, name, cand.title, page=not url, cand=cand.to_json()))
        return items, [], []
    if kind not in ("discovery", "saved"):
        raise bootstrap.ConfigError(f"{spec.id}: access.manual.list.kind {kind!r} unknown")
    pages: dict[str, str] = {}
    saved: SavedPages | None = None
    if kind == "saved":
        for p in cast(list[Mapping[str, Any]], how.get("pages") or []):
            pages[str(p["url"])] = str(p["save_as"])
        saved = SavedPages(folder / LISTING_DIR, pages)
    use = cast(Fetcher, saved) if saved is not None else fetcher
    if use is None:
        raise bootstrap.ConfigError(f"{spec.id}: a discovery list needs a fetcher")
    adapter = DocCollectionAdapter()
    # The real user agent: the discovery's throttle reads every host's robots.txt for it.
    ctx = Ctx(source=spec, fetcher=use, workdir=workdir, user_agent=user_agent)
    env = adapter.env(ctx)
    survey = adapter.survey(ctx, env=env)
    items = _items_from_survey(survey, rule, page_ext)
    to_save = [(_browse(u, rule), pages[u]) for u in (saved.missing if saved else [])]
    notes = list(survey.incomplete[:5]) if not to_save else []
    return items, to_save, notes


def _kiwix_checksum_items(
    specs: Sequence[SourceSpec], fetcher: Fetcher, user_agent: str, workdir: Path, dirs: Sequence[Path]
) -> tuple[list[Item], list[tuple[str, str]], list[str]]:
    from wsp_adapters.kiwix import KiwixMirrorAdapter, official_checksum_url

    adapter = KiwixMirrorAdapter(dirs=list(dirs))
    items: dict[str, Item] = {}
    notes: list[str] = []
    for spec in specs:
        if str(cast(Mapping[str, Any], spec.raw.get("access") or {}).get("adapter")) != "kiwix-opds":
            continue
        if spec.status in (SourceStatus.RETIRED, SourceStatus.EXCLUDED):
            continue
        ctx = Ctx(source=spec, fetcher=fetcher, workdir=workdir, user_agent=user_agent)
        try:
            pick = adapter.choice(ctx)
        except (ValueError, FetchError) as exc:
            notes.append(f"{spec.id}: {exc}"[:200])
            continue
        for e in pick.waiting:
            items.setdefault(
                e.file_name,
                Item(
                    e.file_name,
                    official_checksum_url(e),
                    f"{e.file_name}.sha256",
                    f"{spec.id}: {e.name} ({e.release_date})",
                ),
            )
    return sorted(items.values(), key=lambda i: i.id), [], notes[:5]


def seed_kiwix_checksums(vault_root: Path, specs: Sequence[SourceSpec], dest: Path) -> int:
    """Copy the SHA-256 Kiwix published for each ZIM the library holds (recorded in the generation's
    MANIFEST.json when it was downloaded from Kiwix) into ``dest/<file>.sha256``. Returns how many
    were written."""
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    for spec in specs:
        if str(cast(Mapping[str, Any], spec.raw.get("access") or {}).get("adapter")) != "kiwix-opds":
            continue
        for manifest in vault_root.glob(f"library/*/{spec.id}/*/MANIFEST.json"):
            try:
                files = cast(
                    list[Mapping[str, Any]],
                    json.loads(manifest.read_text(encoding="utf-8")).get("files") or [],
                )
            except (OSError, ValueError):
                continue
            for f in files:
                name = str(f.get("path") or "").rsplit("/", 1)[-1]
                if not name.endswith(".zim"):
                    continue
                ups = cast(list[Mapping[str, Any]], f.get("upstream") or [])
                digest = next(
                    (str(u.get("hexdigest") or "").lower() for u in ups if u.get("algorithm") == "sha256"), ""
                )
                # The upstream digest is Kiwix's own (.sha256 and metalink); the file's sha256 matched it.
                if not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != str(f.get("sha256") or "").lower():
                    continue
                target = dest / f"{name}.sha256"
                if target.exists():
                    continue
                tmp = target.with_name(target.name + ".partial")
                tmp.write_text(f"{digest}  {name}\n", encoding="utf-8")
                tmp.replace(target)
                written += 1
    return written


def import_kiwix_checksums(spec: SourceSpec, queue: Queue, folder: Path, out: TextIO) -> int:
    """A saved ``.sha256`` is taken when it names its ZIM with a SHA-256; anything else (a saved error
    page, another file's checksum) is set aside."""
    from wsp_adapters.kiwix import saved_checksum

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    for item in queue.pending():
        path = folder / item.save_as
        if not path.is_file():
            continue
        if saved_checksum(item.id, [folder]) is None:
            why = f"not Kiwix's checksum of {item.id}"
            out.write(f"{spec.id}: {item.save_as}: {why}; set aside as {path.name}.bad\n")
            _set_aside(path, why, item)
            continue
        item.status, item.taken, item.problem = "taken", day, ""
    return 0


def _pubmed_items(held: str | None, ahead: int) -> list[Item]:
    """The update files after the newest one held (``pubmed26-u1671``: 1672 onwards). NCBI adds about
    one a day, so ``ahead`` numbers are offered; a link that is not out yet answers Not Found."""
    m = PUBMED_GEN.match(held or "")
    if m is None or m.group("upto") is None:
        return []
    yy, last = m.group("yy"), int(m.group("upto"))
    out: list[Item] = []
    for n in range(last + 1, last + 1 + ahead):
        name = f"pubmed{yy}n{n:04d}.xml.gz"
        out.append(Item(name, PUBMED_UPDATES + name, name, f"PubMed update file {n}"))
    return out


# --- the daily page --------------------------------------------------------------------------------


def mark_held(queue: Queue, held: Mapping[str, str]) -> int:
    """Items the library already holds (``held``: document key -> its generation) are taken, dated by
    that generation, so a fresh list never asks for what an earlier fetch brought (GeoNames'
    files, imported the day before the daily page began)."""
    n = 0
    for item in queue.items:
        gen = held.get(item.id)
        if gen is None or item.status == "taken":
            continue  # (offered or not: what is held needs no download)
        stamp = gen[:10] if re.match(r"\d{4}-\d{2}-\d{2}", gen) else date.today().isoformat()
        item.status, item.taken = "taken", stamp
        n += 1
    return n


def held_documents(vault_root: Path, spec: SourceSpec, queue: Queue) -> dict[str, str]:
    """Every document key in the source's current catalogs (each partition the queue names)."""
    from wsp_cli.doccollections import current_catalog

    parts = {str(i.cand.get("partition") or "") or None for i in queue.items}
    out: dict[str, str] = {}
    for part in parts:
        gen, catalog = current_catalog(vault_root, spec, part if spec.partitioned else None)
        for meta in catalog:
            out[meta.doc_key] = meta.generation or gen or ""
    return out


def todays_offer(queue: Queue, daily: int, day: str) -> list[Item]:
    """Up to ``daily`` items: those offered before and not received first, then new ones."""
    pending = queue.pending()
    again = [i for i in pending if i.offered]
    new = [i for i in pending if not i.offered]
    picked = (again + new)[: max(0, daily - len(queue.save_pages))]
    for item in picked:
        if day not in item.offered:
            item.offered.append(day)
    return picked


def render_today(
    day: str, offers: Sequence[tuple[SourceSpec, Queue, list[Item], str, int]], root_win: str | None
) -> tuple[str, str]:
    """``TODAY.html`` and ``TODAY.txt``. ``offers``: (source, queue, today's items, its folder as
    Windows names it, items left)."""
    css = (
        "body{font:15px/1.45 system-ui,sans-serif;margin:24px;max-width:1100px;background:#fff;color:#111}"
        "h2{margin-top:28px}code{background:#eef;padding:1px 4px}li{margin:6px 0}.n{color:#666}"
        "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}code{background:#223}}"
    )
    parts = [
        f"<!doctype html><meta charset=utf-8><title>Manual downloads {day}</title><style>{css}</style>",
        f"<h1>Manual downloads for {day}</h1>",
        "<p>Open each link, then save the file into the source's folder under the name shown "
        "(the library takes it within the hour). These hosts refuse automated clients; nothing here is "
        "fetched by the library.</p>",
    ]
    text = [f"Manual downloads for {day}", ""]
    for spec, queue, items, folder, left in offers:
        if not items and not queue.save_pages:
            continue
        parts.append(f"<h2>{html.escape(spec.name)}</h2>")
        parts.append(
            f"<p>Save into <code>{html.escape(folder)}</code> <span class=n>({left} left)</span></p><ol>"
        )
        text += [f"== {spec.name}", f"   save into: {folder}   ({left} left)"]
        for url, name in queue.save_pages:
            link = f'<a href="{html.escape(url, quote=True)}" target=_blank>{html.escape(url)}</a>'
            how = "save the page (Ctrl+S, &ldquo;Webpage, HTML only&rdquo;) into <code>_listing</code> as"
            parts.append(f"<li>Listing page: {link} &mdash; {how} <code>{html.escape(name)}</code></li>")
            text.append(f"   [page] {url}\n          save the page into _listing as {name}")
        for item in items:
            what = "open the page and save its document" if item.page else "download"
            again = (
                f" <span class=n>(offered {len(item.offered)} times)</span>" if len(item.offered) > 1 else ""
            )
            problem = f" <b>last try: {html.escape(item.problem)}</b>" if item.problem else ""
            label = html.escape(item.title or item.save_as)
            link = f'<a href="{html.escape(item.open, quote=True)}" target=_blank>{label}</a>'
            name = html.escape(item.save_as)
            parts.append(f"<li>{link} &mdash; {what}, save as <code>{name}</code>{again}{problem}</li>")
            text.append(
                f"   {item.open}\n          save as {item.save_as}"
                + (f"  (last try: {item.problem})" if item.problem else "")
            )
        parts.append("</ol>")
        text.append("")
    if root_win:
        parts.append(f"<p class=n>All folders are under <code>{html.escape(root_win)}</code>.</p>")
    return "\n".join(parts) + "\n", "\n".join(text) + "\n"


# --- import ------------------------------------------------------------------------------------------


def _check(path: Path) -> str | None:
    """Why the file cannot be taken, or None. Archives must read whole; a PDF must be one."""
    from wsp_cli.doccollections import whole

    kind, problem = whole(path)
    if problem is not None:
        return problem
    suffix = "".join(path.suffixes[-2:]).lower()
    with path.open("rb") as fh:
        head = fh.read(1024)
    if not kind and suffix.endswith((".zip", ".gz", ".bz2")):
        return "not the archive its name says (an error page saved in its place?)"
    if path.suffix.lower() == ".pdf" and not head.lstrip().startswith(b"%PDF"):
        return "not a PDF (an error or login page saved in its place?)"
    if head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")) and path.suffix.lower() not in (
        ".html",
        ".htm",
    ):
        return "an HTML page saved in the file's place"
    return None


def _set_aside(path: Path, why: str, item: Item) -> None:
    item.problem = why
    path.replace(path.with_name(path.name + ".bad"))


def import_collection(
    args: argparse.Namespace, spec: SourceSpec, queue: Queue, folder: Path, out: TextIO
) -> int:
    """Take the received files of a document collection: one ``wsp collections fetch`` per partition
    with them as the listing, then the ingest."""
    from wsp_cli import doccollections, ops

    present: list[Item] = []
    for item in queue.pending():
        path = folder / item.save_as
        if not path.is_file():
            continue
        if (why := _check(path)) is not None:
            out.write(f"{spec.id}: {item.save_as}: {why}; set aside as {path.name}.bad\n")
            _set_aside(path, why, item)
            continue
        present.append(item)
    if not present:
        return 0
    groups: dict[str, list[Item]] = {}
    for item in present:
        groups.setdefault(str(item.cand.get("partition") or ""), []).append(item)
    worst = 0
    for part, items in groups.items():
        reports: list[doccollections.FetchReport] = []
        fetch_args = argparse.Namespace(
            config_dir=args.config_dir,
            source=spec.id,
            version=None,
            limit=None,
            max_pages=None,
            dry_run=False,
            partition=part or None,
            refetch=None,
            adopt_kept=None,
            manual=folder,
            discovery=[{"kind": "candidates", "items": [i.cand for i in items]}],
            report_sink=reports,
        )
        code = doccollections.cmd_fetch(fetch_args, out)
        report = reports[0] if reports else None
        failed = dict(report.failed) if report else {}
        rejected = dict(report.rejected) if report else {}
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        for item in items:
            if item.id in rejected:  # refused by the screen: asking for it again would be refused again
                item.status, item.problem = "denied", rejected[item.id][:200]
            elif item.id in failed:
                item.problem = failed[item.id][:200]
            elif not (folder / item.save_as).is_file():
                item.status, item.taken, item.problem = "taken", day, ""
        if code not in (doccollections.EXIT_COMPLETE, doccollections.EXIT_ALERT, doccollections.EXIT_PARTIAL):
            worst = max(worst, code)
            continue
        icode = _ingest(args, spec.id, None, out, ops)
        worst = max(worst, icode)
    return worst


def import_pubmed(
    args: argparse.Namespace,
    spec: SourceSpec,
    queue: Queue,
    folder: Path,
    held: str | None,
    vault_root: Path,
    out: TextIO,
) -> int:
    """PubMed's update files after the newest held, in an unbroken run (a generation must hold
    every number up to its own: wsp_adapters.pubmed.content_members), as generation
    ``pubmedYY-uNNNN``."""
    from wsp_cli import ops
    from wsp_cli.bulk import PLANS_SUBDIR

    m = PUBMED_GEN.match(held or "")
    if m is None or m.group("upto") is None:
        out.write(f"{spec.id}: no update generation held to continue from ({held!r})\n")
        return 0
    yy, n = m.group("yy"), int(m.group("upto"))
    run: list[Path] = []
    while (path := folder / f"pubmed{yy}n{n + 1:04d}.xml.gz").is_file():
        item = next((i for i in queue.items if i.id == path.name), None)
        if (why := _check(path)) is not None:
            out.write(f"{spec.id}: {path.name}: {why}; set aside as {path.name}.bad\n")
            if item is not None:
                _set_aside(path, why, item)
            else:
                path.replace(path.with_name(path.name + ".bad"))
            break
        run.append(path)
        n += 1
    if not run:
        return 0
    version = f"pubmed{yy}-u{n:04d}"
    incoming = vault_root / "_incoming"
    dest = incoming / spec.id / version
    dest.mkdir(parents=True, exist_ok=True)
    targets: list[dict[str, Any]] = []
    for path in run:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(8 << 20):
                digest.update(chunk)
        size = path.stat().st_size
        shutil.move(path, dest / path.name)
        targets.append(
            {
                "source_id": spec.id,
                "version": version,
                "name": path.name,
                "uris": [PUBMED_UPDATES + path.name],
                "lane": "collections",
                # The owner's download, read whole (gzip CRC); NCBI's MD5 sidecars are not downloaded
                # by hand, so this digest checks the copy into the vault, not the origin.
                "checksum": ["sha-256", digest.hexdigest()],
                "size": size,
                "meta": {"manual": "downloaded by hand (robots.txt disallows crawlers)"},
            }
        )
    plan = incoming / "_plans" / PLANS_SUBDIR / f"{spec.id}-{version}.json"
    plan.parent.mkdir(parents=True, exist_ok=True)
    tmp = plan.with_name(plan.name + ".partial")
    tmp.write_text(json.dumps(targets, indent=1), encoding="utf-8")
    tmp.replace(plan)
    out.write(f"{spec.id}: {len(run)} update file(s) as {version}\n")
    code = _ingest(args, spec.id, version, out, ops)
    if code == 0:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        names = {p.name for p in run}
        for item in queue.items:
            if item.id in names:
                item.status, item.taken, item.problem = "taken", day, ""
    return code


def _ingest(args: argparse.Namespace, source: str, version: str | None, out: TextIO, ops: Any) -> int:
    ingest_args = argparse.Namespace(
        config_dir=args.config_dir,
        source=source,
        version=version,
        partition=None,
        dry_run=False,
        offline=True,
        json=False,
        settle_seconds=0.0,
        allow_mirror=False,
        max_seconds=None,
        wait=INGEST_WAIT,
    )
    return int(ops.cmd_ingest(ingest_args, out))


INGEST_WAIT = 3 * 3600  # a manual import waits up to three hours in the ingest lock's queue


# --- the command ----------------------------------------------------------------------------------------


def held_generation(cfg: Mapping[str, Any], spec: SourceSpec) -> str | None:
    """The newest sealed PubMed generation (``pubmed26-u1682``), current or not: the next update file
    to ask for follows it (a sealed generation not yet promoted still holds its files)."""
    root = bootstrap.paths(cfg).require_vault() / "library" / spec.vault_domain / spec.id
    best: tuple[int, int, str] | None = None
    for gen in root.iterdir() if root.is_dir() else ():
        m = PUBMED_GEN.match(gen.name)
        if m is None or not (gen / "MANIFEST.json").is_file():
            continue
        key = (int(m.group("yy")), int(m.group("upto") or 0), gen.name)
        best = key if best is None or key > best else best
    return best[2] if best else None


def cmd_sync(args: argparse.Namespace, out: TextIO) -> int:
    """``wsp collections manual --sync``: take in whatever the owner saved, rebuild stale lists, and
    write today's page once a day (``--today``: write it again now)."""
    from wsp_cli.doccollections import MANUAL_DIR, windows_path

    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    specs = bootstrap.load_sources(cfg, lay)
    sources = [s for s in manual_sources(specs) if not args.source or s.id == args.source]
    vault_root = bootstrap.paths(cfg).require_vault()
    root = vault_root / "_incoming" / MANUAL_DIR
    state = bootstrap.paths(cfg).require_state() / "manual"
    today = date.today().isoformat()
    # One source's run (``manual SOURCE --sync``) takes its files in and rebuilds its list; the page,
    # which shows every source, is written by a run over all of them.
    page_due = not args.source and (bool(args.today) or _page_due(root, today))
    user_agent = bootstrap.user_agent(cfg)
    fetcher: Fetcher | None = None
    offers: list[tuple[SourceSpec, Queue, list[Item], str, int]] = []
    worst = 0
    for spec in sources:
        folder = root / spec.id
        (folder / LISTING_DIR).mkdir(parents=True, exist_ok=True)
        qpath = state / f"{spec.id}.json"
        queue = Queue.load(qpath, spec.id)
        mc = manual_config(spec) or {}
        kind = str(cast(Mapping[str, Any], mc.get("list") or {}).get("kind") or "")
        held = held_generation(cfg, spec) if kind == "pubmed" else None
        if again := int(mc.get("again_days") or 0):
            queue.renew(date.today(), again)
        if not args.no_import and queue.items:
            try:
                if kind == "pubmed":
                    worst = max(worst, import_pubmed(args, spec, queue, folder, held, vault_root, out))
                    held = held_generation(cfg, spec)
                elif kind == "kiwix-checksums":
                    worst = max(worst, import_kiwix_checksums(spec, queue, folder, out))
                else:
                    worst = max(worst, import_collection(args, spec, queue, folder, out))
            finally:
                queue.save(qpath)
        stale = not queue.built or (date.today() - date.fromisoformat(queue.built)).days >= REBUILD_DAYS
        # A saved listing is read again while pages are missing (the owner may have saved them since);
        # PubMed's list follows what is held, so it is remade every run.
        if args.rebuild or stale or queue.save_pages or kind == "pubmed":
            if kind in ("discovery", "kiwix-checksums") and fetcher is None:
                fetcher = bootstrap.build_fetcher(cfg)
            kiwix_dirs = bootstrap.paths(cfg).kiwix_checksum_dirs
            if kind == "kiwix-checksums" and kiwix_dirs:
                seeded = seed_kiwix_checksums(vault_root, specs, kiwix_dirs[0])
                if seeded:
                    out.write(f"{spec.id}: {seeded} checksum(s) recorded from the vault manifests\n")
            try:
                items, to_save, notes = build_queue(
                    spec,
                    folder,
                    fetcher=fetcher,
                    held=held,
                    user_agent=user_agent,
                    workdir=state / "work" / spec.id,
                    specs=specs,
                    checksum_dirs=kiwix_dirs,
                )
            except (FetchError, bootstrap.ConfigError, OSError) as exc:
                out.write(f"{spec.id}: list not rebuilt: {exc}\n")
            else:
                if kind == "pubmed":
                    taken = [i for i in queue.items if i.status == "taken"]
                    queue.merge(items)
                    queue.items = [i for i in queue.items if i.status != "taken"] + taken[-50:]
                elif items or not queue.items:
                    queue.merge(items)
                queue.prioritise([str(x) for x in cast(list[object], mc.get("first") or [])])
                if kind not in ("pubmed", "kiwix-checksums"):
                    mark_held(queue, held_documents(vault_root, spec, queue))
                queue.save_pages, queue.notes, queue.built = to_save, notes, today
                queue.save(qpath)
        if page_due:
            picked = todays_offer(queue, int(mc.get("daily") or DEFAULT_DAILY), today)
            queue.save(qpath)
            offers.append((spec, queue, picked, windows_path(folder) or str(folder), len(queue.pending())))
    if page_due:
        page, text = render_today(today, offers, windows_path(root))
        for name, body in (("TODAY.html", page), ("TODAY.txt", text)):
            tmp = root / (name + ".partial")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(root / name)
        (root / ".today").write_text(today, encoding="utf-8")
        out.write(f"today's page: {windows_path(root / 'TODAY.html') or root / 'TODAY.html'}\n")
    return worst


def _page_due(root: Path, today: str) -> bool:
    try:
        return (root / ".today").read_text(encoding="utf-8").strip() != today
    except OSError:
        return True


def status(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    state = bootstrap.paths(cfg).require_state() / "manual"
    for spec in manual_sources(bootstrap.load_sources(cfg, lay)):
        q = Queue.load(state / f"{spec.id}.json", spec.id)
        taken = sum(i.status == "taken" for i in q.items)
        out.write(f"{spec.id:32} {taken:6} taken {len(q.pending()):6} left  built {q.built or '-'}\n")
    return 0


__all__ = ["Item", "Queue", "build_queue", "cmd_sync", "render_today", "status", "todays_offer"]
