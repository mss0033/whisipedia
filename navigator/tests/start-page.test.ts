// The start page's daily content and the suggestion lists (2026-11-05 deep dive).
import { describe, expect, it } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import type { SourceInfo, Suggestions } from '../src/api/types.ts';
import { App } from '../src/app.ts';
import { remoteSuggestions } from '../src/chrome/suggest.ts';
import { localIsoDay } from '../src/core/format.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { pickRandomSites, RANDOM_BUTTONS, shortSiteName } from '../src/pages/home.ts';
import { mockFetch, until } from './helpers.ts';

const site = (id: string, kind: string, name = id, doc_count: number | null = 1000): SourceInfo =>
  ({ id, kind, name, doc_count }) as SourceInfo;

describe('start page', () => {
  it("asks for the reader's own day and the Navigator's language", async () => {
    const log: string[] = [];
    document.body.innerHTML = '<div id="app"></div>';
    history.replaceState(null, '', '/');
    const root = document.getElementById('app')!;
    new App(root, {
      api: new WspClient({ fetch: mockFetch(log) }),
      storage: new SafeStorage(new MemoryStore()),
    }).start();
    await until(() => !!root.querySelector('.page-home'));
    const asked = log.find((l) => l.startsWith('GET /v1/portal'));
    expect(asked).toBe(`GET /v1/portal?lang=en&date=${localIsoDay()}`);
  });

  it('writes the local calendar date, not the UTC one', () => {
    // 21:51 on 5 November in the reader's zone, whatever UTC says.
    expect(localIsoDay(new Date(2026, 10, 5, 21, 51))).toBe('2026-11-05');
    expect(localIsoDay(new Date(2026, 0, 1, 0, 0))).toBe('2026-01-01');
  });

  it('draws random buttons from reading sites of different kinds, changing by day', () => {
    // The buttons were always the first four sites by id, a weather model's 15 files among them.
    const sources = [
      site(
        'american-stories',
        'news',
        'American Stories (Chronicling America, article by article)',
      ),
      site('army-cmh-histories', 'books', 'US Army Center of Military History'),
      site('army-pubs-public', 'law'),
      site('aurora-weather-model', 'papers', 'Aurora (Microsoft)', 15),
      site('gutenberg', 'books', 'Project Gutenberg'),
      site('medlineplus', 'medicine', 'MedlinePlus (Kiwix ZIM)'),
      site('osm-tiles', 'maps'),
      site('pubchem', 'data'),
      site('wikipedia', 'encyclopedia', 'Wikipedia (top 10 languages by article count)'),
      site('wiktionary', 'dictionary'),
      site('empty', 'books', 'Empty', null),
    ];
    const days = Array.from({ length: 30 }, (_, i) => `2026-11-${String(i + 1).padStart(2, '0')}`);
    const picks = days.map((d) => pickRandomSites(sources, d));
    for (const p of picks) {
      expect(p).toHaveLength(RANDOM_BUTTONS);
      expect(new Set(p.map((s) => s.kind)).size).toBe(RANDOM_BUTTONS); // one per kind
      for (const s of p)
        expect(['aurora-weather-model', 'osm-tiles', 'pubchem', 'empty']).not.toContain(s.id);
    }
    expect(pickRandomSites(sources, days[0]!)).toEqual(picks[0]); // the same all day
    expect(new Set(picks.map((p) => p.map((s) => s.id).join())).size).toBeGreaterThan(5);
    // A library of data only still gets buttons.
    expect(pickRandomSites([site('a', 'data'), site('b', 'maps')], days[0]!)).toHaveLength(2);
  });

  it('labels a random button with a short name', () => {
    expect(shortSiteName('Project Gutenberg')).toBe('Project Gutenberg');
    expect(shortSiteName('MedlinePlus (Kiwix ZIM)')).toBe('MedlinePlus');
    expect(shortSiteName('Wikipedia (top 10 languages by article count)')).toBe('Wikipedia');
    expect(shortSiteName('American Stories (Chronicling America, article by article)')).toBe(
      'American Stories',
    );
    expect(shortSiteName('US Army Center of Military History')).toBe('US Army Center…');
    expect(shortSiteName('ManKier - Linux man pages (Kiwix ZIM)')).toBe('ManKier');
    expect(shortSiteName('"Practical Meteorology: An Algebra-based Survey"')).toBe(
      'Practical Meteorology',
    );
    expect(shortSiteName('(odd)')).toBe('(odd)');
  });
});

describe('suggestions', () => {
  it('open their document and show the title beside an alias', () => {
    const r: Suggestions = {
      query: 'pho',
      suggestions: ['Phosphorus', 'Photography'],
      items: [
        { text: 'Phosphorus', title: 'Phosphorus', doc_id: 'wsp:w/en/P', address: 'wsp://w/en/P' },
        { text: 'Photography', title: '摄影', doc_id: 'wsp:w/zh/S', address: 'wsp://w/zh/S' },
      ],
    };
    expect(remoteSuggestions(r)).toEqual([
      { text: 'Phosphorus', address: 'wsp://w/en/P', title: undefined },
      { text: 'Photography', address: 'wsp://w/zh/S', title: '摄影' },
    ]);
  });

  it('from an older API are titles to search for', () => {
    const old = { query: 'al', suggestions: ['Alan Turing'] } as Suggestions; // no `items`
    expect(remoteSuggestions(old)).toEqual([{ text: 'Alan Turing' }]);
  });

  it("are asked for in the Navigator's language and picking one opens it", async () => {
    const log: string[] = [];
    document.body.innerHTML = '<div id="app"></div>';
    history.replaceState(null, '', '/');
    const root = document.getElementById('app')!;
    const app = new App(root, {
      api: new WspClient({ fetch: mockFetch(log) }),
      storage: new SafeStorage(new MemoryStore()),
    });
    app.start();
    await until(() => !!root.querySelector('.page-home') && !root.classList.contains('is-loading'));
    const box = root.querySelector<HTMLInputElement>('main [role="combobox"]')!;
    box.focus();
    box.value = 'father of';
    box.dispatchEvent(new Event('input'));
    await until(() => !!root.querySelector('main .suggest-item'));
    expect(log.find((l) => l.startsWith('GET /v1/suggest'))).toContain('lang=en');
    const item = root.querySelector<HTMLElement>('main .suggest-item')!;
    expect(item.querySelector('.suggest-text')?.textContent).toBe('father of computer science');
    expect(item.querySelector('.suggest-kind')?.textContent).toBe('→ Alan Turing');
    item.click();
    await until(() => !!root.querySelector('main .page-document'));
    expect(location.hash).toBe('#wsp://wikipedia-en-zim/Alan_Turing');
  });
});
