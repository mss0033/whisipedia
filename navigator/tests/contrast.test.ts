// Palette contrast, measured from the stylesheets as a reader gets them: every theme, light and
// dark, normal and High Contrast, with the cascade and var() resolved (review of plan B, F3 item 2).
// The pairs are the foregrounds and backgrounds the pages really put together; a new use of a
// status or link colour on another background belongs in PAIRS.
import { describe, expect, it } from 'vitest';
import { keyClass } from '../src/weather/charts.ts';
import {
  APP_SHEETS,
  allPalettes,
  colourDistance,
  colours,
  contrast,
  declared,
  loadRules,
  type Palette,
  paletteName,
  readCss,
  resolveVars,
  stage,
  token,
} from './cascade.ts';

const rules = loadRules(APP_SHEETS);

/** [foreground, background, least ratio, where it is drawn] */
const PAIRS: Array<[string, string, number, string]> = [
  ['--page-fg', '--page-bg', 4.5, 'page text'],
  ['--page-muted', '--page-bg', 4.5, 'fine print'],
  ['--link', '--page-bg', 4.5, 'links'],
  ['--visited', '--page-bg', 4.5, 'visited links'],
  ['--broken', '--page-bg', 4.5, 'links to pages not held'],
  ['--result-url', '--page-bg', 4.5, 'search result address'],
  ['--ok', '--page-bg', 4.5, 'status lamp, .lamp-ok'],
  ['--warn', '--page-bg', 4.5, 'status lamp, .under-construction'],
  ['--bad', '--page-bg', 4.5, 'status lamp, did-you-mean, tombstones'],
  ['--accent', '--page-bg', 3, 'portal title (large text), elevation profile line'],
  ['--accent-fg', '--accent', 4.5, 'pressed Atlas buttons, title icon, "Kept copy" badge'],
  ['--page-fg', '--notice-bg', 4.5, 'notices'],
  ['--link', '--notice-bg', 4.5, 'links in notices'],
  ['--page-muted', '--notice-bg', 4.5, 'fine print in the translation offer'],
  ['--page-fg', '--notice-warn-bg', 4.5, 'warning notices'],
  ['--link', '--notice-warn-bg', 4.5, 'links in warning notices'],
  ['--page-fg', '--notice-mt-bg', 4.5, 'machine translation banner'],
  ['--page-muted', '--notice-mt-bg', 4.5, 'translation progress'],
  ['--chrome-fg', '--chrome-bg', 4.5, 'window chrome'],
  ['--chrome-muted', '--chrome-bg', 4.5, 'status bar'],
  ['--link', '--chrome-bg', 4.5, 'links in the map credit'],
  ['--field-fg', '--field-bg', 4.5, 'fields, lists, map legend, skip link'],
  ['--badge-fg', '--badge-bg', 4.5, 'badges'],
  ['--menu-hi-fg', '--menu-hi-bg', 4.5, 'menu selection'],
  ['--hl-fg', '--hl-bg', 4.5, 'search hits'],
  ['--box-title-fg', '--box-title-bg', 4.5, 'box titles, site banner'],
  ['--warn-box-fg', '--warn-box-bg', 4.5, 'superseded-edition warning'],
  ['--warn-box-fg', '--warn-box-stripe', 4.5, 'superseded-edition warning, stripes'],
  // Availability badges: the page colour written on a status colour.
  ['--page-bg', '--ok', 4.5, '"In the library" badge'],
  ['--page-bg', '--warn', 4.5, '"Needs a connection" badge'],
  ['--page-bg', '--bad', 4.5, '"stale" badge'],
  // Chart keys are words in the series colour, so they need the ratio of text.
  ['--series-0', '--page-bg', 4.5, 'chart series and its key'],
  ['--series-1', '--page-bg', 4.5, 'chart series and its key'],
  ['--series-2', '--page-bg', 4.5, 'chart series and its key'],
  ['--series-3', '--page-bg', 4.5, 'chart series and its key'],
  ['--series-muted', '--page-bg', 4.5, 'chart normals and their key'],
  ['--focus', '--page-bg', 3, 'focus ring on the page'],
  ['--focus', '--chrome-bg', 3, 'focus ring in the chrome'],
  ['--new-fg', '--new-bg', 4.5, 'NEW! badge'],
];

