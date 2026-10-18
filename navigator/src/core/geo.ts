/**
 * Coordinates for the Atlas: decimal degrees, degrees-minutes-seconds, UTM and MGRS (WGS 84), the
 * great-circle distance and polygon area, and parsing whatever form a reader types or pastes.
 *
 * UTM uses Snyder's series (USGS Professional Paper 1395, pp. 57-64), good to well under a
 * millimetre inside a zone; MGRS adds the 100 km square letters of the AA scheme. Both are checked
 * against PROJ and the GeoTrans-based `mgrs` library (tests/geo.test.ts).
 */

export interface LatLon {
  lat: number;
  lon: number;
}

export interface Utm {
  zone: number;
  band: string;
  easting: number;
  northing: number;
}

const A = 6378137;
const F = 1 / 298.257223563;
const E2 = F * (2 - F);
const EP2 = E2 / (1 - E2);
const K0 = 0.9996;
const BANDS = 'CDEFGHJKLMNPQRSTUVWX';
const COLUMN_SETS = ['ABCDEFGH', 'JKLMNPQR', 'STUVWXYZ'];
const ROWS = 'ABCDEFGHJKLMNPQRSTUV';
const RAD = Math.PI / 180;

export const EARTH_RADIUS_M = 6371008.8;

function meridionalArc(phi: number): number {
  const e4 = E2 * E2;
  const e6 = e4 * E2;
  return (
    A *
    ((1 - E2 / 4 - (3 * e4) / 64 - (5 * e6) / 256) * phi -
      ((3 * E2) / 8 + (3 * e4) / 32 + (45 * e6) / 1024) * Math.sin(2 * phi) +
      ((15 * e4) / 256 + (45 * e6) / 1024) * Math.sin(4 * phi) -
      ((35 * e6) / 3072) * Math.sin(6 * phi))
  );
}

/** The UTM zone of a point, with the Norway and Svalbard exceptions. */
export function utmZone(lat: number, lon: number): number {
  const lon180 = ((((lon + 180) % 360) + 360) % 360) - 180;
  let zone = Math.floor((lon180 + 180) / 6) + 1;
  if (zone > 60) zone = 60;
  if (lat >= 56 && lat < 64 && lon180 >= 3 && lon180 < 12) zone = 32;
  if (lat >= 72 && lat < 84) {
    if (lon180 >= 0 && lon180 < 9) zone = 31;
    else if (lon180 >= 9 && lon180 < 21) zone = 33;
    else if (lon180 >= 21 && lon180 < 33) zone = 35;
    else if (lon180 >= 33 && lon180 < 42) zone = 37;
  }
  return zone;
}

export function latitudeBand(lat: number): string | null {
  if (lat < -80 || lat > 84) return null;
  return BANDS[Math.min(19, Math.floor((lat + 80) / 8))] ?? null;
}

/** UTM of a point between 80°S and 84°N (null outside: polar areas use UPS). */
export function toUtm(p: LatLon, forceZone?: number): Utm | null {
  const band = latitudeBand(p.lat);
  if (!band) return null;
  const zone = forceZone ?? utmZone(p.lat, p.lon);
  const lon0 = ((zone - 1) * 6 - 180 + 3) * RAD;
  const phi = p.lat * RAD;
  const lam = p.lon * RAD;
  const sin = Math.sin(phi);
  const cos = Math.cos(phi);
  const tan = Math.tan(phi);
  const n = A / Math.sqrt(1 - E2 * sin * sin);
  const t = tan * tan;
  const c = EP2 * cos * cos;
  let dl = lam - lon0;
  if (dl > Math.PI) dl -= 2 * Math.PI;
  if (dl < -Math.PI) dl += 2 * Math.PI;
  const a = cos * dl;
  const m = meridionalArc(phi);
  const easting =
    K0 *
      n *
      (a + ((1 - t + c) * a ** 3) / 6 + ((5 - 18 * t + t * t + 72 * c - 58 * EP2) * a ** 5) / 120) +
    500000;
  let northing =
    K0 *
    (m +
      n *
        tan *
        ((a * a) / 2 +
          ((5 - t + 9 * c + 4 * c * c) * a ** 4) / 24 +
          ((61 - 58 * t + t * t + 600 * c - 330 * EP2) * a ** 6) / 720));
  if (p.lat < 0) northing += 10000000;
  return { zone, band, easting, northing };
}

