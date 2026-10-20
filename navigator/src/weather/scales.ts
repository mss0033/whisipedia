/**
 * Colour scales and display units of the weather fields (docs/components/weather.md).
 *
 * Values arrive in the API's units (K, mm, Pa, m/s, %). A scale's stops are in those units; what
 * the reader sees is converted by `display` to metric or US units.
 */

export type Units = 'metric' | 'us';
type Rgba = readonly [number, number, number, number];
interface Stop {
  v: number;
  c: Rgba;
}

const C = (deg: number): number => deg + 273.15;
const hex = (h: string, a = 255): Rgba => [
  Number.parseInt(h.slice(1, 3), 16),
  Number.parseInt(h.slice(3, 5), 16),
  Number.parseInt(h.slice(5, 7), 16),
  a,
];

const TEMPERATURE: Stop[] = [
  { v: C(-45), c: hex('#4b1460') },
  { v: C(-30), c: hex('#3b2f98') },
  { v: C(-20), c: hex('#2a5cbf') },
  { v: C(-10), c: hex('#3a9bd8') },
  { v: C(0), c: hex('#9fdcec') },
  { v: C(5), c: hex('#c9eecb') },
  { v: C(10), c: hex('#a1d76d') },
  { v: C(15), c: hex('#e8e35b') },
  { v: C(20), c: hex('#f5b23c') },
  { v: C(25), c: hex('#ef7b2b') },
  { v: C(30), c: hex('#d8422b') },
  { v: C(35), c: hex('#a3172e') },
  { v: C(45), c: hex('#5a0a24') },
];
const PRECIPITATION: Stop[] = [
  { v: 0.05, c: hex('#a6d9f4', 0) },
  { v: 0.2, c: hex('#a6d9f4', 150) },
  { v: 1, c: hex('#56a8e3', 200) },
  { v: 3, c: hex('#2367c6', 220) },
  { v: 7, c: hex('#2b3aa2', 230) },
  { v: 15, c: hex('#6b2cb4', 240) },
  { v: 30, c: hex('#b52ac3', 245) },
  { v: 60, c: hex('#e8418f', 250) },
  { v: 120, c: hex('#ffa05e', 255) },
];
const PRESSURE: Stop[] = [
  { v: 95000, c: hex('#3b0f70') },
  { v: 98000, c: hex('#3e4a89') },
  { v: 100000, c: hex('#26828e') },
  { v: 101300, c: hex('#35b779') },
  { v: 102500, c: hex('#a5db36') },
  { v: 104000, c: hex('#f1e51d') },
  { v: 105500, c: hex('#fca636') },
];
const SPEED: Stop[] = [
  { v: 0, c: hex('#2b4c7e', 120) },
  { v: 2, c: hex('#2f7fb5', 170) },
  { v: 5, c: hex('#2db3a3', 200) },
  { v: 8, c: hex('#7ccd4f', 215) },
  { v: 11, c: hex('#f0d33c', 225) },
  { v: 14, c: hex('#f19b30', 235) },
  { v: 17, c: hex('#e2532b', 240) },
  { v: 21, c: hex('#b3203c', 245) },
  { v: 25, c: hex('#7c1d6e', 250) },
  { v: 33, c: hex('#4a0f4f', 255) },
];
const CLOUD: Stop[] = [
  { v: 5, c: [255, 255, 255, 0] },
  { v: 50, c: [235, 238, 242, 120] },
  { v: 100, c: [250, 251, 252, 225] },
];

export interface Scale {
  stops: Stop[];
  min: number;
  max: number;
  /** Values for the legend's ticks (API units). */
  ticks: number[];
}

function scale(stops: Stop[], ticks: number[]): Scale {
  const first = stops[0];
  const last = stops[stops.length - 1];
  return { stops, min: first ? first.v : 0, max: last ? last.v : 1, ticks };
}

export const SCALES: Record<string, Scale> = {
  temperature: scale(TEMPERATURE, [-30, -15, 0, 10, 20, 30, 40].map(C)),
  precipitation: scale(PRECIPITATION, [0.2, 1, 3, 7, 15, 30, 60]),
  pressure: scale(PRESSURE, [96000, 98000, 100000, 101300, 103000, 105000]),
  speed: scale(SPEED, [0, 5, 10, 15, 20, 30]),
  percent: scale(CLOUD, [10, 50, 100]),
};

const LUT_SIZE = 1024;
const luts = new Map<string, Uint8ClampedArray>();

