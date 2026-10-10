from __future__ import annotations

from wsp_core.ids import make_doc_id
from wsp_core.model import DocRecord


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
