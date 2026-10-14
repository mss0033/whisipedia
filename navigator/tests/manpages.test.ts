// Man pages: the site's menu becomes a sections box with links back from every heading.
import { describe, expect, it } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { isManPage, looksLikeManPage, manPageView } from '../src/pages/manpages.ts';
import { mockFetch, until } from './helpers.ts';

const PAGE = `<div class="burger"></div><form id="ta"><input name="q"></form>
<nav><ul class="nav-h2"><li><a href="#Synopsis">Synopsis</a></li><li><a href="#Options">Options</a></li></ul></nav>
<main><section id="Synopsis"><a href="#Synopsis"><h2>Synopsis</h2></a><pre>x</pre></section>
<section id="Options"><h2>Options</h2><p>y</p></section></main><footer>site</footer>`;

describe('man pages', () => {
  it('recognises a ManKier page cheaply and by its parts', () => {
    expect(looksLikeManPage(PAGE)).toBe(true);
    expect(looksLikeManPage('<section id="a"><h2>A</h2></section>')).toBe(false);
    expect(isManPage(new DOMParser().parseFromString(PAGE, 'text/html').body)).toBe(true);
  });

  it('folds the section list into a box and links back from every heading', () => {
    const doc = new DOMParser().parseFromString(PAGE, 'text/html');
    expect(manPageView(doc.body)).toBe(2);
    const box = doc.querySelector('details#man-sections')!;
    expect(box.querySelector('summary')?.textContent).toBe('Sections (2)');
    expect(box.nextElementSibling?.id).toBe('Synopsis');
    const back = [...doc.querySelectorAll('a[href="#man-sections"]')];
    expect(back).toHaveLength(2);
    expect(doc.querySelector('.burger, #ta, footer')).toBeNull();
  });

  it('shows the rsync page with its sections box in the app', async () => {
    document.body.innerHTML = '<div id="app"></div>';
    history.replaceState(null, '', '/');
    const root = document.getElementById('app')!;
    const app = new App(root, {
      api: new WspClient({ fetch: mockFetch() }),
      storage: new SafeStorage(new MemoryStore()),
    });
    app.start();
    app.navigate('wsp://kiwix-www-mankier-com-en-all/www.mankier.com/1/rsync');
    const main = () => document.querySelector('main')!;
    await until(
      () => !root.classList.contains('is-loading') && !!main().querySelector('.doc-body details'),
    );
    const box = main().querySelector<HTMLDetailsElement>('.doc-body details')!;
    expect(box.id).toBe('doc-man-sections');
    expect(box.open).toBe(true);
    const back = main().querySelector<HTMLAnchorElement>('.doc-body p small a')!;
    expect(back.textContent).toBe('↑ Sections');
    expect(back.dataset.anchor).toBe('doc-man-sections');
    expect(main().textContent).not.toContain('Blog');
  });
});