/** The point at a UTM position (``south``: the southern hemisphere's false northing). */
export function fromUtm(zone: number, easting: number, northing: number, south: boolean): LatLon {
  const lon0 = ((zone - 1) * 6 - 180 + 3) * RAD;
  const x = easting - 500000;
  const y = south ? northing - 10000000 : northing;
  const m = y / K0;
  const mu = m / (A * (1 - E2 / 4 - (3 * E2 * E2) / 64 - (5 * E2 ** 3) / 256));
  const e1 = (1 - Math.sqrt(1 - E2)) / (1 + Math.sqrt(1 - E2));
  const phi1 =
    mu +
    ((3 * e1) / 2 - (27 * e1 ** 3) / 32) * Math.sin(2 * mu) +
    ((21 * e1 * e1) / 16 - (55 * e1 ** 4) / 32) * Math.sin(4 * mu) +
    ((151 * e1 ** 3) / 96) * Math.sin(6 * mu) +
    ((1097 * e1 ** 4) / 512) * Math.sin(8 * mu);
  const sin = Math.sin(phi1);
  const cos = Math.cos(phi1);
  const tan = Math.tan(phi1);
  const c1 = EP2 * cos * cos;
  const t1 = tan * tan;
  const n1 = A / Math.sqrt(1 - E2 * sin * sin);
  const r1 = (A * (1 - E2)) / (1 - E2 * sin * sin) ** 1.5;
  const d = x / (n1 * K0);
  const lat =
    phi1 -
    ((n1 * tan) / r1) *
      ((d * d) / 2 -
        ((5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * EP2) * d ** 4) / 24 +
        ((61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * EP2 - 3 * c1 * c1) * d ** 6) / 720);
  const lon =
    lon0 +
    (d -
      ((1 + 2 * t1 + c1) * d ** 3) / 6 +
      ((5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * EP2 + 24 * t1 * t1) * d ** 5) / 120) /
      cos;
  return { lat: lat / RAD, lon: normalizeLon(lon / RAD) };
}

export function normalizeLon(lon: number): number {
  return ((((lon + 180) % 360) + 360) % 360) - 180;
}

/** MGRS to the metre ("18SUJ2347806483"); null outside 80°S-84°N. */
export function toMgrs(p: LatLon, digits = 5): string | null {
  const u = toUtm(p);
  if (!u) return null;
  const col = Math.floor(u.easting / 100000);
  const row = Math.floor(u.northing / 100000) % 20;
  const colLetter = COLUMN_SETS[(u.zone - 1) % 3]?.[col - 1];
  const rowLetter = ROWS[(row + (u.zone % 2 === 0 ? 5 : 0)) % 20];
  if (!colLetter || !rowLetter) return null;
  const scale = 10 ** (5 - digits);
  const e = Math.floor((u.easting % 100000) / scale);
  const n = Math.floor((u.northing % 100000) / scale);
  return `${u.zone}${u.band}${colLetter}${rowLetter}${String(e).padStart(digits, '0')}${String(n).padStart(digits, '0')}`;
}

const MGRS_RE = /^(\d{1,2})\s*([C-HJ-NP-X])\s*([A-HJ-NP-Z])([A-HJ-NP-V])\s*(\d*)\s*(\d*)$/i;

