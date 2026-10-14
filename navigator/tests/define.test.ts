// wsp://define/<word>: straight to the dictionary entry, or a dictionary search when none has it.
import { beforeEach, describe, expect, it } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { interpretInput, setReadableHosts, shortcutFor } from '../src/core/address.ts';
import { resolveRoute } from '../src/core/router.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { spellings } from '../src/pages/define.ts';
import { mockFetch, until } from './helpers.ts';

function boot() {
  document.body.innerHTML = '<div id="app"></div>';
  history.replaceState(null, '', '/');
  const root = document.getElementById('app')!;
  const log: string[] = [];
  const app = new App(root, {
    api: new WspClient({ fetch: mockFetch(log) }),
    storage: new SafeStorage(new MemoryStore()),
  });
  app.start();
  const main = () => document.querySelector('main')!;
  const settled = (pred: () => boolean) =>
    until(() => !root.classList.contains('is-loading') && pred());
  return { app, main, settled, log };
}

describe('define', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-theme');
    setReadableHosts(null);
  });

  it('tries the headword as typed, in lower case and capitalised', () => {
    expect(spellings('Paris')).toEqual(['Paris', 'paris']);
    expect(spellings('lighthouse keeper')).toEqual(['lighthouse_keeper', 'Lighthouse_keeper']);
    expect(spellings('  ')).toEqual([]);
  });

  it('is a built-in site with a location-bar shortcut', () => {
    expect(resolveRoute('wsp://define/serendipity')).toEqual({
      page: 'define',
      word: 'serendipity',
    });
    expect(shortcutFor('define serendipity')).toBe('wsp://define/serendipity');
    expect(shortcutFor('d  lighthouse keeper')).toBe('wsp://define/lighthouse%20keeper');
    expect(shortcutFor('defined terms')).toBeNull();
    setReadableHosts(['wiktionary-en']); // built-in sites stay reachable once the list is known
    expect(interpretInput('def lantern')).toEqual({
      type: 'address',
      address: 'wsp://define/lantern',
    });
  });

  it('goes to the entry the library holds, or to a dictionary search', async () => {
    const { app, main, settled, log } = boot();
    app.navigate('wsp://define/Sesquipedalian');
    await settled(() => !!main().querySelector('.page-document'));
    expect(location.hash).toBe('#wsp://wiktionary-en/sesquipedalian');
    expect(main().querySelector('h1')?.textContent).toBe('sesquipedalian');
    // One miss, then the hit, whose document the entry page reuses instead of fetching again.
    expect(log.filter((l) => l.includes('/v1/documents/'))).toHaveLength(2);
    app.navigate('wsp://define/nosuchword');
    await settled(() => location.hash.startsWith('#wsp://search'));
    expect(location.hash).toBe('#wsp://search?q=nosuchword&kind=dictionary');
  });
});
