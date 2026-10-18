// End-to-end-ish: the whole Navigator in jsdom against the mock API.
import { beforeEach, describe, expect, it } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { mockFetch, until } from './helpers.ts';

function boot() {
  document.body.innerHTML = '<div id="app"></div>';
  history.replaceState(null, '', '/');
  const root = document.getElementById('app')!;
  const app = new App(root, {
    api: new WspClient({ fetch: mockFetch() }),
    storage: new SafeStorage(new MemoryStore()),
  });
  app.start();
  const main = () => document.querySelector('main')!;
  const loc = () => document.querySelector<HTMLInputElement>('#location')!;
  const settled = (pred: () => boolean) =>
    until(() => !root.classList.contains('is-loading') && pred());
  return { app, root, main, loc, settled };
}

describe('Navigator app', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-theme');
  });

  it('has landmarks and chrome', async () => {
    const { settled, main } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    expect(
      document.querySelector('[role="menubar"]')?.querySelectorAll('[role="menuitem"]'),
    ).toHaveLength(7);
    expect(document.querySelector('[role="toolbar"]')).not.toBeNull();
    expect(document.querySelector('main#content')).not.toBeNull();
    expect(document.querySelector('[role="search"] #location')).not.toBeNull();
    expect(document.querySelector('.statusbar')?.textContent).toContain('Done');
    expect(document.title).toContain('Whisipedia Start Page');
    expect(document.querySelector('.loc-label')?.textContent).toBe('Netsite:');
  });

  it('turns location-bar text into a search and opens documents', async () => {
    const { app, settled, main, loc } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    app.submitLocation('http caching');
    await settled(() => main().querySelectorAll('li.result').length === 10);
    expect(loc().value).toBe('wsp://search?q=http+caching');
    expect(location.hash).toBe('#wsp://search?q=http+caching');

    main().querySelector<HTMLAnchorElement>('li.result a[data-wsp]')!.click();
    await settled(() => !!main().querySelector('.page-document'));
    expect(main().querySelector('h1')?.textContent).toBeTruthy();
    expect(app.stack.canBack).toBe(true);
  });

  it('jumps straight to pages for navigational shortcuts', async () => {
    const { app, settled, main, loc } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    app.submitLocation('rfc 9110');
    await settled(() => loc().value === 'wsp://rfc/9110');
    expect(main().querySelector('pre.doc-text a[data-wsp="wsp://rfc/9111"]')).not.toBeNull();
    expect(document.querySelector('.loc-label')?.textContent).toBe('Location:');
  });

  it('renders sanitized documents with broken links and the time machine', async () => {
    const { app, settled, main } = boot();
    app.navigate('wsp://wikipedia-en-zim/Alan_Turing');
    await settled(() => !!main().querySelector('.page-document'));
    expect(main().querySelector('script')).toBeNull();
    expect(main().querySelectorAll('a.broken-link').length).toBeGreaterThanOrEqual(2);
    expect(main().querySelector('.webring')).not.toBeNull();
    app.navigate('wsp://wikipedia-en-zim/Alan_Turing?at=2026-01-01&diff=1');
    await settled(() => !!main().querySelector('.diff'));
    expect(main().querySelectorAll('ins.diff-add').length).toBeGreaterThan(0);
  });

  it('bookmarks with Ctrl+D and records history', async () => {
    const { app, settled, main } = boot();
    app.navigate('wsp://gutenberg/84');
    await settled(() => !!main().querySelector('.page-document'));
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'd', ctrlKey: true, bubbles: true }),
    );
    expect(app.hotlist.has('wsp://gutenberg/84')).toBe(true);
    expect(app.history.has('wsp://gutenberg/84')).toBe(true);
  });

  it('switches themes from the View menu', async () => {
    const { settled, main } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    const view = [...document.querySelectorAll<HTMLButtonElement>('.menubar-item')].find((b) =>
      b.textContent?.startsWith('View'),
    )!;
    view.click();
    const item = [...document.querySelectorAll<HTMLElement>('[role="menuitemradio"]')].find((li) =>
      li.textContent?.includes('System 7'),
    )!;
    item.click();
    expect(document.documentElement.dataset.theme).toBe('system7');
    expect(view.getAttribute('aria-expanded')).toBe('false');
  });

  it('shows period error pages for unknown documents and sites', async () => {
    const { app, settled, main } = boot();
    app.navigate('wsp://rfc/99999');
    await settled(() => !!main().querySelector('.page-error'));
    expect(main().textContent).toContain('Not Found');
    expect(app.history.has('wsp://rfc/99999')).toBe(false); // error pages are not places visited
    app.navigate('wsp://nosuchsite');
    await settled(() => main().textContent?.includes('Unable to locate the server') ?? false);
  });

  it('renders every built-in site', async () => {
    const { app, settled, main } = boot();
    for (const [addr, sel] of [
      ['wsp://directory', '.page-directory'],
      ['wsp://directory/Science/Biology/Immunology', '.dir-entries'],
      ['wsp://whatsnew', '.page-whatsnew'],
      ['wsp://system', '.sys-grid'],
      ['wsp://search', '.page-search-home'],
      ['wsp://history', '.page-history'],
      ['wsp://hotlist', '.page-hotlist'],
      ['wsp://about', '.page-about'],
      ['wsp://help', '.page-help'],
      ['wsp://rfc', '.page-site'],
      ['wsp://offline?url=https%3A%2F%2Fexample.org%2Ffoo', '.page-error'],
    ] as const) {
      app.navigate(addr);
      await settled(() => !!main().querySelector(sel));
    }
    // A category that lists only some of its pages says so (audit 2026-10-12 F8).
    app.navigate('wsp://directory/Arts/Literature/Novels');
    await settled(() => !!main().querySelector('.dir-truncated'));
    expect(main().querySelector('.dir-truncated')?.textContent).toContain(
      'Showing 2 of the 7,121 pages',
    );
    // The rest is one search away, scoped to the site (2026-10-16 field exam).
    const box = main().querySelector<HTMLInputElement>('.dir-truncated input[type="search"]')!;
    box.value = 'Emma';
    box.form!.requestSubmit();
    await settled(() =>
      document.querySelector<HTMLInputElement>('#location')!.value.startsWith('wsp://search'),
    );
    expect(document.querySelector<HTMLInputElement>('#location')!.value).toContain('source=');
    app.navigate('wsp://search?q=define+sesquipedalian');
    await settled(() => !!main().querySelector('.instant-definition'));
    app.navigate('wsp://random?source=rfc');
    await settled(() =>
      document.querySelector<HTMLInputElement>('#location')!.value.startsWith('wsp://rfc/'),
    );
    // A link to a page the library holds opens it rather than "Not in library" (2026-10-17).
    app.navigate('wsp://offline?url=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FTuring_machine');
    await settled(
      () =>
        document.querySelector<HTMLInputElement>('#location')!.value ===
        'wsp://wikipedia-en-zim/Turing_machine',
    );
  });
});
