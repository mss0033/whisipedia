import { describe, expect, it } from 'vitest';
import { resolveRoute } from '../src/core/router.ts';
import { age, availabilityBadge } from '../src/ui/components.ts';
import { timeChart } from '../src/weather/charts.ts';
import { gridIndex, NI, valueAt } from '../src/weather/field.ts';
import { parseWeather, weatherAddress } from '../src/weather/route.ts';
import { display, formatValue, legendPosition } from '../src/weather/scales.ts';

describe('wsp://weather addresses', () => {
  it('round-trip what the page shows', () => {
    const r = parseWeather('', {
      var: 'tp',
      model: 'ifs',
      step: '48',
      at: '51.5,-0.12',
      place: 'London',
    });
    expect(r).toEqual({
      tab: 'forecast',
      model: 'ifs',
      variable: 'tp',
      step: 48,
      date: undefined,
      at: { lat: 51.5, lon: -0.12 },
      place: 'London',
    });
    expect(weatherAddress(r)).toBe(
      'wsp://weather?var=tp&model=ifs&step=48&at=51.500,-0.120&place=London',
    );
  });

  it('keep the past on its own path and drop what does not apply', () => {
    const r = parseWeather('past', { date: '2021-07-14', var: 't2m_max', step: '6', model: 'gfs' });
    expect(r.tab).toBe('past');
    expect(weatherAddress(r)).toBe('wsp://weather/past?var=t2m_max&date=2021-07-14');
  });

  it('ignore junk', () => {
    const r = parseWeather('', {
      at: '95,10',
      step: '-3',
      date: '2021-7-1',
      model: 'G F S',
      var: '<b>',
    });
    expect([r.at, r.step, r.date, r.model, r.variable]).toEqual([
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
    ]);
  });

  it('are routed, with forecast and climate as aliases', () => {
    expect(resolveRoute('wsp://weather/past?date=2020-01-01').page).toBe('weather');
  });
});

describe('weather units and scales', () => {
  it('convert to what the reader uses', () => {
    expect(display('temperature', 273.15, 'metric')).toEqual({ value: 0, unit: '°C' });
    expect(display('temperature', 273.15, 'us').value).toBeCloseTo(32);
    expect(display('speed', 10, 'metric')).toEqual({ value: 36, unit: 'km/h' });
    expect(display('pressure', 101325, 'us')).toEqual({ value: 1013.25, unit: 'hPa' });
    expect(formatValue('precipitation', 25.4, 'us')).toBe('1.00 in');
    expect(formatValue('temperature', Number.NaN, 'metric')).toBe('—');
  });

  it('give small precipitation amounts room on the legend', () => {
    const p1 = legendPosition('precipitation', 1);
    const p60 = legendPosition('precipitation', 60);
    expect(p1).toBeGreaterThan(0.2); // linear would put 1 mm of 120 at 0.008
    expect(p60).toBeLessThan(1);
    expect(legendPosition('precipitation', 1e6)).toBe(1);
  });
});

describe('fields', () => {
  it('index the 0.25° grid from the north-west corner at 0°E', () => {
    expect(gridIndex(90, 0)).toBe(0);
    expect(gridIndex(89.75, 0.25)).toBe(NI + 1);
    expect(gridIndex(0, -0.25)).toBe(360 * NI + NI - 1);
    const f = new Float32Array(NI * 721);
    f[gridIndex(51.5, -0.12)] = 7;
    expect(valueAt(f, 51.5, -0.12)).toBe(7);
  });
});

describe('availability badges', () => {
  it('name the three kinds and the age of a kept copy', () => {
    const now = Date.parse('2026-10-20T14:00:00Z');
    expect(age('2026-10-20T11:00:00Z', now)).toBe('3 h');
    expect(age('2026-10-17T14:00:00Z', now)).toBe('3 days');
    const kept = availabilityBadge({
      kind: 'kept',
      label: 'Kept copy',
      fetched_at: new Date(Date.now() - 2 * 3_600_000).toISOString(),
      stale: true,
      note: 'Newer runs should exist.',
    });
    expect(kept.textContent).toBe('Kept copy · 2 h old · out of date');
    expect(kept.className).toContain('badge-avail-stale');
    expect(kept.title).toContain('works offline');
    const offline = availabilityBadge({ kind: 'offline', label: 'In the library' });
    expect(offline.textContent).toBe('In the library');
    expect(availabilityBadge({ kind: 'live', label: 'Needs a connection' }).title).toContain(
      'needs a connection',
    );
  });
});

describe('charts', () => {
  it('break a low-to-high band where a day is missing', () => {
    const fig = timeChart({
      title: 'Measured temperature',
      unit: '°C',
      series: [
        {
          label: 'Low to high',
          cls: 'wx-c-1',
          kind: 'band',
          times: [0, 1, 2, 3, 4, 5].map((d) => d * 86_400_000),
          values: [10, 11, null, 13, 14, 15],
          low: [1, 2, null, 3, 4, 5],
        },
      ],
      timeLabel: String,
    });
    expect(fig.querySelectorAll('polygon.wx-band')).toHaveLength(2);
  });
});
