"""Partitioned sources (ADR-0010): one source acquired as independent partitions.

A very large collection (USGS historical topographic maps per US state, the CIA reading room in
batches of 10,000 items) is fetched one partition per run. Each run seals a generation whose id
carries the partition after the last dot::

    <cycle>.<partition>        2026-10-11.ca    2026-10-11.b0001

A partition is *not* a newer version of another partition. The source's content is the **current
set**: the current generation of every partition. A later generation of the *same* partition
replaces that partition's earlier one; retention keeps history per partition.

The registry marks such a source with ``access.partitioned: true`` (:attr:`SourceSpec.partitioned`).
Partition names are lowercase letters, digits and hyphens, starting with a letter or digit.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence

PARTITION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class PartitionError(ValueError):
    pass


def check_partition(name: str) -> str:
    """``name`` if it is a valid partition name, else :class:`PartitionError`."""
    if not PARTITION_RE.match(name):
        raise PartitionError(f"invalid partition name {name!r} (lowercase letters, digits and '-')")
    return name


def partition_of(generation_id: str) -> str | None:
    """The partition a generation id names (text after the last dot), or ``None`` if it names none.

    Only meaningful for a partitioned source: ``2026-10-11.ca`` -> ``ca``, ``2026-10-11`` -> ``None``.
    """
    cycle, sep, part = generation_id.rpartition(".")
    if not sep or not cycle or not PARTITION_RE.match(part):
        return None
    return part


def partition_generation_id(cycle: str, partition: str) -> str:
    """``<cycle>.<partition>``; a ``cycle`` that already ends with ``.<partition>`` is kept as is."""
    check_partition(partition)
    if partition_of(cycle) == partition:
        return cycle
    return f"{cycle}.{partition}"


def group_by_partition[T](items: Iterable[T], generation_id: Callable[[T], str]) -> dict[str | None, list[T]]:
    """Items grouped by the partition of their generation id (``None``: ids that name no partition)."""
    out: dict[str | None, list[T]] = {}
    for item in items:
        out.setdefault(partition_of(generation_id(item)), []).append(item)
    return out


def same_partition(generation_ids: Sequence[str], generation_id: str) -> list[str]:
    """The ids of ``generation_ids`` in the same partition as ``generation_id`` (order kept)."""
    want = partition_of(generation_id)
    return [g for g in generation_ids if partition_of(g) == want]


def current_set_ids(pointers: Mapping[str, str], current: str | None) -> list[str]:
    """Current set from per-partition pointers plus the source pointer (oldest id first).

    ``current`` (``CURRENT.txt``: the most recently promoted generation) joins the set when its
    partition has no pointer of its own, e.g. a generation promoted before the source was partitioned.
    """
    ids = dict(pointers)
    if current is not None:
        key = partition_of(current)
        if key is None or key not in ids:
            ids.setdefault(key if key is not None else "", current)
    return sorted(set(ids.values()))


__all__ = [
    "PARTITION_RE",
    "PartitionError",
    "check_partition",
    "current_set_ids",
    "group_by_partition",
    "partition_generation_id",
    "partition_of",
    "same_partition",
]