/** The south-west corner of an MGRS square ("32TMS1306970932", spaces allowed), or null. */
export function fromMgrs(text: string): LatLon | null {
  const m = MGRS_RE.exec(text.trim().toUpperCase());
  if (!m) return null;
  const [, z, band = '', colL = '', rowL = '', a = '', b = ''] = m;
  const zone = Number(z);
  if (zone < 1 || zone > 60) return null;
  let digits = a;
  let more = b;
  if (!more && digits.length % 2 === 0) {
    more = digits.slice(digits.length / 2);
    digits = digits.slice(0, digits.length / 2);
  }
  if (digits.length !== more.length || digits.length > 5) return null;
  const scale = 10 ** (5 - digits.length);
  const col = (COLUMN_SETS[(zone - 1) % 3] ?? '').indexOf(colL);
  const rowIndex = ROWS.indexOf(rowL);
  if (col < 0 || rowIndex < 0) return null;
  const easting = (col + 1) * 100000 + (digits ? Number(digits) * scale : 0);
  let northing = (((rowIndex - (zone % 2 === 0 ? 5 : 0)) % 20) + 20) % 20;
  northing = northing * 100000 + (more ? Number(more) * scale : 0);
  const bandIndex = BANDS.indexOf(band);
  const south = bandIndex < BANDS.indexOf('N');
  const bandLat = -80 + bandIndex * 8;
  // The 100 km rows repeat every 2,000 km: take the cycle that falls in the latitude band.
  const lon0 = (zone - 1) * 6 - 180 + 3;
  const floor = toUtm({ lat: bandLat, lon: lon0 }, zone);
  if (!floor) return null;
  const minNorthing = south && bandLat < 0 ? floor.northing : floor.northing;
  while (northing < minNorthing - 100000) northing += 2000000;
  return fromUtm(zone, easting, northing, south);
}

export function formatDecimal(p: LatLon, places = 5): string {
  return `${p.lat.toFixed(places)}, ${p.lon.toFixed(places)}`;
}

export function formatDms(p: LatLon): string {
  const part = (v: number, pos: string, neg: string) => {
    const abs = Math.abs(v);
    let d = Math.floor(abs);
    let m = Math.floor((abs - d) * 60);
    let s = Math.round(((abs - d) * 60 - m) * 600) / 10;
    if (s >= 60) {
      s = 0;
      m += 1;
    }
    if (m >= 60) {
      m = 0;
      d += 1;
    }
    return `${d}°${String(m).padStart(2, '0')}′${s.toFixed(1).padStart(4, '0')}″${v < 0 ? neg : pos}`;
  };
  return `${part(p.lat, 'N', 'S')} ${part(p.lon, 'E', 'W')}`;
}

export function formatUtm(p: LatLon): string {
  const u = toUtm(p);
  if (!u) return 'outside UTM';
  return `${u.zone}${u.band} ${Math.round(u.easting)}E ${Math.round(u.northing)}N`;
}

export type CoordFormat = 'decimal' | 'dms' | 'utm' | 'mgrs';

export const COORD_FORMATS: readonly { id: CoordFormat; label: string }[] = [
  { id: 'decimal', label: 'Decimal degrees' },
  { id: 'dms', label: 'Degrees, minutes, seconds' },
  { id: 'utm', label: 'UTM' },
  { id: 'mgrs', label: 'MGRS' },
];

export function formatCoords(p: LatLon, format: CoordFormat): string {
  switch (format) {
    case 'dms':
      return formatDms(p);
    case 'utm':
      return formatUtm(p);
    case 'mgrs': {
      const m = toMgrs(p);
      return m ? `${m.slice(0, -10)} ${m.slice(-10, -5)} ${m.slice(-5)}` : 'outside MGRS';
    }
    default:
      return formatDecimal(p);
  }
}

