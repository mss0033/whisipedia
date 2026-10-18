import { describe, expect, it } from 'vitest';
import {
  type Chapter,
  forgetPosition,
  isHeading,
  MAX_PARAGRAPH_CHARS,
  PAGE_CHARS,
  pages,
  paragraphs,
  positionOf,
  rememberPosition,
  runLengths,
  splitChapters,
  unfinishedBooks,
} from '../src/core/book.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { chapterToOpen } from '../src/pages/reader.ts';

const BOOK = `FRANKENSTEIN;
OR, THE MODERN PROMETHEUS.

by Mary Shelley

LETTER 1

To Mrs. Saville, England.

You will rejoice to hear that no disaster has accompanied the
commencement of an enterprise.

LETTER 2

How slowly the time passes here.

CHAPTER I

I am by birth a Genevese.

Chapter II

We were brought up together.
`;

describe('books as chapters', () => {
  it('joins wrapped lines into paragraphs and tells headings', () => {
    expect(paragraphs('a\nb\n\n\n c \n')).toEqual(['a b', 'c']);
    expect(isHeading('CHAPTER I')).toBe(true);
    expect(isHeading('Letter 1')).toBe(true);
    expect(isHeading('BOOK THE FIRST')).toBe(true);
    expect(isHeading('To Mrs. Saville, England.')).toBe(false);
    expect(
      isHeading('The chapter of accidents that followed was long and sad, and nobody slept.'),
    ).toBe(false);
    expect(
      isHeading('Chapter headings are not what this long sentence about chapters is at all here'),
    ).toBe(false);
  });

  it('splits a book into front matter and chapters', () => {
    const chapters = splitChapters(BOOK, 'Frankenstein');
    expect(chapters.map((c) => c.title)).toEqual([
      'Frankenstein',
      'LETTER 1',
      'LETTER 2',
      'CHAPTER I',
      'Chapter II',
    ]);
    expect(chapters[0]!.paragraphs).toEqual([
      'FRANKENSTEIN; OR, THE MODERN PROMETHEUS.',
      'by Mary Shelley',
    ]);
    expect(chapters[1]!.paragraphs).toHaveLength(2);
    expect(chapters[4]!.paragraphs).toEqual(['We were brought up together.']);
  });

  it('folds a contents list into the chapter before it', () => {
    const text = [
      'MOBY DICK',
      'by Herman Melville',
      'CONTENTS',
      'CHAPTER 1. Loomings.',
      'CHAPTER 2. The Carpet-Bag.',
      'CHAPTER 3. The Spouter-Inn.',
      'CHAPTER 1. Loomings.',
      'Call me Ishmael.',
      'CHAPTER 2. The Carpet-Bag.',
      'I stuffed a shirt or two into my old carpet-bag.',
      'CHAPTER 3. The Spouter-Inn.',
      'Entering that gable-ended Spouter-Inn, you found yourself in a wide, low, straggling entry.',
    ].join('\n\n');
    const chapters = splitChapters(text, 'Moby Dick');
    expect(chapters.map((c) => c.title)).toEqual([
      'Moby Dick',
      'CHAPTER 1. Loomings.',
      'CHAPTER 2. The Carpet-Bag.',
      'CHAPTER 3. The Spouter-Inn.',
    ]);
    expect(chapters[0]!.paragraphs).toEqual([
      'MOBY DICK',
      'by Herman Melville',
      'CONTENTS',
      'CHAPTER 1. Loomings.',
      'CHAPTER 2. The Carpet-Bag.',
      'CHAPTER 3. The Spouter-Inn.',
    ]);
    expect(chapters[1]!.paragraphs).toEqual(['Call me Ishmael.']);
    expect(runLengths([false, true, true, false, true])).toEqual([0, 2, 2, 0, 1]);
  });

  it('keeps a text with too few headings whole', () => {
    const chapters = splitChapters('CHAPTER I\n\nOnce upon a time.\n\nThe end.', 'Short');
    expect(chapters).toHaveLength(1);
    expect(chapters[0]!.title).toBe('Short');
    expect(chapters[0]!.paragraphs).toEqual(['CHAPTER I', 'Once upon a time.', 'The end.']);
  });

  it('opens the chapter asked for, else the remembered one, else the first', () => {
    expect(chapterToOpen(undefined, null, 5)).toBe(0);
    expect(chapterToOpen(undefined, 3, 5)).toBe(3);
    expect(chapterToOpen(2, 3, 5)).toBe(2);
    expect(chapterToOpen(9, 3, 5)).toBe(4);
    expect(chapterToOpen('all', 3, 5)).toBe('all');
  });

  it('remembers where the viewer is in each book', () => {
    const storage = new SafeStorage(new MemoryStore());
    const pos = (docId: string, chapter: number, paragraph = 0) => ({
      docId,
      address: `wsp://gutenberg/${docId}`,
      title: docId,
      chapter,
      paragraph,
      chapters: 5,
    });
    rememberPosition(storage, pos('a', 1, 4), new Date('2026-10-14T01:00:00Z'));
    rememberPosition(storage, pos('b', 0), new Date('2026-10-14T01:01:00Z'));
    rememberPosition(storage, pos('a', 2, 1), new Date('2026-10-14T01:02:00Z'));
    expect(positionOf(storage, 'a')).toMatchObject({ chapter: 2, paragraph: 1 });
    expect(unfinishedBooks(storage).map((p) => p.docId)).toEqual(['a', 'b']);
    forgetPosition(storage, 'a');
    expect(positionOf(storage, 'a')).toBeNull();
    storage.write('books:positions', 'junk');
    expect(unfinishedBooks(storage)).toEqual([]);
  });
});

