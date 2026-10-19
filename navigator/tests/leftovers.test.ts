import { describe, expect, it } from 'vitest';
import { sanitizeDocumentHtml, withoutTemplateLeftovers } from '../src/core/links.ts';

describe('wiki leftovers an archived page shows (2026-10-19 audit)', () => {
  it('drops template parameters never filled in and nameless category or file links', () => {
    expect(withoutTemplateLeftovers('Habit:\t\t[[Category:]]')).toBe('Habit:\t\t');
    expect(withoutTemplateLeftovers('{{{name}}} LATINNAME')).toBe(' LATINNAME');
    expect(withoutTemplateLeftovers('[[{{{domain}}}]] >\t[[{{{superregnum}}}]] > Plantae')).toBe(
      ' >\t > Plantae',
    );
    expect(withoutTemplateLeftovers('RuTAG\n[[File:|thumb|]]\nOrganization')).toBe(
      'RuTAG\n\nOrganization',
    );
    // A page's own words stay: a real link, a list comprehension, a template a page writes about.
    for (const kept of [
      '[[Main Page]]',
      '[[y for x in xs for y in x]]',
      'the {{Citation needed}} tag',
    ])
      expect(withoutTemplateLeftovers(kept)).toBe(kept);
  });

  it('leaves code alone and drops the notes a wiki stylesheet hides', () => {
    const { fragment } = sanitizeDocumentHtml(
      `<p>Habit: [[Category:]]</p><pre>{{{name}}}</pre>
       <p>J. 2013.<span class="cs1-maint citation-comment">{{cite journal}}: CS1 maint: unflagged free DOI</span></p>`,
      { sourceId: 'w', nativeId: 'P' },
    );
    const div = document.createElement('div');
    div.append(fragment);
    expect(div.textContent).toContain('Habit: ');
    expect(div.textContent).not.toContain('[[Category:]]');
    expect(div.querySelector('pre')?.textContent).toBe('{{{name}}}');
    expect(div.textContent).not.toContain('CS1 maint');
  });
});
