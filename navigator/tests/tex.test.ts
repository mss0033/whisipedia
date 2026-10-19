import { describe, expect, it } from 'vitest';
import { hasTex } from '../src/core/tex.ts';
import { texToMathML, typeset } from '../src/core/texrender.ts';

describe('TeX in an archived page is typeset as MathML (2026-10-19 audit: LibreTexts)', () => {
  it('turns inline and display formulas into MathML, LibreTexts macros included', () => {
    const page = document.createElement('div');
    page.innerHTML = `<h5>Exercise \\(\\PageIndex{55}\\)</h5>
      <p>Simplify \\(−4\\cdot 8\\) and \\[\\frac{a}{b}\\] then stop.</p>
      <pre>\\(left as code\\)</pre><p>\\(\\frac{broken\\)</p>`;
    expect(hasTex(page.innerHTML)).toBe(true);
    expect(typeset(page)).toBe(3);
    const maths = page.querySelectorAll('math');
    expect(maths).toHaveLength(3);
    expect(maths[0]!.textContent).toBe('55');
    expect(maths[2]!.getAttribute('display')).toBe('block');
    expect(maths[2]!.querySelector('mfrac')).not.toBeNull();
    expect(page.querySelector('p')!.textContent).toContain('Simplify');
    expect(page.querySelector('pre')!.textContent).toBe('\\(left as code\\)');
    expect(page.textContent).toContain('\\(\\frac{broken\\)'); // not TeX Temml reads: left as written
  });

  it('keeps only allowlisted MathML: no styles, no links', () => {
    const m = texToMathML('\\href{https://example.com}{x} + \\color{red}{y}', false);
    const html = m?.outerHTML ?? '';
    expect(html).not.toMatch(/href|style=|example\.com/);
  });
});
