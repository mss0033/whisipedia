/**
 * Question pools as practice material: the NCVEC amateur radio pools (Technician, General, Extra)
 * refined to text, one document per pool. Pure parsing and sampling; rendering lives in
 * pages/practice.ts.
 *
 * The text is regular, one question per block:
 *
 *   T1A01 (C) [97.1]
 *   Which of the following is part of the Basis and Purpose of the Amateur Radio Service?
 *   A. Providing personal radio communications for as many citizens as possible
 *   B. ...
 *   ~~
 *
 * The id names the subelement (T1), the group (T1A) and the question; the letter in parentheses is
 * the answer; the bracket is the FCC rule it rests on. Lines wrap where the PDF wrapped them. A
 * syllabus before the questions names each subelement ("SUBELEMENT T1 - COMMISSION'S RULES [6 Exam
 * Questions - 6 Groups]") and group ("T1A Purpose and permissible use ..."). An exam takes one
 * question from every group; 74% passes (26 of 35, 37 of 50).
 */

export type Letter = 'A' | 'B' | 'C' | 'D';
export const LETTERS: readonly Letter[] = ['A', 'B', 'C', 'D'];

export interface Question {
  id: string;
  /** T1A, the exam draws one question from each. */
  group: string;
  /** T1 */
  subelement: string;
  answer: Letter;
  /** FCC rule reference as printed, e.g. "[97.1]", or ''. */
  reference: string;
  text: string;
  choices: Record<Letter, string>;
}

export interface Group {
  id: string;
  title: string;
  questions: Question[];
}

export interface Subelement {
  id: string;
  title: string;
  /** Questions the exam takes from this subelement (one per group). */
  examQuestions: number;
  groups: Group[];
}

export interface Pool {
  /** Technician, General, Extra, or '' when the title does not say. */
  licenseClass: string;
  subelements: Subelement[];
  questions: Question[];
  /** Questions on an exam: one per group. */
  examSize: number;
  /** Correct answers needed: 74%, rounded up. */
  passMark: number;
}

const HEADER_RE = /^([TGE]\d[A-Z]\d{2})\s*\(([A-D])\)\s*(\[[^\]\n]*\])?[ \t]*$/;
const CHOICE_RE = /^([A-D])\.\s+(.*)$/;
const SUBELEMENT_RE =
  /^SUBELEMENT\s+([TGE]\d)\s*[-–—]\s*(.+?)\s*\[(\d+)\s+Exam Questions?\s*[-–—]\s*(\d+)\s+Groups?\]/i;
const GROUP_RE = /^([TGE]\d[A-Z])\s+(?!\d)(\S.*)$/;

function lines(text: string): string[] {
  return text.replace(/\r\n?/g, '\n').split('\n');
}

export function licenseClassOf(title: string): string {
  const m = /\b(Technician|General|Extra)\b/i.exec(title);
  return m ? m[1]![0]!.toUpperCase() + m[1]!.slice(1).toLowerCase() : '';
}

/** The questions of a pool text, by id; a later block with the same id replaces an earlier one. */
export function parseQuestions(text: string): Question[] {
  const out = new Map<string, Question>();
  const ls = lines(text);
  for (let i = 0; i < ls.length; i++) {
    const head = HEADER_RE.exec(ls[i]!.trim());
    if (!head) continue;
    const [, id, answer, reference] = head;
    const body: string[] = [];
    let j = i + 1;
    for (; j < ls.length; j++) {
      const t = ls[j]!.trim();
      if (t === '~~' || HEADER_RE.test(t)) break;
      body.push(t);
    }
    const q = questionFrom(id!, answer as Letter, reference ?? '', body);
    if (q) out.set(q.id, q);
    i = j - 1;
  }
  return [...out.values()];
}

function questionFrom(
  id: string,
  answer: Letter,
  reference: string,
  body: string[],
): Question | null {
  const textLines: string[] = [];
  const choices: Partial<Record<Letter, string>> = {};
  let current: Letter | null = null;
  for (const line of body) {
    if (!line) continue;
    const c = CHOICE_RE.exec(line);
    if (c) {
      current = c[1] as Letter;
      choices[current] = c[2]!.trim();
    } else if (current) {
      choices[current] = `${choices[current]} ${line}`.trim();
    } else {
      textLines.push(line);
    }
  }
  const text = textLines.join(' ').replace(/\s+/g, ' ').trim();
  if (!text || LETTERS.some((l) => !choices[l])) return null; // withdrawn questions keep no choices
  return {
    id,
    group: id.slice(0, 3),
    subelement: id.slice(0, 2),
    answer,
    reference: reference.trim(),
    text,
    choices: choices as Record<Letter, string>,
  };
}