const DMS_PART =
  /(-?\d+(?:\.\d+)?)\s*[°d:\s]\s*(?:(\d+(?:\.\d+)?)\s*['′m:\s]\s*)?(?:(\d+(?:\.\d+)?)\s*(?:["″s]|'')?\s*)?([NSEW])?/gi;

function valid(p: LatLon): LatLon | null {
  return Number.isFinite(p.lat) &&
    Number.isFinite(p.lon) &&
    Math.abs(p.lat) <= 90 &&
    Math.abs(p.lon) <= 180
    ? p
    : null;
}

/**
 * A point from text, or null if it is not coordinates: "46.6863, 7.8632", "46.6863 N 7.8632 E",
 * "46°41′11″N 7°51′48″E", "32T 413069 5170932", "32TMS1306970932".
 */
export function parseCoords(text: string): LatLon | null {
  const s = text.trim();
  if (!s) return null;
  const mgrs = fromMgrs(s);
  if (mgrs) return valid(mgrs);
  const utm =
    /^(\d{1,2})\s*([C-HJ-NP-X])\s+(\d{3,7}(?:\.\d+)?)\s*m?E?\s*[,\s]\s*(\d{1,8}(?:\.\d+)?)\s*m?N?$/i.exec(
      s,
    );
  if (utm) {
    const band = (utm[2] ?? '').toUpperCase();
    return valid(fromUtm(Number(utm[1]), Number(utm[3]), Number(utm[4]), band < 'N'));
  }
  const dec =
    /^(-?\d{1,2}(?:\.\d+)?)\s*°?\s*([NS])?\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*([EW])?$/i.exec(
      s,
    );
  if (dec) {
    let lat = Number(dec[1]);
    let lon = Number(dec[3]);
    if (dec[2]?.toUpperCase() === 'S') lat = -Math.abs(lat);
    if (dec[4]?.toUpperCase() === 'W') lon = -Math.abs(lon);
    return valid({ lat, lon });
  }
  if (!/[°′'″"dms]/i.test(s)) return null;
  const parts = [...s.matchAll(DMS_PART)].filter((m) => m[0].trim());
  if (parts.length !== 2) return null;
  const value = (m: RegExpMatchArray) => {
    const v = Math.abs(Number(m[1])) + Number(m[2] ?? 0) / 60 + Number(m[3] ?? 0) / 3600;
    const hemi = (m[4] ?? '').toUpperCase();
    return { v: Number(m[1]) < 0 || hemi === 'S' || hemi === 'W' ? -v : v, hemi };
  };
  const [first, second] = parts.map(value);
  if (!first || !second) return null;
  if (first.hemi === 'E' || first.hemi === 'W') return valid({ lat: second.v, lon: first.v });
  return valid({ lat: first.v, lon: second.v });
}

/** Great-circle distance in metres. */
export function distance(a: LatLon, b: LatLon): number {
  const p1 = a.lat * RAD;
  const p2 = b.lat * RAD;
  const dp = p2 - p1;
  const dl = (b.lon - a.lon) * RAD;
  const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

export function pathLength(points: readonly LatLon[]): number {
  let total = 0;
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1];
    const b = points[i];
    if (a && b) total += distance(a, b);
  }
  return total;
}

/** Area of a polygon on the sphere in square metres (spherical excess, Chamberlain & Duquette). */
export function polygonArea(points: readonly LatLon[]): number {
  if (points.length < 3) return 0;
  let sum = 0;
  for (let i = 0; i < points.length; i++) {
    const a = points[i];
    const b = points[(i + 1) % points.length];
    if (!a || !b) continue;
    sum += (b.lon - a.lon) * RAD * (2 + Math.sin(a.lat * RAD) + Math.sin(b.lat * RAD));
  }
  return Math.abs((sum * EARTH_RADIUS_M * EARTH_RADIUS_M) / 2);
}

export function formatDistance(m: number, imperial = false): string {
  if (imperial) {
    const ft = m / 0.3048;
    return ft < 5280 ? `${Math.round(ft)} ft` : `${(ft / 5280).toFixed(ft < 52800 ? 2 : 1)} mi`;
  }
  return m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(m < 10000 ? 2 : 1)} km`;
}

export function formatArea(m2: number, imperial = false): string {
  if (imperial) {
    const acres = m2 / 4046.8564224;
    return acres < 640
      ? `${acres.toFixed(acres < 10 ? 2 : 1)} acres`
      : `${(acres / 640).toFixed(2)} sq mi`;
  }
  return m2 < 1e6 ? `${Math.round(m2)} m²` : `${(m2 / 1e6).toFixed(m2 < 1e7 ? 2 : 1)} km²`;
}

/** Elevation in metres from a Terrarium terrain pixel. */
export function terrarium(r: number, g: number, b: number): number {
  return r * 256 + g + b / 256 - 32768;
}
