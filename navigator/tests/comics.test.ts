// Numbered comics: the rewrite before the sanitiser, the navigation bar, and the page in the app.
import { describe, expect, it, vi } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { comicBar, comicNumber, comicView, isComic, looksLikeComic } from '../src/pages/comics.ts';
import { mockFetch, until } from './helpers.ts';

const PAGE = `<ul class="comicNav"><li><a rel="prev" href="wsp://x/xkcd.com/935">&lt; Prev</a></li>
<li><a href="https://c.xkcd.com/random/comic/">Random</a></li></ul>
<div id="comic"><img src="/v1/assets/x/g/a.png" title="The last joke." alt="A"></div>
<div id="transcript">[[Two people talk.]]</div>`;

describe('comics', () => {
  it('recognises a comic page cheaply and by its parts', () => {
    expect(looksLikeComic(PAGE)).toBe(true);
    expect(looksLikeComic('<p>an article with id="comic" but no sequence</p>')).toBe(false);
    const doc = new DOMParser().parseFromString(PAGE, 'text/html');
    expect(isComic(doc.body)).toBe(true);
  });

  it('prints the hover text, keeps Random in the library and folds the transcript', () => {
    const doc = new DOMParser().parseFromString(PAGE, 'text/html');
    comicView(doc.body, 'x');
    expect(doc.querySelector('#comic + p i')?.textContent).toBe('The last joke.');
    const random = [...doc.querySelectorAll('a')].find((a) => a.textContent === 'Random');
    expect(random?.getAttribute('href')).toBe('wsp://random?source=x');
    expect(doc.querySelector('details summary')?.textContent).toBe('Transcript');
    expect(doc.querySelector('#transcript')).toBeNull();
  });

  it('numbers comics and goes to one by number', () => {
    expect(comicNumber('xkcd.com/936/')?.at(937)).toBe('xkcd.com/937/');
    expect(comicNumber('xkcd.com/about')).toBeNull();
    const go = vi.fn();
    const bar = comicBar('x', 'xkcd.com/2/', go)!;
    const links = [...bar.querySelectorAll('a')].map((a) => a.textContent);
    expect(links).toEqual(['|‹ First', '‹ Previous', 'Random', 'Next ›']);
    const field = bar.querySelector('input')!;
    field.value = '1000';
    bar.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }));
    expect(go).toHaveBeenCalledWith('wsp://x/xkcd.com/1000/');
    expect(comicBar('x', 'xkcd.com/1/', go)!.textContent).not.toContain('Previous');
  });

  it('shows an xkcd page with its bar, hover text and checked image variants', async () => {
    document.body.innerHTML = '<div id="app"></div>';
    history.replaceState(null, '', '/');
    const root = document.getElementById('app')!;
    const app = new App(root, {
      api: new WspClient({ fetch: mockFetch() }),
      storage: new SafeStorage(new MemoryStore()),
    });
    app.start();
    app.navigate('wsp://kiwix-xkcd-com-en-all/xkcd.com/936');
    const main = () => document.querySelector('main')!;
    await until(
      () => !root.classList.contains('is-loading') && !!main().querySelector('.comic-bar'),
    );
    const img = main().querySelector<HTMLImageElement>('.doc-body img')!;
    expect(img.title).toContain('sincerely apologize');
    expect(img.getAttribute('srcset')).toBe(
      '/v1/assets/kiwix-xkcd-com-en-all/2026-08/imgs.xkcd.com/comics/password_strength_2x.png 2x',
    );
    expect(main().querySelector('.doc-body #doc-comic + p')?.textContent).toContain(
      'sincerely apologize',
    );
    expect(main().querySelector('.doc-body details')).not.toBeNull();
  });
});
