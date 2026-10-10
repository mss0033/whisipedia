import { describe, expect, it } from 'vitest';
import { GlobalHistory, Hotlist, SessionStack } from '../src/core/history.ts';
import { type KeyValueStore, MemoryStore, SafeStorage } from '../src/core/storage.ts';

describe('SessionStack', () => {
  it('goes back and forward and truncates forward history on push', () => {
    const s = new SessionStack();
    s.push('wsp://home');
    s.push('wsp://rfc/9110');
    s.push('wsp://rfc/9111');
    expect(s.back()?.address).toBe('wsp://rfc/9110');
    expect(s.canForward).toBe(true);
    s.push('wsp://gutenberg/84');
    expect(s.canForward).toBe(false);
    expect(s.list().map((e) => e.address)).toEqual([
      'wsp://home',
      'wsp://rfc/9110',
      'wsp://gutenberg/84',
    ]);
    expect(s.back()?.address).toBe('wsp://rfc/9110');
    expect(s.forward()?.address).toBe('wsp://gutenberg/84');
    expect(s.forward()).toBeUndefined();
  });

  it('does not duplicate reloads and remembers scroll', () => {
    const s = new SessionStack();
    s.push('wsp://home');
    s.setScroll(120);
    s.push('wsp://home');
    expect(s.list()).toHaveLength(1);
    expect(s.current?.scrollY).toBe(120);
  });

  it('respects the limit', () => {
    const s = new SessionStack(3);
    for (let i = 0; i < 5; i += 1) s.push(`wsp://rfc/${i}`);
    expect(s.list().map((e) => e.address)).toEqual(['wsp://rfc/2', 'wsp://rfc/3', 'wsp://rfc/4']);
    expect(s.go(0)?.address).toBe('wsp://rfc/2');
    expect(s.go(9)).toBeUndefined();
  });
});

describe('GlobalHistory', () => {
  it('records visits most-recent-first and persists', () => {
    const store = new MemoryStore();
    const h = new GlobalHistory(new SafeStorage(store));
    h.record('wsp://home', 'Home', 1);
    h.record('wsp://rfc/9110', 'RFC 9110', 2);
    h.record('wsp://home', 'Start Page', 3);
    expect(h.list().map((e) => [e.address, e.visits, e.title])).toEqual([
      ['wsp://home', 2, 'Start Page'],
      ['wsp://rfc/9110', 1, 'RFC 9110'],
    ]);
    const again = new GlobalHistory(new SafeStorage(store));
    expect(again.has('wsp://rfc/9110')).toBe(true);
    again.remove('wsp://rfc/9110');
    expect(again.has('wsp://rfc/9110')).toBe(false);
    again.clear();
    expect(again.list()).toHaveLength(0);
  });

  it('caps the number of entries', () => {
    const h = new GlobalHistory(new SafeStorage(new MemoryStore()), 2);
    h.record('a', 'a');
    h.record('b', 'b');
    h.record('c', 'c');
    expect(h.list().map((e) => e.address)).toEqual(['c', 'b']);
    expect(h.has('a')).toBe(false);
  });

  it('works when storage throws (private mode, quota)', () => {
    const hostile: KeyValueStore = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
      removeItem: () => {
        throw new Error('nope');
      },
    };
    const h = new GlobalHistory(new SafeStorage(hostile));
    expect(() => h.record('wsp://home', 'Home')).not.toThrow();
    expect(h.has('wsp://home')).toBe(true);
    expect(new SafeStorage(null).write('x', 1)).toBe(false);
  });

  it('ignores malformed stored data', () => {
    const store = new MemoryStore();
    store.setItem('wsp.navigator.history', JSON.stringify([{ nope: true }]));
    expect(new GlobalHistory(new SafeStorage(store)).list()).toHaveLength(0);
  });
});

describe('Hotlist', () => {
  it('adds, dedupes, reorders and removes bookmarks', () => {
    const store = new MemoryStore();
    const hl = new Hotlist(new SafeStorage(store));
    expect(hl.add('wsp://rfc/9110', 'RFC 9110')).toBe(true);
    expect(hl.add('wsp://gutenberg/84', 'Frankenstein')).toBe(true);
    expect(hl.add('wsp://rfc/9110', 'HTTP Semantics')).toBe(false);
    hl.move('wsp://gutenberg/84', -1);
    const restored = new Hotlist(new SafeStorage(store));
    expect(restored.list().map((b) => b.title)).toEqual(['Frankenstein', 'HTTP Semantics']);
    restored.remove('wsp://gutenberg/84');
    expect(restored.has('wsp://gutenberg/84')).toBe(false);
  });
});