/** Colours of a token; a transparent or inherited one takes `under`. */
function ink(p: Palette, name: string, under: string): ReturnType<typeof colours> {
  const root = stage(p);
  const value = token(rules, p, root, name);
  if (/^(inherit|transparent)$/.test(value)) return colours(token(rules, p, root, under));
  return colours(value);
}

function failures(p: Palette): string[] {
  const out: string[] = [];
  for (const [fg, bg, least, where] of PAIRS) {
    const fgs = ink(p, fg, '--page-fg');
    const bgs = ink(p, bg, '--page-bg');
    if (!fgs.length || !bgs.length) {
      out.push(`${fg} on ${bg}: not a colour (${where})`);
      continue;
    }
    for (const f of fgs)
      for (const b of bgs) {
        const ratio = contrast(f, b);
        if (ratio < least) out.push(`${fg} on ${bg}: ${ratio.toFixed(2)} < ${least} (${where})`);
      }
  }
  return out;
}

describe('palette contrast', () => {
  it('reads every selector of the stylesheets', () => {
    // A selector the test's document cannot parse would drop out of the cascade unseen.
    const el = stage({ theme: 'navigator96', dark: false, high: false }, [{ cls: 'x' }]);
    const bad = rules
      .map((r) => r.selector.replace(/::[\w-]+(\([^)]*\))?$/, '') || '*')
      .filter((sel) => {
        try {
          el.matches(sel);
          return false;
        } catch {
          return true;
        }
      });
    expect(bad).toEqual([]);
  });

  it('resolves the cascade: theme, night block, High Contrast, var()', () => {
    const at = (p: Palette, name: string) => token(rules, p, stage(p), name);
    expect(at({ theme: 'navigator96', dark: false, high: false }, '--page-bg')).toBe('#fff');
    expect(at({ theme: 'mosaic93', dark: false, high: false }, '--page-bg')).toBe('#bfbfbf');
    expect(at({ theme: 'mosaic93', dark: true, high: false }, '--page-bg')).toBe('#2f2f31');
    expect(at({ theme: 'mosaic93', dark: true, high: false }, '--visited')).toBe('#c7a2ff');
    expect(at({ theme: 'encarta95', dark: true, high: false }, '--page-bg')).toBe('#1b1546');
    expect(at({ theme: 'phosphor-green', dark: false, high: false }, '--page-fg')).toBe('#33ff66');
    expect(at({ theme: 'phosphor-green', dark: false, high: true }, '--chrome-fg')).toBe('#fff');
    expect(at({ theme: 'system7', dark: true, high: true }, '--link')).toBe('#ffff00');
  });

  for (const p of allPalettes())
    it(`${paletteName(p)}: text, links, status and series colours can be read`, () => {
      expect(failures(p)).toEqual([]);
    });
});

