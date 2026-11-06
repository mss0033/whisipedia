// Crystal structures (COD, owner request 2026-11-05): the summary, the drawing of the cell, the CIF.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  bonds,
  cartesian,
  cifNumber,
  column,
  elementOf,
  MAX_ATOMS,
  parseCif,
  parseSymop,
  splitStructure,
  structureView,
  unitCell,
} from '../src/pages/crystal.ts';

const CIF = `#------------------------------------------------------------------------------
data_1000000
loop_
_publ_author_name
'K\\"ohler, J.'
"O'Neil, M."
_publ_section_title
;
 The crystal structure of a test
;
_cell_length_a                   2.0(1)
_cell_length_b                   2.0
_cell_length_c                   2.0
_cell_angle_alpha                90
_cell_angle_beta                 90
_cell_angle_gamma                90
_cell_measurement_temperature    ?
loop_
_space_group_symop_id
_space_group_symop_operation_xyz
1 x,y,z
2 '-x, -y, -z'
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
Na1 Na+ 0 0 0
Cl1 Cl- 0.25 0.25 0.25 # a comment after a row
data_second_block
_cell_length_a 9
`;

const SUMMARY = [
  'Test salt (COD 1000000)',
  'Crystal structure 1000000 of the Crystallography Open Database.',
  'Name: Test salt',
  'Formula: Cl Na',
  'DOI: 10.1107/S0000',
  'From the archive: COD full release (cif/1/00/00/1000000.cif)',
].join('\n');

const TEXT = `${SUMMARY}\n\nCrystallographic Information File 1000000.cif\n${CIF}`;

