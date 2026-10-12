from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from wsp_core.ids import make_doc_id
from wsp_core.model import DocRecord
from wsp_index import TantivyConfig, TantivyIndex


def doc(
    source: str,
    native: str,
    title: str,
    text: str = "",
    *,
    lang: str = "en",
    links: tuple[str, ...] = (),
) -> DocRecord:
    return DocRecord(
        doc_id=make_doc_id(source, native),
        source_id=source,
        native_id=native,
        title=title,
        lang=lang,
        license="CC0-1.0",
        text=text or f"{title} text",
        links=links,
    )


class TextMemo:
    """In-memory text source for :class:`wsp_index.TantivyIndex` snippets (the Refinery's role)."""

    def __init__(self) -> None:
        self.texts: dict[str, str] = {}

    def remember(self, docs: Iterable[DocRecord]) -> list[DocRecord]:
        out = list(docs)
        self.texts.update({d.doc_id: d.text for d in out})
        return out

    def __call__(self, doc_ids: Sequence[str]) -> dict[str, str]:
        return {i: self.texts[i] for i in doc_ids if i in self.texts}


class SnippetIndex(TantivyIndex):
    """A ``TantivyIndex`` whose snippets come from the documents it was given, as in the API."""

    def __init__(self, path: Path, config: TantivyConfig | None = None) -> None:
        self.memo = TextMemo()
        super().__init__(path, config, texts=self.memo)

    def upsert(self, docs: Iterable[DocRecord], *, replace: bool = True) -> int:
        return super().upsert(self.memo.remember(docs), replace=replace)
