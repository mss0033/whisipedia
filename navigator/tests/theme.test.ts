import { describe, expect, it } from 'vitest';
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

describe('sanitizePrefs', () => {
  it('rejects garbage from storage', () => {
    expect(sanitizePrefs(null)).toEqual(DEFAULT_PREFS);
    expect(sanitizePrefs({ theme: 'netscape99', fontScale: 'x', motion: 1 })).toEqual(
      DEFAULT_PREFS,
    );
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
