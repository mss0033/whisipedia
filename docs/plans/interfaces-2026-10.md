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
The ZIM pages render already; worth adding: accepted answer first, vote counts as badges, the question's
tags as searches, and "more like this" from the tags. Effort: a day.

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
