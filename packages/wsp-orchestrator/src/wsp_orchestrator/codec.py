"""JSON round-tripping of wsp-core value objects persisted in the local state store."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from wsp_core.model import Checksum, FetchPlan, FetchResult, ReleaseRef, RemoteFile

JsonObj = dict[str, Any]


def checksum_to_json(c: Checksum) -> JsonObj:
    return {"algorithm": c.algorithm, "hexdigest": c.hexdigest}


def checksum_from_json(d: Mapping[str, Any]) -> Checksum:
    return Checksum(algorithm=str(d["algorithm"]), hexdigest=str(d["hexdigest"]))


def remote_to_json(r: RemoteFile) -> JsonObj:
    return {
        "url": r.url,
        "name": r.name,
        "size": r.size,
        "checksums": [checksum_to_json(c) for c in r.checksums],
        "mirrors": list(r.mirrors),
        "metalink": r.metalink,
    }


def remote_from_json(d: Mapping[str, Any]) -> RemoteFile:
    size = d.get("size")
    return RemoteFile(
        url=str(d["url"]),
        name=str(d["name"]),
        size=int(size) if size is not None else None,
        checksums=tuple(checksum_from_json(c) for c in cast(list[Mapping[str, Any]], d.get("checksums", []))),
        mirrors=tuple(str(m) for m in cast(list[str], d.get("mirrors", []))),
        metalink=str(d["metalink"]) if d.get("metalink") else None,
    )


def release_to_json(r: ReleaseRef) -> JsonObj:
    return {
        "source_id": r.source_id,
        "version": r.version,
        "published": r.published.isoformat() if r.published else None,
        "files": [remote_to_json(f) for f in r.files],
        "meta": dict(r.meta),
        "license": r.license,
        "format": r.format,
    }


def release_from_json(d: Mapping[str, Any]) -> ReleaseRef:
    published = d.get("published")
    return ReleaseRef(
        source_id=str(d["source_id"]),
        version=str(d["version"]),
        published=datetime.fromisoformat(published) if published else None,
        files=tuple(remote_from_json(f) for f in cast(list[Mapping[str, Any]], d["files"])),
        meta={str(k): str(v) for k, v in cast(Mapping[str, Any], d.get("meta", {})).items()},
        license=str(d["license"]) if d.get("license") else None,
        format=str(d["format"]) if d.get("format") else None,
    )


def plan_to_json(p: FetchPlan) -> JsonObj:
    return {
        "release": release_to_json(p.release),
        "files": [remote_to_json(f) for f in p.files],
        "previous_generation": p.previous_generation,
    }


def plan_from_json(d: Mapping[str, Any]) -> FetchPlan:
    prev = d.get("previous_generation")
    return FetchPlan(
        release=release_from_json(cast(Mapping[str, Any], d["release"])),
        files=tuple(remote_from_json(f) for f in cast(list[Mapping[str, Any]], d["files"])),
        previous_generation=str(prev) if prev is not None else None,
    )


def fetch_result_to_json(r: FetchResult) -> JsonObj:
    out: JsonObj = {
        "path": str(r.path),
        "size": r.size,
        "sha256": r.sha256,
        "blake3": r.blake3,
        "verified": [checksum_to_json(c) for c in r.verified],
    }
    if r.corroborated:
        out["corroborated"] = True
    if r.pieces_verified:
        out["pieces_verified"] = r.pieces_verified
    provenance = getattr(r, "provenance", None)  # wsp_fetch.FetchReport (ADR-0009); JSON-safe
    if isinstance(provenance, Mapping) and provenance:
        out["provenance"] = dict(cast(Mapping[str, Any], provenance))
    return out


def fetch_result_from_json(d: Mapping[str, Any]) -> FetchResult:
    return FetchResult(
        path=Path(str(d["path"])),
        size=int(d["size"]),
        sha256=str(d["sha256"]),
        blake3=str(d["blake3"]),
        verified=tuple(checksum_from_json(c) for c in cast(list[Mapping[str, Any]], d.get("verified", []))),
        corroborated=bool(d.get("corroborated", False)),
        pieces_verified=str(d.get("pieces_verified") or ""),
    )
