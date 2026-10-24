# ADR-0013: Labelled machine translation of non-English documents

- Status: Accepted (owner decision D19, 2026-10-24); implementation pending
- Date: 2026-10-24
- Amends: [ADR-0008](0008-no-generative-ui.md) (no generative AI where the user reads), for this one use

## Context
The library holds large non-English collections: ten Wikipedias, DILA's French law, EU law in 24
languages, and Japanese, Russian and Arabic sources. The owner reads English. ADR-0008 keeps generated
text out of every page the reader sees. Its reason is that the Navigator shows the library's own words.

The owner asked for machine translation into English. Every translated page must carry an obvious
warning that the content was machine translated and may be inaccurate (owner request 2026-10-23,
decision D19). A translation is not a summary or an answer: its job is to carry the document's own
content into another language, and the original stays one click away. It is still generated text,
and its failures look like the document's own words. So this ADR states where it is allowed and what
must surround it.

The bake-off on the library's own documents (`docs/research/MT-BAKEOFF-2026-10-24.md`; 153 segments in
de, fr, es, ru, ja, zh and ar, scored with MetricX-24 QE and by hand) measured the candidates on this
machine, beside the running API:

| Model and runtime | QE (lower is better) | Numbers kept | Serious errors in 30 reviewed | ~500-word article |
|---|---|---|---|---|
| MiLMMT-46-12B v1.0, llama.cpp Q8_0 | 3.54 | 95 % | 7 | 10.6 s (first paragraph 5.4 s) |
| MiLMMT-46-12B v1.0, vLLM FP8 | 3.54 | 95 % | (same model) | 5.5 s (1.3 s) |
| MiLMMT-46-4B v1.0, vLLM FP8 | 3.62 | 95 % | 7 | 2.3 s (0.56 s) |
| Tower-Plus-9B | 3.76 | 94 % | 11 (invents facts) | — |
| Gemma-3-12B-it | 4.33 | 98 % | 6 (least fluent) | — |
| OPUS-MT, CTranslate2 | 6.76 (9-10 for ja, zh, ar) | 86 % | ~14 (loops, invents) | 0.23 s |

The failures that matter, in order of how often they occurred:

1. A technical term rendered as a plausible wrong one. Quality estimation mostly misses these:
   Tolstoy's «опрощение» became "forgiveness" with a near-perfect score.
2. Content filled into a gap the text extraction left: dates and life spans added.
3. Numbers changed as words ("eleven countries" became twelve or ten).
4. A direction reversed ("west to east" became "westward").
5. Inline link tags dropped.

No model omitted whole sentences, and none added chat-style framing.

## Decision
Machine translation into English is allowed in the Navigator under these rules. Each rule is a
requirement of the implementation, tested like any other.

1. **Only on request, and always labelled.**
   - A translation is shown only when the reader asks for it: "Read in English (machine
     translation)".
   - The banner sits at the top of every translated page, in a colour no other banner uses, and
     cannot be dismissed: "Machine translation from <language> by <model>. It may be inaccurate:
     names, numbers, technical terms and legal meaning can be wrong. The original is the
     authority." It links to the original.
   - Search results, snippets and titles stay in the original language. A translated title may
     appear next to the original, marked "(machine translation)".
2. **The original stays one click away, paragraph by paragraph.** Every translated block keeps an
   anchor to its source block, and the reader can switch the page, or any one block, back to the
   original.
3. **Checks on every segment before it is shown.** Numbers and units must be identical, including a
   check of numbers written as words. Placeholders and tags must be identical. The length ratio and
   sentence count must be close to the source's. A segment that fails is shown in its original
   language with "(not translated: <reason>)", never silently. A reference-free quality score
   (MetricX-24-QE) marks weak segments with a visible flag.
4. **Gaps are marked, not filled.** Text extraction marks dropped spans (a date or century lost from
   a table or template) so the model sees a placeholder it must copy back unchanged. That stops the
   invented-content failure at its source.
5. **Translations are derived data.** Translations live in their own store, beside the refinery and
   never in the vault's generations. Each records the source document and its generation, the model,
   its version and quantisation, the prompt version and the check results. A cached translation is
   used only while all of those still match. Nothing translated is indexed as the library's own text.
   Search is done on the original; an English query may find a document through its translated title
   only if that is marked.
6. **Models and runtime.**
   - On request: MiLMMT-46-12B v1.0 on llama.cpp (GGUF Q8_0, 8 slots, 8-bit KV cache). It uses about
     0.6 GB of host RAM and 14.5 GB of GPU memory beside the API's ~4-4.5 GB.
   - Bulk pre-translation, if the owner chooses it: MiLMMT-46-4B v1.0 on vLLM (FP8, explicit KV
     cache size), as a capped background job like the other heavy jobs.
   - OPUS-MT is a CPU-only fallback for de, fr, es and ru, and never for ja, zh or ar.
   - Gemma-3 and Tower-Plus are not used.
   - The model weights are under the Gemma terms of use, which allow this personal use; the licence
     is recorded with the model.
7. **The rest of ADR-0008 stands.** There are no summaries, answers or chat, and no translation
   unless the reader asks. An exception beyond translation needs its own ADR and the owner's
   decision.

## Consequences
- **The GPU is shared.** The 12B translator and the API's search models together use about 19 GB
  of the 24 GB card, so a translation server is started on demand and stopped after an idle period
  rather than kept resident, and it never runs at the same time as a GPU index build.
- **Pre-translation is expensive.** Translating the four largest Wikipedias in advance is about 32
  GPU-days with the 4B model (106 with the 12B), so the default is on request. Pre-translation, if
  ever chosen, starts with documents that have no English counterpart (DILA's law, Japanese and
  Russian technical texts).
- **The weak spots are known and visible.** Arabic technical vocabulary is weak in every model
  tested. Legal text gets an extra line in its banner: "Not a legal translation: consult the
  original."
- **More testing is wanted.** The bake-off was small: 153 segments and one reviewer. A larger test
  set per language, and a term glossary pass (fixed renderings of recurring legal and technical
  terms), come next (`docs/research/MT-BAKEOFF-2026-10-24.md` §5).
