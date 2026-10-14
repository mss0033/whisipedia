// Open Library cards say where the book is: in the library, elsewhere, or nowhere free.
import { beforeEach, describe, expect, it } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { cardYear, catalogNotice, publicDomainLine } from '../src/pages/document.ts';
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
  const notice = () => main().querySelector<HTMLElement>('.catalog-notice');
  const settled = (pred: () => boolean) =>
    until(() => !root.classList.contains('is-loading') && pred());
  return { app, main, notice, settled };
}

describe('catalog cards', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-theme');
  });

  it('reads the year and the public-domain line', () => {
    expect(cardYear('First published: 1818\nSubjects: x')).toBe(1818);
    expect(cardYear('Published: Small Press, 1998')).toBe(1998);
    expect(cardYear('Published: Small Press')).toBeNull();
    expect(publicDomainLine(new Date('2026-10-14'))).toBe(1930);
  });

  it('goes right to the book the card links to when the library holds it', async () => {
    const { app, notice, settled } = boot();
    app.navigate('wsp://open-library/works/OL450063W');
    await settled(() => !!notice());
    const el = notice()!;
    expect(el.textContent).toContain('The library holds this book');
    expect(el.textContent).toContain('Project Gutenberg #84');
    expect(el.querySelector('a')?.dataset.wsp).toBe('wsp://gutenberg/84');
  });

  it('finds a book of the same title among the library’s books', async () => {
    const { app, notice, settled } = boot();
    app.navigate('wsp://open-library/works/OL66554W');
    await settled(() => !!notice());
    expect(notice()!.textContent).toContain('Published 1813, it is in the public domain.');
    await until(() => notice()?.textContent?.includes('The library holds it') ?? false);
    const link = notice()!.querySelector<HTMLAnchorElement>('.catalog-lookup a')!;
    expect(link.textContent).toBe('Read Pride and Prejudice by Jane Austen');
    expect(link.dataset.wsp).toBe('wsp://gutenberg/1342');
  });

  it('says why a recent book has no text in the library', async () => {
    const { app, notice, settled } = boot();
    app.navigate('wsp://open-library/books/OL22856696M');
    await settled(() => !!notice());
    const text = notice()!.textContent!;
    expect(text).toContain('Published 1998, it is still in copyright');
    expect(text).toContain('All editions of this work');
    expect(notice()!.querySelector('.catalog-lookup')).toBeNull();
  });

  it('names a copy elsewhere that the library has not shelved, and handles authors', () => {
    const el = catalogNotice(
      {
        title: 'Moby Dick',
        source_id: 'open-library',
        text: 'Open Library catalog record: edition.\nFirst published: 1851\nAlso at: Project Gutenberg #2701',
        links: [{ href: 'wsp:gutenberg/2701', target: null }],
      },
      { domain: ['catalog'] },
      { bookSources: new Set(['gutenberg']) },
    )!;
    expect(el.textContent).toContain(
      'Project Gutenberg has this book (#2701); the library has not shelved it yet',
    );
    const author = catalogNotice(
      {
        title: 'Mary Shelley',
        source_id: 'open-library',
        text: 'Open Library catalog record: author.',
      },
      { domain: ['catalog'] },
    )!;
    expect(author.textContent).toContain('describes an author');
    expect(author.querySelector('a')?.dataset.wsp).toBe('wsp://search?q=Mary+Shelley&kind=books');
  });
});