describe('chart series', () => {
  const SERIES = ['wx-c-0', 'wx-c-1', 'wx-c-2', 'wx-c-3'];

  it('take their colours from the palette, not from the operating system', () => {
    const weather = readCss('weather.css');
    expect(weather).not.toMatch(/prefers-color-scheme/);
    for (const [i, cls] of SERIES.entries()) {
      const p: Palette = { theme: 'navigator96', dark: true, high: false };
      const line = stage(p, [{ tag: 'path', cls: `wx-line ${cls}`, svg: true }]);
      expect(declared(rules, p, line, 'stroke')).toBe(`var(--series-${i})`);
      const light: Palette = { theme: 'navigator96', dark: false, high: false };
      const stroke = (q: Palette) =>
        resolveVars(rules, q, stage(q), declared(rules, q, line, 'stroke') ?? '');
      expect(stroke(p)).not.toBe(stroke(light));
    }
  });

  it('Phosphor and System 7 are palettes of one ink, and get the patterns', () => {
    for (const theme of ['phosphor-amber', 'phosphor-green', 'system7']) {
      const p: Palette = { theme, dark: false, high: false };
      const root = stage(p);
      expect(new Set(SERIES.map((_c, i) => token(rules, p, root, `--series-${i}`))).size).toBe(1);
      const line = stage(p, [{ tag: 'path', cls: 'wx-line wx-c-3', svg: true }]);
      expect(declared(rules, p, line, 'stroke-dasharray')).toBe('9 3 2 3');
    }
    const colour: Palette = { theme: 'navigator96', dark: false, high: false };
    const line = stage(colour, [{ tag: 'path', cls: 'wx-line wx-c-3', svg: true }]);
    expect(declared(rules, colour, line, 'stroke-dasharray')).toBeUndefined();
    // Normals have their own pattern too, and the key of a bar or a band stays a full swatch.
    const mono: Palette = { theme: 'system7', dark: false, high: false };
    const at = (tag: string, cls: string, prop: string, pseudo = '') =>
      declared(rules, mono, stage(mono, [{ tag, cls, svg: tag === 'path' }]), prop, pseudo);
    expect(at('path', 'wx-line wx-normal wx-dashed', 'stroke-dasharray')).toBe('1 4');
    expect(at('span', 'wx-key wx-key-line wx-normal', 'background', '::before')).toMatch(/1px/);
    expect(at('span', 'wx-key wx-c-1', 'background', '::before')).toBe('currentColor');
    expect(keyClass({ cls: 'wx-c-1', kind: 'line' })).toBe('wx-key wx-key-line wx-c-1');
    expect(keyClass({ cls: 'wx-c-1', kind: 'bar' })).toBe('wx-key wx-c-1');
  });

  // A palette of one ink cannot tell four series apart by colour: there each series has its own
  // line pattern, on the line and on its key (review of plan B, F3 item 3).
  for (const p of allPalettes())
    it(`${paletteName(p)}: series differ by colour or by line pattern`, () => {
      const root = stage(p);
      const inks = SERIES.map((_c, i) => colours(token(rules, p, root, `--series-${i}`))[0]);
      for (const c of inks) expect(c).toBeDefined();
      let apart = true;
      for (let a = 0; a < inks.length; a++)
        for (let b = a + 1; b < inks.length; b++)
          if (colourDistance(inks[a]!, inks[b]!) < 25) apart = false;
      if (apart) return;
      const lines = SERIES.map(
        (cls) =>
          declared(
            rules,
            p,
            stage(p, [{ tag: 'path', cls: `wx-line ${cls}`, svg: true }]),
            'stroke-dasharray',
          ) ?? 'none',
      );
      expect(new Set(lines).size, `line patterns ${lines.join(' | ')}`).toBe(SERIES.length);
      const keys = SERIES.map(
        (cls) =>
          declared(
            rules,
            p,
            stage(p, [{ tag: 'span', cls: `wx-key wx-key-line ${cls}` }]),
            'background',
            '::before',
          ) ?? '',
      );
      expect(new Set(keys).size, `key patterns ${keys.join(' | ')}`).toBe(SERIES.length);
    });
});

describe('printing', () => {
  const light: Palette = { theme: 'navigator96', dark: false, high: false };
  // Every colour token a palette block sets; fonts and layout stay the theme's own on paper.
  const tokens = [
    ...new Set(
      rules
        .filter((r) => r.selector.startsWith(':root') && r.media === null)
        .flatMap((r) => r.decls.map(([name]) => name))
        .filter((n) => n.startsWith('--') && !/-font$|^--title-align$|^--window-pad$/.test(n)),
    ),
  ];

  it('finds the colour tokens', () => {
    expect(tokens.length).toBeGreaterThan(60);
    expect(tokens).toContain('--page-bg');
    expect(tokens).toContain('--ink-filter');
  });

  // Phosphor and High Contrast are the palettes a reset that loses the cascade leaves dark.
  for (const p of allPalettes(true))
    it(`${paletteName(p)}: paper takes the light Navigator '96 colours`, () => {
      const root = stage(p);
      const lightRoot = stage(light);
      for (const name of tokens) {
        if (name.startsWith('--phos')) continue; // used only through the tokens reset here
        expect(token(rules, p, root, name), name).toBe(token(rules, light, lightRoot, name));
      }
      expect(failures(p)).toEqual([]);
      const viewport = stage(p, [{ cls: 'window' }, { tag: 'main', cls: 'viewport' }]);
      expect(declared(rules, p, viewport, 'text-shadow') ?? 'none').toBe('none');
      expect(
        resolveVars(rules, p, viewport, declared(rules, p, viewport, 'background') ?? ''),
      ).toBe('#fff');
      const body = viewport.ownerDocument.body;
      expect(
        resolveVars(rules, p, body, declared(rules, p, body, 'background') ?? ''),
        'sheet',
      ).toBe('#fff');
      const win = viewport.parentElement!;
      const overlay = declared(rules, p, win, 'content', '::after');
      if (overlay !== undefined)
        expect(declared(rules, p, win, 'display', '::after'), 'scan lines').toBe('none');
    });

  it('leaves out what only works on a screen', () => {
    const p: Palette = { ...light, print: true };
    for (const cls of [
      'original-frame-slot',
      'player',
      'crystal-controls',
      'mt-offer',
      'mt-controls',
      'mt-toggle',
      'parts-nav',
    ])
      expect(declared(rules, p, stage(p, [{ cls }]), 'display'), cls).toBe('none');
    // The warning that a text is a machine translation is printed with it.
    expect(declared(rules, p, stage(p, [{ cls: 'notice mt-banner' }]), 'display')).toBeUndefined();
  });
});

