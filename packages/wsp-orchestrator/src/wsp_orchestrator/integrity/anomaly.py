"""Content anomaly detection before promotion: classic statistics, no models (ADR-0009).

A :class:`ContentProfile` summarizes one generation: document counts per namespace and MIME type, a
log2 byte-size histogram, and a deterministic *sample* of documents (chosen by a hash of their key,
so the same keys are sampled in every generation) with a short content hash, size and title. HTML
in the sample is also counted for ``<script`` tags, binary bytes and executable signatures.

:func:`compare_profiles` sets the new profile against the last promoted one and checks:

* per-namespace count changes and new namespaces;
* the total-variation distance between the size histograms;
* the fraction of sampled documents that changed, and the fraction deleted (mass deletions);
* MIME types never seen before (and not allow-listed);
* ``<script>`` density per MiB of HTML (ratio and absolute jump);
* binary/executable content in places that held text.

The result is a :class:`PromotionReport` (checks with value and limit, the most changed titles, a
verdict). Thresholds come from ``[integrity.anomaly]``; anything out of range holds the release for
``wsp approve`` / ``wsp reject``. The legacy count/size trend checks (PLAN §6.4) are part of it.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

PROFILE_FORMAT = "whisipedia-content-profile/1"
REPORT_FORMAT = "whisipedia-promotion-report/1"
_SCRIPT = re.compile(rb"<script\b", re.IGNORECASE)
_EXEC_MAGIC = (b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xce", b"\xcf\xfa\xed\xfe", b"#!")
_EXEC_MIMES = frozenset(
    {
        "application/x-msdownload",
        "application/x-dosexec",
        "application/x-executable",
        "application/x-elf",
        "application/x-mach-binary",
        "application/x-sh",
        "application/vnd.microsoft.portable-executable",
        "application/java-archive",
    }
)
MIB = 1024 * 1024


def _bucket(size: int) -> str:
    return "0" if size <= 0 else str(int(math.log2(size)))


def sample_key(key: str) -> float:
    """Deterministic position of ``key`` in [0, 1): the same keys are sampled in every generation."""
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big") / 2**64


def short_hash(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return hashlib.blake2b(raw, digest_size=8).hexdigest()


def is_executable(content: bytes, mimetype: str = "") -> bool:
    return mimetype.lower() in _EXEC_MIMES or content.startswith(_EXEC_MAGIC)


def _is_binary(content: bytes) -> bool:
    head = content[:4096]
    return b"\x00" in head


@dataclass
class ProfileBuilder:
    """Accumulates a :class:`ContentProfile` from documents in any order."""

    sample_rate: float = 1 / 16
    doc_count: int = 0
    total_bytes: int = 0
    namespaces: Counter[str] = field(default_factory=Counter[str])
    mimetypes: Counter[str] = field(default_factory=Counter[str])
    histogram: Counter[str] = field(default_factory=Counter[str])
    sample: dict[str, list[object]] = field(default_factory=dict[str, list[object]])
    html_bytes: int = 0
    script_tags: int = 0
    binary_html: int = 0
    executables: int = 0
    html_sampled: int = 0

    def wants(self, key: str) -> bool:
        return sample_key(key) < self.sample_rate

    def add(
        self,
        key: str,
        *,
        namespace: str = "main",
        mimetype: str = "",
        size: int | None = 0,
        title: str = "",
        content: bytes | None = None,
        content_hash: str | None = None,
    ) -> None:
        """One document. ``content`` (raw bytes) or ``content_hash`` matter only for sampled keys;
        ``size=None`` (unknown without reading the body) leaves the histogram to the sample."""
        self.doc_count += 1
        self.namespaces[namespace] += 1
        if mimetype:
            self.mimetypes[mimetype.split(";", 1)[0].strip().lower()] += 1
        sampled = self.wants(key)
        if size is None and sampled and content is not None:
            size = len(content)
        if size is not None:
            self.total_bytes += size
            self.histogram[_bucket(size)] += 1
        if not sampled:
            return
        digest = content_hash or (short_hash(content) if content is not None else "")
        self.sample[key] = [digest, size or 0, title[:120]]
        if content is not None:
            self._scan(content, mimetype)

    def add_blob(self, key: str, *, mimetype: str = "", content: bytes | None = None) -> None:
        """Container-level evidence (a ZIM entry) for a profile whose documents come from elsewhere:
        counts the MIME type and, for sampled keys, scans the raw bytes. Not a document."""
        if mimetype:
            self.mimetypes[mimetype.split(";", 1)[0].strip().lower()] += 1
        if content is not None and self.wants(key):
            self._scan(content, mimetype)

    def _scan(self, content: bytes, mimetype: str) -> None:
        if is_executable(content, mimetype):
            self.executables += 1
        if "html" in mimetype.lower():
            self.html_sampled += 1
            self.html_bytes += len(content)
            self.script_tags += len(_SCRIPT.findall(content))
            if _is_binary(content):
                self.binary_html += 1

    def build(self) -> ContentProfile:
        return ContentProfile(
            doc_count=self.doc_count,
            total_bytes=self.total_bytes,
            namespaces=dict(self.namespaces),
            mimetypes=dict(self.mimetypes),
            size_histogram=dict(self.histogram),
            sample_rate=self.sample_rate,
            sample={k: (str(v[0]), cast(int, v[1]), str(v[2])) for k, v in self.sample.items()},
            html_sampled=self.html_sampled,
            html_bytes=self.html_bytes,
            script_tags=self.script_tags,
            binary_html=self.binary_html,
            executables=self.executables,
        )


@dataclass(frozen=True, slots=True)
class ContentProfile:
    doc_count: int
    total_bytes: int
    namespaces: Mapping[str, int]
    mimetypes: Mapping[str, int]
    size_histogram: Mapping[str, int]
    sample_rate: float
    sample: Mapping[str, tuple[str, int, str]]  # key -> (content hash, size, title)
    html_sampled: int = 0
    html_bytes: int = 0
    script_tags: int = 0
    binary_html: int = 0
    executables: int = 0

    @property
    def script_density(self) -> float:
        """``<script`` tags per MiB of sampled HTML."""
        return self.script_tags / (self.html_bytes / MIB) if self.html_bytes else 0.0

    @property
    def binary_fraction(self) -> float:
        return self.binary_html / self.html_sampled if self.html_sampled else 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "format": PROFILE_FORMAT,
            "doc_count": self.doc_count,
            "total_bytes": self.total_bytes,
            "namespaces": dict(self.namespaces),
            "mimetypes": dict(self.mimetypes),
            "size_histogram": dict(self.size_histogram),
            "sample_rate": self.sample_rate,
            "sample": {k: list(v) for k, v in self.sample.items()},
            "html_sampled": self.html_sampled,
            "html_bytes": self.html_bytes,
            "script_tags": self.script_tags,
            "binary_html": self.binary_html,
            "executables": self.executables,
        }

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> ContentProfile:
        if doc.get("format") != PROFILE_FORMAT:
            raise ValueError(f"unsupported profile format {doc.get('format')!r}")
        sample = cast(Mapping[str, Sequence[Any]], doc.get("sample", {}))
        return cls(
            doc_count=int(doc["doc_count"]),
            total_bytes=int(doc["total_bytes"]),
            namespaces={str(k): int(v) for k, v in cast(Mapping[str, Any], doc["namespaces"]).items()},
            mimetypes={str(k): int(v) for k, v in cast(Mapping[str, Any], doc["mimetypes"]).items()},
            size_histogram={
                str(k): int(v) for k, v in cast(Mapping[str, Any], doc["size_histogram"]).items()
            },
            sample_rate=float(doc["sample_rate"]),
            sample={str(k): (str(v[0]), int(v[1]), str(v[2])) for k, v in sample.items()},
            html_sampled=int(doc.get("html_sampled", 0)),
            html_bytes=int(doc.get("html_bytes", 0)),
            script_tags=int(doc.get("script_tags", 0)),
            binary_html=int(doc.get("binary_html", 0)),
            executables=int(doc.get("executables", 0)),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")
        tmp.write_bytes(gzip.compress(json.dumps(self.to_json(), sort_keys=True).encode(), mtime=0))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> ContentProfile:
        return cls.from_json(cast(Mapping[str, Any], json.loads(gzip.decompress(path.read_bytes()))))


def merge_profiles(profiles: Iterable[ContentProfile]) -> ContentProfile:
    """Several files of one generation (e.g. the books of a Kiwix bundle) as one profile."""
    items = list(profiles)
    if not items:
        return ProfileBuilder().build()

    def total(attr: str) -> int:
        return sum(cast(int, getattr(p, attr)) for p in items)

    def counts(attr: str) -> dict[str, int]:
        out: Counter[str] = Counter()
        for p in items:
            out.update(cast(Mapping[str, int], getattr(p, attr)))
        return dict(out)

    sample: dict[str, tuple[str, int, str]] = {}
    for p in items:
        sample.update(p.sample)
    return ContentProfile(
        doc_count=total("doc_count"),
        total_bytes=total("total_bytes"),
        namespaces=counts("namespaces"),
        mimetypes=counts("mimetypes"),
        size_histogram=counts("size_histogram"),
        sample_rate=min(p.sample_rate for p in items),
        sample=sample,
        html_sampled=total("html_sampled"),
        html_bytes=total("html_bytes"),
        script_tags=total("script_tags"),
        binary_html=total("binary_html"),
        executables=total("executables"),
    )


# --- thresholds and checks ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnomalyThresholds:
    max_namespace_change: float = 0.10  # relative change of any namespace with >= min_namespace_docs
    min_namespace_docs: int = 100
    max_new_namespace_fraction: float = 0.01  # a namespace that did not exist, as a share of docs
    max_histogram_distance: float = 0.10  # total-variation distance of the size histograms
    max_changed_fraction: float = 0.35  # sampled documents whose content changed
    max_deleted_fraction: float = 0.02  # sampled documents that disappeared
    min_sample: int = 50  # below this, sample-based checks are reported but never hold
    mime_allowlist: frozenset[str] = frozenset()  # new MIME types that never hold
    max_script_ratio: float = 1.5  # script density new/old
    min_script_jump: float = 2.0  # ... and at least this many more tags per MiB
    max_binary_jump: float = 0.01  # increase of the binary fraction of sampled HTML
    max_new_executables: int = 0  # more sampled executables than before
    top_changed: int = 20

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any]) -> AnomalyThresholds:
        base = cls()
        kwargs: dict[str, Any] = {}
        for name in cls.__dataclass_fields__:
            if name not in cfg:
                continue
            default = getattr(base, name)
            value = cfg[name]
            if isinstance(default, frozenset):
                kwargs[name] = frozenset(str(x).lower() for x in cast(Iterable[object], value))
            elif isinstance(default, int) and not isinstance(default, bool):
                kwargs[name] = int(value)
            else:
                kwargs[name] = float(value)
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    value: float | None
    limit: float | None
    detail: str
    holds: bool = True  # False: informational (e.g. sample too small)

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "value": self.value,
            "limit": self.limit,
            "detail": self.detail,
            "holds": self.holds,
        }


def histogram_distance(a: Mapping[str, int], b: Mapping[str, int]) -> float:
    ta, tb = sum(a.values()), sum(b.values())
    if not ta or not tb:
        return 0.0
    return 0.5 * sum(abs(a.get(k, 0) / ta - b.get(k, 0) / tb) for k in set(a) | set(b))


def compare_profiles(
    old: ContentProfile,
    new: ContentProfile,
    t: AnomalyThresholds = AnomalyThresholds(),  # noqa: B008
) -> tuple[list[Check], list[Mapping[str, object]]]:
    """Checks of ``new`` against ``old`` and the most changed sampled titles."""
    checks: list[Check] = []
    for ns in sorted(set(old.namespaces) | set(new.namespaces)):
        before, after = old.namespaces.get(ns, 0), new.namespaces.get(ns, 0)
        if before >= t.min_namespace_docs:
            change = (after - before) / before
            checks.append(
                Check(
                    f"namespace:{ns}",
                    abs(change) <= t.max_namespace_change,
                    round(change, 4),
                    t.max_namespace_change,
                    f"{before} -> {after} documents ({change:+.1%})",
                )
            )
        elif before == 0 and after and new.doc_count:
            share = after / new.doc_count
            checks.append(
                Check(
                    f"namespace:{ns}",
                    share <= t.max_new_namespace_fraction,
                    round(share, 4),
                    t.max_new_namespace_fraction,
                    f"new namespace with {after} documents ({share:.1%} of all)",
                )
            )
    dist = histogram_distance(old.size_histogram, new.size_histogram)
    checks.append(
        Check(
            "size-histogram",
            dist <= t.max_histogram_distance,
            round(dist, 4),
            t.max_histogram_distance,
            f"total-variation distance {dist:.3f} between byte-size distributions",
        )
    )
    common = set(old.sample) & set(new.sample)
    gone = set(old.sample) - set(new.sample)
    enough = len(old.sample) >= t.min_sample
    changed = [k for k in common if old.sample[k][0] != new.sample[k][0]]
    frac_changed = len(changed) / len(common) if common else 0.0
    frac_deleted = len(gone) / len(old.sample) if old.sample else 0.0
    checks.append(
        Check(
            "changed-fraction",
            frac_changed <= t.max_changed_fraction or not enough,
            round(frac_changed, 4),
            t.max_changed_fraction,
            f"{len(changed)} of {len(common)} sampled documents changed",
            holds=enough,
        )
    )
    checks.append(
        Check(
            "deleted-fraction",
            frac_deleted <= t.max_deleted_fraction or not enough,
            round(frac_deleted, 4),
            t.max_deleted_fraction,
            f"{len(gone)} of {len(old.sample)} sampled documents are gone",
            holds=enough,
        )
    )
    new_mimes = sorted(m for m in new.mimetypes if m not in old.mimetypes and m not in t.mime_allowlist)
    checks.append(
        Check(
            "new-mimetypes",
            not new_mimes,
            float(len(new_mimes)),
            0.0,
            f"MIME types never seen before: {', '.join(new_mimes)}" if new_mimes else "no new MIME types",
        )
    )
    before_d, after_d = old.script_density, new.script_density
    ratio = after_d / before_d if before_d else (math.inf if after_d else 1.0)
    jump = after_d - before_d
    script_ok = not (ratio > t.max_script_ratio and jump > t.min_script_jump)
    checks.append(
        Check(
            "script-density",
            script_ok,
            round(after_d, 3),
            round(before_d * t.max_script_ratio, 3),
            f"<script> per MiB of HTML {before_d:.2f} -> {after_d:.2f}",
        )
    )
    binary_jump = new.binary_fraction - old.binary_fraction
    checks.append(
        Check(
            "binary-html",
            binary_jump <= t.max_binary_jump,
            round(new.binary_fraction, 4),
            round(old.binary_fraction + t.max_binary_jump, 4),
            f"binary bytes in sampled HTML {old.binary_fraction:.2%} -> {new.binary_fraction:.2%}",
        )
    )
    exec_jump = new.executables - old.executables
    checks.append(
        Check(
            "executables",
            exec_jump <= t.max_new_executables,
            float(new.executables),
            float(old.executables + t.max_new_executables),
            f"executable signatures in the sample {old.executables} -> {new.executables}",
        )
    )
    top = sorted(changed, key=lambda k: abs(new.sample[k][1] - old.sample[k][1]), reverse=True)[
        : t.top_changed
    ]
    top_changed: list[Mapping[str, object]] = [
        {
            "key": k,
            "title": new.sample[k][2] or old.sample[k][2],
            "old_size": old.sample[k][1],
            "new_size": new.sample[k][1],
        }
        for k in top
    ]
    top_changed += [
        {"key": k, "title": old.sample[k][2], "old_size": old.sample[k][1], "new_size": None}
        for k in sorted(gone)[: max(0, t.top_changed - len(top_changed))]
    ]
    return checks, top_changed


@dataclass(frozen=True, slots=True)
class PromotionReport:
    source_id: str
    generation_id: str
    baseline: str | None  # generation compared against (last promoted with a profile)
    checks: tuple[Check, ...]
    top_changed: tuple[Mapping[str, object], ...]
    at: datetime
    profile: Mapping[str, object] = field(default_factory=dict[str, object])  # summary, not the sample
    decision: str | None = None  # "approved: <note>" when the owner overrode a hold

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.ok and c.holds)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def verdict(self) -> str:
        if self.ok:
            return "pass"
        return "approved" if self.decision else "hold"

    def reasons(self) -> str:
        return "; ".join(f"{c.name}: {c.detail} (limit {c.limit})" for c in self.failed)

    def to_json(self) -> dict[str, object]:
        return {
            "report_format": REPORT_FORMAT,
            "baseline": self.baseline,
            "verdict": self.verdict,
            "decision": self.decision,
            "checked_at": self.at.isoformat(),
            "checks": [c.to_json() for c in self.checks],
            "top_changed": [dict(x) for x in self.top_changed],
            "profile": dict(self.profile),
        }


def profile_summary(p: ContentProfile) -> dict[str, object]:
    return {
        "doc_count": p.doc_count,
        "total_bytes": p.total_bytes,
        "namespaces": dict(p.namespaces),
        "mimetypes": dict(p.mimetypes),
        "size_histogram": dict(p.size_histogram),
        "sample_rate": p.sample_rate,
        "sampled": len(p.sample),
        "script_density": round(p.script_density, 3),
        "binary_fraction": round(p.binary_fraction, 4),
        "executables": p.executables,
    }
