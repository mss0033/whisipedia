# Content interfaces and search cost: what is built, what is planned (2026-10-13)

The library holds kinds of content that a search-results page and a generic document page serve
poorly. This note records the interfaces built on 2026-10-13, the ones designed but not built, and
the search-cost ideas that measurement left on the table. Plans here are proposals for the owner.

## Built

| Interface | Address | Content | Notes |
|---|---|---|---|
| Exam Practice Hall | `wsp://practice` | NCVEC question pools (7 documents) | Parser in `core/exam.ts`; study by group, mock exams by seed; progress in the browser |
| The Screening Room | `wsp://screening-room` | any document whose original file is video or audio | Players in `ui/player.ts`, four skins, resume positions; kinds `video`/`audio` in the API |
| Dictionary lookup | `wsp://define/<word>`, location bar `define`/`def`/`d` | the Wiktionary copies | `pages/define.ts`: headword spellings tried in order, dictionary search as the fallback (2026-10-14) |
| Book reader | document pages of `kind=books` | Gutenberg, Wikisource, Standard Ebooks | `core/book.ts` + `pages/reader.ts`: chapters from heading lines, contents, prev/next, remembered place, Continue Reading on the start page (2026-10-14) |
| Question threads | Stack Exchange document pages | the Stack Exchange ZIMs | `pages/threads.ts` on the raw HTML before the sanitiser: chrome removed, accepted answer first, scores, tags as scoped searches (2026-10-14) |
| Catalog cards that know where the book is | Open Library document pages | Open Library works and editions | `catalogNotice` in `pages/document.ts`: held link, same-title lookup, copy elsewhere, or the copyright reason; adapter keeps external ids (2026-10-14) |

## Designed, not built

### Dictionary lookup
Built 2026-10-14 (see above). Left: a compact entry view that shows only the asked language's section
with a toggle for the rest (the full Wiktionary page opens today).

### Open Library cards: what is left
Built 2026-10-14 (see above). Left: the fuzzy title-and-author match for cards whose title differs
from the book's (translations, subtitles); and a re-refine of Open Library so cards refined before
2026-10-14 carry their Project Gutenberg and Internet Archive ids (114M records: a few hours of
background work over the NAS, for the owner to schedule; otherwise the next monthly dump brings them).

### Book reader
Built 2026-10-14 (see above). Left: font and measure controls (the themes set the type today), and
chapter detection for books whose headings are not on their own lines.

### Stack Exchange threads
Built 2026-10-14 (see above). Left: "more like this" from the tags, and comment folding.

### Comics and man pages
xkcd/SMBC: previous, next and random by number in the webring bar; the image at its native size.
ManKier: section navigation (NAME, SYNOPSIS, OPTIONS) from the page's headings, monospace rendering.
Effort: half a day each.

## Search cost: measured, remaining ideas

Measured 2026-10-13 on the live index (257M documents, 35 segments). Before: a cold search 12-20 s.
After the read-ahead fix, the API memory ceiling, parallel NAS lookups and one deeper window: 2-3.5 s.
What a cold query still pays: the first read of its words' postings (1.3-4.6 s, 0.4-1 GB from disk),
about 1 s of passage reads for the reranker, and the did-you-mean vocabulary on a cold process.

Ideas not done, in order of expected value:
1. **Fewer segments.** 35 segments mean 35 term-dictionary walks and 35 posting lists per field per
   word. A one-off merge to about 8 segments (Tantivy's merge policy with a larger target) should cut
   the cold cost of a query by a third; cost: one rewrite of 169 GB, a few hours in the background.
2. **Read-ahead at 128 KB.** 256 KB halved the bytes read per fault from the 8 MB default by 32x;
   128 KB may halve it again without hurting sequential posting reads. One sudo, one measurement.
3. **Warm the vocabulary.** The speller's 3 GB vocabulary is memory-mapped and cold after a restart:
   did-you-mean took 0.3-1.5 s on the first queries. A sequential read of the file in a startup thread
   fixes it.
4. **Passages from the index.** The reranker reads fifty passages over the NAS per query (about 1 s).
   Storing the first 400 characters of each document in the index (a few GB) would serve the lexical
   hits' passages locally; dense hits already carry their chunk.
5. **Scoped searches on huge sources.** `sources=open-library` (114M documents) ran 30 s: the source
   filter is a term query over 114M postings intersected with the words. A `source_key` fast-column
   filter, or routing scoped searches to a per-source index, would fix it.


## Measured 2026-10-14 (after the search-cost work)

Every search now reports its stages (`timings_ms` in `/v1/search`; searches over 3 s are logged).
On the live library (257M documents), new queries on a warm process:

| Stage | Time |
|---|---|
| lexical retrieval and fusion | 1.4-3.1 s (3.8-5.9 s right after a restart) |
| snippets (document text read from the NAS) | 0.3-0.85 s |
| dense retrieval (beside the lexical part) | 0.3-0.6 s |
| passages and reranking | under 0.1 s |
| did-you-mean | 0.15-0.25 s |

What that settled:
- **Scoped searches on huge sources:** fixed by the earlier work. A search limited to Open Library now
  takes as long as an unscoped one (2.5-6.6 s), not 30 s. Nothing more to build.
- **Passages from the index:** the reranker's passages cost under 0.1 s now. The text the snippets
  need (0.3-0.85 s) would have to be stored in the index, which would roughly double it (170 GB);
  not worth it.
- **Fewer segments:** the lexical stage is most of a search, so the merge is the lever:
  `wsp index merge` (32 segments to 7), first run scheduled for 2026-10-14 23:30.

## Search quality baseline (2026-10-14)

`wsp-oracle-eval --api http://127.0.0.1:8737` runs the 145 gold queries (125 from the Wikipedia
sample, 20 new ones for books, man pages, PEPs, comics, dictionary, medicine, law, Q&A, recipes and
canning) through the live API. A judged Wikipedia article counts in either full English copy; a
Simple English or MDWiki copy counts as also relevant. `wsp quality run --alerts` repeats it every
Sunday at 03:17 and warns when nDCG@10 falls more than 0.03.

| Measure | Live library |
|---|---|
| nDCG@10 | 0.277 |
| Recall@50 | 0.589 |
| MRR | 0.380 |
| latency p50 / p95 | 3.3 s / 6.3 s |

Strongest: navigational queries (0.91), comparisons (0.69), definitions (0.57). Weakest: multilingual
(0.06), everyday (0.11), geography (0.15), computing (0.16). The main cause is visible in the results:
Open Library catalog cards took 289 of the 1,450 top-ten places (a search about the storming of the
Bastille returns book records first), followed by Stack Exchange and Wikibooks pages whose titles
match the words but not the question.

**Target:** nDCG@10 0.40 on this set (provisional). **Next step:** ranking experiments measured
against this baseline: demote catalog cards for questions that are not looking for a book, give
the reranker a deeper and more varied candidate pool, and check the multilingual queries' routing.
