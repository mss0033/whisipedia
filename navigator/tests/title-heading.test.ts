import { describe, expect, it } from 'vitest';
import { titleHeading } from '../src/pages/document.ts';

describe('the title heading of an archived page (2026-10-19 audit)', () => {
  it('is its first h1 with words in it, not a logo or a script-filled one', () => {
    const page = document.createElement('div');
    page.innerHTML = `<h1> <a href="#home"><img alt=""></a> </h1><h1><span></span></h1>
      <h1 id="yellowbookHeader">Burma (Myanmar)</h1>`;
    expect(titleHeading(page)?.textContent).toBe('Burma (Myanmar)');
    page.innerHTML = '<h1> </h1><p>no title heading</p>';
    expect(titleHeading(page)).toBeNull();
  });
});
