# Backend audit, 2026-10-11

On 2026-10-11 a `wsp index build` of `wikipedia-top10` (32M documents) reached 21 GB RSS and 41 GB
of swap. Separately, the orchestrator was OOM-killed six times, and the API crashed three times. The
owner asked for a hostile, line-by-line audit of the whole backend.

Six independent auditors each covered one area. Every claim is measured, reproduced or reasoned
from the code, and each report says which. Line numbers refer to `06fde14`, the commit before the
fixes.

| Area | Report | Fix status |
|---|---|---|
| Index build path (`refinery`, `sync`, `lexical`, `suggest`, `changefeed`, `indexer`) | [index.md](index.md) | core fixed in `b9d64e6`; rest in progress |
| Rank, spelling, catalog, text, Oracle | [rank-oracle.md](rank-oracle.md) | in progress |
| Vault and fetch (`wsp-vault`, `wsp-fetch`, `wsp-core`) | [vault-fetch.md](vault-fetch.md) | in progress |
| Orchestrator (`wsp-orchestrator`, `wiring`, `locks`, units) | [orchestrator.md](orchestrator.md) | in progress |
| Adapters (`wsp-adapters`, doc-collection policy) | [adapters.md](adapters.md) | in progress |
| API, CLI, SDK | [api-cli.md](api-cli.md) | Parquet read race fixed in `b9d64e6`; rest in progress |

## Interim safety measures (2026-10-11)

* The orchestrator is stopped until these are fixed:
  * signing-key pinning (vault-fetch #1);
  * crash accounting (orchestrator C1);
  * the policy screen that is skipped after a digest check (orchestrator H3).
* The small-collections lane is paused until the doc-collection gate fails closed (adapters C3).
* No index build runs until the refine and index paths are fixed. The partial `wikipedia-top10`
  segments (80 GB, never committed) are collected on the next writer open.
* Every background job runs in `whisipedia-background.slice`, which is capped at 11 GiB with
  2 GiB of swap.
