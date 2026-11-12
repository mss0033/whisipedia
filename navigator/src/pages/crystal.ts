/**
 * Crystal structures (the Crystallography Open Database, owner request 2026-11-05): a structure's
 * document is its summary and then its CIF, under the line `Crystallographic Information File
 * <id>.cif` (wsp_adapters.doccollection.cod). The page shows the summary as a table, draws the unit
 * cell and its atoms from the CIF's coordinates, offers the CIF as a download (made in the page: the
 * library is offline) and shows the file itself, folded.
 *
 * The drawing is a small canvas renderer written here, not a library: atoms as shaded spheres
 * painted back to front, bonds from covalent radii, the cell's edges; drag (or the arrow keys) to
 * turn it, + and - (or the wheel, once it has the focus or with Ctrl) to zoom. A CIF lists the asymmetric unit; its symmetry operations
 * fill the cell.
 */
import { makeAddress } from '../core/address.ts';
import { h, s } from '../core/dom.ts';
import { LOCALE } from '../core/format.ts';
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

/** The sources whose documents are COD structures (registry: `reader: cod-cif`). */
export const COD_SOURCES: ReadonlySet<string> = new Set(['cod-full-archive']);
/** A structure's native id: `<archive key>/<COD id>` (`cod:cod-cifs-mysql/1000000`). */
const COD_NATIVE = /^cod:[^/]+\/(\d{7})$/;

/**
 * The structure of a COD structure's document, or null for any other document: a text that only
 * contains a line like `Crystallographic Information File 1000000.cif` (a paper about COD, a mailing
 * list post quoting one) is not drawn as a crystal (2026-11-06 review). The CIF line must name the
 * structure the document's own id names.
 */
