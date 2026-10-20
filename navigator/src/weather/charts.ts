/**
 * Small SVG time-series charts for wsp://weather: lines, bars and a low-high band over a shared time
 * axis, with a readout of the values nearest the pointer. Colours come from CSS classes (`wx-c-N`,
 * `wx-normal`): no inline styles under the CSP.
 */
import { h, s } from '../core/dom.ts';

export interface Series {
  label: string;
  /** Colour class: `wx-c-0`..`wx-c-5`, `wx-normal`. */
  cls: string;
  kind: 'line' | 'bar' | 'band';
  times: number[];
  /** In display units; for a band, the high side. */
  values: (number | null)[];
  /** A band's low side. */
  low?: (number | null)[];
  dashed?: boolean;
}

export interface ChartOptions {
  title: string;
  unit: string;
  series: Series[];
  /** Include zero on the value axis (precipitation, wind, cloud). */
  zero?: boolean;
  /** A time to mark (now). */
  now?: number;
  digits?: number;
  timeLabel: (ms: number) => string;
}

const W = 640;
const H = 170;
const PAD = { left: 44, right: 10, top: 10, bottom: 22 };
const DAY = 86_400_000;

function niceStep(span: number, target: number): number {
  const raw = span / Math.max(1, target);
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  for (const m of [1, 2, 2.5, 5, 10]) if (raw <= m * mag) return m * mag;
  return 10 * mag;
}

const TIME_STEPS = [3, 6, 12, 24, 48, 168].map((h) => h * 3_600_000);

function timeTicks(t0: number, t1: number): { ticks: number[]; label: (ms: number) => string } {
  const span = t1 - t0;
  const step = TIME_STEPS.find((st) => span / st <= 8);
  if (step) {
    const first = new Date(t0);
    first.setHours(
      step >= DAY ? 0 : first.getHours() - (first.getHours() % (step / 3_600_000)),
      0,
      0,
      0,
    );
    const ticks: number[] = [];
    for (let t = first.getTime(); t <= t1; t += step) if (t >= t0) ticks.push(t);
    const fmt = new Intl.DateTimeFormat(
      undefined,
      step >= DAY ? { weekday: 'short', day: 'numeric' } : { hour: 'numeric' },
    );
    return { ticks, label: (ms) => fmt.format(ms) };
  }
  const years = span / (365.25 * DAY);
  if (years < 1.5) {
    const ticks: number[] = [];
    const d = new Date(t0);
    d.setDate(1);
    d.setHours(0, 0, 0, 0);
    const every = years < 0.6 ? 1 : 2;
    while (d.getTime() <= t1) {
      if (d.getTime() >= t0) ticks.push(d.getTime());
      d.setMonth(d.getMonth() + every);
    }
    const fmt = new Intl.DateTimeFormat(undefined, { month: 'short' });
    return { ticks, label: (ms) => fmt.format(ms) };
  }
  const every = niceStep(years, 7);
  const ticks: number[] = [];
  const y0 = Math.ceil(new Date(t0).getFullYear() / every) * every;
  for (let y = y0; y <= new Date(t1).getFullYear(); y += every)
    ticks.push(new Date(y, 0, 1).getTime());
  return { ticks, label: (ms) => String(new Date(ms).getFullYear()) };
}

