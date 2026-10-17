import { describe, expect, it } from 'vitest';
import { resolveHref, sanitizeDocumentHtml } from '../src/core/links.ts';
import { linkify, renderPlainText } from '../src/core/text.ts';

const ctx = {
  sourceId: 'wikipedia-en-zim',
  nativeId: 'Alan_Turing',
  links: [
    { href: 'https://www.rfc-editor.org/rfc/rfc9110', target: 'wsp:rfc/9110' },
    { href: './Bletchley_Park', target: null },
  ],
};

describe('resolveHref', () => {
  it('uses the API resolution table first', () => {
    expect(resolveHref('https://www.rfc-editor.org/rfc/rfc9110', ctx)).toEqual({
      kind: 'local',
      address: 'wsp://rfc/9110',
    });
    expect(resolveHref('./Bletchley_Park', ctx).kind).toBe('missing');
  });

  it('resolves wsp:, relative and anchor links', () => {
    expect(resolveHref('wsp:wikidata/Q7251', ctx)).toEqual({
      kind: 'local',
      address: 'wsp://wikidata/Q7251',
    });
    expect(resolveHref('./Turing_machine', ctx)).toEqual({
      kind: 'local',
      address: 'wsp://wikipedia-en-zim/Turing_machine',
    });
    expect(resolveHref('Enigma_machine#Design', ctx)).toEqual({
      kind: 'local',
      address: 'wsp://wikipedia-en-zim/Enigma_machine#Design',
    });
    expect(resolveHref('#Legacy', ctx)).toEqual({ kind: 'anchor', id: 'Legacy' });
    for (const nowhere of ['#', '', 'javascript:void(0)', 'javascript:void(0);', 'javascript:']) {
      expect(resolveHref(nowhere, ctx)).toEqual({ kind: 'none' });
    }
  });

  it('treats everything remote or scripted as not in library', () => {
    for (const href of [
      'https://example.org/',
      '//cdn.example.org/x.js',
      'javascript:alert(1)',
      'mailto:a@b.c',
      'data:text/html,hi',
    ]) {
      const r = resolveHref(href, ctx);
      expect(r.kind).toBe('missing');
      if (r.kind === 'missing') expect(r.address.startsWith('wsp://offline?url=')).toBe(true);
    }
  });
});

describe('sanitizeDocumentHtml', () => {
  const html = `
    <p style="color:red" onclick="x()" class="c">Hello <a href="./Turing_machine">TM</a>
    <a href="https://evil.example/">out</a> <a href="#Legacy">jump</a></p>
    <script>window.pwned = 1</script><style>p{}</style>
    <iframe src="https://evil.example/"></iframe>
    <font color="red"><b>kept</b></font>
    <h2 id="Legacy">Legacy</h2>
    <img src="https://evil.example/x.png" alt="Portrait" onerror="x()">
    <img src="/v1/assets/x/2026-01/a.png" alt="ok" width="10" height="abc">
    <img src="/v1/search?q=a" alt="busy">
    <table><tr><td colspan="2" style="x">cell</td></tr></table>`;
  const { fragment, stats } = sanitizeDocumentHtml(html, ctx);
  const div = document.createElement('div');
  div.append(fragment);

  it('drops scripts, styles, frames and handlers', () => {
    expect(div.querySelector('script, style, iframe')).toBeNull();
    expect(div.innerHTML).not.toMatch(/onclick|onerror|style=|class="c"/);
    expect((window as unknown as { pwned?: number }).pwned).toBeUndefined();
  });

  it('unwraps unknown tags but keeps content', () => {
    expect(div.querySelector('font')).toBeNull();
    expect(div.querySelector('b')?.textContent).toBe('kept');
  });

  it('rewrites links into the offline web', () => {
    const local = div.querySelector<HTMLAnchorElement>(
      'a[data-wsp="wsp://wikipedia-en-zim/Turing_machine"]',
    );
    expect(local?.getAttribute('href')).toBe('#wsp://wikipedia-en-zim/Turing_machine');
    const broken = div.querySelector<HTMLAnchorElement>('a.broken-link');
    expect(broken?.dataset.missing).toBe('https://evil.example/');
    const anchor = div.querySelector<HTMLAnchorElement>('a[data-anchor]');
    expect(anchor?.dataset.anchor).toBe('doc-Legacy');
    expect(div.querySelector('#doc-Legacy')).not.toBeNull();
    expect(stats.broken).toBe(1);
  });

  it('allows library assets only, after resolving the path', async () => {
    const { allowedImagePath } = await import('../src/core/links.ts');
    expect(allowedImagePath('/v1/assets/s/g/a%20b.png')).toBe('/v1/assets/s/g/a%20b.png');
    for (const bad of [
      '/v1/assets/../search?q=a',
      '/v1/assets/%2e%2e/random',
      '/v1/assets/x/%2E%2E/%2E%2E/history',
      '/v1/files/x/../../history',
      '/v1/files/x/preview',
      '//evil.example/v1/assets/a.png',
      '/v1/assets\\..\\search',
      'v1/assets/a.png',
    ]) {
      expect(allowedImagePath(bad), bad).toBeNull();
    }
  });

  it('replaces remote images and keeps same-origin API images', () => {
    expect(div.querySelector('.broken-image')?.textContent).toBe('[IMAGE: Portrait]');
    const imgs = div.querySelectorAll('img');
    expect(imgs).toHaveLength(1);
    expect(imgs[0]?.getAttribute('src')).toBe('/v1/assets/x/2026-01/a.png');
    // Another API route as an image source would run that route per viewer: replaced.
    expect([...div.querySelectorAll('.broken-image')].map((s) => s.textContent)).toContain(
      '[IMAGE: busy]',
    );
    expect(imgs[0]?.getAttribute('height')).toBeNull();
    expect(div.querySelector('td')?.getAttribute('colspan')).toBe('2');
  });
});

