import { describe, expect, it } from 'vitest';
import {
  forgetPosition,
  isHeading,
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
