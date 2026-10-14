// Browser history entries match the page stack: the same address again adds none.
import { describe, expect, it, vi } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { mockFetch, until } from './helpers.ts';

describe('browser history', () => {
  it('does not push an entry when the same address is visited again', async () => {
    document.body.innerHTML = '<div id="app"></div>';
    history.replaceState(null, '', '/');
    const root = document.getElementById('app')!;
    const app = new App(root, {
      api: new WspClient({ fetch: mockFetch() }),
      storage: new SafeStorage(new MemoryStore()),
    });
    app.start();
    const settled = () => until(() => !root.classList.contains('is-loading'));
    await settled();
    const push = vi.spyOn(history, 'pushState');
    app.navigate('wsp://directory');
    await until(() => location.hash === '#wsp://directory');
    await settled();
    expect(push).toHaveBeenCalledTimes(1);
    app.navigate('wsp://directory');
    await settled();
    await new Promise((r) => setTimeout(r, 50));
    await settled();
    expect(push).toHaveBeenCalledTimes(1);
    push.mockRestore();
  });
});