describe('plain text', () => {
  it('autolinks RFC references, wsp ids and marks URLs broken', () => {
    const div = document.createElement('div');
    div.append(linkify('See RFC 9111, wsp:gutenberg/84 and https://x.example/y.'));
    const links = [...div.querySelectorAll('a')];
    expect(links.map((a) => a.dataset.wsp)).toEqual([
      'wsp://rfc/9111',
      'wsp://gutenberg/84',
      'wsp://offline?url=https%3A%2F%2Fx.example%2Fy.',
    ]);
    expect(links[2]?.classList.contains('broken-link')).toBe(true);
  });

  it('reflows prose and keeps preformatted text', () => {
    expect(
      renderPlainText('CHAPTER I.\n\nIt is\na truth.\n\nAnother one.', false).querySelectorAll('p'),
    ).toHaveLength(2);
    expect(renderPlainText('  x\n  y', true).tagName).toBe('PRE');
  });
});

describe('links to mirrored Wikimedia projects', () => {
  const ctx = { sourceId: 'wikipedia-top10', nativeId: 'en/October_12' };
  it('opens the library copy', () => {
    expect(resolveHref('https://en.wiktionary.org/wiki/calli', ctx)).toEqual({
      kind: 'local',
      address: 'wsp://wiktionary-en/calli',
    });
    expect(resolveHref('https://en.m.wikipedia.org/wiki/Alan%20Turing#Early_life', ctx)).toEqual({
      kind: 'local',
      address: 'wsp://wikipedia-en-zim/Alan_Turing#Early_life',
    });
    expect(resolveHref('https://de.wikipedia.org/w/index.php?title=Berlin', ctx)).toEqual({
      kind: 'local',
      address: 'wsp://wikipedia-top10/de/Berlin',
    });
    expect(resolveHref('https://mdwiki.org/wiki/Diabetes', ctx)).toEqual({
      kind: 'local',
      address: 'wsp://mdwiki/Diabetes',
    });
  });
  it('leaves what the library does not hold offline', () => {
    for (const href of [
      'https://ja.wikipedia.org/wiki/東京',
      'https://fr.wiktionary.org/wiki/chat',
      'https://en.wikipedia.org/wiki/Special:Random',
      'https://en.wikipedia.org/w/index.php?search=x',
      'https://www.britannica.com/topic/Columbus-Day',
    ]) {
      expect(resolveHref(href, ctx).kind).toBe('missing');
    }
  });
});

describe('plain-text documents', () => {
  it('link Wikimedia URLs to the library copy and others to the offline page', () => {
    const div = document.createElement('div');
    div.append(linkify('See https://en.wikipedia.org/wiki/Allah and https://x.example/y.'));
    const [wiki, other] = [...div.querySelectorAll('a')];
    expect(wiki?.dataset.wsp).toBe('wsp://wikipedia-en-zim/Allah');
    expect(wiki?.dataset.missing).toBeUndefined();
    expect(other?.dataset.wsp?.startsWith('wsp://offline')).toBe(true);
  });
});

