/**
 * Books as chapters: the pure part of the reader (pages/reader.ts). A plain-text book (Gutenberg,
 * Wikisource, Standard Ebooks) is split into paragraphs on blank lines, and a short heading-like
 * paragraph ("CHAPTER I", "Letter 1", "BOOK THE FIRST", an all-capitals line) starts a chapter. The
 * front matter before the first heading is a chapter of its own. Fewer than three headings is not a
 * structure worth a table of contents: the book is one chapter then.
 *
 * Also the reader's memory: where the viewer is in each book, kept in this browser only.
 */
import type { SafeStorage } from './storage.ts';

export interface Chapter {
  title: string;
  /** Paragraphs of the chapter, the heading excluded. */
  paragraphs: string[];
}

const HEADING_WORD =
  /^(chapter|letter|book|part|volume|canto|act|scene|stave|prologue|epilogue|introduction|preface|appendix|conclusion)\b/i;
export const MIN_HEADINGS = 3;
/** This many heading lines in a row are a list of contents, not chapters. */
export const LIST_RUN = 3;

/** For each paragraph, the length of the run of consecutive headings it is in (0 for body text). */
export function runLengths(heads: readonly boolean[]): number[] {
  const out = new Array<number>(heads.length).fill(0);
  let start = 0;
  for (let i = 0; i <= heads.length; i++) {
    if (i === heads.length || !heads[i]) {
      for (let k = start; k < i; k++) out[k] = i - start;
      start = i + 1;
    }
  }
  return out;
}
const MAX_HEADING_CHARS = 80;

/** Paragraphs of a plain text: blank lines separate them, wrapped lines are joined. */
export function paragraphs(text: string): string[] {
  const out: string[] = [];
  for (const para of text.split(/\n\s*\n/)) {
    const t = para.replace(/\s*\n\s*/g, ' ').trim();
    if (t) out.push(t);
  }
  return out;
}

/** Whether a paragraph reads as a chapter heading. */
export function isHeading(p: string): boolean {
  if (p.length > MAX_HEADING_CHARS) return false;
  const words = p.split(/\s+/).length;
  if (HEADING_WORD.test(p)) return words <= 12; // "CHAPTER 91. The Pequod Meets the Rose-Bud."
  if (/[.!?]$/.test(p) && words > 6) return false; // a short sentence in capitals is still a sentence
  return p === p.toUpperCase() && /[A-Z]/.test(p) && p.length < 60;
}

export function splitChapters(text: string, bookTitle = ''): Chapter[] {
  const ps = paragraphs(text);
  const heads = ps.map(isHeading);
  const runs = runLengths(heads);
  const chapters: Chapter[] = [];
  let current: Chapter = { title: bookTitle || 'Front matter', paragraphs: [] };
  let headings = 0;
  let started = false; // a heading before any body text is a title line, not a chapter
  const seen = new Set<string>();
  for (const [i, p] of ps.entries()) {
    const heading = heads[i]! && started;
    // A run of heading lines is a contents list (Moby Dick lists its 135 chapters up front): its
    // lines read as paragraphs of the chapter they are in, not as chapters. Real chapters have
    // text; the body's first heading, which often follows the list directly, repeats a line of it.
    const key = p.toLowerCase();
    const repeat = seen.has(key);
    if (heads[i]) seen.add(key);
    const lastOfRun = heads[i + 1] !== true;
    const inList = heading && runs[i]! >= LIST_RUN && !(lastOfRun && repeat);
    if (heading && !inList) {
      headings++;
      chapters.push(current);
      current = { title: p, paragraphs: [] };
    } else {
      current.paragraphs.push(p);
      if (!heads[i]) started = true;
    }
  }
  chapters.push(current);
  if (headings < MIN_HEADINGS) return [{ title: bookTitle || 'Text', paragraphs: ps }];
  // A chapter with no text of its own ("PART I" right before "CHAPTER I") names the next one.
  const out: Chapter[] = [];
  let pending: string[] = [];
  for (const c of chapters) {
    if (c.paragraphs.length === 0 && out.length) {
      pending.push(c.title);
      continue;
    }
    out.push(pending.length ? { ...c, title: [...pending, c.title].join(' · ') } : c);
    pending = [];
  }
  if (pending.length) out.push({ title: pending.join(' · '), paragraphs: [] });
  return out;
}

/* -------------------------------- reading positions -------------------------------- */

export interface ReadingPosition {
  docId: string;
  address: string;
  title: string;
  chapter: number;
  /** Paragraph index within the chapter at the top of the screen. */
  paragraph: number;
  chapters: number;
  updated: string;
}

const KEY = 'books:positions';
export const POSITIONS_KEPT = 24;

function isPosition(v: unknown): v is ReadingPosition {
  if (typeof v !== 'object' || v === null) return false;
  const p = v as Record<string, unknown>;
  return (
    typeof p.docId === 'string' &&
    typeof p.address === 'string' &&
    typeof p.title === 'string' &&
    typeof p.chapter === 'number' &&
    typeof p.paragraph === 'number' &&
    typeof p.chapters === 'number' &&
    typeof p.updated === 'string'
  );
}

export function positions(storage: SafeStorage): ReadingPosition[] {
  return storage.read<ReadingPosition[]>(
    KEY,
    [],
    (v): v is ReadingPosition[] => Array.isArray(v) && v.every(isPosition),
  );
}

export function positionOf(storage: SafeStorage, docId: string): ReadingPosition | null {
  return positions(storage).find((p) => p.docId === docId) ?? null;
}

export function rememberPosition(
  storage: SafeStorage,
  position: Omit<ReadingPosition, 'updated'>,
  now = new Date(),
): void {
  const rest = positions(storage).filter((p) => p.docId !== position.docId);
  storage.write(
    KEY,
    [{ ...position, updated: now.toISOString() }, ...rest].slice(0, POSITIONS_KEPT),
  );
}

export function forgetPosition(storage: SafeStorage, docId: string): void {
  storage.write(
    KEY,
    positions(storage).filter((p) => p.docId !== docId),
  );
}

/** Books started and not finished (the last chapter's end counts as finished), newest first. */
export function unfinishedBooks(storage: SafeStorage): ReadingPosition[] {
  return positions(storage).filter((p) => !(p.chapter >= p.chapters - 1 && p.paragraph < 0));
}
