"""Metalink v4 (RFC 5854) parsing and generation.

Kiwix (MirrorBrain) serves ``<file>.meta4`` with the full-file digests, a mirror list ordered by
priority and per-piece SHA-1 hashes; those let us verify every piece as it lands and re-fetch only a
bad piece, from a different mirror (PLAN §6.5).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Sequence

from wsp_core.metalink import NS as NS
from wsp_core.metalink import MetalinkError as MetalinkError
from wsp_core.metalink import MetalinkFile as MetalinkFile
from wsp_core.metalink import MetalinkUrl as MetalinkUrl
from wsp_core.metalink import Pieces as Pieces
from wsp_core.metalink import parse_metalink as parse_metalink
from wsp_fetch.hashing import normalize_algorithm

_Q = f"{{{NS}}}"


def build_metalink(files: Sequence[MetalinkFile]) -> bytes:
    """Serialize to Metalink v4 (used by tests and by the aria2 driver)."""
    ET.register_namespace("", NS)
    root = ET.Element(f"{_Q}metalink")
    for mf in files:
        f = ET.SubElement(root, f"{_Q}file", name=mf.name)
        if mf.size is not None:
            ET.SubElement(f, f"{_Q}size").text = str(mf.size)
        for c in mf.checksums:
            ET.SubElement(f, f"{_Q}hash", type=_iana(c.algorithm)).text = c.hexdigest
        if mf.pieces is not None:
            p = ET.SubElement(f, f"{_Q}pieces", length=str(mf.pieces.length), type=_iana(mf.pieces.algorithm))
            for h in mf.pieces.hashes:
                ET.SubElement(p, f"{_Q}hash").text = h
        for u in mf.urls:
            attrs = {"priority": str(u.priority)}
            if u.location:
                attrs["location"] = u.location
            ET.SubElement(f, f"{_Q}url", attrs).text = u.url
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _iana(algorithm: str) -> str:
    algo = normalize_algorithm(algorithm)
    return {"sha1": "sha-1", "sha256": "sha-256", "sha512": "sha-512", "sha384": "sha-384"}.get(algo, algo)