beforeEach(() => {
  // No frame is drawn unless a test asks (jsdom has no 2-D canvas).
  vi.spyOn(globalThis, 'requestAnimationFrame').mockImplementation(() => 0);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('a structure document', () => {
  it('is its summary and then its CIF', () => {
    const st = splitStructure(TEXT)!;
    expect(st.summary).toBe(SUMMARY);
    expect(st.cif).toBe(CIF);
    expect(st.fileName).toBe('1000000.cif');
    expect(splitStructure('An ordinary page\n\nwith text')).toBeNull();
    expect(splitStructure(null)).toBeNull();
  });

  it('reads the first data block of a CIF', () => {
    const block = parseCif(CIF)!;
    expect(column(block, '_publ_author_name')).toEqual(['K\\"ohler, J.', "O'Neil, M."]);
    expect(block.items.get('_publ_section_title')).toBe(' The crystal structure of a test');
    expect(block.items.get('_cell_length_a')).toBe('2.0(1)');
    expect(cifNumber(block.items.get('_cell_length_a'))).toBe(2);
    expect(cifNumber(block.items.get('_cell_measurement_temperature'))).toBeNull();
    expect(column(block, '_atom_site_fract_x')).toEqual(['0', '0.25']); // the comment is no value
    expect(column(block, '_space_group_symop_operation_xyz')).toEqual(['x,y,z', '-x, -y, -z']);
    expect(parseCif('no block here')).toBeNull();
  });

  it('reads symmetry operations', () => {
    expect(parseSymop('-y,x-y,z+2/3')).toEqual([
      [0, -1, 0, 0],
      [1, -1, 0, 0],
      [0, 0, 1, 2 / 3],
    ]);
    expect(parseSymop('1/2+X, -x+y, -z+0.25')).toEqual([
      [1, 0, 0, 0.5],
      [-1, 1, 0, 0],
      [0, 0, -1, 0.25],
    ]);
    expect(parseSymop('x,y')).toBeNull();
    expect(parseSymop('x,y,q')).toBeNull();
  });

  it('names the element of a type symbol or a label', () => {
    expect(['O2-', 'Fe3+', 'CL1', 'SiA', 'OW1', 'D1', 'C12', 'Xx'].map(elementOf)).toEqual([
      'O',
      'Fe',
      'Cl',
      'Si',
      'O',
      'H',
      'C',
      '?',
    ]);
  });

  it('fills the cell from the asymmetric unit and its symmetry', () => {
    const uc = unitCell(parseCif(CIF)!)!;
    expect(uc.cell).toEqual({ a: 2, b: 2, c: 2, alpha: 90, beta: 90, gamma: 90 });
    const na = uc.atoms.filter((a) => a.element === 'Na');
    const cl = uc.atoms.filter((a) => a.element === 'Cl');
    expect(na).toHaveLength(8); // the site at the origin, on each of the cell's corners
    expect(cl.map((a) => a.frac)).toEqual([
      [0.25, 0.25, 0.25],
      [0.75, 0.75, 0.75], // the inversion's image, folded into the cell
    ]);
    expect(uc.truncated).toBe(false);
    const few = unitCell(parseCif(CIF)!, 3)!;
    expect(few.atoms).toHaveLength(3);
    expect(few.truncated).toBe(true);
    expect(MAX_ATOMS).toBeGreaterThan(1000);
    expect(unitCell(parseCif('data_x\n_cell_length_a 1\n')!)).toBeNull(); // nothing to draw
  });

  it('turns fractional coordinates into ångströms and finds bonds', () => {
    const cubic = cartesian({ a: 2, b: 2, c: 2, alpha: 90, beta: 90, gamma: 90 });
    expect(cubic([1, 0.5, 0.25]).map((v) => +v.toFixed(6))).toEqual([2, 1, 0.5]);
    const hex = cartesian({ a: 1, b: 1, c: 3, alpha: 90, beta: 90, gamma: 120 });
    expect(hex([0, 1, 0]).map((v) => +v.toFixed(6))).toEqual([
      -0.5,
      +(Math.sqrt(3) / 2).toFixed(6),
      0,
    ]);
    const atoms = ['C', 'C', 'C'].map((element) => ({
      label: element,
      element,
      frac: [0, 0, 0] as [number, number, number],
    }));
    expect(
      bonds(atoms, [
        [0, 0, 0],
        [1.5, 0, 0],
        [4.5, 0, 0],
      ]),
    ).toEqual([[0, 1]]);
  });

  it('shows the facts, the drawing, the download and the file', () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    const st = splitStructure(TEXT)!;
    const el = structureView(
      {
        title: 'Test salt (COD 1000000)',
        source_id: 'cod-full-archive',
        native_id: 'cod:cod-cifs-mysql/1000000',
      },
      st,
    );
    const rows = [...el.querySelectorAll('.crystal-facts tr')].map((tr) => [
      tr.querySelector('th')?.textContent,
      tr.querySelector('td')?.textContent,
    ]);
    expect(rows[0]).toEqual(['Name', 'Test salt']);
    expect(el.querySelector('.crystal-summary > p')?.textContent).toContain(
      'Crystal structure 1000000',
    );
    const back = el.querySelector<HTMLAnchorElement>('.crystal-facts a')!;
    expect(back.dataset.wsp).toBe('wsp://cod-full-archive/cod:cod-cifs-mysql');
    expect(back.textContent).toBe('COD full release (cif/1/00/00/1000000.cif)');
    const canvas = el.querySelector('canvas.crystal-canvas')!;
    expect(canvas.getAttribute('aria-label')).toContain('10 atoms');
    expect(el.querySelector('figcaption')?.textContent).toContain('with 10 atoms');
    expect(el.querySelector('details.crystal-cif pre')?.textContent).toBe(CIF);

    const made: Blob[] = [];
    vi.spyOn(URL, 'createObjectURL').mockImplementation((b) => {
      made.push(b as Blob);
      return 'blob:test/1';
    });
    const clicks: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicks.push(`${this.getAttribute('href')} ${this.getAttribute('download')}`);
    });
    el.querySelector<HTMLButtonElement>('button.crystal-download')!.click();
    expect(clicks).toEqual(['blob:test/1 1000000.cif']);
    expect(made[0]?.type).toBe('chemical/x-cif');
  });

  it('says so when there is nothing to draw, and the document page uses the view', async () => {
    const st = splitStructure(
      `${SUMMARY}\n\nCrystallographic Information File 1000000.cif\ndata_x\n`,
    )!;
    const el = structureView({ title: 'T', source_id: 's', native_id: 'n' }, st);
    expect(el.querySelector('canvas')).toBeNull();
    expect(el.querySelector('.notice')?.textContent).toContain(
      'no cell and fractional coordinates',
    );

    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    const { renderBody } = await import('../src/pages/document.ts');
    const { mockFetch } = await import('./helpers.ts');
    const { WspClient } = await import('../src/api/client.ts');
    const doc = await new WspClient({ fetch: mockFetch() }).document('wsp:gutenberg/84');
    const plain = { ...doc, format: 'text' as const, html: null, text: TEXT, original: null };
    // A book quoting a CIF line is a book (2026-11-06 review): only a COD structure is drawn.
    expect(renderBody(plain).node.querySelector('.crystal')).toBeNull();
    const cod = {
      ...plain,
      source_id: 'cod-full-archive',
      native_id: 'cod:cod-cifs-mysql/1000000',
    };
    expect(renderBody(cod).node.querySelector('.crystal canvas')).not.toBeNull();
    // the CIF line must name the structure the document's id names
    const other = { ...cod, native_id: 'cod:cod-cifs-mysql/1000001' };
    expect(renderBody(other).node.querySelector('.crystal')).toBeNull();
  });

  it('draws the atoms and the cell, and turns and zooms on the keyboard', () => {
    const calls: string[] = [];
    const g = new Proxy(
      {},
      {
        get: (_t, name: string) =>
          name === 'createRadialGradient'
            ? () => ({ addColorStop: () => undefined })
            : (...args: unknown[]) => {
                calls.push(
                  `${name}:${args.map((a) => (typeof a === 'number' ? a.toFixed(1) : a)).join(',')}`,
                );
              },
        set: () => true,
      },
    );
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
      g as CanvasRenderingContext2D,
    );
    vi.spyOn(globalThis, 'requestAnimationFrame').mockImplementation((cb) => {
      cb(0);
      return 0;
    });
    const el = structureView(
      { title: 'Test salt', source_id: 'cod-full-archive', native_id: 'cod:x/1000000' },
      splitStructure(TEXT)!,
    );
    const arcs = () => calls.filter((c) => c.startsWith('arc:'));
    expect(arcs()).toHaveLength(10); // every atom, once
    expect(calls.filter((c) => c.startsWith('fillText:')).map((c) => c.split(',')[0])).toEqual([
      'fillText:a',
      'fillText:b',
      'fillText:c',
    ]);
    const first = arcs();
    calls.length = 0;
    const canvas = el.querySelector('canvas')!;
    const key = new KeyboardEvent('keydown', { key: 'ArrowLeft', cancelable: true, bubbles: true });
    canvas.dispatchEvent(key);
    expect(key.defaultPrevented).toBe(true);
    expect(arcs()).toHaveLength(10);
    expect(arcs()).not.toEqual(first); // turned
    calls.length = 0;
    canvas.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', cancelable: true }));
    expect(calls).toEqual([]); // not its key: left to the app
    el.querySelector<HTMLButtonElement>('button.btn')!.click(); // Zoom in
    expect(arcs()).toHaveLength(10);

    // The wheel scrolls the page past the drawing; it zooms only with the focus or Ctrl (2026-11-06 review).
    document.body.append(el);
    try {
      const wheel = (init: WheelEventInit) => {
        const e = new WheelEvent('wheel', { deltaY: -100, cancelable: true, ...init });
        calls.length = 0;
        canvas.dispatchEvent(e);
        return e.defaultPrevented;
      };
      (document.activeElement as HTMLElement | null)?.blur();
      expect(wheel({})).toBe(false);
      expect(calls).toEqual([]);
      expect(wheel({ ctrlKey: true })).toBe(true);
      expect(arcs()).toHaveLength(10);
      canvas.focus();
      expect(wheel({})).toBe(true);
    } finally {
      el.remove();
    }
  });
});
