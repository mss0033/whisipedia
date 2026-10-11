"""Original HTML for documents, beyond what was refined (PLAN §10.2).

Two cases:

* **Direct ZIM browsing.** A Kiwix-backed source is browsable as soon as it is sealed: a document that
  is not (yet) in the document store is served straight from the source's current ZIM. Refining a
  multi-million-article ZIM is slow (entries are stored in cluster order, not path order), but a single
  entry lookup is fast, so every page and every in-ZIM link works before any indexing.
* **Borrowed renditions.** A source can declare in the registry that its documents render with another
  source's HTML, matched by title, e.g. the English Wikipedia dump (fast to index, plain wikitext)
  borrowing pages from the Kiwix ZIM (rich HTML)::

      access:
        rendition: [{lang: en, source: wikipedia-en-zim}]
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from wsp_api.pages import page_text, page_title
from wsp_api.seams import AssetStore
from wsp_core.ids import DocId, content_hash, make_doc_id
from wsp_core.model import DocRecord, Generation, SourceSpec

_ZIM_ADAPTER = "kiwix-opds"


class _Current(Protocol):
    def current(self, source_id: str) -> Generation | None: ...


@dataclass(frozen=True, slots=True)
class Rendition:
    """Where a document's HTML comes from when it is not its own ``html_ref``."""

    source_id: str
    generation: Generation
    html_ref: str


def _current(vault: _Current, source_id: str) -> Generation | None:
    try:
        return vault.current(source_id)
    except Exception:  # an unreachable or missing source is simply not browsable
        return None


def zim_document(
    spec: SourceSpec | None, native_id: str, vault: _Current, assets: AssetStore
) -> DocRecord | None:
    """A document read live from the source's current ZIM, or ``None`` if it has no such entry.

    The API serves these through ``wsp_api.views.Library.live_zim`` (one read, cached render); this
    is the same record for any :class:`AssetStore`."""
    if spec is None or spec.adapter != _ZIM_ADAPTER:
        return None
    gen = _current(vault, spec.id)
    if gen is None:
        return None
    html = assets.html(spec.id, gen.generation_id, f"zim:{native_id}")
    if html is None:
        return None
    title = page_title(html)
    text = page_text(html)  # readable text only (E2E-9)
    return DocRecord(
        doc_id=make_doc_id(spec.id, native_id),
        source_id=spec.id,
        native_id=native_id,
        title=title or native_id.rsplit("/", 1)[-1].replace("_", " "),
        lang="",
        license=spec.license,
        text=text,
        revision=gen.generation_id,
        timestamp=gen.sealed_at,
        html_ref=f"zim:{native_id}",
        content_hash=content_hash(text),
    )


def _rules(spec: SourceSpec) -> Sequence[Mapping[str, Any]]:
    access = spec.raw.get("access")
    if not isinstance(access, Mapping):
        return ()
    rules = cast(Mapping[str, Any], access).get("rendition")
    if not isinstance(rules, list):
        return ()
    return [cast(Mapping[str, Any], r) for r in cast(list[object], rules) if isinstance(r, Mapping)]


def borrowed(spec: SourceSpec | None, doc: DocRecord, vault: _Current) -> Rendition | None:
    """The HTML another source holds for this document's title, per the registry's ``rendition``."""
    if spec is None:
        return None
    for rule in _rules(spec):
        lang = rule.get("lang")
        if lang and doc.lang and lang != doc.lang:
            continue
        target = str(rule.get("source", ""))
        gen = _current(vault, target) if target else None
        if gen is not None:
            return Rendition(target, gen, f"zim:{doc.title.replace(' ', '_')}")
    return None


def rendition_doc_id(rendition: Rendition) -> DocId:
    return make_doc_id(rendition.source_id, rendition.html_ref.removeprefix("zim:"))
