/**
 * Live library only: the reader's chapters and pages over a sample of real Gutenberg books, a
 * different sample each day (WSP_E2E_BOOK_SEED to repeat one). Toy texts in the unit tests never had
 * a contents list, a caption, a signature or a 4 MB book; the 2026-10-16 survey of 397 books found
 * 615 chapters that stopped mid-sentence. What a reader must be able to count on:
 *
 *  - nothing is lost: every paragraph of the text is on some page, once;
 *  - a page is a page: no screen holds more than PAGE_CHARS (or one paragraph, however long);
 *  - a book that heads its chapters "CHAPTER …" is read by chapters.
 */
import { headingFlags, PAGE_CHARS, pages, paragraphs, splitChapters } from '../src/core/book.ts';
import { expect, test } from './fixtures.ts';

const SAMPLE = Number(process.env.WSP_E2E_BOOKS ?? 40);

/** A small deterministic generator (mulberry32). */
function random(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

test.beforeEach(({ target }) => {
  test.skip(target !== 'live', 'needs the real library (npm run e2e:live)');
});

test('a sample of real books reads whole, by bounded pages, by chapters where it has them', async ({
  request,
}) => {
  test.setTimeout(600_000);
  const seed = Number(
    process.env.WSP_E2E_BOOK_SEED ?? new Date().toISOString().slice(0, 10).replace(/-/g, ''),
  );
  const next = random(seed);
  const problems: string[] = [];
  let read = 0;
  for (let tries = 0; read < SAMPLE && tries < SAMPLE * 8; tries++) {
    const n = 1 + Math.floor(next() * 76_000);
    const r = await request.get(`/v1/documents/${encodeURIComponent(`wsp:gutenberg/${n}`)}`);
    if (!r.ok()) continue;
    const doc = (await r.json()) as { title: string; text: string | null; lang: string };
    const text = doc.text ?? '';
    if (doc.lang !== 'en' || text.length < 20_000) continue; // catalog records, other languages
    read++;
    const id = `#${n} ${doc.title.slice(0, 50)}`;
    const chapters = splitChapters(text, doc.title);
    const units = pages(chapters);

    // nothing is lost
    const shown = units.flatMap((u) => [
      ...(u.page && u.page.index > 0 ? [] : [u.title]),
      ...u.paragraphs,
    ]);
    const norm = (p: string) => p.replace(/\s+/g, ' ').replace(/\]$/, '').trim();
    const have = new Set(shown.flatMap((p) => [norm(p), ...p.split(' · ').map(norm)]));
    const lost = paragraphs(text).filter((p) => !have.has(norm(p)));
    if (lost.length)
      problems.push(`${id}: ${lost.length} paragraphs not shown, e.g. "${lost[0]!.slice(0, 60)}"`);

    // a page is a page
    for (const u of units) {
      const size = u.paragraphs.reduce((s, p) => s + p.length, 0);
      if (size > PAGE_CHARS * 1.5 && u.paragraphs.length > 1)
        problems.push(`${id}: a page of ${size} characters (${u.title.slice(0, 40)})`);
    }

    // a book with CHAPTER headings is read by chapters
    const ps = paragraphs(text);
    const flags = headingFlags(ps);
    const headed = ps.filter((p, i) => flags[i] && /^chapter\b/i.test(p)).length;
    if (headed >= 6 && chapters.length < 3)
      problems.push(`${id}: ${headed} CHAPTER headings but ${chapters.length} chapter(s)`);
  }
  test.info().annotations.push({ type: 'sample', description: `seed ${seed}: ${read} books` });
  expect(read, 'books read').toBeGreaterThanOrEqual(SAMPLE * 0.8);
  expect(problems).toEqual([]);
});
