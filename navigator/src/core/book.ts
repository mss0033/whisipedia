/**
 * Books as chapters: the pure part of the reader (pages/reader.ts). A plain-text book (Gutenberg,
 * Wikisource, Standard Ebooks) is split into paragraphs on blank lines, and a short heading-like
 * paragraph ("CHAPTER I", "Letter 1", "BOOK THE FIRST", an all-capitals line) starts a chapter. The
 * front matter before the first heading is a chapter of its own. Fewer than three headings is not a
 * structure worth a table of contents: the book is one chapter then. A chapter too long for one
 * screenful of scrolling is cut into pages (`pages`).
 *
 * Also the reader's memory: where the viewer is in each book, kept in this browser only.
 */
import type { SafeStorage } from './storage.ts';

export interface Chapter {
  title: string;
  /** Paragraphs of the chapter, the heading excluded. A verse paragraph keeps its lines ("\n"). */
  paragraphs: string[];
  /** Paragraphs that are headings folded into the chapter (a contents list, a short poem's title). */
  subheads?: number[];
  /** Of a chapter cut into pages: this page (from 0) and how many. */
  page?: { index: number; of: number };
  /** The volume ("Vol. II") a book in volumes put the chapter in, from its title page. */
  volume?: string;
}

/** "VOL. II.", "Volume 3": a volume's title page line. */
const VOLUME = /^(?:vol\.?|volume)\s+([ivxlc]+|\d{1,2})\.?$/i;

const HEADING_WORD =
  /^(chapter|letter|book|part|volume|canto|act|scene|stave|prologue|epilogue|introduction|preface|appendix|conclusion|lecture|section|sermon|lesson|essay)\b/i;
/**
 * "IV. SIGNED, VERONICA", "XII.": a numbered heading in capitals. In a book split on keywords it is a
 * chapter too (The Filigree Ball: three BOOKs of numbered chapters); it does not make a book one.
 */
const NUMBERED = /^(?:[IVXLC]+\.|(?:[IVXLC]+|\d{1,3})\.\s+[^a-z]+)$/;
/** A keyword heading; "SCENE: Partly in France" is a stage note (it made Shakespeare's ACT I SCENE I a contents list). */
const keywordHeading = (p: string) => HEADING_WORD.test(p) && !/^\p{L}+:/u.test(p);
const strong = (p: string) => keywordHeading(p) || NUMBERED.test(p);
/** Headings that open a group of chapters: a short one carries on into the chapter after it. */
const GROUP_WORD = /^(book|part|volume)\b/i;
export const MIN_HEADINGS = 3;
/** This many heading lines in a row are a list of contents, not chapters. */
export const LIST_RUN = 3;
/** A chapter with less text than this whose title comes again later is a contents entry. */
export const MIN_CHAPTER_CHARS = 1000;
/** Chapters longer than this are read a page at a time (about 9,000 words). */
export const PAGE_CHARS = 60_000;

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
/** "CHAPTER VII—WHICH EXPLAINS HOW … UNDER HIS ARM": a keyword heading in capitals may be long. */
const MAX_CAPS_HEADING_CHARS = 200;

/** The width the text was wrapped at: most lines of prose are about this long. */
function wrapWidth(lines: readonly string[]): number {
  const lengths = lines.map((l) => l.trimEnd().length).filter((n) => n > 0);
  if (lengths.length < 20) return 0;
  lengths.sort((a, b) => a - b);
  return lengths[Math.floor(lengths.length * 0.9)]!;
}

/** A paragraph of short lines in a wrapped text is verse (or a list, an address): its lines stay. */
function isVerse(lines: readonly string[], wrap: number): boolean {
  if (lines.length < 2 || wrap < 40) return false;
  const short = Math.min(wrap, 80) * 0.7;
  const body = lines.slice(0, -1);
  return body.filter((l) => l.length < short).length * 2 > body.length;
}

/** A block longer than this is cut at line ends: a page can only end between paragraphs. */
export const MAX_PARAGRAPH_CHARS = 10_000;

