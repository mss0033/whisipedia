// The Exam Practice Hall in jsdom against the mock API: the hall, a pool's syllabus and card, a mock exam.
import { beforeEach, describe, expect, it } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { mockFetch, until } from './helpers.ts';

const POOL = 'wsp://practice/ncvec:2026-2030-Technician-Pool-and-Syllabus-Public-Release:2026';

function boot() {
  document.body.innerHTML = '<div id="app"></div>';
  history.replaceState(null, '', '/');
  const root = document.getElementById('app')!;
  const store = new MemoryStore();
  const app = new App(root, {
    api: new WspClient({ fetch: mockFetch() }),
    storage: new SafeStorage(store),
  });
  app.start();
  const main = () => document.querySelector('main')!;
  const settled = (pred: () => boolean) =>
    until(() => !root.classList.contains('is-loading') && pred());
  return { app, main, settled, store };
}

describe('Exam Practice Hall', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-theme');
  });

  it('lists the pools by licence class', async () => {
    const { app, main, settled } = boot();
    app.navigate('wsp://practice');
    await settled(() => !!main().querySelector('.pool-list'));
    expect(main().querySelector('h1')?.textContent).toBe('Exam Practice Hall');
    expect(main().querySelector('h2')?.textContent).toBe('Technician class');
    const link = main().querySelector<HTMLAnchorElement>('.pool-list a')!;
    expect(link.textContent).toContain('2026-2030 Technician Pool');
    expect(link.getAttribute('href')).toContain('#wsp://practice/ncvec');
    // The superseded pool is labelled and listed after the current one.
    await settled(() => main().querySelectorAll('.pool-list li').length === 2);
    const items = [...main().querySelectorAll('.pool-list li')];
    expect(items[0]?.textContent).toContain('2026-2030');
    expect(items[0]?.classList.contains('superseded')).toBe(false);
    expect(items[1]?.textContent).toContain('2022-2026');
    expect(items[1]?.textContent).toContain('superseded by a newer pool');
  });

  it('shows the syllabus and drills a question, remembering the answer', async () => {
    const { app, main, settled, store } = boot();
    app.navigate(`${POOL}?group=T1A`);
    await settled(() => !!main().querySelector('.practice-card'));
    expect(main().querySelector('h1')?.textContent).toBe('Technician class question pool');
    const groups = [...main().querySelectorAll('.practice-syllabus tbody a')].map(
      (a) => a.textContent,
    );
    expect(groups).toEqual(['T1A', 'T1B', 'T2A']);
    expect(main().querySelector('.practice-where')?.textContent).toContain('T1A01');
    expect(main().querySelector('.practice-question')?.textContent).toMatch(/Basis and Purpose/);
    const choices = [...main().querySelectorAll<HTMLButtonElement>('.practice-choice')];
    expect(choices).toHaveLength(4);
    choices[0]!.click(); // A: wrong, the answer is C
    expect(main().querySelector('.practice-status')?.textContent).toContain('the answer is C');
    expect(main().querySelector('.practice-status')?.textContent).toContain('97.1');
    expect(choices[2]!.classList.contains('is-right')).toBe(true);
    expect(choices[0]!.classList.contains('is-wrong')).toBe(true);
    expect(choices.every((b) => b.disabled)).toBe(true);
    const saved = JSON.parse(
      store.getItem(
        'wsp.navigator.practice:wsp:ncvec-question-pools/ncvec:2026-2030-Technician-Pool-and-Syllabus-Public-Release:2026',
      )!,
    );
    expect(saved.answers).toEqual({ T1A01: false });
    // The next question of the group, then the tally in the syllabus.
    main().querySelector<HTMLAnchorElement>('.practice-nav a.btn-like, .practice-nav a')!.click();
    await settled(
      () => main().querySelector('.practice-where')?.textContent?.includes('T1A02') ?? false,
    );
    expect(main().querySelector('.practice-meta')?.textContent).toContain('Answered 1, right 0');
  });

  it('draws a mock exam from a seed and grades it', async () => {
    const { app, main, settled } = boot();
    app.navigate(`${POOL}?mode=exam`);
    await settled(() => !!main().querySelector('.practice-card'));
    expect(location.hash).toMatch(/mode=exam&seed=\d+/);
    expect(main().querySelector('.practice-where')?.textContent).toContain('Question 1 of 3');
    for (let i = 0; i < 3; i++) {
      const right = main().querySelector<HTMLButtonElement>('.practice-choice[data-letter="C"]')!;
      const picked =
        i === 2
          ? main().querySelector<HTMLButtonElement>('.practice-choice[data-letter="D"]')!
          : right;
      picked.click();
      await settled(
        () =>
          !main()
            .querySelector(`.practice-where`)
            ?.textContent?.includes(`Question ${i + 1} of`),
      );
    }
    await settled(() => !!main().querySelector('.practice-result'));
    const result = main().querySelector('.practice-result')!.textContent!;
    expect(result).toMatch(/of 3 right/);
    expect(result).toContain('3 of 3, 74%');
    expect(main().querySelectorAll('.grid-table tbody tr')).toHaveLength(3);
  });
});
