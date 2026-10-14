// History and Hotlist keep the keyboard where it was after Remove, Move and Delete.
import { describe, expect, it } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { sanitizeDocumentHtml } from '../src/core/links.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { Lru } from '../src/pages/document.ts';
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
  return { app, root, main };
}

describe('keyboard focus on local pages', () => {
  it('stays on the Hotlist after Move and Delete', async () => {
    const { app, root, main } = boot();
    for (const [a, t] of [
      ['wsp://gutenberg/84', 'Frankenstein'],
      ['wsp://rfc/1149', 'Avian carriers'],
      ['wsp://gutenberg/1342', 'Pride'],
    ])
      app.hotlist.add(a!, t!);
    app.navigate('wsp://hotlist');
    await until(() => !root.classList.contains('is-loading') && !!main().querySelector('.hotlist'));
    const buttons = (action: string) => [
      ...main().querySelectorAll<HTMLButtonElement>(`button[data-action="${action}"]`),
    ];
    buttons('down')[0]!.click(); // the first item moves to second place
    expect(document.activeElement).toBe(buttons('down')[1]);
    buttons('delete')[1]!.click(); // delete it: focus goes to the item now in its place
    expect(document.activeElement).toBe(buttons('delete')[1]);
    buttons('delete')[1]!.click(); // the last one: focus goes to the new last
    expect(document.activeElement).toBe(buttons('delete')[0]);
  });
});

describe('sanitiser styling hook', () => {
  it('keeps a short data-wsp-role and drops every other data attribute', () => {
    const ctx = { sourceId: 's', nativeId: 'n', docId: 'wsp:s/n', links: [] };
    const { fragment } = sanitizeDocumentHtml(
      '<small data-wsp-role="thread-score" data-x="1">3</small><b data-wsp-role="Bad Value!">x</b>',
      ctx,
    );
    const div = document.createElement('div');
    div.append(fragment);
    expect(div.querySelector('small')?.getAttribute('data-wsp-role')).toBe('thread-score');
    expect(div.querySelector('small')?.hasAttribute('data-x')).toBe(false);
    expect(div.querySelector('b')?.hasAttribute('data-wsp-role')).toBe(false);
  });
});

describe('document cache', () => {
  it('evicts by size as well as by count, keeping the newest', () => {
    const lru = new Lru<string>(10, 100, (v) => v.length);
    lru.set('a', 'x'.repeat(60));
    lru.set('b', 'x'.repeat(30));
    expect(lru.size).toBe(2);
    lru.set('c', 'x'.repeat(30)); // 120 > 100: the oldest goes
    expect(lru.get('a')).toBeUndefined();
    expect(lru.size).toBe(2);
    lru.set('d', 'x'.repeat(500)); // larger than the bound alone: kept, everything else goes
    expect(lru.size).toBe(1);
    expect(lru.get('d')).toBeDefined();
  });
});