/** Lines of one over-long block in pieces of about MAX_PARAGRAPH_CHARS / 3, ending where a sentence does. */
function cutLines(lines: readonly string[]): string[][] {
  const out: string[][] = [[]];
  let n = 0;
  for (const l of lines) {
    out.at(-1)!.push(l);
    n += l.length;
    if (n >= MAX_PARAGRAPH_CHARS / 3 && /[.!?:;"”’)\]]$/.test(l)) {
      out.push([]);
      n = 0;
    }
  }
  return out.filter((g) => g.length);
}

/**
 * Paragraphs of a plain text: blank lines separate them; wrapped lines are joined, verse lines kept.
 * A block of tens of thousands of characters (the World English Bible sets a whole book of verses
 * without a blank line) is cut at sentence ends.
 */
export function paragraphs(text: string): string[] {
  const out: string[] = [];
  const wrap = wrapWidth(text.split('\n'));
  for (const para of text.split(/\n\s*\n/)) {
    const all = para
      .split('\n')
      .map((l) => l.trim())
      .filter(Boolean);
    if (all.length === 0) continue;
    const groups = para.length > MAX_PARAGRAPH_CHARS ? cutLines(all) : [all];
    for (const lines of groups) out.push(isVerse(lines, wrap) ? lines.join('\n') : lines.join(' '));
  }
  return out;
}

const flat = (p: string) => p.replace(/\n/g, ' ');

/**
 * "Chapter VI: Death Of Severus, Tyranny Of Caracalla, Usurpation Of Marcinus.—Part I.": a numbered
 * keyword heading in Title Case may be as long as one in capitals. Gibbon's longer chapter titles
 * were read as text, and chapters VI and VII were pages of chapter V (2026-10-17 field exam).
 */
function numberedTitle(p: string): boolean {
  if (!/^\p{L}+\s+(?:[IVXLC]+|\d{1,3})\b[.:]?/u.test(p)) return false;
  const words = p.split(/\s+/).filter((w) => /^\p{L}{4,}/u.test(w));
  return words.length > 0 && words.every((w) => /^\p{Lu}/u.test(w));
}

/** Whether a paragraph reads as a chapter heading. */
export function isHeading(para: string): boolean {
  const p = flat(para).replace(/\]$/, '');
  const caps = p === p.toUpperCase() && /[A-Z]/.test(p);
  if (strong(p)) {
    // "CHAPTER 91. The Pequod Meets the Rose-Bud."
    if (p.length <= MAX_HEADING_CHARS) return p.split(/\s+/).length <= 12 || numberedTitle(p);
    return (caps || numberedTitle(p)) && p.length <= MAX_CAPS_HEADING_CHARS;
  }
  if (p.length > MAX_HEADING_CHARS) return false;
  if (/[.!?]$/.test(p) && p.split(/\s+/).length > 6) return false; // a short sentence in capitals is still a sentence
  return caps && p.length < 60;
}

/**
 * A paragraph that stops mid-sentence ("as follows:--", "the sincere wish of"): what follows goes on
 * with it. A title ("Hard Vvordes Explicated") ends on a capitalised word, and a list or a verse
 * keeps its lines: neither is an unfinished sentence.
 */
function unfinished(p: string): boolean {
  return !p.includes('\n') && /(?:[,;:\-—–]|(?:^|\s)\p{Ll}\p{L}*)$/u.test(p);
}

/**
 * Which paragraphs start chapters. A book with enough keyword headings ("CHAPTER I", "INTRODUCTION")
 * is split on those only, and a capitalised line right after one is its subtitle; otherwise any
 * capitalised line is a title page's author, publisher or dedication, not a chapter (field exam
 * 2026-10-14: "ALFRED P MORGAN" was chapter 1 of The Boy Electrician). A capitalised line after an
 * unfinished sentence is part of it: a signature, a quoted notice (2026-10-16: chapters of Famous
 * Americans of Recent Times stopped at "Is the sincere wish of").
 */