describe('colours come from the palette', () => {
  it('weather and Atlas styles name no colour of their own', () => {
    for (const file of ['weather.css', 'atlas.css']) {
      const text = readCss(file);
      expect(text.match(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/gi) ?? [], file).toEqual([]);
    }
  });

  it('the superseded-edition warning and the frame of an original file use tokens', () => {
    const p: Palette = { theme: 'navigator96', dark: false, high: false };
    const get = (cls: string, prop: string) => declared(rules, p, stage(p, [{ cls }]), prop) ?? '';
    expect(get('doc-warning', 'background')).toBe('var(--warn-box-bg)');
    expect(get('doc-warning', 'color')).toBe('var(--warn-box-fg)');
    expect(get('doc-warning doc-warning-historical', 'background')).not.toMatch(/#/);
    expect(get('original-frame', 'background')).toBe('var(--frame-bg)');
    // A stored page with no background of its own is dark text: its frame is light in every palette.
    for (const q of allPalettes()) {
      const bg = colours(token(rules, q, stage(q), '--frame-bg'))[0]!;
      expect(contrast(bg, [0, 0, 0]), paletteName(q)).toBeGreaterThan(12);
    }
  });
});

describe('player skins', () => {
  const player = loadRules(['player.css']);
  const skins = ['sk-theater', 'sk-steel', 'sk-desk', 'sk-tube'];

  it('the ring round a focused player is drawn on the page in the page text colour', () => {
    // Not a box-shadow (the skins set their own and replaced it) and not the link colour.
    const ring = player.find((r) => r.selector === '.player:focus-visible');
    const get = (prop: string) => ring?.decls.find(([n]) => n === prop)?.[1];
    expect(get('outline')).toBe('3px solid var(--page-fg)');
    expect(get('outline-offset')).toBe('2px');
    expect(get('box-shadow')).toBeUndefined();
    for (const r of player)
      if (/^\.player\.sk-[\w-]+$/.test(r.selector))
        expect(r.decls.map(([n]) => n)).not.toContain('outline');
  });

  it('in full screen the skin keeps its surface, and the ring is inside it in the skin colour', () => {
    const full = player.find((r) => r.selector === '.player:fullscreen');
    expect(full?.decls.map(([n]) => n)).not.toContain('background');
    const ring = player.find((r) => r.selector === '.player:fullscreen:focus-visible');
    expect(ring?.decls).toEqual([
      ['outline-color', 'var(--focus)', false],
      ['outline-offset', '-3px', false],
    ]);
  });

  for (const skin of skins)
    it(`${skin}: the focus colour stands out from the skin`, () => {
      const own = player.find((r) => r.selector === `.player.${skin}`);
      const focus = colours(own?.decls.find(([n]) => n === '--focus')?.[1] ?? '')[0];
      expect(focus, '--focus is set by the skin').toBeDefined();
      // What a focused control sits on: the skin, its control strip and its buttons.
      const surfaces = player
        .filter((r) =>
          [
            `.player.${skin}`,
            `.player.${skin} .pl-controls`,
            `.player.${skin} .pl-controls button`,
            `.player.${skin} .pl-controls select`,
            `.player.${skin} .pl-play`,
          ].includes(r.selector),
        )
        .flatMap((r) => r.decls.filter(([n]) => n === 'background').flatMap(([, v]) => colours(v)));
      expect(surfaces.length).toBeGreaterThan(0);
      for (const s of surfaces)
        expect(contrast(focus!, s), `on rgb(${s.join(',')})`).toBeGreaterThanOrEqual(3);
    });
});
