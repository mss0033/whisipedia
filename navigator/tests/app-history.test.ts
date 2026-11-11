// Browser history entries match the page stack: the same address again adds none.
import { afterEach, describe, expect, it, vi } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { mockFetch, until } from './helpers.ts';

const apps: App[] = [];
afterEach(() => {
  for (const a of apps.splice(0)) a.dispose();
});

describe('browser history', () => {
  it('does not push an entry when the same address is visited again', async () => {
    document.body.innerHTML = '<div id="app"></div>';
    history.replaceState(null, '', '/');
    const root = document.getElementById('app')!;
    const app = new App(root, {
      api: new WspClient({ fetch: mockFetch() }),
      storage: new SafeStorage(new MemoryStore()),
    });
    apps.push(app);
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

  const FRANK = 'wsp://gutenberg-zim/Frankenstein%3B%20Or%2C%20The%20Modern%20Prometheus.84';

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
    const settled = (pred: () => boolean = () => true) =>
      until(() => !root.classList.contains('is-loading') && pred());
    const at = (address: string) => app.stack.list().find((e) => e.address === address)!;
    return { app, root, main, settled, at };
  }

  it('keeps the place of the page being left in its own entry (navigator-code-04)', async () => {
    const { app, main, settled, at } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    app.navigate('wsp://directory');
    await settled(() => app.stack.current?.address === 'wsp://directory');
    main().scrollTop = 300;
    app.navigate('wsp://about');
    await settled(() => app.stack.current?.address === 'wsp://about');
    main().scrollTop = 50;

    history.back();
    await until(() => app.stack.current?.address === 'wsp://directory');
    await settled(() => main().scrollTop === 300);
    // Before: the 50 of the page being left was written into the entry being opened.
    expect(at('wsp://directory').scrollY).toBe(300);
    expect(at('wsp://about').scrollY).toBe(50);

    history.forward();
    await until(() => app.stack.current?.address === 'wsp://about');
    await settled(() => main().scrollTop === 50);
  });

  it('Back pressed twice saves the page on screen once, into its own entry', async () => {
    // The first Back's page is slow: the second Back comes while "about" is still on screen.
    const inner = mockFetch();
    let gate: Promise<void> | null = null;
    const { app, main, settled, at } = boot(async (input, init) => {
      if (gate && input.includes('/v1/directory')) await gate;
      return inner(input, init);
    });
    await settled(() => !!main().querySelector('.page-home'));
    main().scrollTop = 10;
    app.navigate('wsp://directory');
    await settled(() => app.stack.current?.address === 'wsp://directory');
    main().scrollTop = 20;
    app.navigate('wsp://about');
    await settled(() => app.stack.current?.address === 'wsp://about');
    main().scrollTop = 30;

    let open: () => void = () => undefined;
    gate = new Promise<void>((r) => {
      open = r;
    });
    history.back();
    await until(() => app.stack.position === 1);
    expect(main().querySelector('h1')?.textContent).toMatch(/About/); // still the page on screen
    main().scrollTop = 35; // it is still being read
    history.back();
    await until(() => app.stack.position === 0);
    open();
    await settled(() => !!main().querySelector('.page-home'));
    expect(at('wsp://about').scrollY).toBe(35);
    expect(at('wsp://directory').scrollY).toBe(20); // never shown in between: not overwritten
    expect(main().scrollTop).toBe(10);
  });

  it('opens an address with a #section at the section, and Back returns to where the reader was', async () => {
    const into = vi.fn();
    (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView = into;
    try {
      const { app, main, settled } = boot();
      await settled(() => !!main().querySelector('.page-home'));
      app.navigate(`${FRANK}#chap01`);
      await settled(() => !!main().querySelector('.page-document'));
      // The page's own id, under the sanitiser's prefix: "doc-chap01" never existed.
      const target = main().querySelector('#pg-chap01')!;
      expect(target.textContent).toBe('Chapter 1');
      expect(into).toHaveBeenCalledTimes(1);
      expect(into.mock.instances[0]).toBe(target);
      expect(document.activeElement).toBe(target); // the next Tab starts from the section

      main().scrollTop = 120; // the reader reads on
      app.navigate('wsp://about');
      await settled(() => app.stack.current?.address === 'wsp://about');
      history.back();
      await until(() => app.stack.current?.address === `${FRANK}#chap01`);
      await settled(() => !!main().querySelector('.page-document'));
      // The saved place wins over the section on a history traversal.
      expect(main().scrollTop).toBe(120);
      expect(into).toHaveBeenCalledTimes(1);
    } finally {
      delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    }
  });

  it('opens a section redirect at its section, also when the id is percent-encoded in the page', async () => {
    const into = vi.fn();
    (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView = into;
    const inner = mockFetch();
    try {
      // The library answers a redirect-to-section with the page and the section's name.
      const { app, main, settled } = boot(async (input, init) => {
        const res = await inner(input, init);
        if (!input.includes('/v1/documents/')) return res;
        const doc = (await res.json()) as { section: string | null; html: string | null };
        doc.section = 'Early life';
        doc.html = `<p>Lead.</p><h2 id="Early_life">Early life</h2><p>Born.</p><h2 id="Caf%C3%A9">Café</h2>`;
        return new Response(JSON.stringify(doc), {
          status: res.status,
          headers: { 'Content-Type': 'application/json' },
        });
      });
      await settled(() => !!main().querySelector('.page-home'));
      app.navigate('wsp://wikipedia-en-zim/Alan_Turing');
      await settled(() => !!main().querySelector('.page-document'));
      expect(into.mock.instances[0]).toBe(main().querySelector('#pg-Early_life'));

      app.navigate('wsp://wikipedia-en-zim/Alan_Turing#Caf%C3%A9'); // the address decodes it: "Café"
      await settled(() => app.stack.current?.address.endsWith('#Caf%C3%A9') === true);
      expect(into.mock.instances[1]).toBe(main().querySelector('[id="pg-Caf%C3%A9"]'));
    } finally {
      delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    }
  });

  it('after a reload, an old entry’s position is trusted only when it names the address shown', async () => {
    const { app, main, settled } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    // The browser's entry says "position 0", from the stack of before the reload; position 0 of
    // the new stack is the Start Page.
    history.pushState({ pos: 0 }, '', '#wsp://directory');
    window.dispatchEvent(new PopStateEvent('popstate', { state: { pos: 0 } }));
    await settled(() => app.stack.current?.address === 'wsp://directory');
    expect(main().querySelector('.page-home')).toBeNull();
  });

  it('a non-ASCII address is the same address in the stack and in the hash: Forward is kept', async () => {
    const { app, main, settled } = boot();
    await settled(() => !!main().querySelector('.page-home'));
    const cafe = 'wsp://search?q=café crème'; // stored as given; the browser percent-encodes the hash
    app.navigate(cafe);
    await settled(() => app.stack.current?.address === cafe);
    app.navigate('wsp://about');
    await settled(() => app.stack.current?.address === 'wsp://about');
    history.back();
    await until(() => app.stack.current?.address === cafe);
    await settled();
    expect(app.stack.canForward).toBe(true);
    expect(app.stack.list()).toHaveLength(3);
  });
});