describe('front matter of a book with keyword chapters', () => {
  it('does not make the title page into chapters', () => {
    const body = (n: number) => `Text of part ${n} goes on for a while, as text does.`;
    const text = [
      'Transcriber’s note: scanned from the original.',
      'By',
      'ALFRED P MORGAN',
      'BOSTON',
      'LOTHROP, LEE & SHEPARD CO.',
      'INTRODUCTION',
      body(0),
      'ALFRED P. MORGAN',
      'CHAPTER I',
      'MAGNETS AND MAGNETISM',
      body(1),
      'CHAPTER II',
      'STATIC ELECTRICITY',
      body(2),
    ].join('\n\n');
    const titles = splitChapters(text, 'The Boy Electrician').map((c) => c.title);
    expect(titles).toEqual([
      'The Boy Electrician',
      'INTRODUCTION',
      'CHAPTER I · MAGNETS AND MAGNETISM',
      'CHAPTER II · STATIC ELECTRICITY',
    ]);
  });
});

/** Every paragraph of the text, once each, as a chapter title or a paragraph: nothing cut off. */
function readsWhole(text: string, chapters: Chapter[]): void {
  const shown = chapters.flatMap((c) => [
    ...(c.page && c.page.index > 0 ? [] : [c.title]),
    ...c.paragraphs,
  ]);
  const flat = (p: string) => p.replace(/\s+/g, ' ').replace(/\]$/, '');
  const want = paragraphs(text).map(flat);
  const got = shown.map(flat).filter((t) => want.includes(t) || !t.includes(' · '));
  for (const p of want) expect(got.join('\n')).toContain(p);
}

