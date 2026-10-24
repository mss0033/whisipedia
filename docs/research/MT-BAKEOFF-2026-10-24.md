# Machine-translation bake-off on the library's own documents

Date: 2026-10-24. Follows `research/2026-10-23/research-translation.md` (§4.3). The owner approved
labelled MT into English (PLAN §16 D19). Nothing in `~/dev/whisipedia` or `~/dev/wsp-extra` was
changed, and no whisipedia service was stopped or restarted. GPU work started only after
`whisipedia-job-source-audit-21k` finished (01:33). The API stayed healthy throughout:
`/v1/health` ok, no CUDA out-of-memory errors in the user journal.

## 0. Recommendation in brief

| Role | Model | Runtime and quantisation | Why |
|---|---|---|---|
| **Primary (on demand, on open)** | **MiLMMT-46-12B v1.0** | **llama.cpp `llama-server`, GGUF Q8_0** (8 slots, unified q8_0 KV, 16k tokens) | Best fluency and adequacy in the hand review, tied for the best MetricX. It fits the host: **0.6 GB anon RAM** and 14.5 GB VRAM next to the API. A 500-word article takes about 10 s with all paragraphs sent at once; the first paragraph arrives in about 5 s. |
| Primary, if it can have about 5 GB host RAM | MiLMMT-46-12B v1.0 | vLLM 0.32, FP8 W8A8 (I made the checkpoint; none is published) | Same quality (QE 3.54 against 3.54). 3.7× the throughput (361 tok/s at 8 streams, 741 at 24). Article in 5.5 s, first paragraph 1.3 s. But it uses **3.8 GB anon host RAM**, hit the 4 GB cap 34 times, and has only 2 GB of KV cache beside the API. |
| **Bulk / pre-translation speed tier** | **MiLMMT-46-4B v1.0** | **vLLM, FP8 W8A8** (made the same way), KV cache set explicitly to 6 GB | **2,437 tok/s** at 32 streams. Article in 2.3 s, first paragraph 0.56 s. 11 GB VRAM. Its MetricX equals the 12B (3.62 against 3.54 for FP8) and its serious-error count in the hand review is the same (7 of 30), with somewhat more term errors in science text. On llama.cpp it also makes a light on-demand fallback: 5.2 GB VRAM, 0.4 GB RAM, 95 tok/s single stream. |
| CPU-only fallback, de/fr/es/ru only | OPUS-MT (tc-big / tc-bible-big) | CTranslate2 4.8.3 int8 on CPU, or int8_float16 on GPU | 10,115 tok/s on GPU, 488 tok/s on 4 CPU threads, 0.5 GB VRAM. **Not usable for ja, zh or ar** (QE 9–10, repetition loops, invented sentences). It also breaks `⟦n⟧` tokens and `=` signs. |
| Not recommended | Gemma-3-12B-it | — | Lowest LLM QE (4.33). It is very sensitive to the prompt: with a generic tag rule it invented `<x1>` tags in 91 of 153 segments. It also rewrote `⟦n⟧` tokens as `<xN>n</xN>` (1 of 12 kept) and changed "eleven" to "twelve". It did keep every `<xN>` link tag (14 of 14), so it is useful only as a tag-repair retry where VRAM allows. |
| Not recommended | Tower-Plus-9B | — | Second-best QE (3.76), but the most serious errors in the hand review (11 of 30). It **filled gaps in the source with invented facts** ("1st of January", "21st century"), changed "eleven" to "ten", and got place names wrong. Its licence is CC-BY-NC-SA. |

This changes the research report in two places:

- **The speed tier should be MiLMMT-4B on vLLM, not OPUS-MT.** The 4B model is only about 4× slower than OPUS on the GPU, and it is an LLM-grade translator in every language. OPUS is unusable for ja, zh and ar.
- **vLLM is not automatically the right runtime here.** It runs on this machine only with workarounds (§1.3). Its host-RAM footprint is close to all the RAM that is free, and the co-resident API makes its memory profiler fail. llama.cpp is the safe default for on-demand use; vLLM is worth it for bulk runs.

## 1. Setup that worked

### 1.1 Environment

