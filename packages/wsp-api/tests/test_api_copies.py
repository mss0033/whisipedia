from types import SimpleNamespace
from typing import Any, cast

from wsp_api.schemas import DocumentLink
from wsp_api.views import Library


def _spec(sid: str, **raw: Any) -> Any:
    return SimpleNamespace(id=sid, raw=raw)


def test_copies_by_native_id_prefix_and_link() -> None:
    specs = {
        "wikisource-en": _spec(
            "wikisource-en",
            access={"copies": [{"source": "kiwix-wikisource-en-all", "match": "native", "why": "complete"}]},
        ),
        "kiwix-wikisource-en-all": _spec("kiwix-wikisource-en-all"),
        "simple": _spec(
            "simple",
            access={
                "copies": [{"source": "wikipedia-top10", "match": "native", "prefix": "en/", "why": "full"}]
            },
        ),
        "wikipedia-top10": _spec("wikipedia-top10"),
        "gutenberg": _spec(
            "gutenberg",
            access={"copies": [{"source": "gutenberg-zim", "match": "link", "why": "illustrated"}]},
        ),
        "gutenberg-zim": _spec("gutenberg-zim"),
    }
    held = {"wsp:kiwix-wikisource-en-all/Life_of_Marius", "wsp:wikipedia-top10/en/Antimony"}

    def exists(did: str) -> bool:
        return did in held

    def links(_doc: object) -> list[DocumentLink]:
        return [
            DocumentLink(href="x", target="wsp:gutenberg-zim/Frankenstein.84"),
            DocumentLink(href="y", target=None),
        ]

    fake = SimpleNamespace(s=SimpleNamespace(source=specs.get), exists=exists, links=links)
    copies = cast(Any, Library.copies)

    def doc(native: str) -> Any:
        return SimpleNamespace(native_id=native)

    got = copies(fake, doc("Life_of_Marius"), specs["wikisource-en"])
    assert [(c.doc_id, c.why, c.address) for c in got] == [
        (
            "wsp:kiwix-wikisource-en-all/Life_of_Marius",
            "complete",
            "wsp://kiwix-wikisource-en-all/Life_of_Marius",
        )
    ]
    assert [c.doc_id for c in copies(fake, doc("Antimony"), specs["simple"])] == [
        "wsp:wikipedia-top10/en/Antimony"
    ]
    assert [c.doc_id for c in copies(fake, doc("84"), specs["gutenberg"])] == [
        "wsp:gutenberg-zim/Frankenstein.84"
    ]
    assert copies(fake, doc("Nothing"), specs["wikisource-en"]) == []
    assert copies(fake, doc("x"), _spec("plain")) == []