describe('real books (2026-10-16 field exam: chapters cut off mid-sentence)', () => {
  const prose = (n: number) =>
    `This is the text of part ${n}, which goes on for a good while and ends as sentences do. `.repeat(
      14,
    );

  it('does not end a chapter at a signature or a notice that finishes a sentence', () => {
    // Famous Americans of Recent Times: "…Is the sincere wish of" / "THEO. BURR ALSTON."
    const text = [
      'By A. Writer.',
      'CHAPTER I',
      prose(1),
      'The letter closed: "Is the sincere wish of',
      'THEO. BURR ALSTON."',
      'She never wrote again.',
      'It was as follows:--',
      '"COMMERCIAL.',
      'CHAPTER II',
      prose(2),
      'CHAPTER III',
      prose(3),
    ].join('\n\n');
    const chapters = splitChapters(text, 'Book');
    expect(chapters.map((c) => c.title)).toEqual([
      'Book',
      'CHAPTER I',
      'CHAPTER II',
      'CHAPTER III',
    ]);
    expect(chapters[1]!.paragraphs).toContain('THEO. BURR ALSTON."');
    readsWhole(text, chapters);
  });

  it('folds a contents list of entries and summaries back where it stands', () => {
    // The Tragedies of the Medici: "CHAPTER I" / "_The Pazzi Conspiracy_--…" in the contents
    const list = [1, 2, 3, 4].flatMap((n) => [
      `CHAPTER ${'I'.repeat(n)}`,
      `_Summary ${n}_--names--and more names.`,
    ]);
    const text = [
      'By A. Writer.',
      'PREFACE',
      prose(0),
      'CONTENTS',
      ...list,
      ...[1, 2, 3, 4].flatMap((n) => [`CHAPTER ${'I'.repeat(n)}`, prose(n)]),
    ].join('\n\n');
    const chapters = splitChapters(text, 'The Tragedies');
    expect(chapters.map((c) => c.title)).toEqual([
      'The Tragedies',
      'PREFACE',
      'CHAPTER I',
      'CHAPTER II',
      'CHAPTER III',
      'CHAPTER IIII',
    ]);
    expect(chapters[1]!.paragraphs).toContain('_Summary 3_--names--and more names.');
    readsWhole(text, chapters);
  });

  it('reads a long chapter title in capitals as a heading (Les Misérables)', () => {
    const long =
      'CHAPTER VII—WHICH EXPLAINS HOW THE PORTER CAME TO BE THERE THAT M. FAUCHELEVENT SHOULD HAVE ENTERED WITH SOMETHING UNDER HIS ARM';
    expect(isHeading(long)).toBe(true);
    expect(isHeading(long.toLowerCase())).toBe(false);
  });

  it('takes a chapter heading from an illustration caption and not a caption in capitals', () => {
    // Pride and Prejudice: "[Illustration: ·PRIDE AND PREJUDICE·" … "Chapter I.]"
    const text = [
      'By A. Writer.',
      'CHAPTER IV',
      prose(0),
      '[Illustration: ·PRIDE AND PREJUDICE·',
      'Chapter I.]',
      prose(1),
      '[Illustration:',
      '“SOME OF OUR CAVALRY CAUGHT HIM” (p. 12).]',
      prose(2),
      'Chapter II.',
      prose(3),
    ].join('\n\n');
    const chapters = splitChapters(text, 'P');
    expect(chapters.map((c) => c.title)).toEqual(['P', 'CHAPTER IV', 'Chapter I.', 'Chapter II.']);
    readsWhole(text, chapters);
  });

  it('keeps a stage note from turning a play’s first scene into a contents list', () => {
    // Shakespeare: "SCENE: Partly in France…" / "ACT I" / "SCENE I. Rossillon…"
    const text = [
      'SCENE: Partly in France, and partly in Tuscany.',
      'ACT I',
      'SCENE I. Rossillon. A room in the Countess’s palace.',
      prose(1),
      'SCENE II. Paris.',
      prose(2),
      'ACT II',
      'SCENE I. Paris.',
      prose(3),
    ].join('\n\n');
    expect(splitChapters(text, 'Play').map((c) => c.title)).toEqual([
      'Play',
      'ACT I · SCENE I. Rossillon. A room in the Countess’s palace.',
      'SCENE II. Paris.',
      'ACT II · SCENE I. Paris.',
    ]);
  });

  it('splits on numbered chapters inside BOOKs (The Filigree Ball)', () => {
    const text = [
      'By A. Writer.',
      'BOOK I THE FORBIDDEN ROOM',
      'I. “THE MOORE HOUSE?”',
      prose(1),
      'II. I ENTER',
      prose(2),
      'BOOK II THE LAW',
      'III. DETAILS',
      prose(3),
      'BOOK III THE HOUSE OF DOOM',
      'IV. IN TAMPA',
      prose(4),
      'She was by',
      'L. FRANK BAUM',
    ].join('\n\n');
    expect(splitChapters(text, 'F').map((c) => c.title)).toEqual([
      'F',
      'BOOK I THE FORBIDDEN ROOM · I. “THE MOORE HOUSE?”',
      'II. I ENTER',
      'BOOK II THE LAW · III. DETAILS',
      'BOOK III THE HOUSE OF DOOM · IV. IN TAMPA',
    ]);
  });

  it('keeps the lines of verse and joins the lines of prose', () => {
    const wrapped = 'word '.repeat(400).replace(/(.{68}) /g, '$1\n');
    const verse =
      'Shall I compare thee\nto a summer’s day?\nThou art more lovely\nand more temperate.';
    const ps = paragraphs(`${wrapped}\n\n${verse}`);
    expect(ps[0]).not.toContain('\n');
    expect(ps[1]).toBe(verse);
  });

  it('cuts a block of verses with no blank line at sentence ends (World English Bible)', () => {
    const verses = Array.from(
      { length: 600 },
      (_, i) => `001:${i} He shall burn the whole on the altar, for a burnt offering.`,
    ).join('\n');
    const ps = paragraphs(verses);
    expect(ps.length).toBeGreaterThan(5);
    expect(Math.max(...ps.map((p) => p.length))).toBeLessThan(MAX_PARAGRAPH_CHARS);
    expect(ps.join(' ')).toBe(verses.replace(/\n/g, ' '));
  });
});