- Venv: `~/.cache/whisipedia/mt/venv`, created with uv on Python 3.12. It holds vllm 0.32.0 (torch 2.13.0+cu130), ctranslate2 4.8.3, transformers 5.19.0, sentencepiece and accelerate.
- llama.cpp: the existing build `~/.local/share/whisipedia/llama.cpp/current` (b11541, CUDA), used read-only.
- Every model process ran under `systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0`, and no run raised the cap. Peak and anon figures below come from each scope's `memory.peak` and `memory.stat`. The peak includes reclaimable page cache; anon is the real resident memory.
- GPU baseline used by the API was 3.9–4.5 GB (it moves). The VRAM figures below are the total `nvidia-smi` reading minus that baseline.
- Disk: 46 GB under `~/.cache/whisipedia/mt`, within the 60 GB budget. Tower and Gemma GGUFs were deleted after their runs, and each bf16 shard was deleted as it was converted.

### 1.2 Models (all from Hugging Face, ungated repos, generic user agent)

| Model | Files used | Licence | Notes |
|---|---|---|---|
| MiLMMT-46-12B v1.0 | `mradermacher/MiLMMT-46-12B-v1.0-GGUF` Q8_0 (12.5 GB, sha256 verified); `xiaomi-research/MiLMMT-46-12B-v1.0` bf16, converted to FP8 | Gemma terms | No FP8 checkpoint is published. I converted it with `bakeoff/quantize_fp8.py`: streaming, one shard at a time, 4 GB cap, 5.5 CPU-min, 13 GB output. The format matches RedHatAI's Gemma-3 FP8-dynamic (per-channel weights, dynamic per-token activations, vision tower and lm_head left in bf16). |
| MiLMMT-46-4B v1.0 | mradermacher Q8_0 GGUF (4.1 GB); bf16 converted to FP8 the same way (5.1 GB) | Gemma terms | The research report names it as the 4B option. |
| Gemma-3-12B-it | `ggml-org/gemma-3-12b-it-GGUF` Q8_0 | Gemma terms | **`google/gemma-3-12b-it` is gated (manual approval) and was skipped.** The ggml-org GGUF is an ungated mirror. |
| Tower-Plus-9B | `mradermacher/Tower-Plus-9B-GGUF` Q8_0 | CC-BY-NC-SA-4.0 | |
| OPUS-MT | de: `opus-mt-tc-bible-big-gmw-en`; fr: `tc-big-fr-en`; es: `tc-bible-big-roa-en`; ru: `tc-big-zle-en`; ar: `opus-mt-ar-en` (base); ja: `opus-mt-ja-en` (base); zh: `tc-bible-big-zhx-en`. All converted to CT2 int8. | CC-BY 4.0 / Apache-2.0 | **`opus-mt-tc-big-ar-en` produces garbage** in both transformers 5.19 and CT2 (checked directly), so the base model replaced it. **No tc-big ja-en model exists.** The zhx-en model emits `>>cmn_Hant<<` tokens, which are stripped. |
| QE: MetricX-24-Hybrid-Large v2p6 (bf16), QE mode | `google/metricx-24-hybrid-large-v2p6-bfloat16` plus the `google/mt5-large` tokenizer | Apache-2.0 | 3.6–4.0 GB VRAM peak, batch 8. Sanity check: copying the source as the "translation" scores 10.6, an unrelated sentence scores 17.6, and the models score about 3.5. **CometKiwi (`Unbabel/wmt22-cometkiwi-da`) is gated, so it was skipped.** |

### 1.3 Runtime settings and memory

