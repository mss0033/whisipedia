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
    <img src="/v1/documents/wsp%3Ax/assets/a.png" alt="ok" width="10" height="abc">
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

  it('replaces remote images and keeps same-origin API images', () => {
    expect(div.querySelector('.broken-image')?.textContent).toBe('[IMAGE: Portrait]');
    const imgs = div.querySelectorAll('img');
    expect(imgs).toHaveLength(1);
    expect(imgs[0]?.getAttribute('src')).toBe('/v1/documents/wsp%3Ax/assets/a.png');
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
