# Translation: English machine translation on the reader's request

[ADR-0013](../adr/0013-labelled-machine-translation.md) sets the rules: translation only when the
reader asks, under a banner that cannot be dismissed, with the original one click away and every
segment checked. The owner chose on-request translation only, with nothing translated in advance
(2026-10-24).

## What the reader sees

A page whose language is not English shows "Read in English (machine translation)" under its
warnings, when the library can translate that language (`GET /v1/translate`). On request:

* **The banner.** "Machine translation from <language> by <model>. It may be inaccurate: names,
  numbers, technical terms and legal meaning can be wrong. The original is the authority." It has its
  own colour (`--notice-mt-bg` in every theme) and no close button. A source in the `law` domain adds
  "Not a legal translation: consult the original." The banner shows progress, and "Show the original"
  switches the whole page back and forth.
* **Block by block.** Each paragraph, heading, list item, table cell and caption is translated as one
  segment. Code, formulas and pictures are kept as they are, and a nested list is its own segment.
  Each translated block has a small ⇄ switch to its original. The title's translation appears under
  the heading, marked "(machine translation)".
* **A failed segment** stays in its own language, followed by "(not translated: <reason>)".

## How it works

* **Navigator (`src/core/translate.ts`).**
  - A block becomes text with placeholders: a link or a formatted span is `<xN>…</xN>`, and a picture,
    formula, code or nested block is `⟦N⟧`.
  - Segments go to the API 8 at a time, the title first.
  - The page is rebuilt from the translated text and shallow clones of the original elements. Anything
    else the model writes is shown as text, never markup.
  - While the server starts, the API answers 503 with `Retry-After`, and the page waits and asks again.
* **API (`POST /v1/translate`).** Up to 32 segments of at most 4,000 characters, in one language. Each
  segment comes back with `text` (null when it failed a check), `ok`, `reason` and `cached`. The
  route takes a `Translator` port (`Services.translator`); none means 501.
* **Oracle (`wsp_oracle.translation`).**
  - `llama-server` (llama.cpp) runs MiLMMT-46-12B v1.0, GGUF Q8_0, with 8 slots of 2,048 tokens. It is
    started on the first request and stopped after `idle_seconds` (15 minutes) without one.
  - It runs in its own transient scope (`whisipedia-translate.scope`, `MemoryMax=4G`), outside the
    API's cgroup.
  - It starts only with `min_free_vram_mb` (15,500) free. First the API gives back the GPU memory its
    own PyTorch allocator keeps cached: 8.6 GB after 13 hours, where the search models need about 4.5.
  - Translations are cached in SQLite (`<state>/translations/cache.sqlite`) by model, prompt version,
    language and text.
* **Checks (`wsp_oracle.mtcheck`).**
  - Numbers are compared by value: digits across localisation, the magnitude words (万, Millionen,
    million), and number words 2-90 in eight languages.
  - Placeholders must come back exactly and well nested.
  - The length must be within a band per language.
  - At most one sentence fewer than the source.
  - No source-language text left, and no added framing.

## Configuration

`[oracle.translation]` (every key has a default; unknown keys are refused):

| key | default | |
|---|---|---|
| `enabled` | `true` | |
| `model` | `~/.cache/whisipedia/mt/models/gguf/MiLMMT-46-12B-v1.0.Q8_0.gguf` | the bake-off's choice |
| `server` | `~/.local/share/whisipedia/llama.cpp/current/llama-server` | |
| `port`, `slots`, `context_per_slot` | 8790, 8, 2048 | |
| `idle_seconds` | 900 | the GPU memory goes back to the index after this |
| `min_free_vram_mb` | 15500 | |
| `memory_max` | `4G` | the server scope's host-memory cap |
| `request_seconds` | 180 | one segment's limit |
| `cache` | `<state>/translations/cache.sqlite` | |

The server's log is `<state>/translations/llama-server.log`.

## Measured (2026-10-24, beside the running API)

* Server start: about 40 s. Three German paragraphs: under 1 s once the server was ready. A repeated
  page comes from the cache.
* Quality: [docs/research/MT-BAKEOFF-2026-10-24.md](../research/MT-BAKEOFF-2026-10-24.md) (MetricX QE
  3.54; 7 serious errors in 30 hand-reviewed segments, mostly wrong technical terms).
