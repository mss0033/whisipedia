"""``wsp index rebuild-lexical``: a fresh search index beside the live one, from the indexed generations.

A schema change (new fast columns, ADR-0011) needs every document indexed again. ``wsp index build``
on an empty index root would also refine and republish the whole library: 256M "added" change records
into a new feed epoch, every client cursor invalidated, though no document changed. This rebuild:

* reads the Refinery generations that ``index-state.json`` records as indexed, and nothing else;
* writes only into the new lexical directory: no change records, no index state, no catalog;
* commits and checkpoints every ``commit_bytes`` of Refinery parts (``REBUILD.json``), so a stopped
  run resumes; few large commits, because each commit's segments are written and merged on the
  index's file system, and on the NAS every file costs network round trips;
* paces its reads of the Refinery (``read_limiter``): on the NAS the site reads documents over the
  same link, and an unpaced bulk read starves it;
* runs without the index lock, so the live index keeps serving and being updated meanwhile; then,
  under the lock, catches up with every generation indexed live during the rebuild, carries over the
  static rank and spelling vocabulary (keyed by doc id and word, not by index) and builds the suggest
  trie once.

The switch is done by hand (docs/runbooks/move-to-nas.md): stop the API, put the new directory in
place of ``lexical/``, start the API with the code that reads the new schema.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from wsp_index import TantivyIndex, refresh
from wsp_index.linkrank import RANK_FILE, fence_path
from wsp_index.refinery import COLUMNS, RefineryReader, generation_dir, is_sealed, record_from_row
from wsp_index.spelling import SPELL_FILE
from wsp_vault.ratelimit import RateLimiter

CHECKPOINT = "REBUILD.json"
CATCH_UP_ROUNDS = 3
COMMIT_BYTES = 2 << 30  # of Arrow table data between commits
ROWS_PER_UPSERT = 256  # rows turned into Python objects at a time (a part of whole books is gigabytes)


@dataclass(frozen=True, slots=True)
class RelexReport:
    sources: int
    documents: int
    caught_up: tuple[str, ...]


class Relexer:
    """Builds ``index`` (a fresh :class:`TantivyIndex`) from the generations ``state()`` names."""

    def __init__(
        self,
        index: TantivyIndex,
        refinery: Path,
        state: Callable[[], Mapping[str, str]],
        *,
        say: Callable[[str], None] = lambda _: None,
        commit_bytes: int = COMMIT_BYTES,
        read_limiter: RateLimiter | None = None,
    ) -> None:
        self.index = index
        self.commit_bytes = commit_bytes
        self.read_limiter = read_limiter
        self.refinery = refinery
        self.state = state
        self.say = say
        self.path = index.path / CHECKPOINT

    # --- checkpoint ----------------------------------------------------------------------------

    def _load(self) -> dict[str, dict[str, object]]:
        if not self.path.is_file():
            return {}
        return dict(json.loads(self.path.read_text(encoding="utf-8")).get("sources", {}))

    def _save(self, done: Mapping[str, Mapping[str, object]]) -> None:
        partial = self.path.with_name(CHECKPOINT + ".partial")
        partial.write_text(json.dumps({"sources": done}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        with partial.open("rb") as fh:
            os.fsync(fh.fileno())
        os.replace(partial, self.path)

    # --- passes --------------------------------------------------------------------------------

    def load(self) -> int:
        """Index every recorded generation, part by part (resumes from the checkpoint).

        Before a commit the checkpoint records the parts it covers (``replace_until``): a run stopped
        between the commit and the checkpoint replaces those parts on resuming, never adds them twice.
        """
        done = self._load()
        total = 0
        for sid, gen in sorted(self.state().items()):
            gen_dir = generation_dir(self.refinery, sid, gen)
            if not is_sealed(gen_dir):
                self.say(f"relex: {sid}/{gen} is not in the Refinery; skipped")
                continue
            mark = done.get(sid, {})
            if mark.get("generation") != gen:
                if mark:  # started on another generation: the catch-up pass reconciles it
                    continue
                mark = {"generation": gen, "parts": 0}
            reader = RefineryReader(gen_dir, map_ids=False)
            start = int(str(mark.get("parts", 0)))
            if start >= reader.n_parts:
                continue
            # A resumed run may find parts committed already: they are replaced, never added twice.
            replace_until = int(str(mark.get("replace_until", start + 1 if start > 0 else 0)))
            batch_start, pending = start, 0
            for part in range(start, reader.n_parts):
                if self.read_limiter is not None:
                    self.read_limiter.consume(reader.parts[part].stat().st_size)
                table, _ = reader.part_table(part, COLUMNS)
                pending += table.nbytes
                # Rows become Python objects a batch at a time, never a whole part: a Gutenberg part
                # (whole books) is ~2 GB of strings and dicts, and the job swapped at 5 GB under a
                # 3.5 GB ceiling (2026-10-13). Tantivy copies each batch into its own heap.
                for batch in table.to_batches(max_chunksize=ROWS_PER_UPSERT):
                    docs = [record_from_row(row) for row in batch.to_pylist()]
                    total += self.index.upsert(docs, replace=part < replace_until)
                del table
                last = part == reader.n_parts - 1
                if pending >= self.commit_bytes or last:
                    done[sid] = {"generation": gen, "parts": batch_start, "replace_until": part + 1}
                    self._save(done)
                    self.index.commit(rebuild_suggest=False)
                    done[sid] = {"generation": gen, "parts": part + 1}
                    self._save(done)
                    self.say(f"relex: {sid}/{gen}: part {part + 1}/{reader.n_parts}")
                    batch_start, pending = part + 1, 0
        return total

    def catch_up(self) -> list[str]:
        """Bring the new index to the live state: generations indexed live meanwhile (diffed, without
        change records), sources added or no longer indexed. Run under the index lock."""
        changed: list[str] = []
        for _ in range(CATCH_UP_ROUNDS):
            done = self._load()
            live = dict(self.state())
            stale = sorted(
                sid
                for sid in set(done) | set(live)
                if sid not in live
                or sid not in done
                or done[sid].get("generation") != live[sid]
                or not self._complete(sid, live[sid], done)
            )
            if not stale:
                return changed
            for sid in stale:
                changed.append(sid)
                if sid not in live:
                    self.index.delete_source(sid)
                    self.index.commit(rebuild_suggest=False)
                    del done[sid]
                    self._save(done)
                    self.say(f"relex: {sid}: no longer indexed; removed")
                    continue
                new_dir = generation_dir(self.refinery, sid, live[sid])
                if not is_sealed(new_dir):
                    self.say(
                        f"relex: {sid}/{live[sid]} is not in the Refinery at {self.refinery}; NOT caught up"
                    )
                    continue
                new = RefineryReader(new_dir, map_ids=False)
                old_gen = str(done.get(sid, {}).get("generation") or "")
                old_dir = generation_dir(self.refinery, sid, old_gen) if old_gen else None
                old = RefineryReader(old_dir, map_ids=False) if old_dir and is_sealed(old_dir) else None
                if old is not None and not self._complete(sid, old_gen, done):
                    old = None  # partly loaded: reload it whole (stale rows are found from other gens)
                stats = refresh(self.index, None, old, new, progress=self.say)
                self.index.commit(rebuild_suggest=False)
                done[sid] = {"generation": live[sid], "parts": new.n_parts}
                self._save(done)
                self.say(f"relex: {sid}: caught up to {live[sid]} (+{stats.upserted:,} -{stats.deleted:,})")
        return changed

    def _complete(self, sid: str, gen: str, done: Mapping[str, Mapping[str, object]]) -> bool:
        mark = done.get(sid)
        if not mark or mark.get("generation") != gen:
            return False
        gen_dir = generation_dir(self.refinery, sid, gen)
        if not is_sealed(gen_dir):
            return False
        return int(str(mark.get("parts", 0))) >= RefineryReader(gen_dir, map_ids=False).n_parts

    def finish(self, live_lexical: Path) -> None:
        """Carry over the static rank and the spelling vocabulary, then build the suggest trie once."""
        for name in (RANK_FILE, fence_path(Path(RANK_FILE)).name, SPELL_FILE):
            src, dst = live_lexical / name, self.index.path / name
            if src.is_file() and not dst.exists():
                partial = dst.with_name(dst.name + ".partial")
                partial.unlink(missing_ok=True)
                try:
                    os.link(src, partial)  # same disk: no copy (writers replace these files, never edit)
                except OSError:
                    shutil.copyfile(src, partial)
                os.replace(partial, dst)
                self.say(f"relex: carried over {name}")
        self.index.reload_static_rank()
        self.index.mark_suggest_stale()
        self.index.commit()
        self.say("relex: suggest trie built")


def relex(
    relexer: Relexer, live_lexical: Path, lock: Callable[[], AbstractContextManager[object]]
) -> RelexReport:
    """The whole rebuild: load without the lock, then catch up and finish under it."""
    documents = relexer.load()
    with lock():
        caught = relexer.catch_up()
        relexer.finish(live_lexical)
    return RelexReport(len(relexer.state()), documents, tuple(caught))
