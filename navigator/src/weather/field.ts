/**
 * A weather field: 1440 x 721 values on the 0.25° grid (90°N to 90°S, 0° to 359.75°E), sent by the
 * API as a PNG whose red and green bytes hold a 16-bit code (docs/components/weather.md §3).
 * Drawn by reprojecting it onto a Web Mercator canvas the map shows as a canvas source.
 */
import { lut, lutIndex } from './scales.ts';

export const NI = 1440;
export const NJ = 721;
const MISSING = 65535;
export const MERCATOR_LAT = 85.051129;
/** Height of the Mercator canvas: square, like the world in Web Mercator. */
export const CANVAS_SIZE = 1440;

export interface Coding {
  offset: number;
  scale: number;
}

/** Values (NaN: missing), row-major from the north-west corner at 0°E. */
export type Field = Float32Array;

export async function loadField(url: string, coding: Coding, signal?: AbortSignal): Promise<Field> {
  const res = await fetch(url, signal ? { signal } : {});
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url}`);
  const bmp = await createImageBitmap(await res.blob(), {
    colorSpaceConversion: 'none',
    premultiplyAlpha: 'none',
  });
  if (bmp.width !== NI || bmp.height !== NJ) throw new Error(`field ${bmp.width}x${bmp.height}`);
  const canvas = document.createElement('canvas');
  canvas.width = NI;
  canvas.height = NJ;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) throw new Error('no 2D canvas');
  ctx.drawImage(bmp, 0, 0);
  bmp.close();
  const px = ctx.getImageData(0, 0, NI, NJ).data;
  const out = new Float32Array(NI * NJ);
  for (let i = 0, p = 0; i < out.length; i++, p += 4) {
    const n = ((px[p] ?? 0) << 8) | (px[p + 1] ?? 0);
    out[i] = n === MISSING ? Number.NaN : coding.offset + coding.scale * n;
  }
  return out;
}

/** Wind speed from its eastward and northward components. */
export function speedOf(u: Field, v: Field): Field {
  const out = new Float32Array(u.length);
  for (let i = 0; i < u.length; i++) out[i] = Math.hypot(u[i] ?? Number.NaN, v[i] ?? Number.NaN);
  return out;
}

export function gridIndex(lat: number, lon: number): number {
  const row = Math.min(NJ - 1, Math.max(0, Math.round((90 - lat) / 0.25)));
  const col = ((Math.round((((lon % 360) + 360) % 360) / 0.25) % NI) + NI) % NI;
  return row * NI + col;
}

export function valueAt(field: Field, lat: number, lon: number): number {
  return field[gridIndex(lat, lon)] ?? Number.NaN;
}

// For each canvas row: the two grid rows around its latitude and the weight of the second.
let rowTable: { r0: Int32Array; r1: Int32Array; t: Float32Array } | null = null;

function rows(): { r0: Int32Array; r1: Int32Array; t: Float32Array } {
  if (rowTable) return rowTable;
  const r0 = new Int32Array(CANVAS_SIZE);
  const r1 = new Int32Array(CANVAS_SIZE);
  const t = new Float32Array(CANVAS_SIZE);
  for (let y = 0; y < CANVAS_SIZE; y++) {
    const m = Math.PI * (1 - (2 * (y + 0.5)) / CANVAS_SIZE);
    const lat = (Math.atan(Math.sinh(m)) * 180) / Math.PI;
    const r = (90 - lat) / 0.25;
    const lo = Math.max(0, Math.min(NJ - 1, Math.floor(r)));
    r0[y] = lo;
    r1[y] = Math.min(NJ - 1, lo + 1);
    t[y] = r - lo;
  }
  rowTable = { r0, r1, t };
  return rowTable;
}

/** Paint `field` coloured by `kind`'s scale onto `canvas` (Web Mercator, -180..180°). */
export function paint(canvas: HTMLCanvasElement, field: Field, kind: string): void {
  canvas.width = CANVAS_SIZE;
  canvas.height = CANVAS_SIZE;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const img = ctx.createImageData(CANVAS_SIZE, CANVAS_SIZE);
  const out = img.data;
  const table = lut(kind);
  const { r0, r1, t } = rows();
  const half = NI / 2;
  for (let y = 0; y < CANVAS_SIZE; y++) {
    const a = (r0[y] ?? 0) * NI;
    const b = (r1[y] ?? 0) * NI;
    const w = t[y] ?? 0;
    for (let x = 0; x < CANVAS_SIZE; x++) {
      const col = (x + half) % NI; // canvas x 0 is 180°W, grid column 0 is 0°E
      const va = field[a + col] ?? Number.NaN;
      const vb = field[b + col] ?? Number.NaN;
      const v = Number.isNaN(va) ? vb : Number.isNaN(vb) ? va : va + (vb - va) * w;
      const o = (y * CANVAS_SIZE + x) * 4;
      if (Number.isNaN(v)) {
        out[o + 3] = 0;
        continue;
      }
      const i = lutIndex(kind, v) * 4;
      out[o] = table[i] ?? 0;
      out[o + 1] = table[i + 1] ?? 0;
      out[o + 2] = table[i + 2] ?? 0;
      out[o + 3] = table[i + 3] ?? 0;
    }
  }
  ctx.putImageData(img, 0, 0);
}

/** The canvas's corners for MapLibre: top left, top right, bottom right, bottom left. */
export const CANVAS_CORNERS: [
  [number, number],
  [number, number],
  [number, number],
  [number, number],
] = [
  [-180, MERCATOR_LAT],
  [180, MERCATOR_LAT],
  [180, -MERCATOR_LAT],
  [-180, -MERCATOR_LAT],
];