export function structureOf(doc: {
  source_id: string;
  native_id: string;
  text?: string | null | undefined;
}): StructureText | null {
  if (!COD_SOURCES.has(doc.source_id)) return null;
  const id = COD_NATIVE.exec(doc.native_id)?.[1];
  const st = id ? splitStructure(doc.text) : null;
  return st && st.fileName === `${id}.cif` ? st : null;
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
  /** The element drawn: on a site shared by several, the one with the largest share. */
  element: string;
  /** Fractional coordinates, in [0, 1] (atoms on a face or an edge appear on each). */
  frac: [number, number, number];
  /** Only on a site several elements share (a solid solution, a disordered site): each element's
   * share of the site, largest first. */
  shares?: { element: string; occupancy: number }[];
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
  const occupancies = column(block, '_atom_site_occupancy');
  // A position in the cell holds one atom, whatever lists it: a site's own images under the
  // symmetry, or several elements sharing it (each with its occupancy; a missing one is 1). All the
  // species of a position are gathered before one is chosen to draw.
  interface Place {
    frac: [number, number, number];
    /** element -> the asymmetric-unit sites that put it here, each counted once. */
    species: Map<string, Map<number, number>>;
    /** element -> the label of the first site that put it here. */
    labels: Map<string, string>;
  }
  const places = new Map<string, Place>();
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
    const occupancy = Math.max(0, cifNumber(occupancies?.[i]) ?? 1);
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
        const key = img.map((v) => Math.round(v * 500)).join(',');
        let place = places.get(key);
        if (!place) {
          if (places.size >= max) {
            truncated = true;
            break sites;
          }
          place = { frac: img as [number, number, number], species: new Map(), labels: new Map() };
          places.set(key, place);
        }
        let from = place.species.get(element);
        if (!from) {
          from = new Map();
          place.species.set(element, from);
          place.labels.set(element, label);
        }
        from.set(i, occupancy);
      }
    }
  }
  const atoms: Atom[] = [];
  for (const place of places.values()) {
    const shares = [...place.species].map(([element, from]) => ({
      element,
      occupancy: [...from.values()].reduce((sum, o) => sum + o, 0),
    }));
    // The largest share; equal shares (to a rounding of the sums: 0.1 + 0.2 is not 0.3) go to the
    // element listed first (the sort is stable).
    shares.sort((x, y) =>
      Math.abs(y.occupancy - x.occupancy) < 1e-6 ? 0 : y.occupancy - x.occupancy,
    );
    const element = shares[0]!.element;
    const atom: Atom = { label: place.labels.get(element) ?? '', element, frac: place.frac };
    if (shares.length > 1) atom.shares = shares;
    atoms.push(atom);
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

/** Covalent radii (Å, Cordero et al. 2008), hydrogen to curium; heavier elements and unknowns 1.5. */
const RADIUS = pairs(
  'H 0.31 He 0.28 Li 1.28 Be 0.96 B 0.84 C 0.76 N 0.71 O 0.66 F 0.57 Ne 0.58 Na 1.66 Mg 1.41 ' +
    'Al 1.21 Si 1.11 P 1.07 S 1.05 Cl 1.02 Ar 1.06 K 2.03 Ca 1.76 Sc 1.7 Ti 1.6 V 1.53 Cr 1.39 ' +
    'Mn 1.39 Fe 1.32 Co 1.26 Ni 1.24 Cu 1.32 Zn 1.22 Ga 1.22 Ge 1.2 As 1.19 Se 1.2 Br 1.2 Kr 1.16 ' +
    'Rb 2.2 Sr 1.95 Y 1.9 Zr 1.75 Nb 1.64 Mo 1.54 Tc 1.47 Ru 1.46 Rh 1.42 Pd 1.39 Ag 1.45 Cd 1.44 ' +
    'In 1.42 Sn 1.39 Sb 1.39 Te 1.38 I 1.39 Xe 1.4 Cs 2.44 Ba 2.15 La 2.07 Ce 2.04 Pr 2.03 Nd 2.01 ' +
    'Pm 1.99 Sm 1.98 Eu 1.98 Gd 1.96 Tb 1.94 Dy 1.92 Ho 1.92 Er 1.89 Tm 1.9 Yb 1.87 Lu 1.87 ' +
    'Hf 1.75 Ta 1.7 W 1.62 Re 1.51 Os 1.44 Ir 1.41 Pt 1.36 Au 1.36 Hg 1.32 Tl 1.45 Pb 1.46 ' +
    'Bi 1.48 Po 1.4 At 1.5 Rn 1.5 Fr 2.6 Ra 2.21 Ac 2.15 Th 2.06 Pa 2 U 1.96 Np 1.9 Pu 1.87 ' +
    'Am 1.8 Cm 1.69',
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

/**
 * Element colours, hydrogen to lawrencium, after the CPK convention as Jmol extends it. They are the
 * chemist's code for the elements (oxygen red, nitrogen blue), the same in every palette, so they
 * are data and not theme tokens; an unknown symbol is Jmol's pink.
 */
const COLOR = pairs(
  'H #ffffff He #d9ffff Li #cc80ff Be #c2ff00 B #ffb5b5 C #909090 N #3050f8 O #ff0d0d F #90e050 ' +
    'Ne #b3e3f5 Na #ab5cf2 Mg #8aff00 Al #bfa6a6 Si #f0c8a0 P #ff8000 S #ffff30 Cl #1ff01f ' +
    'Ar #80d1e3 K #8f40d4 Ca #3dff00 Sc #e6e6e6 Ti #bfc2c7 V #a6a6ab Cr #8a99c7 Mn #9c7ac7 ' +
    'Fe #e06633 Co #f090a0 Ni #50d050 Cu #c88033 Zn #7d80b0 Ga #c28f8f Ge #668f8f As #bd80e3 ' +
    'Se #ffa100 Br #a62929 Kr #5cb8d1 Rb #702eb0 Sr #00ff00 Y #94ffff Zr #94e0e0 Nb #73c2c9 ' +
    'Mo #54b5b5 Tc #3b9e9e Ru #248f8f Rh #0a7d8c Pd #006985 Ag #c0c0c0 Cd #ffd98f In #a67573 ' +
    'Sn #668080 Sb #9e63b5 Te #d47a00 I #940094 Xe #429eb0 Cs #57178f Ba #00c900 La #70d4ff ' +
    'Ce #ffffc7 Pr #d9ffc7 Nd #c7ffc7 Pm #a3ffc7 Sm #8fffc7 Eu #61ffc7 Gd #45ffc7 Tb #30ffc7 ' +
    'Dy #1fffc7 Ho #00ff9c Er #00e675 Tm #00d452 Yb #00bf38 Lu #00ab24 Hf #4dc2ff Ta #4da6ff ' +
    'W #2194d6 Re #267dab Os #266696 Ir #175487 Pt #d0d0e0 Au #ffd123 Hg #b8b8d0 Tl #a6544d ' +
    'Pb #575961 Bi #9e4fb5 Po #ab5c00 At #754f45 Rn #428296 Fr #420066 Ra #007d00 Ac #70abfa ' +
    'Th #00baff Pa #00a1ff U #008fff Np #0080ff Pu #006bff Am #545cf2 Cm #785ce3 Bk #8a4fe3 ' +
    'Cf #a136d4 Es #b31fd4 Fm #b31fba Md #b30da6 No #bd0d87 Lr #c70066',
);
const UNKNOWN_COLOR = '#ff1493';
export const colorOf = (el: string) => COLOR.get(el) ?? UNKNOWN_COLOR;

/** The elements drawn, most atoms first, and the sites several elements share (their make-up as
 * `Cu 0.5, Au 0.5`, with how many atoms have it). */
export function legendOf(atoms: readonly Atom[]): {
  elements: { element: string; count: number }[];
  shared: { mix: string; count: number }[];
} {
  const counts = new Map<string, number>();
  const mixes = new Map<string, number>();
  for (const a of atoms) {
    counts.set(a.element, (counts.get(a.element) ?? 0) + 1);
    if (a.shares) {
      const mix = a.shares
        .map((sh) => `${sh.element} ${Number(sh.occupancy.toFixed(3))}`)
        .join(', ');
      mixes.set(mix, (mixes.get(mix) ?? 0) + 1);
    }
  }
  return {
    elements: [...counts]
      .map(([element, count]) => ({ element, count }))
      .sort((x, y) => y.count - x.count),
    shared: [...mixes].map(([mix, count]) => ({ mix, count })),
  };
}

/** What the colours mean, under the drawing: one entry per element drawn, and what a shared site holds. */
function legend(atoms: readonly Atom[]): HTMLElement {
  const { elements, shared } = legendOf(atoms);
  const n = (v: number) => v.toLocaleString(LOCALE);
  const list = h(
    'ul',
    { class: 'crystal-legend', 'aria-label': 'Elements drawn' },
    ...elements.map(({ element, count }) =>
      h(
        'li',
        null,
        // The colour is an attribute of the drawing, not a style: allowed under the page's policy.
        s(
          'svg',
          { class: 'crystal-swatch', viewBox: '0 0 12 12', 'aria-hidden': 'true' },
          s('circle', { cx: 6, cy: 6, r: 5, fill: colorOf(element) }),
        ),
        ` ${element === '?' ? 'unknown element' : element} × ${n(count)}`,
      ),
    ),
  );
  if (!shared.length) return list;
  const shown = shared.slice(0, 4).map(({ mix, count }) => `${mix} (${n(count)})`);
  return h(
    'div',
    null,
    list,
    h(
      'p',
      { class: 'fineprint crystal-shared' },
      `Sites that several elements share are drawn once, as the element with the largest share: ${shown.join('; ')}${shared.length > shown.length ? '; and others' : ''}.`,
    ),
  );
}

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

/** Told when the palette changes; answers with how to stop being told (ThemeManager.onChange). */
export interface PaletteWatch {
  onChange(listener: () => void): () => void;
}

/** The drawing of a cell: canvas, controls, what the colours mean and a line saying what is shown. */
export function structureViewer(
  uc: UnitCell,
  title: string,
  life?: AbortSignal,
  theme?: PaletteWatch,
): HTMLElement {
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
    g.fillStyle = style.backgroundColor || 'Canvas';
    g.fillRect(0, 0, w, hgt);
    const scale = (zoom * 0.45 * Math.min(w, hgt)) / Math.max(extent, 1);
    const project = (p: Vec): Vec => {
      const d: Vec = [p[0] - centre[0], p[1] - centre[1], p[2] - centre[2]];
      const r = view.map((row) => row[0] * d[0] + row[1] * d[1] + row[2] * d[2]) as Vec;
      return [w / 2 + r[0] * scale, hgt / 2 - r[1] * scale, r[2]];
    };
    const ink = style.color || 'CanvasText';
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
    // Bonds in the page's quiet colour (read from the palette; the canvas takes no CSS itself).
    g.strokeStyle = style.getPropertyValue('--page-muted').trim() || ink;
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
      // Only once the drawing is chosen (it has the focus: clicked or tabbed to) or with Ctrl held:
      // a reader scrolling the page past it with the wheel zoomed it instead (2026-11-06 review).
      if (!e.ctrlKey && document.activeElement !== canvas) return;
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
    `The unit cell (a, b and c marked) with ${uc.atoms.length.toLocaleString(LOCALE)} atoms`,
    uc.truncated ? ` (the first ${MAX_ATOMS.toLocaleString(LOCALE)} only)` : '',
    links.length ? ` and ${links.length.toLocaleString(LOCALE)} bonds` : '',
    '. Drag to turn it, or use the arrow keys; + and − zoom, and the wheel once it is selected (or with Ctrl).',
  );
  // Drawn again at its new size when the window (or the text size) changes the canvas's width.
  // The observer ends with the page (`life`): it held the canvas and its atoms after the page was left.
  if (typeof ResizeObserver === 'function' && !life?.aborted) {
    const watcher = new ResizeObserver(redraw);
    watcher.observe(canvas);
    life?.addEventListener('abort', () => watcher.disconnect(), { once: true });
  }
  // The canvas is painted from the palette's colours: a new theme, Light or Dark, or High Contrast
  // paints it again. The listeners end with the page (`life`); without one they are not set, as
  // nothing would ever release them.
  if (theme && life && !life.aborted) {
    const off = theme.onChange(redraw);
    life.addEventListener('abort', off, { once: true });
  }
  // Paper is light whatever the palette (themes.css): the canvas is a picture, so it is painted
  // again, at once, when the page turns to print and when it turns back.
  const paper = globalThis.matchMedia?.('print');
  if (paper?.addEventListener && life && !life.aborted) {
    paper.addEventListener('change', draw);
    life.addEventListener('abort', () => paper.removeEventListener('change', draw), {
      once: true,
    });
  }
  redraw();
  return h(
    'figure',
    { class: 'crystal-viewer' },
    canvas,
    controls,
    legend(uc.atoms),
    h('figcaption', null, note),
  );
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
  life?: AbortSignal,
  theme?: PaletteWatch,
): HTMLElement {
  const block = parseCif(st.cif);
  const uc = block ? unitCell(block) : null;
  return h(
    'div',
    { class: 'crystal' },
    summaryTable(st.summary, doc.source_id, doc.native_id),
    uc?.atoms.length
      ? structureViewer(uc, doc.title, life, theme)
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
