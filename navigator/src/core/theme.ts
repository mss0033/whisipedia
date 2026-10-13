/**
 * Themes and display preferences. Themes are CSS custom-property sets keyed
 * by `data-theme` on <html> (see styles/themes.css); this module only flips
 * attributes and persists the choice.
 *
 * Dark mode is a palette per theme, not a theme: `data-dark="on"` gives the
 * light themes their night colours. Themes that are dark already ignore it.
 */
import type { SafeStorage } from './storage.ts';

export const THEMES = [
  {
    id: 'navigator96',
    name: "Navigator '96",
    hint: 'Win95 grey bevels, blue and purple links',
    dark: false,
  },
  {
    id: 'mosaic93',
    name: "Mosaic '93",
    hint: 'The earliest web: grey page, serif text',
    dark: false,
  },
  { id: 'encarta95', name: "Encarta '95", hint: 'Multimedia encyclopedia', dark: true },
  { id: 'phosphor-amber', name: 'Phosphor (Amber)', hint: 'Amber CRT terminal', dark: true },
  { id: 'phosphor-green', name: 'Phosphor (Green)', hint: 'Green CRT terminal', dark: true },
  { id: 'system7', name: 'System 7', hint: 'Classic Mac, 1-bit chrome', dark: false },
] as const;

export type ThemeId = (typeof THEMES)[number]['id'];
export const DEFAULT_THEME: ThemeId = 'navigator96';

export type MotionPref = 'system' | 'reduce' | 'full';

/** Light or dark palette: `system` follows the operating system's setting. */
export type SchemePref = 'system' | 'light' | 'dark';

const DARK_QUERY = '(prefers-color-scheme: dark)';

export interface DisplayPrefs {
  theme: ThemeId;
  scheme: SchemePref;
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
  scheme: 'system',
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
  const scheme = o.scheme;
  return {
    theme: isThemeId(o.theme) ? o.theme : DEFAULT_PREFS.theme,
    scheme:
      scheme === 'system' || scheme === 'light' || scheme === 'dark'
        ? scheme
        : DEFAULT_PREFS.scheme,
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

/** Whether the dark palette applies, given the operating system's setting. */
export function isDark(scheme: SchemePref, systemDark: boolean): boolean {
  return scheme === 'dark' || (scheme === 'system' && systemDark);
}

/** Apply preferences to the root element. CSSOM writes are CSP-safe. */
export function applyPrefs(prefs: DisplayPrefs, root: HTMLElement, systemDark = false): void {
  root.dataset.theme = prefs.theme;
  root.dataset.dark = isDark(prefs.scheme, systemDark) ? 'on' : 'off';
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
  private readonly system: MediaQueryList | null;

  constructor(storage: SafeStorage, root: HTMLElement) {
    this.storage = storage;
    this.root = root;
    this.prefs = sanitizePrefs(storage.read<unknown>(ThemeManager.KEY, {}));
    this.system = darkQuery();
    // The OS switching between light and dark (e.g. at sunset) restyles the page at once.
    this.system?.addEventListener?.('change', () => {
      if (this.prefs.scheme !== 'system') return;
      applyPrefs(this.prefs, this.root, this.systemDark());
      for (const l of this.listeners) l(this.prefs);
    });
    applyPrefs(this.prefs, root, this.systemDark());
  }

  get current(): Readonly<DisplayPrefs> {
    return this.prefs;
  }

  update(patch: Partial<DisplayPrefs>): void {
    this.prefs = sanitizePrefs({ ...this.prefs, ...patch });
    applyPrefs(this.prefs, this.root, this.systemDark());
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

  /** Effective dark palette: explicit preference, else the OS setting. */
  dark(): boolean {
    return isDark(this.prefs.scheme, this.systemDark());
  }

  private systemDark(): boolean {
    return this.system?.matches ?? false;
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

function darkQuery(): MediaQueryList | null {
  try {
    return globalThis.matchMedia?.(DARK_QUERY) ?? null;
  } catch {
    return null;
  }
}