/** RGBA for LUT_SIZE evenly spaced values from the scale's min to its max. */
export function lut(kind: string): Uint8ClampedArray {
  const hit = luts.get(kind);
  if (hit) return hit;
  const sc = SCALES[kind] ?? SCALES.temperature;
  if (!sc) throw new Error('no temperature scale');
  const out = new Uint8ClampedArray(LUT_SIZE * 4);
  for (let i = 0; i < LUT_SIZE; i++) {
    const v = sc.min + ((sc.max - sc.min) * i) / (LUT_SIZE - 1);
    const c = colorAt(sc.stops, v);
    out.set(c, i * 4);
  }
  luts.set(kind, out);
  return out;
}

export function lutIndex(kind: string, v: number): number {
  const sc = SCALES[kind] ?? SCALES.temperature;
  if (!sc) return 0;
  const t = (v - sc.min) / (sc.max - sc.min);
  return Math.max(0, Math.min(LUT_SIZE - 1, Math.round(t * (LUT_SIZE - 1))));
}

function colorAt(stops: Stop[], v: number): Rgba {
  const first = stops[0];
  const last = stops[stops.length - 1];
  if (!first || !last) return [0, 0, 0, 0];
  if (v <= first.v) return first.c;
  if (v >= last.v) return last.c;
  for (let i = 1; i < stops.length; i++) {
    const a = stops[i - 1];
    const b = stops[i];
    if (!a || !b || v > b.v) continue;
    const t = (v - a.v) / (b.v - a.v);
    return [
      a.c[0] + (b.c[0] - a.c[0]) * t,
      a.c[1] + (b.c[1] - a.c[1]) * t,
      a.c[2] + (b.c[2] - a.c[2]) * t,
      a.c[3] + (b.c[3] - a.c[3]) * t,
    ];
  }
  return last.c;
}

/** Where a value sits on the legend (0..1): each pair of colour stops gets an equal share, so the
 * many small precipitation amounts are as readable as the rare large ones. */
export function legendPosition(kind: string, v: number): number {
  const stops = (SCALES[kind] ?? SCALES.temperature)?.stops ?? [];
  const last = stops.length - 1;
  if (last < 1) return 0;
  for (let i = 1; i <= last; i++) {
    const a = stops[i - 1];
    const b = stops[i];
    if (!a || !b || v > b.v) continue;
    const t = Math.max(0, (v - a.v) / (b.v - a.v));
    return (i - 1 + t) / last;
  }
  return 1;
}

/** The legend's gradient: each colour stop at its position. */
export function legendStops(kind: string): { offset: number; color: string }[] {
  const stops = (SCALES[kind] ?? SCALES.temperature)?.stops ?? [];
  return stops.map((st, i) => ({
    offset: stops.length > 1 ? i / (stops.length - 1) : 0,
    color: `rgba(${st.c[0]}, ${st.c[1]}, ${st.c[2]}, ${(st.c[3] / 255).toFixed(2)})`,
  }));
}

/** CSS colour of a value (legend swatches). */
export function cssColor(kind: string, v: number): string {
  const sc = SCALES[kind] ?? SCALES.temperature;
  const [r, g, b, a] = sc ? colorAt(sc.stops, v) : [0, 0, 0, 0];
  return `rgba(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)}, ${(a / 255).toFixed(2)})`;
}

/** A value in the reader's units. */
export function display(kind: string, v: number, units: Units): { value: number; unit: string } {
  switch (kind) {
    case 'temperature':
      return units === 'us'
        ? { value: ((v - 273.15) * 9) / 5 + 32, unit: '°F' }
        : { value: v - 273.15, unit: '°C' };
    case 'precipitation':
      return units === 'us' ? { value: v / 25.4, unit: 'in' } : { value: v, unit: 'mm' };
    case 'pressure':
      return { value: v / 100, unit: 'hPa' };
    case 'speed':
    case 'component':
      return units === 'us'
        ? { value: v * 2.23694, unit: 'mph' }
        : { value: v * 3.6, unit: 'km/h' };
    case 'percent':
      return { value: v, unit: '%' };
    default:
      return { value: v, unit: '' };
  }
}

/** "12.3 °C", rounded to what the unit deserves. */
export function formatValue(kind: string, v: number | null | undefined, units: Units): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const d = display(kind, v, units);
  const digits =
    d.unit === 'in' ? 2 : d.unit === 'mm' ? 1 : d.unit === '%' || d.unit === 'hPa' ? 0 : 1;
  return `${d.value.toFixed(digits)} ${d.unit}`;
}
