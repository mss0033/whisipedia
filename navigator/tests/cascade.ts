/**
 * A small reader of the Navigator's own stylesheets for tests: which declaration wins for an
 * element in a given palette (theme x light/dark x contrast, on screen or on paper), with `var()`
 * followed, and the WCAG contrast of two colours. It resolves the cascade the way a browser does
 * for these files: specificity, then order of import, `!important` first, `@media print` only on
 * paper. Other media rules (widths, forced colours, motion) are left out.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export interface Rule {
  selector: string;
  decls: Array<[prop: string, value: string, important: boolean]>;
  media: string | null;
  order: number;
}

export interface Palette {
  theme: string;
  dark: boolean;
  high: boolean;
  print?: boolean;
}

export const THEME_IDS = [
  'navigator96',
  'mosaic93',
  'encarta95',
  'phosphor-amber',
  'phosphor-green',
  'system7',
] as const;

/** Every palette a reader can be in: 6 themes x light/dark x normal/high contrast. */
export function allPalettes(print = false): Palette[] {
  const out: Palette[] = [];
  for (const theme of THEME_IDS)
    for (const dark of [false, true])
      for (const high of [false, true]) out.push({ theme, dark, high, print });
  return out;
}

export function paletteName(p: Palette): string {
  return `${p.theme}${p.dark ? ' dark' : ''}${p.high ? ' high-contrast' : ''}${p.print ? ' (print)' : ''}`;
}

export function readCss(name: string): string {
  return readFileSync(join(process.cwd(), 'src', 'styles', name), 'utf8').replace(
    /\/\*[\s\S]*?\*\//g,
    '',
  );
}

function splitTop(text: string, sep: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let cur = '';
  for (const ch of text) {
    if (ch === '(' || ch === '[') depth++;
    else if (ch === ')' || ch === ']') depth--;
    if (ch === sep && depth === 0) {
      out.push(cur);
      cur = '';
    } else cur += ch;
  }
  out.push(cur);
  return out.map((s) => s.trim()).filter(Boolean);
}

function parseInto(text: string, media: string | null, rules: Rule[]): void {
  let i = 0;
  while (i < text.length) {
    const open = text.indexOf('{', i);
    if (open < 0) return;
    const prelude = text.slice(i, open).trim().replace(/\s+/g, ' ');
    let depth = 1;
    let j = open + 1;
    while (j < text.length && depth > 0) {
      if (text[j] === '{') depth++;
      else if (text[j] === '}') depth--;
      j++;
    }
    const body = text.slice(open + 1, j - 1);
    i = j;
    if (prelude.startsWith('@media')) {
      const cond = prelude.slice(6).trim();
      parseInto(body, media ? `${media} and ${cond}` : cond, rules);
    } else if (prelude.startsWith('@')) {
      // @keyframes, @font-face, @supports: nothing the palette tests read
    } else {
      const decls: Rule['decls'] = [];
      for (const d of splitTop(body, ';')) {
        const colon = d.indexOf(':');
        if (colon < 0) continue;
        const raw = d
          .slice(colon + 1)
          .trim()
          .replace(/\s+/g, ' ');
        const important = /!important$/.test(raw);
        decls.push([d.slice(0, colon).trim(), raw.replace(/\s*!important$/, ''), important]);
      }
      for (const selector of splitTop(prelude, ','))
        rules.push({ selector, decls, media, order: rules.length });
    }
  }
}

/** The rules of the named stylesheets, in the order given (the order they are imported in). */
export function loadRules(files: string[]): Rule[] {
  const rules: Rule[] = [];
  for (const f of files) parseInto(readCss(f), null, rules);
  return rules;
}

/** The application's sheets in import order (main.ts, then the lazily loaded Atlas and Weather). */
export const APP_SHEETS = [
  'base.css',
  'themes.css',
  'pages.css',
  'player.css',
  'atlas.css',
  'weather.css',
];

