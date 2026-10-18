import { describe, expect, it } from 'vitest';
import { parseCase, renderCase } from '../src/pages/legal.ts';

const ORDER =
  'Zabel v. Tabb\nSupreme Court of the United States. Filed 1971-02-22. Docket No. 955\nC. A. 5th Cir. Certiorari denied.';
const OPINION = [
  'Deal v. State',
  'Court of Appeals of Georgia. Filed 1998-06-10. Docket A98A0333',
  'Judges: Harold R. Banke',
  'Opinion by Harold',
  'Judge Harold R. Banke.',
  'David Eric Deal was convicted of kidnapping. He enumerates 15 errors on appeal.',
  'This case arose during the victim’s early morning jog when Deal',
  '*80',
  'ran up to her. See RFC 9110.',
  '*81Opinion continues here with a page break *82 inside it.',
].join('\n');

describe('court opinions (CourtListener)', () => {
  it('reads the caption the adapter writes', () => {
    const c = parseCase(OPINION, 'Deal v. State')!;
    expect([c.court, c.filed, c.docket, c.judges, c.author]).toEqual([
      'Court of Appeals of Georgia',
      '1998-06-10',
      'A98A0333',
      'Harold R. Banke',
      'Harold',
    ]);
    expect(c.body).toHaveLength(6);
  });

  it('frames a one-line order as the complete order, not a bare header', () => {
    const el = renderCase(parseCase(ORDER, 'Zabel v. Tabb')!);
    expect(el.querySelector('.case-court')?.textContent).toBe('Supreme Court of the United States');
    expect(el.querySelector('.case-order p')?.textContent).toBe(
      'C. A. 5th Cir. Certiorari denied.',
    );
    expect(el.textContent).toContain('Docket');
    expect(el.textContent).toContain('February 22, 1971');
  });

  it('gives a long opinion paragraphs and page markers', () => {
    const long = `${OPINION}\n${'The court considered every enumeration of error in turn. '.repeat(10)}`;
    const el = renderCase(parseCase(long, 'Deal v. State')!);
    expect(el.querySelectorAll('.case-body p').length).toBeGreaterThan(4);
    const marks = [...el.querySelectorAll('.page-mark')].map((m) => m.textContent);
    expect(marks).toEqual(['p. 80', 'p. 81', 'p. 82']);
    expect(el.textContent).toContain('when Deal');
    expect(el.textContent).not.toContain('*8');
  });

  it('leaves other text to the plain renderer', () => {
    expect(parseCase('Title 18\n§ 1. Definitions', 'Title 18')).toBeNull();
    expect(parseCase('Something else\nNo court here', 'Other')).toBeNull();
  });
});

describe('paragraphs across page breaks', () => {
  it('rejoins a sentence split by a page and keeps the marker inline', async () => {
    const { joinPages } = await import('../src/pages/legal.ts');
    expect(
      joinPages(['This case arose when Deal', '*80', 'ran up to her.', 'A new paragraph.']),
    ).toEqual(['This case arose when Deal *80 ran up to her.', 'A new paragraph.']);
  });
});

describe('US Code cross references', () => {
  it('links the sections the library holds, in this title or another', async () => {
    const { linkCodeSections } = await import('../src/pages/legal.ts');
    const p = document.createElement('p');
    p.textContent =
      'under section 509(a)(2) (relating to x), section 1795 of title 12, section 401(a) of the Social Security Act, and section 9999.';
    linkCodeSections(p, 'wsp:us-code/t26/s501', [
      { href: 'wsp:us-code/t26/s509', target: 'wsp:us-code/t26/s509' },
      { href: 'wsp:us-code/t12/s1795', target: 'wsp:us-code/t12/s1795' },
      { href: 'wsp:us-code/t26/s401', target: 'wsp:us-code/t26/s401' },
      { href: 'wsp:us-code/t26/s9999', target: null },
    ]);
    const links = [...p.querySelectorAll('a')].map((a) => [a.textContent, a.dataset.wsp]);
    expect(links).toEqual([
      ['section 509(a)(2)', 'wsp://us-code/t26/s509'],
      ['section 1795 of title 12', 'wsp://us-code/t12/s1795'],
    ]);
    expect(p.textContent).toContain('section 401(a) of the Social Security Act');
  });
});
