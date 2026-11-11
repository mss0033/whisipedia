// A page's life and a load's signal are two things (navigator-code-03): the page on screen is taken
// apart when the next page is shown, never when a navigation starts, is stopped or is superseded;
// a page that is rendered and never shown is taken apart at once.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import type { PageContext, PageResult } from '../src/pages/types.ts';
import { mockFetch, until } from './helpers.ts';

const seen: PageContext[] = [];
// Every App started here is taken down after its test: a stale one would answer the next one's Back.
const apps: App[] = [];
afterEach(() => {
  for (const a of apps.splice(0)) a.dispose();
});
let next: (ctx: PageContext) => Promise<PageResult> = async () => page('System');

function page(title: string): PageResult {
  const body = document.createElement('div');
  body.innerHTML = `<h1>${title}</h1>`;
  return { title, body };
}

// The System Monitor stands in for "a page": the test decides what its renderer does.
vi.mock('../src/pages/system.ts', () => ({
  render: (ctx: PageContext) => {
    seen.push(ctx);
    return next(ctx);
  },
}));

function boot(fetchImpl = mockFetch()) {
  document.body.innerHTML = '<div id="app"></div>';
  history.replaceState(null, '', '/');
  const root = document.getElementById('app')!;
  const app = new App(root, {
    api: new WspClient({ fetch: fetchImpl }),
    storage: new SafeStorage(new MemoryStore()),
  });
  apps.push(app);
  app.start();
  const main = () => document.querySelector('main')!;
  const idle = () => !root.classList.contains('is-loading');
  const settled = (pred: () => boolean = () => true) => until(() => idle() && pred());
  return { app, root, main, settled, idle };
}

describe('page life', () => {
  beforeEach(() => {
    seen.length = 0;
    next = async () => page('System');
  });
  afterEach(() => vi.restoreAllMocks());

  it('keeps the page on screen alive when the next navigation is stopped', async () => {
    const { app, settled, main, idle } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    app.navigate('wsp://system');
    await settled(() => main().textContent === 'System');
    const shown = seen[0]!;
    expect(shown.life.aborted).toBe(false);

    // A link is followed and never answers; the reader presses Stop.
    next = () => new Promise<PageResult>(() => undefined);
    app.navigate('wsp://system?again=1');
    await until(() => seen.length === 2);
    expect(shown.signal.aborted).toBe(true); // its load is long over
    expect(shown.life.aborted).toBe(false);
    app.stop();
    expect(idle()).toBe(true);
    expect(shown.life.aborted).toBe(false); // still the page on screen
    expect(main().textContent).toBe('System');
  });

  it('ends a page that was rendered and never shown: stopped', async () => {
    const { app, settled, main } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    let finish: (r: PageResult) => void = () => undefined;
    next = () =>
      new Promise<PageResult>((r) => {
        finish = r;
      });
    app.navigate('wsp://system');
    await until(() => seen.length === 1);
    app.stop();
    finish(page('Late'));
    await until(() => seen[0]!.life.aborted);
    expect(main().querySelector('.page-home')).not.toBeNull();
  });

  it('ends a page that was rendered and never shown: superseded, redirected, failed', async () => {
    const { app, settled, main } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    let finish: (r: PageResult) => void = () => undefined;
    next = () =>
      new Promise<PageResult>((r) => {
        finish = r;
      });
    app.navigate('wsp://system');
    await until(() => seen.length === 1);
    app.navigate('wsp://about');
    await settled(() => /About/.test(main().querySelector('h1')?.textContent ?? ''));
    finish(page('Late'));
    await until(() => seen[0]!.life.aborted);

    next = async () => ({ redirect: 'wsp://about' });
    app.navigate('wsp://system?r=1');
    await until(() => seen.length === 2);
    await until(() => seen[1]!.life.aborted);

    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    next = async () => {
      throw new Error('drawing failed');
    };
    app.navigate('wsp://system?r=2');
    await until(() => seen.length === 3);
    await settled(() => /error showing this page/.test(main().textContent ?? ''));
    expect(seen[2]!.life.aborted).toBe(true);
  });

  it('ends the page on screen when the page after it is shown', async () => {
    const { app, settled, main } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    app.navigate('wsp://system');
    await settled(() => main().textContent === 'System');
    expect(seen[0]!.life.aborted).toBe(false);
    app.navigate('wsp://about');
    await settled(() => /About/.test(main().querySelector('h1')?.textContent ?? ''));
    expect(seen[0]!.life.aborted).toBe(true);
  });
});

