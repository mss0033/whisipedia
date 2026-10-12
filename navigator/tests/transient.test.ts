// A page the server cannot serve yet (503 while a large ZIM opens after a restart) retries by itself.
import { describe, expect, it } from 'vitest';
import type { FetchLike } from '../src/api/client.ts';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { mockFetch, until } from './helpers.ts';

describe('transient failures', () => {
  it('shows "still loading" and retries a page the server could not serve yet', async () => {
    document.body.innerHTML = '<div id="app"></div>';
    history.replaceState(null, '', '/');
    const root = document.getElementById('app')!;
    const inner = mockFetch();
    let refused = 0;
    const fetch: FetchLike = async (input, init) => {
      if (input.includes('Alan_Turing') && refused < 2) {
        refused++;
        return new Response(
          JSON.stringify({
            error: { code: 'zim_unavailable', message: 'ZIM read timed out after 15 s' },
          }),
          { status: 503, headers: { 'content-type': 'application/json' } },
        );
      }
      return inner(input, init);
    };
    const app = new App(root, {
      api: new WspClient({ fetch }),
      storage: new SafeStorage(new MemoryStore()),
      transientRetryMs: 300,
    });
    app.start();
    const main = () => document.querySelector('main')!;
    await until(() => !!main().querySelector('.page-home'));
    app.navigate('wsp://wikipedia-en-zim/Alan_Turing');
    await until(() => main().textContent?.includes('This page is still loading') ?? false);
    await until(() => !!main().querySelector('.page-document'));
    expect(refused).toBe(2);
  });
});
