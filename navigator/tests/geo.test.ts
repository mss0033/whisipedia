import { describe, expect, it } from 'vitest';
import {
  distance,
  formatArea,
  formatCoords,
  formatDms,
  fromMgrs,
  fromUtm,
  parseCoords,
  polygonArea,
  terrarium,
  toMgrs,
  toUtm,
} from '../src/core/geo.ts';

// [lat, lon, zone, easting, northing, MGRS]: PROJ (pyproj) and the `mgrs` library, 2026-10-18.
const REFERENCE: [number, number, number, number, number, string][] = [
  [38.8895, -77.0353, 18, 323478.063, 4306483.242, '18SUJ2347806483'],
  [46.6863, 7.8632, 32, 413069.912, 5170932.246, '32TMS1306970932'],
  [-33.8568, 151.2153, 56, 334900.57, 6252288.753, '56HLH3490052288'],
  [0.0, 3.0, 31, 500000.0, 0.0, '31NEA0000000000'],
  [64.1466, -21.9426, 27, 454138.377, 7113689.869, '27WVM5413813689'],
  [-54.8019, -68.303, 19, 544805.097, 3927029.885, '19FEV4480527029'],
  [60.0, 5.0, 32, 276979.926, 6658157.202, '32VKM7697958157'],
  [78.2232, 15.6267, 33, 514278.715, 8683355.469, '33XWG1427883355'],
  [35.6586, 139.7454, 54, 386437.603, 3946808.155, '54SUE8643746808'],
  [-0.5, -0.5, 30, 778265.778, 9944681.96, '30MYE7826544681'],
];

describe('UTM and MGRS', () => {
  it.each(REFERENCE)('%f, %f matches PROJ and mgrs', (lat, lon, zone, e, n, mgrs) => {
    const u = toUtm({ lat, lon });
    expect(u?.zone).toBe(zone);
    expect(u?.easting).toBeCloseTo(e, 2);
    expect(u?.northing).toBeCloseTo(n, 2);
    expect(toMgrs({ lat, lon })).toBe(mgrs);
    const back = fromUtm(zone, e, n, lat < 0);
    // Inverse to a centimetre (the series drifts a few millimetres far from the central meridian).
    expect(distance(back, { lat, lon })).toBeLessThan(0.01);
    const sq = fromMgrs(mgrs);
    expect(sq).not.toBeNull();
    // The square's south-west corner: within 1.5 m of the point.
    expect(distance({ lat, lon }, sq ?? { lat: 0, lon: 0 })).toBeLessThan(1.5);
  });

  it('round-trips points all over the UTM area', () => {
    for (let lat = -79.5; lat < 84; lat += 7.3) {
      for (let lon = -179.5; lon < 180; lon += 13.7) {
        const m = toMgrs({ lat, lon });
        expect(m).not.toBeNull();
        const back = fromMgrs(m ?? '');
        expect(distance({ lat, lon }, back ?? { lat: 0, lon: 0 })).toBeLessThan(1.5);
      }
    }
    expect(toUtm({ lat: 85, lon: 0 })).toBeNull(); // UPS, not UTM
  });

  it('reads shorter MGRS precisions and spaces', () => {
    const p = fromMgrs('18S UJ 234 064');
    expect(p).not.toBeNull();
    expect(distance(p ?? { lat: 0, lon: 0 }, { lat: 38.8895, lon: -77.0353 })).toBeLessThan(150);
  });
});

describe('parsing what readers type', () => {
  const near = (text: string, lat: number, lon: number, tol = 1e-4) => {
    const p = parseCoords(text);
    expect(p, text).not.toBeNull();
    expect(p?.lat).toBeCloseTo(lat, -Math.log10(tol));
    expect(p?.lon).toBeCloseTo(lon, -Math.log10(tol));
  };
  it('reads decimal, hemisphere letters, DMS, UTM and MGRS', () => {
    near('46.6863, 7.8632', 46.6863, 7.8632);
    near('-33.8568 151.2153', -33.8568, 151.2153);
    near('38.8895 N, 77.0353 W', 38.8895, -77.0353);
    near('46°41′11″N 7°51′48″E', 46.6864, 7.8633, 1e-3);
    near('38°53\'22"N 77°02\'07"W', 38.8894, -77.0353, 1e-3);
    near('32T 413070 5170932', 46.6863, 7.8632);
    near('32TMS1306970932', 46.6863, 7.8632);
  });
  it('is not fooled by ordinary searches', () => {
    for (const text of ['Interlaken', 'route 66', '1999', 'Paris 75001', '']) {
      expect(parseCoords(text), text).toBeNull();
    }
    expect(parseCoords('95, 10')).toBeNull();
  });
});

describe('measures and formats', () => {
  it('measures distances and areas', () => {
    // One degree of longitude on the equator.
    expect(distance({ lat: 0, lon: 0 }, { lat: 0, lon: 1 })).toBeCloseTo(111195, -1);
    // A 1x1 degree square at the equator: ~12,364 km².
    const sq = [
      { lat: 0, lon: 0 },
      { lat: 0, lon: 1 },
      { lat: 1, lon: 1 },
      { lat: 1, lon: 0 },
    ];
    expect(polygonArea(sq) / 1e6).toBeCloseTo(12364, -1);
    expect(formatArea(2.5e6)).toBe('2.50 km²');
  });
  it('formats every coordinate form', () => {
    const p = { lat: 46.6863, lon: 7.8632 };
    expect(formatDms(p)).toBe('46°41′10.7″N 7°51′47.5″E');
    expect(formatCoords(p, 'utm')).toBe('32T 413070E 5170932N');
    expect(formatCoords(p, 'mgrs')).toBe('32TMS 13069 70932');
    expect(formatCoords(p, 'decimal')).toBe('46.68630, 7.86320');
  });
  it('decodes Terrarium elevation', () => {
    expect(terrarium(128, 0, 0)).toBe(0);
    expect(terrarium(144, 72, 128)).toBeCloseTo(4168.5, 3);
  });
});