/** A chart with its legend and readout. */
export function timeChart(opts: ChartOptions): HTMLElement {
  const all = opts.series.flatMap((se) => [
    ...se.values.filter((v): v is number => v !== null && Number.isFinite(v)),
    ...(se.low ?? []).filter((v): v is number => v !== null && Number.isFinite(v)),
  ]);
  const times = opts.series.flatMap((se) => se.times);
  const fig = h(
    'figure',
    { class: 'wx-chart' },
    h('figcaption', null, `${opts.title} (${opts.unit})`),
  );
  if (!all.length || !times.length) {
    fig.append(h('p', { class: 'wx-chart-empty' }, 'No values for this range.'));
    return fig;
  }
  let lo = Math.min(...all);
  let hi = Math.max(...all);
  if (opts.zero) lo = Math.min(lo, 0);
  if (hi - lo < 1e-9) hi = lo + 1;
  const step = niceStep(hi - lo, 4);
  lo = Math.floor(lo / step) * step;
  hi = Math.ceil(hi / step) * step;
  const t0 = Math.min(...times);
  const t1 = Math.max(...times);
  const tspan = Math.max(t1 - t0, 1);
  const x = (t: number): number => PAD.left + ((t - t0) / tspan) * (W - PAD.left - PAD.right);
  const y = (v: number): number =>
    PAD.top + (1 - (v - lo) / (hi - lo)) * (H - PAD.top - PAD.bottom);
  const svg = s('svg', {
    viewBox: `0 0 ${W} ${H}`,
    class: 'wx-chart-svg',
    role: 'img',
    'aria-label': `${opts.title}: ${opts.series.map((se) => se.label).join(', ')}`,
  });
  for (let v = lo; v <= hi + step / 2; v += step) {
    svg.append(
      s('line', { x1: PAD.left, x2: W - PAD.right, y1: y(v), y2: y(v), class: 'wx-grid' }),
      s('text', { x: PAD.left - 4, y: y(v) + 4, class: 'wx-tick wx-tick-y' }, fmtNum(v, step)),
    );
  }
  const tt = timeTicks(t0, t1);
  for (const t of tt.ticks) {
    svg.append(
      s('line', {
        x1: x(t),
        x2: x(t),
        y1: PAD.top,
        y2: H - PAD.bottom,
        class: 'wx-grid wx-grid-t',
      }),
      s('text', { x: x(t), y: H - 6, class: 'wx-tick wx-tick-x' }, tt.label(t)),
    );
  }
  if (opts.now !== undefined && opts.now >= t0 && opts.now <= t1)
    svg.append(
      s('line', {
        x1: x(opts.now),
        x2: x(opts.now),
        y1: PAD.top,
        y2: H - PAD.bottom,
        class: 'wx-now',
      }),
    );
  for (const se of opts.series) {
    if (se.kind === 'bar') {
      const gaps = se.times.slice(1).map((t, i) => t - (se.times[i] ?? t));
      const width = Math.max(
        1,
        (Math.min(...gaps, tspan) / tspan) * (W - PAD.left - PAD.right) * 0.8,
      );
      const g = s('g', { class: `wx-bars ${se.cls}` });
      se.values.forEach((v, i) => {
        const t = se.times[i];
        if (v === null || t === undefined || !Number.isFinite(v) || v <= 0) return;
        g.append(s('rect', { x: x(t) - width, y: y(v), width, height: Math.max(0, y(lo) - y(v)) }));
      });
      svg.append(g);
    } else if (se.kind === 'band' && se.low) {
      // One polygon per run of days with both a low and a high: a gap breaks the band, as it breaks a
      // line (a station that reports highs only now and then drew one wedge across months).
      let top: string[] = [];
      let bottom: string[] = [];
      const flush = (): void => {
        if (top.length > 1)
          svg.append(
            s('polygon', { points: [...top, ...bottom].join(' '), class: `wx-band ${se.cls}` }),
          );
        top = [];
        bottom = [];
      };
      se.values.forEach((v, i) => {
        const l = se.low?.[i];
        const t = se.times[i];
        if (v === null || l === null || l === undefined || t === undefined) {
          flush();
          return;
        }
        top.push(`${x(t).toFixed(1)},${y(v).toFixed(1)}`);
        bottom.unshift(`${x(t).toFixed(1)},${y(l).toFixed(1)}`);
      });
      flush();
    } else {
      let d = '';
      let pen = false;
      se.values.forEach((v, i) => {
        const t = se.times[i];
        if (v === null || t === undefined || !Number.isFinite(v)) {
          pen = false;
          return;
        }
        d += `${pen ? 'L' : 'M'}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
        pen = true;
      });
      if (d)
        svg.append(s('path', { d, class: `wx-line ${se.cls}${se.dashed ? ' wx-dashed' : ''}` }));
    }
  }
  const cursor = s('line', {
    x1: 0,
    x2: 0,
    y1: PAD.top,
    y2: H - PAD.bottom,
    class: 'wx-cursor',
    visibility: 'hidden',
  });
  svg.append(cursor);
  const readout = h('p', { class: 'wx-readout', 'aria-live': 'polite' }, ' ');
  const digits = opts.digits ?? 1;
  const show = (t: number): void => {
    cursor.setAttribute('x1', String(x(t)));
    cursor.setAttribute('x2', String(x(t)));
    cursor.setAttribute('visibility', 'visible');
    const parts = opts.series
      .filter((se) => se.kind !== 'band' || se.low)
      .map((se) => {
        const i = nearest(se.times, t);
        const v = i < 0 ? null : se.values[i];
        if (v === null || v === undefined || !Number.isFinite(v)) return null;
        const l = se.kind === 'band' ? se.low?.[i] : undefined;
        const text =
          l !== undefined && l !== null
            ? `${l.toFixed(digits)}–${v.toFixed(digits)}`
            : v.toFixed(digits);
        return h('span', { class: `wx-key ${se.cls}` }, `${se.label} ${text}`);
      })
      .filter((p): p is HTMLElement => p !== null);
    readout.replaceChildren(
      h('b', null, opts.timeLabel(t)),
      ' ',
      ...parts.flatMap((p) => [p, ' ']),
    );
  };
  svg.addEventListener('pointermove', (ev: PointerEvent) => {
    const box = svg.getBoundingClientRect();
    const vx = ((ev.clientX - box.left) / box.width) * W;
    const t = t0 + ((vx - PAD.left) / (W - PAD.left - PAD.right)) * tspan;
    show(Math.max(t0, Math.min(t1, t)));
  });
  svg.addEventListener('pointerleave', () => cursor.setAttribute('visibility', 'hidden'));
  const legend = h(
    'p',
    { class: 'wx-legend-keys' },
    ...opts.series.map((se) =>
      h('span', { class: `wx-key ${se.cls}${se.dashed ? ' wx-key-dashed' : ''}` }, se.label),
    ),
  );
  fig.append(svg, legend, readout);
  return fig;
}

function nearest(times: number[], t: number): number {
  let best = -1;
  let gap = Number.POSITIVE_INFINITY;
  times.forEach((ti, i) => {
    const g = Math.abs(ti - t);
    if (g < gap) {
      gap = g;
      best = i;
    }
  });
  return best;
}

function fmtNum(v: number, step: number): string {
  const digits = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
  return v.toFixed(digits);
}
