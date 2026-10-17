"""Tiny real ZIM files for tests (written with libzim)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from libzim.writer import (  # pyright: ignore[reportMissingModuleSource]
    Creator,
    Hint,
    Item,
    StringProvider,
)


class _Item(Item):
    def __init__(self, path: str, mimetype: str, content: str) -> None:
        super().__init__()
        self._path, self._mimetype, self._content = path, mimetype, content

    def get_path(self) -> str:
        return self._path

    def get_title(self) -> str:
        return self._path

    def get_mimetype(self) -> str:
        return self._mimetype

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._content)

    def get_hints(self) -> dict[Hint, int]:
        return {Hint.FRONT_ARTICLE: self._mimetype == "text/html"}


def write_zim(
    path: Path,
    name: str,
    items: Sequence[tuple[str, str, str]],
    redirects: Sequence[tuple[str, str]] = (),
    metadata: Mapping[str, str] | None = None,
) -> Path:
    """A ZIM at ``path`` with ``Name`` metadata ``name``; ``items`` are ``(path, mimetype, content)``
    (the first is the main page) and ``redirects`` are ``(path, target)``; ``metadata`` adds more
    (``Scraper``, ``Source``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with Creator(path).config_indexing(False, "eng").config_verbose(False) as creator:
        creator.set_mainpath(items[0][0])
        for p, mime, body in items:
            creator.add_item(_Item(p, mime, body))
        for p, target in redirects:
            creator.add_redirection(p, p, target, {Hint.FRONT_ARTICLE: True})
        creator.add_metadata("Name", name)
        creator.add_metadata("Language", "eng")
        creator.add_metadata("Date", "2026-09-01")
        for key, value in (metadata or {}).items():
            creator.add_metadata(key, value)
    return path
