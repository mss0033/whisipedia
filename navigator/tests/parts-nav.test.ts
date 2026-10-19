import { describe, expect, it } from 'vitest';
import { partsNav } from '../src/pages/document.ts';

const parts = [
  { doc_id: 'wsp:liii/b/p1', address: 'wsp://liii/b/p1', title: 'Front matter', first_page: 1 },
  { doc_id: 'wsp:liii/b/p5', address: 'wsp://liii/b/p5', title: 'Calculus', first_page: 5 },
  { doc_id: 'wsp:liii/b/p90', address: 'wsp://liii/b/p90', title: 'Physics', first_page: 90 },
];
const whole = { doc_id: 'wsp:liii/b', address: 'wsp://liii/b', title: 'Lessons', first_page: 1 };

describe('a document read in parts', () => {
  it('leads a chapter to its whole and its neighbours', () => {
    const el = partsNav({ parts: { whole, parts, index: 1 } })!;
    expect(el.textContent).toContain('Part 2 of 3 of Lessons');
    const hrefs = [...el.querySelectorAll('a')].map((a) => a.getAttribute('data-wsp'));
    expect(hrefs).toContain('wsp://liii/b');
    expect(el.querySelector('.parts-steps')!.textContent).toBe(
      '‹ Previous: Front matter · Next: Physics ›',
    );
    // The contents mark this chapter and link the others.
    expect(el.querySelector('li.is-current')!.textContent).toContain('Calculus');
    expect(el.querySelectorAll('.parts-toc a')).toHaveLength(2);
  });

  it('gives the last chapter only a previous one, and the bottom bar no contents', () => {
    const el = partsNav({ parts: { whole, parts, index: 2 } }, 'bottom')!;
    expect(el.querySelector('.parts-steps')!.textContent).toBe('‹ Previous: Calculus');
    expect(el.querySelector('.parts-toc')).toBeNull();
  });

  it('lists the parts on the whole, open', () => {
    const el = partsNav({ parts: { whole, parts, index: null } })!;
    expect(el.querySelectorAll('.parts-toc a')).toHaveLength(3);
    expect(el.querySelector('details')!.hasAttribute('open')).toBe(true);
    expect(partsNav({ parts: { whole, parts, index: null } }, 'bottom')).toBeNull();
  });

  it('is absent for a document in one piece', () => {
    expect(partsNav({ parts: null })).toBeNull();
  });
});
