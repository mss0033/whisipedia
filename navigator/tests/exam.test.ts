import { describe, expect, it } from 'vitest';
import {
  drawExam,
  isProgress,
  licenseClassOf,
  parsePool,
  parseQuestions,
  parseSyllabus,
  seeded,
  tally,
} from '../src/core/exam.ts';

const SAMPLE_POOL = [
  '2026-2030 Technician Pool and Syllabus Public Release.pdf',
  'Issuer: NCVEC Question Pool Committee',
  '',
  'SUBELEMENT T1 - COMMISSION’S RULES [2 Exam Questions - 2 Groups] 4 Questions',
  'T1A Purpose and permissible use of the Amateur Radio Service; Operator/primary station license grant; ',
  'Meanings of basic terms used in FCC rules',
  'T1B Frequency allocations; Emission modes',
  'SUBELEMENT T2 - OPERATING PROCEDURES [1 Exam Question - 1 Group] 2 Questions',
  'T2A Station operation',
  '',
  'T1A01 (C) [97.1]',
  'Which of the following is part of the Basis and Purpose of the Amateur Radio Service?',
  'A. Providing personal radio communications for as many citizens as possible',
  'B. Providing communications for international contesting',
  'C. Advancing skills in the technical and communication phases of the radio art',
  'D. All these choices are correct',
  '~~',
  'T1A02 (B)',
  'What do the FCC rules state regarding the use of a phonetic alphabet for station identification in the Amateur ',
  'Radio Service?',
  'A. It is required when transmitting emergency messages',
  'B. It is encouraged when using phone emissions and the ',
  'operator wants to be clear',
  'C. It is required when in contact with foreign stations',
  'D. All these choices are correct',
  '~~',
  'T1A03 (A) [97.3(a)(5)]',
  'Question withdrawn from the pool.',
  '~~',
  'T1B01 (D) [97.301]',
  'Which frequency is in the 6 meter band?',
  'A. 49.00 MHz',
  'B. 52.525 MHz',
  'C. 28.50 MHz',
  'D. Both A and B',
  '~~',
  'T1B02 (A) [97.305]',
  'Which emission is phone?',
  'A. Voice',
  'B. CW',
  'C. Data',
  'D. Image',
  '~~',
  'T2A01 (B) [97.119]',
  'How often must you identify?',
  'A. Every 5 minutes',
  'B. Every 10 minutes',
  'C. Every 15 minutes',
  'D. Only at the end',
  '~~',
  'T2A02 (C)',
  'Which call is a distress call?',
  'A. CQ',
  'B. QRZ',
  'C. Mayday',
  'D. 73',
  '~~',
].join('\r\n');

describe('question pools', () => {
  it('parses questions, joins wrapped lines and drops withdrawn ones', () => {
    const qs = parseQuestions(SAMPLE_POOL);
    expect(qs.map((q) => q.id)).toEqual(['T1A01', 'T1A02', 'T1B01', 'T1B02', 'T2A01', 'T2A02']);
    const q = qs[1]!;
    expect(q.text).toMatch(/station identification in the Amateur Radio Service\?$/);
    expect(q.choices.B).toBe(
      'It is encouraged when using phone emissions and the operator wants to be clear',
    );
    expect(q.answer).toBe('B');
    expect(q.reference).toBe('');
    expect(qs[0]!.reference).toBe('[97.1]');
    expect(qs[0]!.group).toBe('T1A');
    expect(qs[0]!.subelement).toBe('T1');
  });

  it('reads the syllabus titles once, from the first mention', () => {
    const s = parseSyllabus(
      `${SAMPLE_POOL}\nSUBELEMENT T1 - SOMETHING ELSE [9 Exam Questions - 9 Groups]`,
    );
    expect(s.subelements.get('T1')).toEqual({ title: 'Commission’s Rules', examQuestions: 2 });
    expect(s.subelements.get('T2')?.examQuestions).toBe(1);
    expect(s.groups.get('T1A')).toMatch(/^Purpose and permissible use/);
    expect(s.groups.get('T2A')).toBe('Station operation');
  });

  it('builds the pool with exam size and pass mark', () => {
    const pool = parsePool(SAMPLE_POOL, '2026-2030 Technician Pool');
    expect(pool.licenseClass).toBe('Technician');
    expect(pool.subelements.map((s) => s.id)).toEqual(['T1', 'T2']);
    expect(pool.subelements[0]!.groups.map((g) => g.id)).toEqual(['T1A', 'T1B']);
    expect(pool.examSize).toBe(3);
    expect(pool.passMark).toBe(3); // ceil(3 * 0.74)
    expect(parsePool(SAMPLE_POOL).licenseClass).toBe('');
    expect(licenseClassOf('2024-2028 Extra Class Question Pool')).toBe('Extra');
  });

  it('draws one question per group, the same for the same seed', () => {
    const pool = parsePool(SAMPLE_POOL);
    const a = drawExam(pool, 42);
    const b = drawExam(pool, 42);
    expect(a.map((q) => q.group)).toEqual(['T1A', 'T1B', 'T2A']);
    expect(a.map((q) => q.id)).toEqual(b.map((q) => q.id));
    const seen = new Set<string>();
    for (let seed = 1; seed < 40; seed++) seen.add(drawExam(pool, seed)[0]!.id);
    expect(seen).toEqual(new Set(['T1A01', 'T1A02'])); // both questions of the group come up
    const rng = seeded(7);
    const x = rng();
    expect(x).toBeGreaterThanOrEqual(0);
    expect(x).toBeLessThan(1);
    expect(seeded(7)()).toBe(x);
  });

  it('tallies progress and validates stored shapes', () => {
    const pool = parsePool(SAMPLE_POOL);
    const progress = { answers: { T1A01: true, T1B01: false, bogus: true }, updated: '2026-10-13' };
    expect(isProgress(progress)).toBe(true);
    expect(isProgress({ answers: { T1A01: 'yes' }, updated: '' })).toBe(false);
    expect(isProgress(null)).toBe(false);
    expect(tally(progress, pool.questions)).toEqual({ seen: 2, right: 1 });
    expect(tally(progress, pool.subelements[1]!.groups[0]!.questions)).toEqual({
      seen: 0,
      right: 0,
    });
  });
});