describe('navigation blocks', () => {
  it('folds a sidebar and a navbox into one closed details each, inner ones kept as they are', () => {
    const html = `<table class="sidebar nomobile"><tbody><tr><th class="sidebar-title">Thermodynamics</th></tr>
      <tr><td><table class="sidebar-subgroup"><tbody><tr><td>Branches</td></tr></tbody></table></td></tr></tbody></table>
      <p>Lead.</p><div class="navbox"><div class="navbox"><span class="navbox-title">Voting</span></div></div>`;
    const div = document.createElement('div');
    div.append(sanitizeDocumentHtml(html, ctx).fragment);
    const boxes = div.querySelectorAll('details[data-wsp-role="navigation"]');
    expect(boxes.length).toBe(2);
    expect(boxes[0]?.hasAttribute('open')).toBe(false);
    expect(boxes[0]?.querySelector('summary')?.textContent).toBe('Thermodynamics');
    expect(boxes[0]?.querySelectorAll('table').length).toBe(2);
    expect(boxes[1]?.querySelector('summary')?.textContent).toBe('Voting');
    expect(div.querySelector('p')?.textContent).toBe('Lead.');
  });
});

describe('formulas', () => {
  it("keeps MathML formulas (allowlisted, in the MathML namespace) and drops Wikipedia's hidden copy", () => {
    const html = `<p>Let <math alttext="(X,d)" display="inline" onclick="x()"><mrow><mo stretchy="false">(</mo><mi href="https://evil.example/">X</mi><mo>,</mo><mi>d</mi><mo>)</mo></mrow></math> be complete.</p>
      <p><span class="mwe-math-element"><span class="mwe-math-mathml-inline mwe-math-mathml-a11y"><math><semantics><mi>y</mi><annotation encoding="application/x-tex">y</annotation></semantics></math></span><img src="/v1/assets/w/1/y.svg" alt="y"></span></p>
      <p><math><semantics><mi>z</mi><annotation encoding="application/x-tex">z</annotation></semantics></math></p>`;
    const div = document.createElement('div');
    div.append(sanitizeDocumentHtml(html, ctx).fragment);
    const maths = div.querySelectorAll('math');
    expect(maths.length).toBe(2);
    expect(maths[0]?.namespaceURI).toBe('http://www.w3.org/1998/Math/MathML');
    expect(maths[0]?.textContent).toBe('(X,d)');
    expect(maths[0]?.getAttribute('alttext')).toBe('(X,d)');
    expect(div.innerHTML).not.toMatch(/onclick|evil|annotation/);
    expect(maths[1]?.textContent).toBe('z');
    expect(div.querySelector('img')?.getAttribute('alt')).toBe('y');
  });
});

describe('the Gutenberg cover page (2026-10-16 field exam)', () => {
  const cover = {
    sourceId: 'gutenberg-zim',
    nativeId: "Alice's Adventures in Wonderland_cover.11",
    links: [],
  };
  const render = (html: string) => {
    const div = document.createElement('div');
    div.append(sanitizeDocumentHtml(html, cover).fragment);
    return div;
  };

  it('saves the EPUB instead of opening it as a page, and names icon-only links', () => {
    const div = render(
      `<a title="Alice's Adventures in Wonderland: EPUB" href="/v1/assets/gutenberg-zim/2025-11/Alice%27s%20Adventures%20in%20Wonderland.11.epub"> <i class="fa fa-download fa-6x"></i> </a>` +
        `<a title="Alice's Adventures in Wonderland: HTML" href="wsp://gutenberg-zim/Alice%27s%20Adventures%20in%20Wonderland.11"> <i class="fa fa-html5"></i> </a>`,
    );
    const [epub, html] = [...div.querySelectorAll('a')];
    expect(epub!.getAttribute('href')).toBe(
      '/v1/assets/gutenberg-zim/2025-11/Alice%27s%20Adventures%20in%20Wonderland.11.epub',
    );
    expect(epub!.getAttribute('download')).toBe("Alice's Adventures in Wonderland.11.epub");
    expect(epub!.dataset.wsp).toBeUndefined(); // the browser saves it; the Navigator does not route it
    expect(epub!.textContent).toBe("Alice's Adventures in Wonderland: EPUB");
    expect(html!.textContent).toBe("Alice's Adventures in Wonderland: HTML");
    expect(html!.dataset.wsp).toBe("wsp://gutenberg-zim/Alice's%20Adventures%20in%20Wonderland.11");
  });

  it('does not offer a download outside the library’s assets', () => {
    expect(resolveHref('/v1/assets/../search?q=x', cover).kind).toBe('missing');
    expect(resolveHref('/v1/files/x.epub', cover).kind).toBe('local');
  });

  it('turns the author button into a search for the author’s books', async () => {
    const { authorButtons } = await import('../src/pages/document.ts');
    const body = document.createElement('div');
    body.innerHTML = '<a class="author-btn" data-author-name="Lewis Carroll">Lewis Carroll</a>';
    expect(authorButtons(body, 'gutenberg-zim')).toBe(1);
    const a = render(body.innerHTML).querySelector('a')!;
    expect(a.dataset.wsp).toBe('wsp://search?q=Lewis+Carroll&source=gutenberg-zim');
  });
});
