/**
 * Themes and display preferences. Themes are CSS custom-property sets keyed
 * by `data-theme` on <html> (see styles/themes.css); this module only flips
 * attributes and persists the choice.
 */
import type { SafeStorage } from './storage.ts';

export const THEMES = [
  { id: 'navigator96', name: "Navigator '96", hint: 'Win95 grey bevels, blue and purple links' },
  { id: 'mosaic93', name: "Mosaic '93", hint: 'The earliest web: grey page, serif text' },
  { id: 'encarta95', name: "Encarta '95", hint: 'Multimedia encyclopedia' },
  { id: 'phosphor-amber', name: 'Phosphor (Amber)', hint: 'Amber CRT terminal' },
  { id: 'phosphor-green', name: 'Phosphor (Green)', hint: 'Green CRT terminal' },
  { id: 'system7', name: 'System 7', hint: 'Classic Mac, 1-bit chrome' },
] as const;

export type ThemeId = (typeof THEMES)[number]['id'];
export const DEFAULT_THEME: ThemeId = 'navigator96';

export type MotionPref = 'system' | 'reduce' | 'full';

export interface DisplayPrefs {
  theme: ThemeId;
  highContrast: boolean;
  /** Root font scale, 0.75–2.0. */
  fontScale: number;
  scanlines: boolean;
  motion: MotionPref;
  showToolbar: boolean;
  showDirectoryButtons: boolean;
}

export const DEFAULT_PREFS: DisplayPrefs = {
  theme: DEFAULT_THEME,
  highContrast: false,
  fontScale: 1,
  scanlines: true,
  motion: 'system',
  showToolbar: true,
  showDirectoryButtons: true,
};

export const FONT_SCALE_MIN = 0.75;
export const FONT_SCALE_MAX = 2;
export const FONT_SCALE_STEP = 0.125;

export function isThemeId(v: unknown): v is ThemeId {
  return typeof v === 'string' && THEMES.some((t) => t.id === v);
}

export function clampScale(n: number): number {
  if (!Number.isFinite(n)) return 1;
  return Math.min(FONT_SCALE_MAX, Math.max(FONT_SCALE_MIN, Math.round(n * 1000) / 1000));
}

export function sanitizePrefs(v: unknown): DisplayPrefs {
  const o = (typeof v === 'object' && v !== null ? v : {}) as Partial<Record<string, unknown>>;
  const motion = o.motion;
  return {
    theme: isThemeId(o.theme) ? o.theme : DEFAULT_PREFS.theme,
    highContrast: typeof o.highContrast === 'boolean' ? o.highContrast : DEFAULT_PREFS.highContrast,
    fontScale: typeof o.fontScale === 'number' ? clampScale(o.fontScale) : DEFAULT_PREFS.fontScale,
    scanlines: typeof o.scanlines === 'boolean' ? o.scanlines : DEFAULT_PREFS.scanlines,
    motion:
      motion === 'system' || motion === 'reduce' || motion === 'full'
        ? motion
        : DEFAULT_PREFS.motion,
    showToolbar: typeof o.showToolbar === 'boolean' ? o.showToolbar : DEFAULT_PREFS.showToolbar,
    showDirectoryButtons:
      typeof o.showDirectoryButtons === 'boolean'
        ? o.showDirectoryButtons
        : DEFAULT_PREFS.showDirectoryButtons,
  };
}

/** Apply preferences to the root element. CSSOM writes are CSP-safe. */
export function applyPrefs(prefs: DisplayPrefs, root: HTMLElement): void {
  root.dataset.theme = prefs.theme;
  root.dataset.contrast = prefs.highContrast ? 'high' : 'normal';
  root.dataset.scanlines = prefs.scanlines ? 'on' : 'off';
  root.dataset.motion = prefs.motion;
  root.dataset.toolbar = prefs.showToolbar ? 'on' : 'off';
  root.dataset.dirbuttons = prefs.showDirectoryButtons ? 'on' : 'off';
  root.style.setProperty('--font-scale', String(prefs.fontScale));
}

type Listener = (prefs: DisplayPrefs) => void;

export class ThemeManager {
  static readonly KEY = 'prefs';
  private prefs: DisplayPrefs;
  private readonly listeners = new Set<Listener>();
  private readonly storage: SafeStorage;
  private readonly root: HTMLElement;

  constructor(storage: SafeStorage, root: HTMLElement) {
    this.storage = storage;
    this.root = root;
    this.prefs = sanitizePrefs(storage.read<unknown>(ThemeManager.KEY, {}));
    applyPrefs(this.prefs, root);
  }

  get current(): Readonly<DisplayPrefs> {
    return this.prefs;
  }

  update(patch: Partial<DisplayPrefs>): void {
    this.prefs = sanitizePrefs({ ...this.prefs, ...patch });
    applyPrefs(this.prefs, this.root);
    this.storage.write(ThemeManager.KEY, this.prefs);
    for (const l of this.listeners) l(this.prefs);
  }

  setTheme(theme: ThemeId): void {
    this.update({ theme });
  }

  cycleTheme(): ThemeId {
    const i = THEMES.findIndex((t) => t.id === this.prefs.theme);
    const next = THEMES[(i + 1) % THEMES.length]!.id;
    this.setTheme(next);
    return next;
  }

  zoom(direction: 1 | -1 | 0): void {
    const fontScale = direction === 0 ? 1 : this.prefs.fontScale + direction * FONT_SCALE_STEP;
    this.update({ fontScale });
  }

  /** Effective reduced motion: explicit preference, else the OS setting. */
  reducedMotion(): boolean {
    if (this.prefs.motion !== 'system') return this.prefs.motion === 'reduce';
    try {
      return globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    } catch {
      return false;
    }
  }

  onChange(l: Listener): () => void {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }
}
