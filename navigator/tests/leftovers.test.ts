import { describe, expect, it, vi } from 'vitest';
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
    expect(withoutTemplateLeftovers('[[Image:{{{range_map}}}|{{{range_map_width}}}|]]x')).toBe('x');
    expect(withoutTemplateLeftovers('story)? -- [[User:|]] (talk)')).toBe('story)? --  (talk)');
    expect(withoutTemplateLeftovers('[[User talk:|]]x [[Paris]]')).toBe('x Paris');
    // explain xkcd's unsigned comment, its parameter never filled in (2026-10-22 audit): no link, no
    // empty brackets; brackets the page writes itself stay.
    expect(
      withoutTemplateLeftovers(
        'DC.  [[Special:Contributions/{{{1}}}|{{{1}}}]] ([[User talk:{{{1}}}|talk]])  (please sign)',
      ),
    ).toBe('DC.     (please sign)');
    expect(withoutTemplateLeftovers('call main() then [[User:|]] ok')).toBe('call main() then  ok');
    expect(
      withoutTemplateLeftovers(
        '{{Information |Description =  |Source =  |Date = ~~~~~ <!-- do not change -->  |Permission =  }}  Solar',
      ),
    ).toBe('  Solar');
    expect(withoutTemplateLeftovers('Jordan [[Category:MENA_(Middle_East_and_N...')).toBe(
      'Jordan ',
    );
    expect(withoutTemplateLeftovers('See {{cite}} and [[Category:Solar]] then...')).toBe(
      'See {{cite}} and Category:Solar then...', // a whole link is not a cut one
    );
    expect(
      withoutTemplateLeftovers('Funding\n[[Template:Pub Tag Improved Cookstoves}]]\nmore'),
    ).toBe('Funding\n\nmore');
    expect(withoutTemplateLeftovers('和 {{Package|net-wireless/wpa_supplicant} }。')).toBe(
      '和 net-wireless/wpa_supplicant。',
    );
    expect(withoutTemplateLeftovers('{{Ref HospAuto|')).toBe('');
    expect(
      withoutTemplateLeftovers(
        '{{CodeBox|title=Eksempel på åpen diskusjon|lang=text|1=\n{{Talk|open|date=28 mars, 2017}}\nHei',
      ),
    ).toBe('\n\nHei');
    expect(withoutTemplateLeftovers('the {{Citation needed}} tag')).toBe(
      'the {{Citation needed}} tag',
    );
    expect(withoutTemplateLeftovers('use {{Ref HospAuto|x}} here')).toBe(
      'use {{Ref HospAuto|x}} here',
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

describe('a list item that held only a template leftover (2026-10-21 audit)', () => {
  it('is left out, not shown as an empty bullet', () => {
    const { fragment } = sanitizeDocumentHtml(
      '<ul><li>Financing and Funding</li><li>[[Template:Pub Tag Improved Cookstoves}]]</li><li></li></ul>',
      { sourceId: 'energypedia', nativeId: 'Pub' },
    );
    expect([...fragment.querySelectorAll('li')].map((li) => li.textContent)).toEqual([
      'Financing and Funding',
      '',
    ]);
  });
});

describe("a wiki's blank lines (2026-10-21 audit)", () => {
  it('are one empty paragraph however many there were', () => {
    const { fragment } = sanitizeDocumentHtml(
      '<p>a</p><p><br> </p><p><br></p>\n<p><br></p><p>b</p><p><br></p><p><img src="/v1/assets/s/g/x.png"></p>',
      { sourceId: 'energypedia', nativeId: 'Pub' },
    );
    expect([...fragment.querySelectorAll('p')].map((p) => p.innerHTML.trim())).toEqual([
      'a',
      '<br>',
      'b',
      '<br>',
      expect.stringContaining('<img'),
    ]);
  });

  it('keep the line around words between them', () => {
    const { fragment } = sanitizeDocumentHtml('<div><p><br></p>words<p><br></p></div>', {
      sourceId: 'energypedia',
      nativeId: 'Pub',
    });
    expect(fragment.querySelectorAll('p')).toHaveLength(2);
  });
});

describe('a page that is a form (2026-10-21 audit)', () => {
  it('keeps its heading and labels, not its controls', () => {
    const { fragment } = sanitizeDocumentHtml(
      '<form class="form-signin"><h1>Please sign in</h1><label for="e">Email address</label>' +
        '<input type="email" id="e"><button type="submit">Sign in</button><p>&copy; 2017-2018</p></form>',
      { sourceId: 'bootstrap', nativeId: 'sign-in/' },
    );
    expect(fragment.querySelector('h1')?.textContent).toBe('Please sign in');
    expect(fragment.textContent).toContain('Email address');
    expect(fragment.querySelector('form, input, button')).toBeNull();
  });
});

describe('an image the archive answers with no picture (2026-10-19 audit)', () => {
  it('becomes the placeholder of an image the library does not hold, with its caption', async () => {
    const { replaceBrokenImages } = await import('../src/core/links.ts');
    const page = document.createElement('div');
    page.innerHTML = '<p><img src="/v1/assets/s/g/fig.png" alt="Figure 4.18"></p>';
    document.body.append(page);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('', { status: 404 })),
    );
    try {
      replaceBrokenImages(page);
      page.querySelector('img')!.dispatchEvent(new Event('error'));
      await vi.waitFor(() => expect(page.querySelector('img')).toBeNull());
      expect(page.querySelector('.broken-image')?.textContent).toBe('[IMAGE: Figure 4.18]');
    } finally {
      vi.unstubAllGlobals();
      page.remove();
    }
  });

  it('is asked for again while the library is busy, not called missing (2026-10-21 audit)', async () => {
    const { replaceBrokenImages } = await import('../src/core/links.ts');
    const page = document.createElement('div');
    page.innerHTML = '<p><img src="/v1/assets/s/g/map.png" alt="Map"></p>';
    document.body.append(page);
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('', { status: 503, headers: { 'Retry-After': '2' } })),
    );
    vi.useFakeTimers();
    try {
      replaceBrokenImages(page);
      const img = page.querySelector('img')!;
      const set = vi.spyOn(img, 'setAttribute');
      img.dispatchEvent(new Event('error'));
      await vi.advanceTimersByTimeAsync(2000);
      expect(set).toHaveBeenCalledWith('src', '/v1/assets/s/g/map.png');
      expect(page.querySelector('img')).toBe(img);
      expect(page.querySelector('.broken-image')).toBeNull();
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
      page.remove();
    }
  });
});
