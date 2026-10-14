// Question pages read as threads: chrome gone, accepted answer first, scores, tags as searches.
import { beforeEach, describe, expect, it } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { isThread, threadView } from '../src/pages/threads.ts';
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
  const settled = (pred: () => boolean) =>
    until(() => !root.classList.contains('is-loading') && pred());
  return { app, main, settled };
}

describe('thread view', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-theme');
  });

  it('knows a question page and rewrites it in place', () => {
    const root = document.createElement('div');
    root.innerHTML = `<header>bar</header><div id="question" data-score="3"><div class="postcell">Q?</div></div>
      <div id="answers"><h2>2 Answers</h2>
      <div class="answer" data-answerid="2" data-score="9"><div class="answercell">later</div></div>
      <div class="answer accepted-answer" data-answerid="1" data-score="4"><div class="answercell">yes</div></div></div>
      <a class="post-tag" href="#x">dns</a><footer>f</footer>`;
    expect(isThread(root)).toBe(true);
    const stats = threadView(root, 'stackexchange');
    expect(stats).toEqual({ answers: 2, accepted: true, tags: 1 });
    expect(root.querySelector('header, footer')).toBeNull();
    const order = [...root.querySelectorAll('.answer')].map((a) => a.getAttribute('data-answerid'));
    expect(order).toEqual(['1', '2']);
    expect(root.querySelector('#question .thread-score')?.textContent).toBe('▲ 3');
    expect(root.querySelector('.accepted-answer .thread-accepted')?.textContent).toBe('✓ accepted');
    expect(root.querySelector('#question small')?.getAttribute('title')).toBe('thread-score');
    const tag = root.querySelector<HTMLAnchorElement>('a.post-tag')!;
    expect(tag.dataset.wsp).toBe('wsp://search?q=dns&source=stackexchange');
    expect(tag.getAttribute('href')).toBe('wsp://search?q=dns&source=stackexchange');
    const plain = document.createElement('div');
    plain.innerHTML = '<p>Just a page.</p>';
    expect(isThread(plain)).toBe(false);
  });

  it('shows a Stack Exchange question as a thread', async () => {
    const { app, main, settled } = boot();
    app.navigate('wsp://stackexchange/webmasters/1007');
    await settled(() => !!main().querySelector('.page-document'));
    const body = main().querySelector('.doc-body')!;
    const text = body.textContent ?? '';
    expect(text).not.toContain('Site footer');
    expect(text).not.toContain('Questions');
    expect(text).toContain('▲ 37');
    expect(text).toContain('✓ accepted');
    // The accepted answer (212) leads, then 40, then 5.
    expect(text.indexOf('Both are validators')).toBeLessThan(text.indexOf('Prefer the ETag'));
    expect(text.indexOf('Prefer the ETag')).toBeLessThan(text.indexOf('Send both'));
    const badges = [...body.querySelectorAll('small[title="thread-score"]')].map(
      (b) => b.textContent,
    );
    expect(badges).toEqual(['▲ 37', '▲ 212', '▲ 40', '▲ 5']);
    const tags = [...body.querySelectorAll<HTMLAnchorElement>('a[title^="Search "]')].map(
      (a) => a.dataset.wsp,
    );
    expect(tags).toEqual([
      'wsp://search?q=http&source=stackexchange',
      'wsp://search?q=caching&source=stackexchange',
    ]);
  });
});
