/**
 * Crystal structures (the Crystallography Open Database, owner request 2026-11-05): a structure's
 * document is its summary and then its CIF, under the line `Crystallographic Information File
 * <id>.cif` (wsp_adapters.doccollection.cod). The page shows the summary as a table, draws the unit
 * cell and its atoms from the CIF's coordinates, offers the CIF as a download (made in the page: the
 * library is offline) and shows the file itself, folded.
 *
 * The drawing is a small canvas renderer written here, not a library: atoms as shaded spheres
 * painted back to front, bonds from covalent radii, the cell's edges; drag (or the arrow keys) to
 * turn it, the wheel (or + and -) to zoom. A CIF lists the asymmetric unit; its symmetry operations
 * fill the cell.
 */
import { makeAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { wspLink } from '../ui/components.ts';

const CIF_LINE = /\n\nCrystallographic Information File (\d+)\.cif\n/;
/** The most atoms drawn: a protein-sized cell would be too slow to turn on an old laptop. */
export const MAX_ATOMS = 3000;
/** Bonds are looked for only below this many atoms (every pair is compared). */
const MAX_BONDED = 1500;

export interface StructureText {
  summary: string;
  cif: string;
  fileName: string;
}

/** A structure document's summary and CIF, or null when the text is not one. */
export function splitStructure(text: string | null | undefined): StructureText | null {
  const m = CIF_LINE.exec(text ?? '');
  if (!m || text == null) return null;
  return {
    summary: text.slice(0, m.index),
    cif: text.slice(m.index + m[0].length),
    fileName: `${m[1]}.cif`,
  };
}

// --- CIF ------------------------------------------------------------------------------------------

export interface CifLoop {
  tags: string[];
  rows: string[][];
}

export interface CifBlock {
  items: Map<string, string>;
  loops: CifLoop[];
}

const TOKEN = /\s*(?:(#.*)|'(.*?)'(?=\s|$)|"(.*?)"(?=\s|$)|(\S+))/gy;

type Token = [kind: 'data' | 'loop' | 'tag' | 'value' | 'save', text: string];

function* tokens(text: string): Generator<Token> {
  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i++) {
    let line = (lines[i] ?? '').replace(/\r$/, '');
    if (line.startsWith(';')) {
      const body = [line.slice(1)];
      i++;
      while (i < lines.length && !(lines[i] ?? '').startsWith(';')) body.push(lines[i++] ?? '');
      line = (lines[i] ?? '').slice(1);
      yield ['value', body.join('\n').replace(/^\n+|\n+$/g, '')];
    }
    TOKEN.lastIndex = 0;
    for (let m = TOKEN.exec(line); m && m[0].length > 0; m = TOKEN.exec(line)) {
      const [, comment, single, double, bare] = m;
      if (comment !== undefined) break;
      if (single !== undefined || double !== undefined) {
        yield ['value', single ?? double ?? ''];
        continue;
      }
      if (bare === undefined) continue;
      const low = bare.toLowerCase();
      if (low.startsWith('data_')) yield ['data', bare.slice(5)];
      else if (low === 'loop_') yield ['loop', ''];
      else if (low.startsWith('save_')) yield ['save', bare.slice(5)];
      else if (bare.startsWith('_')) yield ['tag', low];
      else if (low !== 'global_' && low !== 'stop_') yield ['value', bare];
    }
  }
}

/** The first data block of a CIF (tags lower-cased); forgiving, as the Python reader is. */
export function parseCif(text: string): CifBlock | null {
  const block: CifBlock = { items: new Map(), loops: [] };
  let started = false;
  let tag: string | null = null;
  const loop = { open: false, header: false, tags: [] as string[], values: [] as string[] };
  const close = () => {
    const w = loop.tags.length;
    if (loop.open && w) {
      const rows: string[][] = [];
      for (let k = 0; k + w <= loop.values.length; k += w) rows.push(loop.values.slice(k, k + w));
      block.loops.push({ tags: loop.tags, rows });
    }
    Object.assign(loop, { open: false, header: false, tags: [], values: [] });
  };
  for (const [kind, val] of tokens(text)) {
    if (kind === 'data') {
      if (started) break; // the first block is the structure
      started = true;
      tag = null;
      continue;
    }
    if (!started) continue;
    if (kind === 'loop') {
      close();
      Object.assign(loop, { open: true, header: true });
      tag = null;
    } else if (kind === 'tag') {
      if (loop.open && loop.header) {
        loop.tags.push(val);
        continue;
      }
      close();
      tag = val;
    } else if (kind === 'save') {
      close();
      tag = null;
    } else if (loop.open) {
      loop.header = false;
      loop.values.push(val);
    } else if (tag) {
      if (!block.items.has(tag)) block.items.set(tag, val);
      tag = null;
    }
  }
  close();
  return started ? block : null;
}

/** The values of the first of `tags` some loop has, as a column. */
export function column(block: CifBlock, ...tags: string[]): string[] | null {
  for (const t of tags) {
    for (const l of block.loops) {
      const i = l.tags.indexOf(t);
      if (i >= 0) return l.rows.map((r) => r[i] ?? '');
    }
  }
  return null;
}

/** A CIF number without its uncertainty (`4.9134(2)`); null for `?`, `.` or anything else. */
export function cifNumber(v: string | undefined): number | null {
  if (v === undefined) return null;
  const n = Number.parseFloat(v.replace(/\(\d+\)$/, ''));
  return Number.isFinite(n) && /^[-+]?(\d|\.\d)/.test(v) ? n : null;
}

function itemNumber(block: CifBlock, tag: string): number | null {
  return cifNumber(block.items.get(tag) ?? column(block, tag)?.[0]);
}

// --- symmetry -------------------------------------------------------------------------------------

/** A symmetry operation: three rows of (x, y, z coefficients, translation). */
export type SymOp = [number, number, number, number][];

/** `-x+1/2, y-x, z+0.25` as an operation; null when a part cannot be read. */
export function parseSymop(expr: string): SymOp | null {
  const parts = expr.toLowerCase().replace(/\s+/g, '').split(',');
  if (parts.length !== 3) return null;
  const op: SymOp = [];
  for (const part of parts) {
    const row: [number, number, number, number] = [0, 0, 0, 0];
    const terms = part.match(/[+-]?[^+-]+/g);
    if (!terms) return null;
    for (const term of terms) {
      const sign = term.startsWith('-') ? -1 : 1;
      const body = term.replace(/^[+-]/, '');
      const axis = /([xyz])/.exec(body);
      if (axis) {
        const coef = body.replace(axis[1]!, '').replace('*', '');
        const k = coef === '' ? 1 : fraction(coef);
        if (k === null) return null;
        row['xyz'.indexOf(axis[1]!)]! += sign * k;
      } else {
        const t = fraction(body);
        if (t === null) return null;
        row[3] += sign * t;
      }
    }
    op.push(row);
  }
  return op;
}

function fraction(s: string): number | null {
  const m = /^(\d*\.?\d+)(?:\/(\d*\.?\d+))?$/.exec(s);
  if (!m) return null;
  const n = Number(m[1]);
  const d = m[2] === undefined ? 1 : Number(m[2]);
  return d === 0 ? null : n / d;
}

function apply(op: SymOp, p: readonly number[]): [number, number, number] {
  const out: [number, number, number] = [0, 0, 0];
  for (let r = 0; r < 3; r++) {
    const row = op[r]!;
    out[r] = row[0] * p[0]! + row[1] * p[1]! + row[2] * p[2]! + row[3];
  }
  return out;
}

// --- the cell -------------------------------------------------------------------------------------

export interface Cell {
  a: number;
  b: number;
  c: number;
  alpha: number;
  beta: number;
  gamma: number;
}

export interface Atom {
  label: string;
  element: string;
  /** Fractional coordinates, in [0, 1] (atoms on a face or an edge appear on each). */
  frac: [number, number, number];
}

export interface UnitCell {
  cell: Cell;
  atoms: Atom[];
  /** More atoms than {@link MAX_ATOMS}: only the first were kept. */
  truncated: boolean;
}

const ELEMENTS = new Set(
  (
    'H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr ' +
    'Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm ' +
    'Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf D'
  ).split(' '),
);

/** The element of `O2-`, `Fe3+`, `CL1`, `SiA` or `OW1`: one or two letters that name one, else '?'. */
export function elementOf(symbol: string): string {
  const m = /^([A-Za-z])([A-Za-z]?)/.exec(symbol.trim());
  if (!m) return '?';
  const one = m[1]!.toUpperCase();
  const two = one + (m[2] ?? '').toLowerCase();
  if (m[2] && ELEMENTS.has(two)) return two === 'D' ? 'H' : two;
  return ELEMENTS.has(one) ? (one === 'D' ? 'H' : one) : '?';
}

/** The atoms of the whole cell: each site under every symmetry operation, folded into the cell,
 * once each; null without a cell or fractional coordinates (some CIFs give only Cartesian ones). */
export function unitCell(block: CifBlock, max = MAX_ATOMS): UnitCell | null {
  const [a, b, c] = ['a', 'b', 'c'].map((x) => itemNumber(block, `_cell_length_${x}`));
  const angle = (x: string) => itemNumber(block, `_cell_angle_${x}`) ?? 90;
  const [alpha, beta, gamma] = [angle('alpha'), angle('beta'), angle('gamma')];
  const xs = column(block, '_atom_site_fract_x');
  const ys = column(block, '_atom_site_fract_y');
  const zs = column(block, '_atom_site_fract_z');
  if (!a || !b || !c || !xs || !ys || !zs) return null;
  const labels = column(block, '_atom_site_label') ?? [];
  const types = column(block, '_atom_site_type_symbol');
  const ops = (
    column(block, '_space_group_symop_operation_xyz', '_symmetry_equiv_pos_as_xyz') ?? ['x,y,z']
  )
    .map(parseSymop)
    .filter((o): o is SymOp => o !== null);
  if (!ops.length) ops.push(parseSymop('x,y,z')!);
  const atoms: Atom[] = [];
  const seen = new Set<string>();
  let truncated = false;
  const fold = (v: number) => {
    const f = v - Math.floor(v);
    return f > 1 - 1e-4 ? 0 : f;
  };
  sites: for (let i = 0; i < xs.length; i++) {
    const p = [cifNumber(xs[i]), cifNumber(ys[i]), cifNumber(zs[i])];
    if (p.some((v) => v === null)) continue;
    const label = labels[i] ?? '';
    const element = elementOf(types?.[i] || label);
    for (const op of ops) {
      const q = apply(op, p as number[]).map(fold);
      // A site on a face, an edge or a corner is drawn on each (the cell's eight corners).
      const images: number[][] = [[]];
      for (const v of q) {
        const next: number[][] = [];
        for (const img of images) {
          next.push([...img, v]);
          if (v < 1e-3) next.push([...img, 1]);
        }
        images.splice(0, images.length, ...next);
      }
      for (const img of images) {
        const key = `${element}:${img.map((v) => Math.round(v * 500)).join(',')}`;
        if (seen.has(key)) continue;
        seen.add(key);
        if (atoms.length >= max) {
          truncated = true;
          break sites;
        }
        atoms.push({ label, element, frac: img as [number, number, number] });
      }
    }
  }
  return { cell: { a, b, c, alpha, beta, gamma }, atoms, truncated };
}

type Vec = [number, number, number];

/** Fractional to Cartesian coordinates (Å): a along x, b in the xy plane. */
export function cartesian(cell: Cell): (f: readonly number[]) => Vec {
  const rad = Math.PI / 180;
  const [ca, cb, cg] = [cell.alpha, cell.beta, cell.gamma].map((x) => Math.cos(x * rad)) as Vec;
  const sg = Math.sin(cell.gamma * rad);
  const cx = cell.c * cb;
  const cy = (cell.c * (ca - cb * cg)) / sg;
  const cz = Math.sqrt(Math.max(cell.c * cell.c - cx * cx - cy * cy, 0));
  return (f) => [
    f[0]! * cell.a + f[1]! * cell.b * cg + f[2]! * cx,
    f[1]! * cell.b * sg + f[2]! * cy,
    f[2]! * cz,
  ];
}

/** `'H 0.31 B 0.84'` as `{H: '0.31', B: '0.84'}`: a table kept on a few lines. */
function pairs(table: string): Map<string, string> {
  const words = table.trim().split(/\s+/);
  const out = new Map<string, string>();
  for (let i = 0; i + 1 < words.length; i += 2) out.set(words[i]!, words[i + 1]!);
  return out;
}

/** Covalent radii (Å, Cordero 2008) of the common elements; others 1.5. */
const RADIUS = pairs(
  'H 0.31 B 0.84 C 0.76 N 0.71 O 0.66 F 0.57 Na 1.66 Mg 1.41 Al 1.21 Si 1.11 P 1.07 S 1.05 Cl 1.02 ' +
    'K 2.03 Ca 1.76 Ti 1.6 V 1.53 Cr 1.39 Mn 1.39 Fe 1.32 Co 1.26 Ni 1.24 Cu 1.32 Zn 1.22 Ga 1.22 ' +
    'Ge 1.2 As 1.19 Se 1.2 Br 1.2 Zr 1.75 Mo 1.54 Ru 1.46 Rh 1.42 Pd 1.39 Ag 1.45 Cd 1.44 Sn 1.39 ' +
    'Sb 1.39 Te 1.38 I 1.39 Ba 2.15 La 2.07 W 1.62 Pt 1.36 Au 1.36 Hg 1.32 Pb 1.46 Bi 1.48 U 1.96',
);
const radius = (el: string) => Number(RADIUS.get(el) ?? 1.5);

/** Pairs of atoms closer than the sum of their covalent radii and a tolerance. */
export function bonds(atoms: readonly Atom[], points: readonly Vec[]): [number, number][] {
  const out: [number, number][] = [];
  if (atoms.length > MAX_BONDED) return out;
  for (let i = 0; i < atoms.length; i++) {
    for (let j = i + 1; j < atoms.length; j++) {
      const p = points[i]!;
      const q = points[j]!;
      const d = Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
      const max = radius(atoms[i]!.element) + radius(atoms[j]!.element) + 0.45;
      if (d > 0.4 && d < max) out.push([i, j]);
    }
  }
  return out;
}

/** Colours after the CPK and Jmol conventions. */
const COLOR = pairs(
  'H #ffffff C #909090 N #3050f8 O #ff0d0d F #90e050 Cl #1ff01f Br #a62929 I #940094 S #ffff30 ' +
    'P #ff8000 B #ffb5b5 Si #f0c8a0 Na #ab5cf2 K #8f40d4 Mg #8aff00 Ca #3dff00 Al #bfa6a6 ' +
    'Fe #e06633 Cu #c88033 Zn #7d80b0 Ni #50d050 Co #f090a0 Mn #9c7ac7 Ti #bfc2c7',
);
export const colorOf = (el: string) => COLOR.get(el) ?? '#ff1493';

// --- the viewer -----------------------------------------------------------------------------------

type Mat = [Vec, Vec, Vec];

function rotate(m: Mat, ax: number, ay: number): Mat {
  const [sx, cx, sy, cy] = [Math.sin(ax), Math.cos(ax), Math.sin(ay), Math.cos(ay)];
  const rx: Mat = [
    [1, 0, 0],
    [0, cx, -sx],
    [0, sx, cx],
  ];
  const ry: Mat = [
    [cy, 0, sy],
    [0, 1, 0],
    [-sy, 0, cy],
  ];
  return mul(rx, mul(ry, m));
}

function mul(a: Mat, b: Mat): Mat {
  return [0, 1, 2].map((i) =>
    [0, 1, 2].map((j) => a[i]![0] * b[0]![j]! + a[i]![1] * b[1]![j]! + a[i]![2] * b[2]![j]!),
  ) as Mat;
}

const START: Mat = rotate(
  [
    [1, 0, 0],
    [0, 1, 0],
    [0, 0, 1],
  ],
  -0.45,
  0.6,
);

/** The drawing of a cell: canvas, controls and a line saying what is shown. */
export function structureViewer(uc: UnitCell, title: string): HTMLElement {
  const toCart = cartesian(uc.cell);
  const points = uc.atoms.map((a) => toCart(a.frac));
  const links = bonds(uc.atoms, points);
  const corners = [0, 1].flatMap((x) =>
    [0, 1].flatMap((y) => [0, 1].map((z) => toCart([x, y, z]))),
  );
  const centre = toCart([0.5, 0.5, 0.5]);
  const extent = Math.max(...corners.map((p) => Math.hypot(...p.map((v, i) => v - centre[i]!))));
  // Corners are numbered by their (x, y, z) bits; an edge joins two that differ in one bit.
  const edges = [0, 1, 2, 3, 4, 5, 6, 7].flatMap((i) =>
    [1, 2, 4].filter((bit) => !(i & bit)).map((bit) => [i, i | bit] as const),
  );
  const canvas = h('canvas', {
    class: 'crystal-canvas',
    width: '640',
    height: '480',
    tabindex: '0',
    role: 'img',
    'aria-label': `Unit cell of ${title}: ${uc.atoms.length} atoms. Drag or use the arrow keys to turn it.`,
  });
  let view: Mat = START;
  let zoom = 1;
  let queued = false;

  const draw = () => {
    queued = false;
    const g = canvas.getContext('2d');
    if (!g) return;
    const ratio = globalThis.devicePixelRatio || 1;
    const w = canvas.clientWidth || 640;
    const hgt = canvas.clientHeight || 480;
    if (canvas.width !== Math.round(w * ratio)) canvas.width = Math.round(w * ratio);
    if (canvas.height !== Math.round(hgt * ratio)) canvas.height = Math.round(hgt * ratio);
    g.setTransform(ratio, 0, 0, ratio, 0, 0);
    const style = getComputedStyle(canvas);
    g.fillStyle = style.backgroundColor || '#fff';
    g.fillRect(0, 0, w, hgt);
    const scale = (zoom * 0.45 * Math.min(w, hgt)) / Math.max(extent, 1);
    const project = (p: Vec): Vec => {
      const d: Vec = [p[0] - centre[0], p[1] - centre[1], p[2] - centre[2]];
      const r = view.map((row) => row[0] * d[0] + row[1] * d[1] + row[2] * d[2]) as Vec;
      return [w / 2 + r[0] * scale, hgt / 2 - r[1] * scale, r[2]];
    };
    const ink = style.color || '#000';
    const at = points.map(project);
    const box = corners.map(project);
    g.lineWidth = 1;
    g.strokeStyle = ink;
    g.globalAlpha = 0.6;
    for (const [i, j] of edges) {
      g.beginPath();
      g.moveTo(box[i]![0], box[i]![1]);
      g.lineTo(box[j]![0], box[j]![1]);
      g.stroke();
    }
    g.globalAlpha = 1;
    g.font = '12px sans-serif';
    g.fillStyle = ink;
    for (const [k, name] of [
      [4, 'a'],
      [2, 'b'],
      [1, 'c'],
    ] as const)
      g.fillText(name, box[k]![0] + 4, box[k]![1] - 4);
    g.lineWidth = Math.max(1.5, scale * 0.12);
    g.strokeStyle = '#7f7f7f';
    for (const [i, j] of links) {
      g.beginPath();
      g.moveTo(at[i]![0], at[i]![1]);
      g.lineTo(at[j]![0], at[j]![1]);
      g.stroke();
    }
    const order = at.map((_, i) => i).sort((i, j) => at[i]![2] - at[j]![2]);
    g.lineWidth = 1;
    for (const i of order) {
      const [x, y] = at[i]!;
      const el = uc.atoms[i]!.element;
      const r = Math.max(2, (el === 'H' ? 0.25 : 0.35) * Math.min(radius(el), 1.6) * scale);
      const shade = g.createRadialGradient(x - r / 3, y - r / 3, r / 6, x, y, r);
      shade.addColorStop(0, '#ffffff');
      shade.addColorStop(0.35, colorOf(el));
      shade.addColorStop(1, '#202020');
      g.fillStyle = shade;
      g.beginPath();
      g.arc(x, y, r, 0, 2 * Math.PI);
      g.fill();
    }
  };
  const redraw = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(draw);
  };
  const turn = (ax: number, ay: number) => {
    view = rotate(view, ax, ay);
    redraw();
  };
  const zoomBy = (k: number) => {
    zoom = Math.min(8, Math.max(0.25, zoom * k));
    redraw();
  };

  let drag: { x: number; y: number } | null = null;
  canvas.addEventListener('pointerdown', (e) => {
    drag = { x: e.clientX, y: e.clientY };
    canvas.setPointerCapture?.(e.pointerId);
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!drag) return;
    turn((e.clientY - drag.y) * 0.01, (e.clientX - drag.x) * 0.01);
    drag = { x: e.clientX, y: e.clientY };
  });
  const stop = () => {
    drag = null;
  };
  canvas.addEventListener('pointerup', stop);
  canvas.addEventListener('pointercancel', stop);
  canvas.addEventListener(
    'wheel',
    (e) => {
      e.preventDefault();
      zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15);
    },
    { passive: false },
  );
  canvas.addEventListener('keydown', (e) => {
    const step = 0.15;
    const keys: Record<string, () => void> = {
      ArrowLeft: () => turn(0, -step),
      ArrowRight: () => turn(0, step),
      ArrowUp: () => turn(-step, 0),
      ArrowDown: () => turn(step, 0),
      '+': () => zoomBy(1.2),
      '=': () => zoomBy(1.2),
      '-': () => zoomBy(1 / 1.2),
    };
    const act = keys[e.key];
    if (!act || e.ctrlKey || e.metaKey || e.altKey) return;
    e.preventDefault();
    e.stopPropagation(); // the browser's own shortcuts (Back on an arrow) stay off while it has focus
    act();
  });

  const button = (label: string, title: string, act: () => void) =>
    h('button', { type: 'button', class: 'btn', title, onclick: act }, label);
  const controls = h(
    'p',
    { class: 'crystal-controls' },
    button('Zoom in', 'Larger (+)', () => zoomBy(1.25)),
    ' ',
    button('Zoom out', 'Smaller (−)', () => zoomBy(1 / 1.25)),
    ' ',
    button('Reset view', 'Back to the first view', () => {
      view = START;
      zoom = 1;
      redraw();
    }),
  );
  const note = h(
    'p',
    { class: 'fineprint' },
    `The unit cell (a, b and c marked) with ${uc.atoms.length.toLocaleString('en-US')} atoms`,
    uc.truncated ? ` (the first ${MAX_ATOMS.toLocaleString('en-US')} only)` : '',
    links.length ? ` and ${links.length.toLocaleString('en-US')} bonds` : '',
    '. Drag to turn it, or use the arrow keys; the wheel or + and − zoom.',
  );
  // Drawn again at its new size when the window (or the text size) changes the canvas's width.
  if (typeof ResizeObserver === 'function') new ResizeObserver(redraw).observe(canvas);
  redraw();
  return h('figure', { class: 'crystal-viewer' }, canvas, controls, h('figcaption', null, note));
}

