// Which way a document runs (views-documents-rtl): right to left only where it is known, never
// "ltr" from a language tag, the page's own `dir` above its tag, and English translations ltr.
import { describe, expect, it } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import type { DocumentResponse } from '../src/api/types.ts';
import { dirOf, isDefaultLanguage, ownDir } from '../src/core/dir.ts';
import { sanitizeDocumentHtml } from '../src/core/links.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { renderPlainText } from '../src/core/text.ts';
import { applyToBlock, blockText } from '../src/core/translate.ts';
import { renderBody, storedDir } from '../src/pages/document.ts';
import { mockFetch } from './helpers.ts';

const HEBREW = 'בראשית ברא אלהים את השמים ואת הארץ. והארץ היתה תהו ובהו וחשך על פני תהום.';

describe('dirOf', () => {
  it('is rtl for languages written right to left, by script', () => {
    for (const tag of ['he', 'ar', 'fa', 'ur', 'yi', 'ps', 'dv', 'syr', 'ckb', 'ug', 'sd', 'ks'])
      expect(dirOf(tag), tag).toBe('rtl');
    for (const tag of ['arz', 'azb', 'mzn', 'pnb', 'glk', 'nqo'])
      expect(dirOf(tag), tag).toBe('rtl');
    // The old codes for Hebrew and Yiddish; a region or an underscore changes nothing.
    for (const tag of ['iw', 'ji', 'ar-EG', 'he_IL', ' fa ', 'AR'])
      expect(dirOf(tag), tag).toBe('rtl');
  });

  it('follows a script subtag both ways', () => {
    expect(dirOf('ku-Arab')).toBe('rtl');
    expect(dirOf('pa-Arab')).toBe('rtl');
    expect(dirOf('ug-Cyrl')).toBeUndefined();
    expect(dirOf('ku-Latn')).toBeUndefined();
    expect(dirOf('ku')).toBeUndefined();
    expect(dirOf('pa')).toBeUndefined();
  });

  it('never says ltr: a language tag is no proof of it', () => {
    for (const tag of ['en', 'fr', 'de', 'zh', 'ja', 'ru', '', null, undefined, 'not a tag!', 'x'])
      expect(dirOf(tag), String(tag)).toBeUndefined();
  });

  it('knows a source default from a language, and a page’s own dir from noise', () => {
    for (const tag of ['', null, undefined, 'en', 'en-US', 'und', 'mul'])
      expect(isDefaultLanguage(tag), String(tag)).toBe(true);
    for (const tag of ['he', 'fr', 'de-CH']) expect(isDefaultLanguage(tag), tag).toBe(false);
    expect(ownDir('RTL')).toBe('rtl');
    expect(ownDir(' ltr ')).toBe('ltr');
    expect(ownDir('auto')).toBe('auto');
    expect(ownDir('sideways')).toBeUndefined();
    expect(ownDir(null)).toBeUndefined();
  });
});

