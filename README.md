# Whisipedia

A local, verified, self-maintaining library of the best openly available stores of human knowledge, surfed through a late-80s/90s-style offline web browser with a built-in natural-language search engine (an "offline Google").

> **Status:** planning. Nothing in this repository downloads data yet.

- **Plan:** [`docs/PLAN.md`](docs/PLAN.md)
- **Source research:** [`docs/research/SOURCES.md`](docs/research/SOURCES.md)
- **Source registry (machine-readable):** [`registry/sources.yaml`](registry/sources.yaml)
- **Architecture decisions:** [`docs/adr/`](docs/adr/)

## Principles
1. **Known good.** Every stored byte is covered by a signed manifest and re-verified on a schedule.
2. **Human-readable underneath.** The data on disk can be browsed without this software.
3. **Offline by construction.** Only the fetcher talks to the network.
4. **Swappable parts.** Ports and adapters, with contract tests.
5. **Golden data is never changed in place.** Whisipedia Living works on an overlay.

## License
Proprietary, all rights reserved, until a license is chosen (see PLAN §16, D8). Content mirrored by this software is governed by each upstream source's license.
