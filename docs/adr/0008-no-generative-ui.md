# ADR-0008: No generative AI in the user experience

- Status: Accepted
- Date: 2026-10-10
- Supersedes: the LLM part of [ADR-0007](0007-oracle-models.md); amends ADR-0006 (`/v1/summarize`) and
  ADR-0005 (the `Generator` port)

## Context
Phase 3 added an optional "Summarize these results" button: a local LLM (Qwen3.5-9B through
llama.cpp) wrote a short summary of the top passages, with sentence-level citation checks. The owner
reviewed it and decided that the Navigator should feel like the authentic late-90s web: ten blue
links, snippets, directories and hyperlinks. Generated prose does not fit that, however well it is
cited.

The owner was explicit about the boundary:

1. **No generative AI anywhere the user reads.** No summaries, no generated answers, no chat.
2. **The backend may use every modern ranking technique**: embeddings, dense retrieval, cross-encoder
   reranking, learned ranking, link analysis. These decide the *order* of real documents; they do not
   write text.
3. **Extractive features stay**: snippets, facets, "did you mean", and instant-answer boxes built from
   structured data (a Wiktionary definition, Wikidata facts, a map locator), because they show the
   library's own words.

## Decision
Remove the summary feature end to end:

| Layer | Removed |
|---|---|
| Navigator | "Summarize these results" button and panel, `WspClient.summarize`, mock summarizer, styles, help text |
| API | `POST /v1/summarize`; schemas `SummarizeRequest`, `SummaryToken`, `SummaryCitation`, `SummaryNotice`, `SummaryDone`, `SummaryError`, `SummaryEvent`; `Services.generator`; `wsp_api.summaries` |
| SDK | `WspClient.summarize`, `AsyncWspClient.summarize`, the summary models |
| Core | the `Generator` port (port revision v0.3). Nothing else used it. |
| Oracle | `wsp_oracle.generator` (llama.cpp client, `Summarizer`, `CitationGuard`, `CitedGenerator`), `wsp-oracle-llm` (`llmserver.py`), `[oracle.generator]` config, summary faithfulness evaluation, the `httpx` dependency |
| CLI / config | the `Generator` wiring in `wsp_cli.bootstrap`, `[api] demo_generator` |

An `[oracle.generator]` table in a local config is now rejected as an unknown key, so a stale config
fails loudly instead of silently doing nothing.

The dense retriever and the cross-encoder reranker stay. The reranker's probability remains the
retrieval-confidence signal behind `low_confidence`.

### API version
`API_VERSION` goes from 1.2.0 to **1.3.0**. Removing an endpoint is breaking, but v1 is not frozen
until Phase 6 (PLAN §11.1), and ADR-0006 set the precedent of a minor bump for pre-freeze breaking
changes. The same release adds `SearchResults.scope` (site scoping, see `docs/components/api.md`),
which is additive. Clients built against 1.2.0 keep working except for `summarize`, which now
answers 404 (no route) or 405.

## Consequences
- The Navigator's results page is fully extractive. "Did you mean", PageRank-based static rank and
  site-scoped totals replace the summary as the Phase 3 search improvements.
- No LLM runs on the workstation for the library, which frees ~7.5 GB of VRAM and removes the
  llama.cpp install step. The weights under `~/.cache/whisipedia/models/llm/` can be deleted.
- Whisipedia Living (PLAN §11) is unaffected: it is a separate project whose models write an overlay,
  never the Navigator's pages.
- Reintroducing generated text in the UI would need a new ADR and the owner's decision.