describe('a document body says which way it runs', () => {
  const base = async (): Promise<DocumentResponse> =>
    new WspClient({ fetch: mockFetch() }).document('wsp:wikipedia-en-zim/Alan_Turing');
  const html = (doc: DocumentResponse, over: Partial<DocumentResponse>) =>
    renderBody({ ...doc, format: 'html', text: null, ...over }).node;
  const text = (doc: DocumentResponse, over: Partial<DocumentResponse>) =>
    renderBody({ ...doc, format: 'text', html: null, kind: 'encyclopedia', ...over }).node;

  it('an HTML page: its own dir first, else its language when that is right to left', async () => {
    const doc = await base();
    expect(html(doc, { lang: 'he', html: `<p>${HEBREW}</p>` }).getAttribute('dir')).toBe('rtl');
    // The page says so itself; its tag is the source's default.
    const own = `<html dir="rtl"><body><p>${HEBREW}</p></body></html>`;
    expect(html(doc, { lang: 'en', html: own }).getAttribute('dir')).toBe('rtl');
    expect(sanitizeDocumentHtml(own, { sourceId: 's', nativeId: 'n' }).dir).toBe('rtl');
    // <body dir> is nearer than <html dir>; an English quote page inside a Hebrew site stays ltr.
    const ltr = '<html dir="rtl"><body dir="ltr"><p>English.</p></body></html>';
    expect(html(doc, { lang: 'he', html: ltr }).getAttribute('dir')).toBe('ltr');
    expect(sanitizeDocumentHtml('<p dir="bogus">x</p>', { sourceId: 's', nativeId: 'n' }).dir).toBe(
      undefined,
    );
  });

  it('a page rewritten for its view keeps the direction of the page as stored', async () => {
    const doc = await base();
    expect(storedDir('<html dir="rtl"><body class="x">a</body></html>')).toBe('rtl');
    expect(storedDir("<html dir='rtl'><body dir=ltr>a</body></html>")).toBe('ltr');
    expect(storedDir('<html><body><p dir="rtl">a</p></body></html>')).toBeUndefined();
    // A man page is rewritten to its body's markup, where <html dir> is gone; and the sanitiser's
    // count for the "empty page" notice is not passed on, for the view added words to it.
    const man = `<html dir="rtl"><body><ul class="nav-h2"><li><a href="#NAME">NAME</a></li></ul><section id="NAME"><h2>NAME</h2><p>ls - ${HEBREW}</p></section></body></html>`;
    const page = { ...doc, format: 'html' as const, text: null, lang: 'en' };
    expect(renderBody({ ...page, html: `<p>${HEBREW}</p>` }).seen).toBeDefined();
    const r = renderBody({ ...page, html: man });
    expect(r.node.getAttribute('dir')).toBe('rtl');
    expect(r.seen).toBeUndefined();
  });

  it('never writes dir="ltr" from a language tag', async () => {
    const doc = await base();
    for (const lang of ['en', 'fr', '']) {
      const node = html(doc, { lang, html: `<p>${HEBREW}</p>` });
      expect(node.hasAttribute('dir'), lang).toBe(false);
      expect(node.querySelector('[dir="ltr"]'), lang).toBeNull();
    }
  });

  it('a plain text: rtl as a whole for an rtl language; by paragraph when the tag says nothing', async () => {
    const doc = await base();
    const he = text(doc, { lang: 'he', text: `${HEBREW}\n\n${HEBREW}` });
    expect(he.getAttribute('dir')).toBe('rtl');
    expect(he.querySelector('p')?.hasAttribute('dir')).toBe(false);

    // The case of the finding: a Hebrew text its source tagged "en" (or not at all).
    for (const lang of ['en', '']) {
      const mixed = text(doc, { lang, text: `${HEBREW}\n\nAn English paragraph follows it.` });
      expect(mixed.hasAttribute('dir'), lang).toBe(false);
      const ps = [...mixed.querySelectorAll('p')];
      expect(ps.length).toBe(2);
      for (const p of ps) expect(p.getAttribute('dir'), lang).toBe('auto');
    }
    // A language that is known and runs left to right needs nothing said.
    const fr = text(doc, { lang: 'fr', text: 'Un paragraphe.\n\nUn autre paragraphe.' });
    expect(fr.querySelector('[dir]')).toBeNull();
    expect(fr.hasAttribute('dir')).toBe(false);
  });

  it('renderPlainText marks paragraphs and headings only when asked', () => {
    const plain = renderPlainText(`TITLE\n\n${HEBREW}`, false);
    expect(plain.querySelector('[dir]')).toBeNull();
    const auto = renderPlainText(`TITLE\n\n${HEBREW}`, false, { autoDir: true });
    expect([...auto.children].map((c) => c.getAttribute('dir'))).toEqual(['auto', 'auto']);
  });

  it('a book: its text runs its way, the reader’s own contents and navigation do not', async () => {
    const doc = await new WspClient({ fetch: mockFetch() }).document('wsp:gutenberg/84', {
      text: true,
    });
    const reader = {
      app: { storage: new SafeStorage(new MemoryStore()) },
      chapter: undefined,
      address: 'wsp://gutenberg/84',
    };
    const he = renderBody({ ...doc, lang: 'he' }, reader).node;
    expect(he.classList.contains('reader')).toBe(true);
    expect(he.hasAttribute('dir')).toBe(false); // the wrapper holds the Navigator's own words too
    expect(he.querySelector('.reader-text')?.getAttribute('dir')).toBe('rtl');
    expect(he.querySelector('.reader-toc')?.closest('[dir]')).toBeNull();
    expect(he.querySelector('.reader-nav')?.closest('[dir]')).toBeNull();

    const en = renderBody({ ...doc, lang: 'en' }, reader).node;
    expect(en.querySelector('.reader-text')?.hasAttribute('dir')).toBe(false);
    expect(en.querySelector('.reader-text p')?.getAttribute('dir')).toBe('auto');
    const fr = renderBody({ ...doc, lang: 'fr' }, reader).node;
    expect(fr.querySelector('[dir]')).toBeNull();
  });
});

describe('a translated passage of a right-to-left page', () => {
  it('runs left to right, and gets its own direction back with its original', () => {
    const div = document.createElement('div');
    div.dir = 'rtl';
    div.innerHTML = `<p>${HEBREW}</p><p dir="rtl">${HEBREW}</p>`;
    document.body.append(div);
    const plain = div.querySelectorAll('p')[0]!;
    const own = div.querySelectorAll('p')[1]!;
    const a = applyToBlock(plain, blockText(plain), 'In the beginning.')!;
    const b = applyToBlock(own, blockText(own), 'In the beginning.')!;
    expect(plain.getAttribute('dir')).toBe('ltr');
    expect(own.getAttribute('dir')).toBe('ltr');
    a.toggle.click(); // show the original
    b.toggle.click();
    expect(plain.hasAttribute('dir')).toBe(false); // back to the page's direction
    expect(own.getAttribute('dir')).toBe('rtl');
    a.toggle.click(); // and the translation again
    expect(plain.getAttribute('dir')).toBe('ltr');
    div.remove();
  });
});