export function specificity(selector: string): number {
  let s = selector;
  // :where() counts for nothing
  for (;;) {
    const at = s.indexOf(':where(');
    if (at < 0) break;
    let depth = 0;
    let k = at + 6;
    do {
      if (s[k] === '(') depth++;
      else if (s[k] === ')') depth--;
      k++;
    } while (depth > 0 && k < s.length);
    s = s.slice(0, at) + s.slice(k);
  }
  const pseudoEl = (s.match(/::[\w-]+/g) ?? []).length;
  s = s.replace(/::[\w-]+/g, '');
  const ids = (s.match(/#[\w-]+/g) ?? []).length;
  const attrs = (s.match(/\[[^\]]*\]/g) ?? []).length;
  s = s.replace(/\[[^\]]*\]/g, '');
  const classes = (s.match(/\.[\w-]+/g) ?? []).length;
  const pseudo = (s.match(/:(?!not\(|is\()[\w-]+/g) ?? []).length;
  s = s.replace(/[.:#][\w-]+(\([^)]*\))?/g, ' ');
  const tags = (s.match(/[a-zA-Z][\w-]*/g) ?? []).length;
  return ids * 10000 + (attrs + classes + pseudo) * 100 + tags + pseudoEl;
}

/** A page skeleton in the given palette; `path` lists the elements from <body> down to the one asked about. */
export function stage(
  p: Palette,
  path: Array<{ tag?: string; cls?: string; svg?: boolean }> = [],
): Element {
  const doc = document.implementation.createHTMLDocument('');
  const root = doc.documentElement;
  root.setAttribute('data-theme', p.theme);
  root.setAttribute('data-dark', p.dark ? 'on' : 'off');
  root.setAttribute('data-contrast', p.high ? 'high' : 'normal');
  root.setAttribute('data-scanlines', 'on');
  let at: Element = doc.body;
  for (const step of path) {
    const el = step.svg
      ? doc.createElementNS('http://www.w3.org/2000/svg', step.tag ?? 'path')
      : doc.createElement(step.tag ?? 'div');
    if (step.cls) el.setAttribute('class', step.cls);
    at.append(el);
    at = el;
  }
  return path.length ? at : root;
}

function mediaApplies(media: string | null, p: Palette): boolean {
  if (media === null) return true;
  return media === 'print' && p.print === true;
}

function matches(el: Element, selector: string): boolean {
  try {
    return el.matches(selector);
  } catch {
    return false;
  }
}

/**
 * The winning declared value of `prop` for `el` (or its pseudo-element), or undefined. Inherited
 * values are found by asking for the ancestors: custom properties with `token()`.
 */
export function declared(
  rules: Rule[],
  p: Palette,
  el: Element,
  prop: string,
  pseudo = '',
): string | undefined {
  let best: { rank: number; order: number; value: string } | undefined;
  for (const r of rules) {
    if (!mediaApplies(r.media, p)) continue;
    if (!r.decls.some(([name]) => name === prop)) continue;
    const hasPseudo = /::[\w-]+$/.exec(r.selector)?.[0] ?? '';
    if (hasPseudo !== pseudo) continue;
    const sel = hasPseudo ? r.selector.slice(0, -hasPseudo.length) : r.selector;
    if (!matches(el, sel || '*')) continue;
    for (const [name, value, important] of r.decls) {
      if (name !== prop) continue;
      const rank = specificity(r.selector) + (important ? 1e9 : 0);
      if (!best || rank > best.rank || (rank === best.rank && r.order >= best.order))
        best = { rank, order: r.order, value };
    }
  }
  return best?.value;
}

/** A custom property as `el` sees it: its own declaration or the nearest ancestor's, `var()` followed. */
export function token(rules: Rule[], p: Palette, el: Element, name: string, depth = 0): string {
  if (depth > 12) throw new Error(`var() loop at ${name}`);
  const isRoot = el === el.ownerDocument.documentElement;
  const key = `${paletteName(p)}|${name}`;
  let memo = rootTokens.get(rules);
  if (!memo) {
    memo = new Map();
    rootTokens.set(rules, memo);
  }
  if (isRoot) {
    const known = memo.get(key);
    if (known !== undefined) return known;
  }
  const value = tokenAt(rules, p, el, name, depth);
  if (isRoot) memo.set(key, value);
  return value;
}

const rootTokens = new WeakMap<Rule[], Map<string, string>>();

function tokenAt(rules: Rule[], p: Palette, el: Element, name: string, depth: number): string {
  let at: Element | null = el;
  let raw: string | undefined;
  let owner: Element = el;
  while (at && raw === undefined) {
    raw = declared(rules, p, at, name);
    owner = at;
    at = at.parentElement;
  }
  if (raw === undefined) return '';
  return resolveVars(rules, p, owner, raw, depth);
}

export function resolveVars(
  rules: Rule[],
  p: Palette,
  el: Element,
  value: string,
  depth = 0,
): string {
  return value.replace(
    /var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)/g,
    (_m, name: string, fallback: string | undefined) =>
      token(rules, p, el, name, depth + 1) || (fallback ?? '').trim(),
  );
}

export type Rgb = [number, number, number];

/** Every plain colour written in a value: one for a colour, the stops for a gradient. */
export function colours(value: string): Rgb[] {
  const out: Rgb[] = [];
  for (const m of value.matchAll(/#([0-9a-f]{3,8})\b/gi)) {
    const hex = m[1]!;
    const full =
      hex.length === 3 || hex.length === 4
        ? [...hex.slice(0, 3)].map((c) => c + c).join('')
        : hex.slice(0, 6);
    out.push([0, 2, 4].map((i) => Number.parseInt(full.slice(i, i + 2), 16)) as Rgb);
  }
  return out;
}

function lin(c: number): number {
  const v = c / 255;
  return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

export function luminance([r, g, b]: Rgb): number {
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/** WCAG 2 contrast ratio, 1 to 21. */
export function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

/** How far apart two colours look (CIE76 in L*a*b*): about 2 is just noticeable. */
export function colourDistance(a: Rgb, b: Rgb): number {
  const lab = ([r, g, bl]: Rgb): [number, number, number] => {
    const [R, G, B] = [lin(r), lin(g), lin(bl)];
    const f = (t: number) => (t > 216 / 24389 ? Math.cbrt(t) : (24389 / 27 / 116) * t + 16 / 116);
    const x = f((0.4124 * R + 0.3576 * G + 0.1805 * B) / 0.95047);
    const y = f(0.2126 * R + 0.7152 * G + 0.0722 * B);
    const z = f((0.0193 * R + 0.1192 * G + 0.9505 * B) / 1.08883);
    return [116 * y - 16, 500 * (x - y), 200 * (y - z)];
  };
  const [p, q] = [lab(a), lab(b)];
  return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
}