/** Subelement and group titles from the syllabus; later lines with the same id are ignored. */
export function parseSyllabus(text: string): {
  subelements: Map<string, { title: string; examQuestions: number }>;
  groups: Map<string, string>;
} {
  const subelements = new Map<string, { title: string; examQuestions: number }>();
  const groups = new Map<string, string>();
  for (const raw of lines(text)) {
    const line = raw.trim();
    const s = SUBELEMENT_RE.exec(line);
    if (s) {
      if (!subelements.has(s[1]!)) {
        subelements.set(s[1]!, { title: titleCase(s[2]!), examQuestions: Number(s[3]) });
      }
      continue;
    }
    const g = GROUP_RE.exec(line);
    if (g && !groups.has(g[1]!)) groups.set(g[1]!, g[2]!.replace(/\s+$/, '').slice(0, 160));
  }
  return { subelements, groups };
}

function titleCase(s: string): string {
  const t = s.trim();
  if (t !== t.toUpperCase()) return t;
  return t
    .toLowerCase()
    .replace(/(^|[\s(/-])([a-z])/g, (_m, p: string, c: string) => p + c.toUpperCase());
}

export function parsePool(text: string, title = ''): Pool {
  const questions = parseQuestions(text);
  const syllabus = parseSyllabus(text);
  const bySub = new Map<string, Map<string, Question[]>>();
  for (const q of questions) {
    let groups = bySub.get(q.subelement);
    if (!groups) {
      groups = new Map();
      bySub.set(q.subelement, groups);
    }
    let list = groups.get(q.group);
    if (!list) {
      list = [];
      groups.set(q.group, list);
    }
    list.push(q);
  }
  const subelements: Subelement[] = [...bySub.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([id, groups]) => {
      const info = syllabus.subelements.get(id);
      const gs: Group[] = [...groups.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([gid, qs]) => ({ id: gid, title: syllabus.groups.get(gid) ?? '', questions: qs }));
      return {
        id,
        title: info?.title ?? '',
        examQuestions: info?.examQuestions ?? gs.length,
        groups: gs,
      };
    });
  const examSize = subelements.reduce((n, s) => n + Math.min(s.examQuestions, s.groups.length), 0);
  return {
    licenseClass: licenseClassOf(title),
    subelements,
    questions,
    examSize,
    passMark: Math.ceil(examSize * 0.74),
  };
}

/** A small deterministic generator (mulberry32) so an exam can be rebuilt from its seed. */
export function seeded(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** One question from each group, as the exam is drawn, in syllabus order. */
export function drawExam(pool: Pool, seed: number): Question[] {
  const rng = seeded(seed);
  const out: Question[] = [];
  for (const s of pool.subelements) {
    const groups = s.groups.filter((g) => g.questions.length > 0);
    const take = Math.min(s.examQuestions, groups.length);
    for (const g of groups.slice(0, take)) {
      out.push(g.questions[Math.floor(rng() * g.questions.length)]!);
    }
  }
  return out;
}

export function newSeed(): number {
  return Math.floor(Math.random() * 0x7fffffff) + 1;
}

/** What a learner has answered in a pool: question id -> whether the last answer was right. */
export interface Progress {
  answers: Record<string, boolean>;
  updated: string;
}

export function isProgress(v: unknown): v is Progress {
  if (typeof v !== 'object' || v === null) return false;
  const p = v as Record<string, unknown>;
  return (
    typeof p.answers === 'object' &&
    p.answers !== null &&
    Object.values(p.answers as Record<string, unknown>).every((x) => typeof x === 'boolean') &&
    typeof p.updated === 'string'
  );
}

export function tally(progress: Progress, questions: Question[]): { seen: number; right: number } {
  let seen = 0;
  let right = 0;
  for (const q of questions) {
    const a = progress.answers[q.id];
    if (a === undefined) continue;
    seen++;
    if (a) right++;
  }
  return { seen, right };
}