| Run | Settings | Host RAM peak / anon | My VRAM (total − API) |
|---|---|---|---|
| llama.cpp, 12B Q8_0 | `-ngl 999 -fa on -np 8 -c 16384 -kvu -ctk q8_0 -ctv q8_0 -lm dio --cache-ram 0` | 2.0 GB / **0.62 GB** | **14.5 GB** |
| llama.cpp, Gemma-3-12B Q8_0 | same | 2.1 GB / 0.65–0.74 GB | 14.5 GB |
| llama.cpp, Tower-Plus-9B Q8_0 | same | 1.8 GB / 0.48 GB | 12.8 GB |
| llama.cpp, 4B Q8_0 | same | 1.4 GB / 0.40 GB | 5.2 GB |
| vLLM, 12B FP8 | `--max-model-len 4096 --max-num-seqs 32 --max-num-batched-tokens 2048 --kv-cache-memory-bytes 2.2e9 --limit-mm-per-prompt {"image":0}`, `VLLM_USE_FLASHINFER_SAMPLER=0` | **4.0 GB (at the cap) / 3.79 GB** | 15.4 GB (12.7 GiB weights + 2.05 GiB KV = 5,576 tokens) |
| vLLM, 4B FP8 | same, with `--kv-cache-memory-bytes 6e9` | 4.0 GB (at the cap) / 3.74 GB | 11.0 GB (KV 41,711 tokens) |
| CT2 OPUS, GPU | int8_float16, one pair model resident at a time | under 1 GB | about 0.5 GB |
| MetricX-Large | bf16, batch 8 | under 3 GB | 3.6–4.0 GB |

Failures found on the way (these matter for production):

- **llama.cpp per-slot KV is large**: 8 slots × 3,072 tokens with f16 KV took 17.9 GB, over budget. A unified KV pool (`-kvu`) with q8_0 KV brought it to 14.5 GB. The q8_0 KV costs about 5% decode speed (llama-bench: 52.6 against 49.8 tok/s).
- **llama.cpp's default `--cache-ram 8192` (a host-RAM prompt cache) must be set to 0** under a 4 GB cap. `-lm dio` (DirectIO loading) keeps the GGUF out of the page cache.
- **vLLM's `gpu_memory_utilization` profiling fails beside the API.** At 0.72 and 0.74 it reported −1.73, +0.35 and −1.18 GiB of KV across retries, because the API's GPU use changes while vLLM is profiling. Only `--kv-cache-memory-bytes`, which skips profiling, works.
- **vLLM Gemma-3 KV costs about 393 KB per token**, with no sliding-window saving, so 2 GB holds only about 5.6k tokens. That is about 8–20 paragraphs in flight, and it is the throughput limit.
- **vLLM's FlashInfer sampler JIT build fails** (nvcc). The workaround is `VLLM_USE_FLASHINFER_SAMPLER=0`. FP8 KV cache also needs a FlashInfer JIT build, so it was not used.
- vLLM on WSL: "Pinned memory is not available". The first start takes about 90 s because of torch.compile.
- Running all seven OPUS models in one process, plus a large batch on CPU, hit the 4 GB cap and was OOM-killed. The cap did its job. The runner now loads one pair model at a time.

## 2. Test set (from the library, not FLORES)

There are **153 segments** in `~/.cache/whisipedia/mt/testset/testset.jsonl`, built by `bakeoff/build_testset.py` with 60 API requests in total, one at a time. A ~500-word de article (Stahlbeton) and a ~500-word ja article (富士山) are in `articles.json`.

| Lang | wiki | tech | fmt | Sources |
|---|---|---|---|---|
| de | 8 | 8 | 7 | Bodensee, Bach; BGB, Grundgesetz, Photosynthese, Stahlbeton, DIN; Ohmsches Gesetz |
| fr | 8 | 8 | 8 | Victor Hugo, Notre-Dame; Code civil, Béton armé, Mitochondrie; **ERG 2024 (FR)** prose and bullet lists; Loi d'Ohm |
| es | 8 | 8 | 8 | Amazonas, García Márquez; Constitución 1978, Fotosíntesis; **GRE 2024 (ES)**; **es.stackoverflow**; **ready.gov/es HTML**; Ley de Ohm |
| ru | 8 | 7 | 7 | Байкал, Толстой; ГК РФ, Транзистор, Фотосинтез; **ru.stackoverflow**; Закон Ома |
| ja | 7 | 8 | 7 | 富士山, 夏目漱石; 日本国憲法, 日本産業規格, トランジスタ; **ja.stackoverflow**; オームの法則 |
| zh | 7 | 8 | 7 | 长城, 李白; 民法典, 晶体管, 光合作用; 欧姆定律 (with LaTeX) |
| ar | 6 | 4 | 6 | نهر النيل, ابن سينا; ترانزستور; قانون أوم |

The fmt segments cover:

- number- and unit-dense prose (flood levels in m, bird counts with `56.798`, Arabic `5ر20 مترا`);
- formulas (`R = U / I`, LaTeX `\sigma=1/\rho`);
- bullet lists (ERG);
- link tags `<x1>…</x1>` taken from the document's link table or real ZIM HTML;
- numbers frozen as `⟦n⟧`.

The library has no non-English standards or law sources other than ERG and the Wikipedia law and science articles. Codes Canada and ETSI are English-only here.

**A library finding:** Wikipedia text extraction drops template output: dates (`né le à Besançon`), centuries (`entrée dans le .`), and birth/death parentheses (`李白（）`). This turned out to provoke hallucination (§4).

## 3. Results

Scoring and checks (`bakeoff/common.py`, `aggregate.py`):

- Numbers: multiset identity of digit strings, tolerant of decimal and thousands marks. Japanese and Chinese month numbers and era years are exempt.
- Placeholders: identity and order of `<xN>`, `</xN>` and `⟦n⟧`.
- Length ratio: an absolute band per language, plus at least 0.7× the cross-model median for the segment.
- Sentence count: flags when the output has more than one sentence fewer than the source. Not used for Arabic.
- Untranslated: the share of output in the source script or with source-language stopwords. Wrapper and commentary text are also flagged.
- QE: MetricX-QE on a 0–25 scale (lower is better); a segment is flagged above 5.

### 3.1 Overall (153 segments)

| Model / runtime | MetricX-QE mean (median) | QE>5 | numbers ok | placeholders ok (27) | sentences ok | length ok | flagged |
|---|---|---|---|---|---|---|---|
| MiLMMT-12B Q8_0, llama.cpp | 3.54 (3.06) | 26 | 94.8% | 55.6% | 97.4% | 100% | 27.5% |
| **MiLMMT-12B FP8, vLLM** | **3.54 (3.05)** | **25** | 95.4% | 59.3% | 98.7% | 100% | **24.8%** |
| MiLMMT-4B Q8_0, llama.cpp | 3.54 (3.05) | 30 | 95.4% | 59.3% | 99.3% | 100% | 28.1% |
| MiLMMT-4B FP8, vLLM | 3.62 (3.11) | 30 | 95.4% | 66.7% | 99.3% | 99.3% | 27.5% |
| Gemma-3-12B-it Q8_0 (prompt v2) | 4.33 (3.78) | 44 | **98.0%** | 55.6% | 98.7% | 100% | 36.6% |
| Gemma-3-12B-it (prompt v1: tag rule always sent) | 4.62 (3.81) | 49 | 92.2% | **20.2% of 114**: tags invented | 97.4% | 98.0% | 73.2% |
| Tower-Plus-9B Q8_0 | 3.76 (3.20) | 30 | 94.1% | 63.0% | 99.3% | 100% | 29.4% |
| OPUS-MT, CT2 GPU | 6.76 (5.41) | 84 | 86.3% | 40.7% | 97.4% | 87.6% | 58.8% |

No LLM run produced wrapper text ("Here is the translation") or untranslated output. Only Gemma prompt v1 was truncated (4 segments).

### 3.2 MetricX-QE mean by language and category

| Model | de | fr | es | ru | ja | ar | zh | wiki | tech | fmt |
|---|---|---|---|---|---|---|---|---|---|---|
| MiLMMT-12B Q8_0 | 3.88 | 3.67 | 2.49 | 3.31 | 3.25 | 3.00 | 5.12 | 4.51 | 2.64 | 3.45 |
| MiLMMT-12B FP8 | 3.87 | 3.60 | 2.47 | 3.30 | 3.28 | 3.09 | 5.14 | 4.49 | 2.67 | 3.45 |
| MiLMMT-4B Q8_0 | 4.03 | 3.65 | 2.45 | 3.45 | 3.25 | 3.13 | 4.75 | 4.43 | 2.74 | 3.42 |
| MiLMMT-4B FP8 | 4.08 | 3.62 | 2.58 | 3.46 | 3.36 | 3.40 | 4.84 | 4.58 | 2.82 | 3.43 |
| Gemma-3-12B-it | 4.77 | 5.13 | 2.96 | 4.00 | 3.67 | 3.90 | 5.79 | 5.11 | 3.31 | 4.56 |
| Tower-Plus-9B | 4.23 | 3.79 | 2.58 | 3.73 | 3.23 | 3.27 | 5.45 | 4.68 | 2.90 | 3.69 |
| OPUS-MT | 5.72 | 5.56 | 3.16 | 5.12 | 10.43 | 9.08 | 9.36 | 8.67 | 5.41 | 6.14 |