// --- the page -------------------------------------------------------------------------------------

/** The summary's `Key: value` lines as a table; its first line (the title) is the page heading. */
function summaryTable(summary: string, source: string, nativeId: string): HTMLElement {
  const lines = summary.split('\n').slice(1);
  const intro = lines[0] && !lines[0].includes(': ') ? lines.shift() : undefined;
  const rows = lines.map((line) => {
    const at = line.indexOf(': ');
    const key = at > 0 ? line.slice(0, at) : '';
    const value = at > 0 ? line.slice(at + 2) : line;
    const parent = nativeId.includes('/') ? nativeId.slice(0, nativeId.lastIndexOf('/')) : '';
    const cell =
      key === 'From the archive' && parent
        ? wspLink(makeAddress(source, parent), value)
        : key === 'DOI'
          ? h('code', null, value)
          : value;
    return h('tr', null, h('th', { scope: 'row' }, key), h('td', null, cell));
  });
  return h(
    'div',
    { class: 'crystal-summary' },
    intro ? h('p', null, intro) : null,
    h('table', { class: 'crystal-facts' }, h('tbody', null, ...rows)),
  );
}

/** The CIF as a file to save, made in the page when asked for (the library is offline; nothing is
 * fetched) and let go of once the browser has it. */
export function downloadButton(st: StructureText): HTMLButtonElement {
  return h(
    'button',
    {
      type: 'button',
      class: 'btn crystal-download',
      onclick: () => {
        const url = URL.createObjectURL(new Blob([st.cif], { type: 'chemical/x-cif' }));
        h('a', { href: url, download: st.fileName }).click();
        setTimeout(() => URL.revokeObjectURL(url), 10_000);
      },
    },
    `Download ${st.fileName}`,
  );
}

/** A crystal structure's page body: summary, drawing, download and the CIF itself. */
export function structureView(
  doc: { title: string; source_id: string; native_id: string },
  st: StructureText,
): HTMLElement {
  const block = parseCif(st.cif);
  const uc = block ? unitCell(block) : null;
  return h(
    'div',
    { class: 'crystal' },
    summaryTable(st.summary, doc.source_id, doc.native_id),
    uc?.atoms.length
      ? structureViewer(uc, doc.title)
      : h(
          'p',
          { class: 'notice' },
          'This file gives no cell and fractional coordinates to draw the structure from.',
        ),
    h('p', { class: 'crystal-file' }, downloadButton(st)),
    h(
      'details',
      { class: 'crystal-cif' },
      h('summary', null, `The CIF file (${st.fileName})`),
      h('pre', { class: 'doc-text' }, st.cif),
    ),
  );
}
