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
    expect(withoutTemplateLeftovers('*** (***** | *****) {{PAGENAME}} {{PAGENAME}}')).toBe(
      '*** (***** | *****)  ',
    );
    expect(withoutTemplateLeftovers('text "You&#039;re an Indian?"')).toBe(
      `text "You're an Indian?"`,
    );
    expect(withoutTemplateLeftovers('or &quot;Mountain of Mercy,&quot; lies')).toBe(
      'or "Mountain of Mercy," lies',
    );
    // A wiki link left unlinked is its words; a chemical name, a list comprehension and a template a
    // page writes about stay as written.
    expect(withoutTemplateLeftovers('A151 / [[Colsterworth]] / [[Main Page|home]]')).toBe(
      'A151 / Colsterworth / home',
    );
    for (const kept of [
      '§ 721.11502 Copper, [[2,2′,2″-(nitrilo-.kappa.N)tris[ethanolato]]',
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

  it('turns a frameset page into links to its frames and shows its no-frames text', () => {
    const { fragment } = sanitizeDocumentHtml(
      `<frameset><frame src="/v1/assets/thales/2025-12/docs/javadoc/overview-summary.html" title="Package descriptions">
       </frameset><noframes><noscript><div>JavaScript is disabled.</div></noscript>
       <h2>Frame Alert</h2><p>Link to <a href="overview-summary.html">Non-frame version</a>.</p></noframes>`,
      { sourceId: 'thales', nativeId: 'docs/javadoc/index.html' },
    );
    const div = document.createElement('div');
    div.append(fragment);
    expect(div.textContent).not.toContain('<noscript>');
    expect(div.querySelector('h2')?.textContent).toBe('Frame Alert');
    const frame = [...div.querySelectorAll('a')].find(
      (a) => a.textContent === 'Package descriptions',
    );
    expect(frame?.dataset.wsp).toBe('wsp://thales/docs/javadoc/overview-summary.html');
    expect([...div.querySelectorAll('a')].some((a) => a.textContent === 'Non-frame version')).toBe(
      true,
    );
  });
});

describe('an image the archive answers with no picture (2026-10-19 audit)', () => {
  it('becomes the placeholder of an image the library does not hold, with its caption', async () => {
    const { replaceBrokenImages } = await import('../src/core/links.ts');
    const page = document.createElement('div');
    page.innerHTML = '<p><img src="/v1/assets/s/g/fig.png" alt="Figure 4.18"></p>';
    replaceBrokenImages(page);
    page.querySelector('img')!.dispatchEvent(new Event('error'));
    expect(page.querySelector('img')).toBeNull();
    expect(page.querySelector('.broken-image')?.textContent).toBe('[IMAGE: Figure 4.18]');
  });
});