Flagged % (any check failed) by language:

| Model | de | fr | es | ru | ja | ar | zh |
|---|---|---|---|---|---|---|---|
| MiLMMT-12B FP8 | 17 | 21 | 8 | 27 | 27 | 31 | 45 |
| MiLMMT-4B FP8 | 17 | 25 | 13 | 27 | 32 | 31 | 50 |
| Gemma-3-12B | 43 | 38 | 21 | 41 | 27 | 31 | 55 |
| Tower-Plus-9B | 13 | 25 | 13 | 41 | 46 | 19 | 50 |
| OPUS-MT | 52 | 50 | 17 | 55 | 86 | 94 | 73 |

zh scores worst for every model. Its worst segments are long lists of Chinese place names (Great Wall routes), where every model mis-romanises names.

### 3.3 Placeholder fidelity by type

| Type (n) | MiLMMT-12B | MiLMMT-4B | Gemma-12B | Tower-9B | OPUS |
|---|---|---|---|---|---|
| `<xN>…</xN>` link tags (14) | **4** | 6 | **14** | 6 | 5 |
| `⟦n⟧` frozen tokens (12) | **11** | 11 | **1**: rewrote them as `<xN>n</xN>` | 10 | 5: SentencePiece turns `⟦` into `⁇` for fr and ru |

MiLMMT drops inline tags from long paragraphs; it is a pure translator. Adding a one-line tag rule to its prompt raised this from 15 to 18 of 27. A cascade of MiLMMT, then Gemma when the placeholder check fails, would reach 26 of 27, but the two 12B models do not fit in VRAM together. Note that Gemma's "passing" tags can be on the wrong words: in ja-fmt-08 the `<x1>` and `<x2>` labels were swapped. Placeholder identity does not detect that.

### 3.4 Speed (RTX 4090 under WSL2, shared with the API; greedy decoding)

| Model / runtime | single-stream tok/s | aggregate tok/s (concurrency) | segment latency p50 / p95 | ~500-word article, all paragraphs at once: first paragraph / complete |
|---|---|---|---|---|
| MiLMMT-12B Q8_0, llama.cpp | 45 (llama-bench tg128: 52.6) | 98 (8) | 7.7 / 20.1 s | de 5.4 / 10.6 s; ja 5.9 / 7.4 s |
| **MiLMMT-12B FP8, vLLM** | 50 | **361 (8), 741 (24, KV-bound)** | 2.0 / 5.6 s | **de 1.3 / 5.5 s; ja 2.7 / 3.8 s** |
| MiLMMT-4B Q8_0, llama.cpp | 95 | 156 (8) | 5.2 / 12.6 s | de 3.5 / 5.9 s |
| **MiLMMT-4B FP8, vLLM** | 123 | **896 (8), 2,437 (32)** | 0.8 / 2.1 s | **de 0.56 / 2.3 s; ja 1.1 / 1.7 s** |
| Gemma-3-12B Q8_0, llama.cpp | 47 | 93 (8) | 8.2 / 22.6 s | de 4.9 / 10.1 s |
| Tower-Plus-9B Q8_0, llama.cpp | 56 | 145 (8) | 5.1 / 12.9 s | de 3.4 / 7.7 s |
| OPUS-MT, CT2 GPU int8_float16 | 756 (sequential, per segment) | **10,115** (batched) | 0.12 / 0.24 s | de 0.23 s |
| OPUS-MT, CT2 CPU int8, 4 threads | 231 | 488 | 0.43 / 0.87 s | de 1.9 s |

Single-stream decode is about 50 tok/s for a 12B model, below the research report's 70–80 estimate (WSL2 and a shared GPU). The article's "complete" time is set by its longest paragraph, so splitting long paragraphs would cut it.

**Mass-run arithmetic with the measured numbers.** The four Wikipedias are about 6.8 B output tokens (research §3.1).

