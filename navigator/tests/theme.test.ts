import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import {
  clampScale,
  DEFAULT_PREFS,
  sanitizePrefs,
  THEMES,
  ThemeManager,
} from '../src/core/theme.ts';

function setup(store = new MemoryStore()) {
  const root = document.createElement('html');
  return { root, store, tm: new ThemeManager(new SafeStorage(store), root) };
}

describe('ThemeManager', () => {
  it('applies the default theme', () => {
    const { root } = setup();
    expect(root.dataset.theme).toBe('navigator96');
    expect(root.dataset.contrast).toBe('normal');
    expect(root.style.getPropertyValue('--font-scale')).toBe('1');
  });

  it('switches, persists and restores themes', () => {
    const store = new MemoryStore();
    const a = setup(store);
    a.tm.setTheme('phosphor-green');
    a.tm.update({ highContrast: true, scanlines: false });
    expect(a.root.dataset.theme).toBe('phosphor-green');
    expect(a.root.dataset.contrast).toBe('high');
    expect(a.root.dataset.scanlines).toBe('off');
    const b = setup(store);
    expect(b.root.dataset.theme).toBe('phosphor-green');
    expect(b.tm.current.highContrast).toBe(true);
  });

  it('cycles through every theme', () => {
    const { tm } = setup();
    const seen = new Set<string>([tm.current.theme]);
    for (let i = 0; i < THEMES.length; i += 1) seen.add(tm.cycleTheme());
    expect(seen.size).toBe(THEMES.length);
    expect(tm.current.theme).toBe('navigator96');
  });

  it('zooms within bounds and notifies listeners', () => {
    const { tm, root } = setup();
    const seen: number[] = [];
    tm.onChange((p) => seen.push(p.fontScale));
    tm.zoom(1);
    expect(root.style.getPropertyValue('--font-scale')).toBe('1.125');
    for (let i = 0; i < 20; i += 1) tm.zoom(1);
    expect(tm.current.fontScale).toBe(2);
    tm.zoom(0);
    expect(tm.current.fontScale).toBe(1);
    expect(seen.length).toBe(22);
  });

  it('respects explicit motion preferences', () => {
    const { tm, root } = setup();
    tm.update({ motion: 'reduce' });
    expect(root.dataset.motion).toBe('reduce');
    expect(tm.reducedMotion()).toBe(true);
    tm.update({ motion: 'full' });
    expect(tm.reducedMotion()).toBe(false);
  });
});

/** A stand-in for the OS light/dark setting: jsdom has no matchMedia. */
function fakeSystem(dark: boolean) {
  const listeners = new Set<() => void>();
  const mql = {
    matches: dark,
    media: '(prefers-color-scheme: dark)',
    addEventListener: (_: string, l: () => void) => listeners.add(l),
    removeEventListener: (_: string, l: () => void) => listeners.delete(l),
  };
  vi.stubGlobal('matchMedia', (q: string) => (q === mql.media ? mql : { matches: false }));
  return {
    set(next: boolean) {
      mql.matches = next;
      for (const l of listeners) l();
    },
  };
}

describe('dark mode', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('follows the system setting by default', () => {
    fakeSystem(true);
    const { root, tm } = setup();
    expect(tm.current.scheme).toBe('system');
    expect(root.dataset.dark).toBe('on');
    expect(tm.dark()).toBe(true);
  });

  it('restyles when the system setting changes, unless a choice was made', () => {
    const system = fakeSystem(false);
    const { root, tm } = setup();
    const seen: boolean[] = [];
    tm.onChange(() => seen.push(tm.dark()));
    expect(root.dataset.dark).toBe('off');
    system.set(true);
    expect(root.dataset.dark).toBe('on');
    tm.update({ scheme: 'light' });
    system.set(false);
    system.set(true);
    expect(root.dataset.dark).toBe('off');
    expect(seen).toEqual([true, false]);
  });

  it('keeps an explicit choice over the system setting, and restores it', () => {
    fakeSystem(false);
    const store = new MemoryStore();
    const a = setup(store);
    a.tm.update({ scheme: 'dark' });
    expect(a.root.dataset.dark).toBe('on');
    const b = setup(store);
    expect(b.tm.current.scheme).toBe('dark');
    expect(b.root.dataset.dark).toBe('on');
  });

  it('is off without matchMedia, unless chosen', () => {
    const { root, tm } = setup();
    expect(root.dataset.dark).toBe('off');
    tm.update({ scheme: 'dark' });
    expect(root.dataset.dark).toBe('on');
  });

  it('marks which themes are dark already', () => {
    expect(THEMES.filter((t) => !t.dark).map((t) => t.id)).toEqual([
      'navigator96',
      'mosaic93',
      'system7',
    ]);
  });
});

describe('sanitizePrefs', () => {
  it('rejects garbage from storage', () => {
    expect(sanitizePrefs(null)).toEqual(DEFAULT_PREFS);
    expect(
      sanitizePrefs({ theme: 'netscape99', fontScale: 'x', motion: 1, scheme: 'dusk' }),
    ).toEqual(DEFAULT_PREFS);
    expect(clampScale(9)).toBe(2);
    expect(clampScale(Number.NaN)).toBe(1);
  });

  it('survives corrupted JSON in storage', () => {
    const store = new MemoryStore();
    store.setItem('wsp.navigator.prefs', '{not json');
    const { root } = setup(store);
    expect(root.dataset.theme).toBe('navigator96');
  });
});