describe('failed pages say what failed (navigator-code-10)', () => {
  beforeEach(() => {
    seen.length = 0;
    next = async () => page('System');
  });
  afterEach(() => vi.restoreAllMocks());

  it('a rendering bug is the Navigator’s error, not an unreachable server', async () => {
    const { app, settled, main } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    next = async () => {
      // The commonest bug: a TypeError, the class fetch() also throws.
      throw new TypeError("Cannot read properties of undefined (reading 'x')");
    };
    app.navigate('wsp://system');
    await settled(() => /hit an error showing this page/.test(main().textContent ?? ''));
    expect(main().textContent).not.toMatch(/unable to reach the Whisipedia daemon/);
    expect(main().textContent).toContain("reading 'x'");
  });

  it('a request that never got an answer is the unreachable server', async () => {
    const inner = mockFetch();
    const { app, settled, main } = boot((input, init) =>
      input.includes('/v1/documents/')
        ? Promise.reject(new TypeError('Failed to fetch'))
        : inner(input, init),
    );
    await settled(() => !!main().querySelector('.page-home'));
    app.navigate('wsp://rfc/9110');
    await settled(() => /Unable to locate the server/.test(main().textContent ?? ''));
    expect(main().textContent).toMatch(/unable to reach the Whisipedia daemon/);
  });

  it('ends loading when showing the page throws', async () => {
    const { app, settled, main, root } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    const said = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    next = async () => ({
      ...page('Broken'),
      focus: {
        hasAttribute() {
          throw new Error('boom');
        },
      } as unknown as HTMLElement,
    });
    app.navigate('wsp://system');
    await until(() => said.mock.calls.length > 0);
    expect(root.classList.contains('is-loading')).toBe(false);
    expect(root.hasAttribute('aria-busy')).toBe(false);
  });

  it('clears the "waiting" timer of a load that failed before 120 ms', async () => {
    const { app, settled, main } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    next = async () => {
      throw new Error('at once');
    };
    app.navigate('wsp://system');
    await settled(() => /error showing this page/.test(main().textContent ?? ''));
    const status = () => document.querySelector('.statusbar .status-text')?.textContent ?? '';
    await new Promise((r) => setTimeout(r, 200));
    // Before: the timer of the failed load wrote "Waiting for reply" over "Done" 120 ms later.
    expect(status()).not.toMatch(/Waiting for reply/);
    expect(status()).toMatch(/Done/);
  });

  it('a map that moves after Back was pressed and stopped does not write over the entry asked for', async () => {
    const { app, settled, main } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    app.navigate('wsp://system');
    await settled(() => main().textContent === 'System');
    // Back, to a page that never answers; Stop. "System" is still the page on screen.
    const held = vi.spyOn(app.api, 'portal').mockReturnValue(new Promise(() => undefined));
    history.back();
    await until(() => app.stack.position === 0);
    app.stop();
    app.replaceAddress('wsp://system?moved=1'); // what a live map does when it is panned
    expect(app.stack.list()[0]?.address).toBe('wsp://home');
    expect(app.stack.list()[1]?.address).toBe('wsp://system');
    held.mockRestore();
  });

  it('an address read from a cut connection is the unreachable server too', async () => {
    const inner = mockFetch();
    const { app, settled, main } = boot(async (input, init) => {
      const res = await inner(input, init);
      if (!input.includes('/v1/documents/')) return res;
      return {
        ok: true,
        status: 200,
        json: () => Promise.reject(new TypeError('network error')),
      } as unknown as Response;
    });
    await settled(() => !!main().querySelector('.page-home'));
    app.navigate('wsp://rfc/9110');
    await settled(() => /Unable to locate the server/.test(main().textContent ?? ''));
  });
});

describe('keyboard (navigator-code-11)', () => {
  const press = (
    init: KeyboardEventInit & { altGraph?: boolean },
    target: EventTarget = document,
  ) => {
    const ev = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init });
    if (init.altGraph)
      Object.defineProperty(ev, 'getModifierState', { value: (k: string) => k === 'AltGraph' });
    target.dispatchEvent(ev);
    return ev;
  };

  it('AltGr never runs a Ctrl shortcut: Ctrl+Alt+[ types a bracket and does not zoom', async () => {
    const { app, settled, main } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    const zoom = vi.spyOn(app.theme, 'zoom');
    // Windows reports AltGr as Ctrl+Alt; Linux as the AltGraph modifier.
    expect(press({ key: '[', ctrlKey: true, altKey: true }).defaultPrevented).toBe(false);
    expect(press({ key: ']', ctrlKey: true, altKey: true }).defaultPrevented).toBe(false);
    expect(press({ key: '[', ctrlKey: true, altGraph: true }).defaultPrevented).toBe(false);
    expect(zoom).not.toHaveBeenCalled();
    // The shortcut itself still works, outside a text field only.
    expect(press({ key: ']', ctrlKey: true }).defaultPrevented).toBe(true);
    expect(zoom).toHaveBeenCalledTimes(1);
    const loc = document.querySelector<HTMLInputElement>('#location')!;
    expect(press({ key: '[', ctrlKey: true }, loc).defaultPrevented).toBe(false);
    expect(zoom).toHaveBeenCalledTimes(1);
  });

  it('Ctrl+H, Ctrl+B and Ctrl+U are taken only without Shift or Alt', async () => {
    const { settled, main } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    for (const key of ['h', 'b', 'u']) {
      expect(press({ key, ctrlKey: true, shiftKey: true }).defaultPrevented).toBe(false);
      expect(press({ key, ctrlKey: true, altKey: true }).defaultPrevented).toBe(false);
    }
    expect(press({ key: 'u', ctrlKey: true }).defaultPrevented).toBe(true);
  });
});