| Engine | Measured throughput | Time for the four Wikipedias |
|---|---|---|
| 12B FP8, vLLM, beside the API | 741 tok/s | about 106 GPU-days |
| 4B FP8, vLLM | 2,437 tok/s | about 32 GPU-days |
| OPUS-MT, GPU | 10,115 tok/s | about 8 GPU-days, de/fr/es/ru only |

The research estimate was 31–52 days for the 12B and 13–20 days for the 4B, so the measured times are about 2–3× longer. With the API stopped, or in a larger KV window (night runs), the 12B and 4B throughput should rise, since both were KV-bound. I did not measure this.

## 4. Hand review

I reviewed 30 segments against the source: 4–5 per language, mostly tech and fmt. They are in `runs/review.md`, with 6 extra worst-QE segments in `runs/review2.md`. A "serious" error is one that changes facts or meaning or gets a technical term wrong; style is not counted.

| Model | Segments with ≥1 serious error (of 30) |
|---|---|
| MiLMMT-12B | 7 |
| MiLMMT-4B | 7 |
| Gemma-3-12B | 6 (and the least fluent output; it keeps source titles untranslated) |
| Tower-Plus-9B | 11 |
| OPUS-MT | about 14 |

MiLMMT-12B is the most fluent and the most accurate on Japanese legal text. The 4B model is close, but it made more science-term errors.

### Concrete examples

- **Added content (hallucination into source gaps).**
  - fr-wiki-06 (Notre-Dame): the extracted text has gaps, `entrée dans le .` and `sonnent pour la première fois le .`. **Tower** filled them with "21st century" and "**on the 1st of January**", an invented date (the real date is 23 March 2013). **OPUS** wrote "20th century" and "on the 17th". MiLMMT and Gemma kept the gaps.
  - zh-wiki-02 (李白（）): **MiLMMT-12B and Tower inserted "(701–762)"**, and MiLMMT-12B also added "also known as Li Po". The facts are correct, but they are not in the source.
  - ja-wiki-04: Gemma invented "Bunkyo University's" Dean of Literature.

  The "numbers added" check caught all of the digit cases.