export function headingFlags(ps: readonly string[]): boolean[] {
  // "[Illustration:" … "PROLOGUE.]": a caption split by a blank line holds no heading. Only a short
  // opening is a caption, and it ends within two paragraphs (an editor's note may never close).
  let open = 0;
  const any = ps.map((p, i) => {
    const inside = open > 0;
    open = inside && p.endsWith(']') ? 0 : Math.max(0, open - 1);
    if (p.length < 100 && p.lastIndexOf('[') > p.lastIndexOf(']')) open = 2;
    if (!isHeading(p)) return false;
    if (inside) return keywordHeading(p); // "[Illustration: ·PRIDE AND PREJUDICE·" … "Chapter I.]"
    return keywordHeading(p) || i === 0 || !unfinished(ps[i - 1]!);
  });
  if (ps.filter((p, i) => any[i]! && keywordHeading(p)).length < MIN_HEADINGS) return any;
  const keyword = ps.map((p, i) => any[i]! && strong(p));
  return ps.map((_, i) => keyword[i]! || (any[i]! && i > 0 && keyword[i - 1]!));
}

const textLength = (c: Chapter) => c.paragraphs.reduce((n, p) => n + p.length, 0);

/** A chapter made mostly of heading lines is a list of contents (often of the volume that follows). */
function isContentsList(c: Chapter): boolean {
  const lines = c.paragraphs.flatMap((p) => p.split('\n')); // a list kept as lines is one paragraph
  if (lines.length < 4 || textLength(c) / lines.length > 100) return false;
  // "CHAPTER XIV THE THREE TORPEDOED CRUISERS        169 ABLE-SEAMAN C. C. NURSE.": a page number set off by spaces
  const entry = (l: string) => isHeading(l) || strong(l) || /\S {4,}(?:\d+|[ivxlc]+)\b/.test(l);
  return lines.filter(entry).length >= lines.length * 0.6;
}

/** `into` gains the chapter `c`: its title as a subheading, then its paragraphs. */
function fold(into: Chapter, c: Chapter): void {
  into.subheads = [...(into.subheads ?? []), into.paragraphs.length];
  into.paragraphs.push(c.title, ...c.paragraphs);
  for (const s of c.subheads ?? [])
    into.subheads.push(into.paragraphs.length - c.paragraphs.length + s);
}

export function splitChapters(text: string, bookTitle = ''): Chapter[] {
  const ps = paragraphs(text);
  const heads = headingFlags(ps);
  const runs = runLengths(heads);
  const chapters: Chapter[] = [];
  let current: Chapter = { title: bookTitle || 'Front matter', paragraphs: [] };
  let headings = 0;
  let started = false; // a heading before any body text is a title line, not a chapter
  const seen = new Set<string>();
  let volume = '';
  for (const [i, p] of ps.entries()) {
    const vol = VOLUME.exec(flat(p).trim());
    if (vol) volume = `Vol. ${vol[1]!.toUpperCase()}`;
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
      current = {
        title: flat(p).replace(/\]$/, ''),
        paragraphs: [],
        ...(volume ? { volume } : {}),
      };
    } else {
      current.paragraphs.push(p);
      if (!heads[i]) started = true;
    }
  }
  chapters.push(current);
  if (headings < MIN_HEADINGS) return [{ title: bookTitle || 'Text', paragraphs: ps }];
  // A chapter with no text of its own ("PART I" right before "CHAPTER I") names the next one.
  const named: Chapter[] = [];
  let pending: string[] = [];
  for (const c of chapters) {
    if (c.paragraphs.length === 0 && named.length) {
      pending.push(c.title);
      continue;
    }
    named.push(pending.length ? { ...c, title: [...pending, c.title].join(' · ') } : c);
    pending = [];
  }
  if (pending.length) named.push({ title: pending.join(' · '), paragraphs: [] });
  // A contents list cut into "chapters" (an entry and its one-line summary, entries too long to read
  // as headings) is not a place to read: it goes back into the chapter before, where the list stands
  // (2026-10-16 field exam: Les Misérables opened on chapters "V", "VI", "V" of its contents; The
  // Tragedies of the Medici had six chapters of one line each). A short "PART II" with a line of
  // introduction opens the chapter after it.
  const out: Chapter[] = [];
  let carry: Chapter | null = null;
  // How many chapters from here on carry each title: a small chapter whose title comes again later
  // is that chapter's line in a contents list.
  const key = (t: string) =>
    t
      .split(' · ')[0]!
      .toLowerCase()
      .replace(/[^\p{L}\p{N}]+/gu, '');
  const later = new Map<string, number>();
  for (const c of named) later.set(key(c.title), (later.get(key(c.title)) ?? 0) + 1);
  for (const [i, c] of named.entries()) {
    const next = named[i + 1];
    const k = key(c.title);
    later.set(k, later.get(k)! - 1);
    const small = textLength(c) < MIN_CHAPTER_CHARS;
    // Lines naming chapters still to come: a stretch of a contents list whose entries carry summaries
    // (Gibbon: "Chapter VI: … Part I." and a paragraph of its subjects), too long to be "small".
    const echoes = c.paragraphs.filter((p) => isHeading(p) && (later.get(key(p)) ?? 0) > 0).length;
    const entry = later.get(k)! > 0 && (small || echoes >= 3);
    if (i > 0 && small && next && GROUP_WORD.test(c.title) && !carry) {
      carry = c;
      continue;
    }
    let chapter = c;
    if (carry) {
      const offset = carry.paragraphs.length;
      const subheads = [...(carry.subheads ?? []), ...(c.subheads ?? []).map((s) => s + offset)];
      chapter = {
        title: `${carry.title} · ${c.title}`,
        paragraphs: [...carry.paragraphs, ...c.paragraphs],
        ...(subheads.length ? { subheads } : {}),
        ...(c.volume ? { volume: c.volume } : {}),
      };
      carry = null;
    }
    if (out.length && (entry || isContentsList(chapter))) fold(out.at(-1)!, chapter);
    else out.push(chapter);
  }
  if (carry) fold(out.at(-1)!, carry);
  return out.length < 2 ? [{ title: bookTitle || 'Text', paragraphs: ps }] : withVolumes(out);
}

