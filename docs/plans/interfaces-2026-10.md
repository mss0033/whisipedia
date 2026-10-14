# Content interfaces and search cost: what is built, what is planned (2026-10-13)

The library holds kinds of content that a search-results page and a generic document page serve
poorly. This note records the interfaces built on 2026-10-13, the ones designed but not built, and
the search-cost ideas that measurement left on the table. Plans here are proposals for the owner.

## Built

| Interface | Address | Content | Notes |
|---|---|---|---|
| Exam Practice Hall | `wsp://practice` | NCVEC question pools (7 documents) | Parser in `core/exam.ts`; study by group, mock exams by seed; progress in the browser |
| The Screening Room | `wsp://screening-room` | any document whose original file is video or audio | Players in `ui/player.ts`, four skins, resume positions; kinds `video`/`audio` in the API |

## Designed, not built

### Dictionary lookup: `wsp://define/<word>`
A headword goes straight to the entry instead of a 2-3 s search: resolve `wsp:kiwix-wiktionary-en-all/<word>`
(the HTML Wiktionary; titles are case-sensitive, try the word, then lower case, then the dump-derived
`wiktionary-en` entry), render only the asked language's section with a toggle for the rest, and fall back
to a `kind=dictionary` search. A location-bar shortcut (`define serendipity`, `d serendipity`). The
search page's instant definition box already parses senses; the page reuses it. Effort: a day.

### Open Library cards that know where the book is
A card describes a book the library may hold. Today it offers a books-only search. Planned:
1. The adapter keeps the edition's external ids (`identifiers.project_gutenberg`, `librivox`,
   `standard_ebooks`, `wikisource`) in the record's links; the card then links straight to the book
   (and the audiobook when the Screening Room has it) when the library holds that id.
2. Without an id: a title-and-author match against the books the library holds (`search_hits` within
   `gutenberg`, `standard-ebooks`, `wikisource-en`, exact title key first, then every-word title), shown
   as "probably this book" with the match shown.
3. The reason when nothing is found, from the card's publication year: after 1929 (US public-domain
   line, moving yearly) "still in copyright, so no free text exists"; before it "public domain, but no
   transcription in the library yet", with the search offered.
Effort: a day for ids and reasons; the fuzzy match needs an evaluation set of fifty cards.

### Book reader for `kind=books`
Gutenberg, Standard Ebooks and Wikisource texts are long: a reader mode with a table of contents from
headings, reading position per book in the browser, font and measure controls, "continue reading" on the
start page, and chapter-at-a-time rendering (a 2 MB text renders slowly as one page). Effort: two days.

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