- **Changed numbers written as words.** ar-wiki-01: `إحدى عشرة دولة` (eleven countries) became "**twelve** countries" (Gemma) and "**ten** countries" (Tower). The digit check cannot see this. MiLMMT 12B and 4B got it right. OPUS wrote "6.650 km", which reads as 6.65 in English, and dropped the Coptic, Luganda and Nubian names.
- **Formula damage.** fr-fmt-01, OPUS: `R = U / I` became `R ⁇ U / I`. zh-fmt-02: Gemma rewrote LaTeX `\mathbf{E}` as Markdown `**E**`, and Tower wrapped it in `\( \)`. MiLMMT kept the LaTeX verbatim.
- **Reversed direction.** zh-wiki-06 `自西向东` (west to east): MiLMMT-12B and Gemma wrote "**westward**". MiLMMT also duplicated Laishui County and dropped Laiyuan County (涞源县), a silent omission in a list.
- **Terminology.** These are the commonest serious class, and MetricX mostly misses them:
  - ru-wiki-07 «опрощение» (Tolstoy's "simplification"): MiLMMT-12B "**forgiveness**" (QE 1.41, looks perfect), MiLMMT-4B "atonement", OPUS "forgiveness". Gemma and Tower were right.
  - de-wiki-01 "Hochrhein": MiLMMT 12B/4B and Tower wrote "**Upper Rhine**", a different river section. Gemma and OPUS wrote "High Rhine" (QE 3.06, not flagged).
  - de-fmt-03 bird species (Reiherente = tufted duck, Tafelente = common pochard, Kolbenente = red-crested pochard): every model got most species wrong, for example "mallards" and "Eurasian teals". This case **was** flagged (QE 9.8–13.6). Bird counts like `56.798` were kept correctly by all models.
  - ar-tech-01 `المقحل` (transistor): rendered as "amplifier" (MiLMMT-12B, Gemma) and "operational amplifier" (MiLMMT-4B, Tower). Every LLM was wrong and QE was about 1.8–2.6.
  - ar-tech-04: `مقحل التوصيل` became "diode rectifier", "tunnel diode" and so on, all wrong.
  - ar-fmt-04: `المصب` (drain) became "collector", "cathode" or "emitter".

  **Arabic technical vocabulary is weak in every model.**
  - ru-tech-07: MiLMMT-4B wrote "intermembrane space" and "oxidative phosphorylation" in a chloroplast passage. Both terms are wrong and the second adds a concept.
  - fr-fmt-07 "Trois Lustres à peine" (barely fifteen years): MiLMMT-12B "Barely Three Decades", Tower "Barely Three Chandeliers".
- **Named entities.** ja-wiki-05: Tower changed 富士吉田市 to "Fujisawa". ja-wiki-01 (Gemma, prompt v1): 富士宮市 became "Fujimi City" and 剣ヶ峰 became "Kengake". ja-wiki-04: 外山正一 became "Tonoyama" (MiLMMT-12B). zh-wiki-01: Tower turned 上万華里 into "tens of thousands of li".
- **Meaning shift.** ja-fmt-08: Tower moved "silk" (蚕糸) into the title of the Act. Gemma swapped the link tags onto the wrong words and added "Paragraph 1".
- **Frozen-token "repair".** zh-fmt-06: the freezer had turned `核酮糖-1,5-二磷酸` into `核酮糖-⟦1⟧-二磷酸`. MiLMMT and Tower "fixed" it back to "ribulose-1,5-bisphosphate" and dropped the token. The freezer must not split chemical locants.
- **OPUS failure modes.**
  - Repetition loop (ja-wiki-05: "the crop crop crop crop…").
  - Invented sentences (ja-tech-02: "I'm going to show you how to do this.").
  - Literalisms: "Constitutional Violence" for verfassungsgebende Gewalt, "bumblebee" for bourdon, "he swore at the missing classes" for ругался.
- **Lists.** The ERG bullet lists (es-fmt-10, fr list) came through intact, item for item, from every model, including OPUS.
- **Not seen:** no LLM dropped a whole sentence in the 30 reviewed segments, and none produced wrapper text or commentary.

### What the automatic checks catch

| Check | Result |
|---|---|
| MetricX-QE > 5 | Caught the bird names, the place-name lists and garbled OPUS output. **Missed almost every terminology error** (опрощение 1.41, Upper Rhine 3.06, Arabic transistor terms about 1.8–2.6). Flags 16% of MiLMMT-12B segments, mostly zh/ja proper-noun lists. |
| Numbers added | Caught the hallucinated fills (Li Bai dates, Notre-Dame dates). |
| Numbers missing | Mostly benign (numbers spelled as words, "Two plastoquinones"). Blind to number words, which is how "eleven→twelve" got through. |
| Placeholder identity | Reliable for loss of tags or tokens; blind to tags put on the wrong words. |

## 5. Production constraints found

1. **Host RAM.**
   - llama.cpp needs well under 1 GB of anon RAM for any of these models; peak including page cache is 1.4–2.1 GB with `-lm dio --cache-ram 0`.
   - **vLLM needs about 3.8 GB anon**: the API-server and engine-core processes. Under a 4 GB cap it ran, but hit the limit 34 times (reclaim, no OOM). Give vLLM `MemoryMax≈5G` or do not run it. With about 5 GB free on the host, that leaves almost nothing for other jobs, so vLLM should only run when the nightly heavy jobs are not.
   - CT2 OPUS needs under 1 GB if one pair model is loaded at a time.
2. **VRAM beside the API's ~4.2 GB.**

   | Runtime | Total of mine |
   |---|---|
   | 12B on llama.cpp | 14.5 GB |
   | 12B FP8 on vLLM (with only 2 GB of KV cache) | 15.4 GB |
   | 4B FP8 on vLLM (6 GB of KV cache) | 11 GB |
   | 4B Q8_0 on llama.cpp | 5.2 GB |
   | MetricX-Large | 3.6–4.0 GB (it does not fit beside the 12B vLLM: 19.9 + 4 > 23 GB) |

   Running QE alongside the 12B means either llama.cpp with fewer slots, or running QE in a pass after translation.
3. **Throughput.** On-demand use is fine with any LLM option. Bulk runs are the 4B's job: about 2.4k tok/s, about a month of GPU time for the four Wikipedias, against about 3.5 months for the 12B beside the API.
4. **vLLM on this box needs:**
   - an explicit `--kv-cache-memory-bytes`, because the profiler is confused by the co-resident API;
   - `VLLM_USE_FLASHINFER_SAMPLER=0`, because the JIT build fails;
   - bf16 KV, because FP8 KV needs the same failing JIT;
   - `--limit-mm-per-prompt {"image":0}`;
   - a self-made FP8 checkpoint for MiLMMT (`bakeoff/quantize_fp8.py`, CPU, under 4 GB RAM, about 5 min for the 12B).

   Pin vllm 0.32.0.
5. **Prompts.**
   - MiLMMT: the card's raw prompt (`Translate this from X to English:\nX: …\nEnglish:`). Pick the language name for zh as Simplified or Traditional per text; this test set is a zh-Hans/zh-Hant mix.
   - Gemma needs strict, conditional instructions: send the tag rule only when the segment has tags.
6. **Pipeline consequences.**
   - Keep the number guard, and add **number-word** checks (en "eleven" against ar `إحدى عشرة`, de "fünf").
   - Do **not** freeze numbers inside chemical names or identifiers.
   - **Fix or mark extraction gaps.** Empty `（）` or `le .` invite invented fills; the extractor should keep `{{date}}` and century templates, or the segmenter should mark the gaps.
   - Do not trust QE for terminology. A glossary pass (Wikidata labels, IATE) is the remedy the research report already proposes, and the Arabic and German examples above show it is needed.
   - Inline tags: the Wikipedia text has none (it is plain text plus a link table), so the tag problem only affects ZIM HTML sources. For those, translate plain text and re-project link anchors by alignment or string match, or retry with the tag-rule prompt (18 of 27).

## 6. Reproduce

Everything is in `~/.cache/whisipedia/mt/`:

- `bakeoff/` holds the scripts;
- `testset/` holds the test set;
- `runs/<name>/` holds `translations.jsonl`, `metricx.jsonl` and `speed.json` for each run;
- `runs/segments.jsonl` holds every check for every segment;
- `runs/tables.md` holds the tables;
- `runs/review.md` and `runs/review2.md` hold the hand-review material;
- `logs/` holds the server logs and `mem.log`.

```
cd ~/.cache/whisipedia/mt/bakeoff
../venv/bin/python build_testset.py && ../venv/bin/python build_article.py      # API on :8737
./download_gguf.sh; ../venv/bin/python -I download_hf.py; ./convert_opus.sh
./run_all_llamacpp.sh "milmmt12-q8-llamacpp milmmt MiLMMT-46-12B-v1.0.Q8_0.gguf" ...   # one GGUF at a time
REPO=xiaomi-research/MiLMMT-46-12B-v1.0 systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 \
  ../venv/bin/python -I quantize_fp8.py ../models/hf/milmmt12-bf16 ../models/hf/milmmt12-fp8 ../models/hf/redhat-fp8-config.json
./serve_vllm.sh mt-vllm ../models/hf/milmmt12-fp8 8791 0.74 --max-num-batched-tokens 2048 --kv-cache-memory-bytes 2200000000
../venv/bin/python run_llm.py --name milmmt12-fp8-vllm --family milmmt --port 8791 --conc 8 --model mt
systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 ../venv/bin/python run_opus.py --name opus-ct2-gpu
systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 ../venv/bin/python score_metricx.py <runs...>
../venv/bin/python aggregate.py <runs...> > ../runs/tables.md
```

(`run_all_llamacpp.sh`, `run_opus.py` on GPU and `serve_llama.sh` need `LD_LIBRARY_PATH` set to the llama.cpp directory for libcublas 12; the scripts set it. CT2 4.8.3 needs `libcublas.so.12`, which comes from that directory.)

## 7. Limits of this bake-off

- 153 segments, not the 300 per language the research report proposed.
- One reviewer: me. I read these languages, but I am not a domain expert in Arabic electronics or ornithology.
- MetricX-Large only. CometKiwi is gated, and MetricX-XL was not tried.
- No Seed-X, Qwen3.5 or Gemma 4 runs: the scope was the four named candidates plus the 4B.
- Throughput was measured beside the live API only.
- vLLM with FP8 KV, or with a larger KV pool, was not measured.