/**
 * Chapters of a book in volumes are named with their volume when their titles repeat: Pride and
 * Prejudice's contents listed "CHAPTER I." three times (2026-10-17 field exam).
 */
function withVolumes(chapters: Chapter[]): Chapter[] {
  const vols = new Map<string, Set<string>>();
  for (const c of chapters) {
    if (!c.volume) continue;
    const k = c.title.toLowerCase();
    vols.set(k, (vols.get(k) ?? new Set()).add(c.volume));
  }
  if (![...vols.values()].some((v) => v.size > 1)) return chapters;
  return chapters.map((c) => (c.volume ? { ...c, title: `${c.volume} · ${c.title}` } : c));
}

/**
 * Chapters cut into pages of at most about `max` characters, at paragraph ends, as even as the
 * paragraphs allow: a book with no chapters (the King James Bible, 4.3 MB) or a chapter of 300,000
 * characters was one page with nothing to move by but the scroll bar (2026-10-16 field exam).
 */
export function pages(chapters: readonly Chapter[], max = PAGE_CHARS): Chapter[] {
  const out: Chapter[] = [];
  for (const c of chapters) {
    const total = textLength(c);
    if (total <= max) {
      out.push(c);
      continue;
    }
    const of = Math.ceil(total / max);
    const target = total / of;
    const heads = new Set(c.subheads ?? []);
    const cuts: number[] = [0];
    let acc = 0;
    for (const [i, p] of c.paragraphs.entries()) {
      acc += p.length;
      if (acc < target * cuts.length || cuts.length >= of || i + 1 >= c.paragraphs.length) continue;
      cuts.push(heads.has(i) && i > cuts.at(-1)! ? i : i + 1); // a heading goes with the text under it
    }
    cuts.push(c.paragraphs.length);
    const n = cuts.length - 1;
    for (let k = 0; k < n; k++) {
      const [from, to] = [cuts[k]!, cuts[k + 1]!];
      const subheads = (c.subheads ?? []).filter((s) => s >= from && s < to).map((s) => s - from);
      out.push({
        title: c.title,
        paragraphs: c.paragraphs.slice(from, to),
        ...(subheads.length ? { subheads } : {}),
        page: { index: k, of: n },
      });
    }
  }
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
  /** "Chapter 3 of 12 · page 2 of 4", as the reader says it (positions kept before pages lack it). */
  where?: string;
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