describe('pages', () => {
  it('reads a long chapter a page at a time, evenly, at paragraph ends, losing nothing', () => {
    const para = (n: number) => `Paragraph ${n} ${'x'.repeat(990)}`;
    const long: Chapter = {
      title: 'One',
      paragraphs: Array.from({ length: 250 }, (_, i) => para(i)),
    };
    const short: Chapter = { title: 'Two', paragraphs: ['short'] };
    const units = pages([long, short], 60_000);
    expect(units.map((u) => u.page?.of)).toEqual([5, 5, 5, 5, 5, undefined]);
    expect(units.map((u) => u.page?.index)).toEqual([0, 1, 2, 3, 4, undefined]);
    expect(units.slice(0, 5).flatMap((u) => u.paragraphs)).toEqual(long.paragraphs);
    for (const u of units.slice(0, 5)) {
      expect(u.title).toBe('One');
      expect(u.paragraphs.join('').length).toBeLessThanOrEqual(PAGE_CHARS);
      expect(u.paragraphs.join('').length).toBeGreaterThan(40_000);
    }
    expect(units[5]).toBe(short);
  });

  it('keeps folded headings with the page they fall on', () => {
    const c: Chapter = {
      title: 'C',
      paragraphs: ['a'.repeat(700), 'HEADING', 'b'.repeat(700)],
      subheads: [1],
    };
    const units = pages([c], 800);
    expect(units.map((u) => u.subheads ?? [])).toEqual([[], [0]]);
  });
});

describe('long Title Case chapter titles and a contents list with summaries (Gibbon, 2026-10-17)', () => {
  const body = (n: number) =>
    Array.from({ length: 8 }, (_, i) => `Text ${n}.${i} ${'word '.repeat(60)}.`);
  const head = (n: string, t: string, part: string) => `Chapter ${n}: ${t}.—Part ${part}.`;
  const t6 = 'Death Of Severus, Tyranny Of Caracalla, Usurpation Of Marcinus';
  const t1 = 'The Extent Of The Empire In The Age Of The Antonines';
  const text = [
    'Contents',
    head('I', t1, 'I'),
    'The Extent And Military Force Of The Empire.',
    head('V', 'Sale Of The Empire To Didius Julianus', 'II'),
    head('VI', t6, 'I'),
    'The Death Of Severus.—Tyranny Of Caracalla.—Follies Of Elagabalus.',
    head('VI', t6, 'II'),
    head('VI', t6, 'III'),
    head('VI', t6, 'IV'),
    'Preface',
    ...body(0),
    head('I', t1, 'I'),
    ...body(1),
    head('V', 'Sale Of The Empire To Didius Julianus', 'II'),
    ...body(5),
    head('VI', t6, 'I'),
    ...body(61),
    head('VI', t6, 'II'),
    ...body(62),
    head('VI', t6, 'III'),
    ...body(63),
    head('VI', t6, 'IV'),
    ...body(64),
  ].join('\n\n');

  it('reads every chapter, and the contents stay in the front matter', () => {
    const titles = splitChapters(text, 'Gibbon').map((c) => c.title);
    expect(titles[0]).toBe('Gibbon');
    expect(titles.slice(-6)).toEqual([
      head('I', t1, 'I'),
      head('V', 'Sale Of The Empire To Didius Julianus', 'II'),
      head('VI', t6, 'I'),
      head('VI', t6, 'II'),
      head('VI', t6, 'III'),
      head('VI', t6, 'IV'),
    ]);
    expect(titles.filter((t) => t.startsWith('Chapter V:'))).toHaveLength(1);
  });
});
