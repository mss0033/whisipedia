/**
 * The mock's map library (`/v1/maps/...`) for dev and e2e: PMTiles archives, fonts and sprites from
 * a directory (`$WSP_MOCK_MAPS`, else mock/fixtures/maps: a few streets of Interlaken to z14 and
 * terrain at z13, cut from the Protomaps and Mapterhorn planets on 2026-10-18; Noto Sans glyphs,
 * OFL; Protomaps sprites, MIT-derived). A small PMTiles v3 reader; no overzoom.
 */
import { existsSync, openSync, readdirSync, readFileSync, readSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { gunzipSync } from 'node:zlib';

const DIR = resolve(
  process.env.WSP_MOCK_MAPS ?? new URL('./fixtures/maps', import.meta.url).pathname,
);

interface Entry {
  id: number;
  offset: number;
  length: number;
  run: number;
}

interface Archive {
  file: number;
  tileType: number;
  tileCompression: number;
  minZoom: number;
  maxZoom: number;
  bounds: [number, number, number, number];
  root: Entry[];
  leafOffset: number;
  dataOffset: number;
  internal: number;
  attribution: string;
}

function read(fd: number, offset: number, length: number): Buffer {
  const buf = Buffer.alloc(length);
  readSync(fd, buf, 0, length, offset);
  return buf;
}

function varints(buf: Buffer): () => number {
  let pos = 0;
  return () => {
    let result = 0;
    let shift = 0;
    for (;;) {
      const b = buf[pos++] ?? 0;
      result += (b & 0x7f) * 2 ** shift;
      if (!(b & 0x80)) return result;
      shift += 7;
    }
  };
}

function directory(raw: Buffer, compression: number): Entry[] {
  const buf = compression === 2 ? gunzipSync(raw) : raw;
  const next = varints(buf);
  const n = next();
  const ids: number[] = [];
  let last = 0;
  for (let i = 0; i < n; i++) {
    last += next();
    ids.push(last);
  }
  const runs = ids.map(() => next());
  const lengths = ids.map(() => next());
  const out: Entry[] = [];
  for (let i = 0; i < n; i++) {
    const v = next();
    const prev = out[i - 1];
    const offset = v === 0 && prev ? prev.offset + prev.length : v - 1;
    out.push({ id: ids[i] ?? 0, offset, length: lengths[i] ?? 0, run: runs[i] ?? 0 });
  }
  return out;
}

function open(path: string): Archive {
  const fd = openSync(path, 'r');
  const h = read(fd, 0, 127);
  const u64 = (o: number) => Number(h.readBigUInt64LE(o));
  const internal = h[97] ?? 1;
  const metaLen = u64(32);
  let attribution = '';
  if (metaLen) {
    const raw = read(fd, u64(24), metaLen);
    try {
      const meta = JSON.parse((internal === 2 ? gunzipSync(raw) : raw).toString('utf8')) as {
        attribution?: string;
      };
      attribution = (meta.attribution ?? '').replace(/<[^>]+>/g, '');
    } catch {
      /* no metadata */
    }
  }
  return {
    file: fd,
    root: directory(read(fd, u64(8), u64(16)), internal),
    leafOffset: u64(40),
    dataOffset: u64(56),
    internal,
    tileCompression: h[98] ?? 1,
    tileType: h[99] ?? 0,
    minZoom: h[100] ?? 0,
    maxZoom: h[101] ?? 0,
    bounds: [
      h.readInt32LE(102) / 1e7,
      h.readInt32LE(106) / 1e7,
      h.readInt32LE(110) / 1e7,
      h.readInt32LE(114) / 1e7,
    ],
    attribution,
  };
}

function zxyToId(z: number, x: number, y: number): number {
  let acc = (4 ** z - 1) / 3;
  let d = 0;
  for (let s = 2 ** (z - 1); s >= 1; s /= 2) {
    const rx = x & s ? 1 : 0;
    const ry = y & s ? 1 : 0;
    d += s * s * ((3 * rx) ^ ry);
    if (ry === 0) {
      if (rx === 1) {
        x = s - 1 - (x & (s - 1));
        y = s - 1 - (y & (s - 1));
      }
      [x, y] = [y, x];
    }
  }
  acc += d;
  return acc;
}

function find(entries: Entry[], id: number): Entry | undefined {
  let lo = 0;
  let hi = entries.length - 1;
  let best: Entry | undefined;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const e = entries[mid] as Entry;
    if (e.id <= id) {
      best = e;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  if (!best) return undefined;
  return best.run === 0 || id < best.id + best.run ? best : undefined;
}

function tile(a: Archive, z: number, x: number, y: number): Buffer | null {
  if (z < a.minZoom || z > a.maxZoom) return null;
  const id = zxyToId(z, x, y);
  let entries = a.root;
  for (let depth = 0; depth < 4; depth++) {
    const e = find(entries, id);
    if (!e) return null;
    if (e.run > 0) return read(a.file, a.dataOffset + e.offset, e.length);
    entries = directory(read(a.file, a.leafOffset + e.offset, e.length), a.internal);
  }
  return null;
}

let archives: Map<string, Archive> | null = null;

function held(): Map<string, Archive> {
  if (archives) return archives;
  archives = new Map();
  if (existsSync(DIR)) {
    for (const name of readdirSync(DIR)) {
      if (name.endsWith('.pmtiles'))
        archives.set(name.replace(/\.pmtiles$/, '').replace(/-.*/, ''), open(join(DIR, name)));
    }
  }
  return archives;
}

const KINDS: Record<string, string> = {
  basemap: 'vector',
  terrain: 'raster-dem',
  imagery: 'raster',
};
const TYPES = ['unknown', 'mvt', 'png', 'jpeg', 'webp', 'avif'];
const MEDIA: Record<string, string> = {
  mvt: 'application/vnd.mapbox-vector-tile',
  png: 'image/png',
  jpeg: 'image/jpeg',
  webp: 'image/webp',
};

export interface BinaryResult {
  status: number;
  bytes: Buffer;
  contentType: string;
  encoding?: string;
}

export function tilesets(): unknown {
  const sets = [...held()].map(([id, a]) => ({
    id,
    kind: KINDS[id] ?? 'raster',
    encoding: id === 'terrain' ? 'terrarium' : '',
    tile_type: TYPES[a.tileType] ?? 'unknown',
    tile_size: 512,
    min_zoom: a.minZoom,
    max_zoom: a.maxZoom,
    held_max_zoom: a.maxZoom,
    bounds: a.bounds,
    archives: 1,
    attribution: a.attribution || id,
    tiles: [`/v1/maps/tiles/${id}/{z}/{x}/{y}`],
  }));
  const fonts = existsSync(join(DIR, 'fonts'))
    ? readdirSync(join(DIR, 'fonts')).filter((f) => existsSync(join(DIR, 'fonts', f, '0-255.pbf')))
    : [];
  return {
    tilesets: sets,
    glyphs: fonts.length ? '/v1/maps/glyphs/{fontstack}/{range}.pbf' : null,
    sprite: existsSync(join(DIR, 'sprites', 'v4')) ? '/v1/maps/sprites/v4' : null,
    fonts,
  };
}

/** A tile, glyphs or a sprite; null when the path is not one of those routes. */
export function mapBinary(path: string): BinaryResult | { status: 204 | 404 } | null {
  const t = /^\/v1\/maps\/tiles\/([a-z][a-z0-9-]*)\/(\d+)\/(\d+)\/(\d+)$/.exec(path);
  if (t) {
    const a = held().get(t[1] ?? '');
    if (!a) return { status: 404 };
    const blob = tile(a, Number(t[2]), Number(t[3]), Number(t[4]));
    if (!blob) return { status: 204 };
    const type = TYPES[a.tileType] ?? 'unknown';
    return {
      status: 200,
      bytes: blob,
      contentType: MEDIA[type] ?? 'application/octet-stream',
      ...(a.tileCompression === 2 ? { encoding: 'gzip' } : {}),
    };
  }
  const g = /^\/v1\/maps\/glyphs\/([^/]+)\/(\d+-\d+\.pbf)$/.exec(path);
  if (g) {
    for (const font of decodeURIComponent(g[1] ?? '').split(',')) {
      const f = join(DIR, 'fonts', font.trim(), g[2] ?? '');
      if (existsSync(f))
        return { status: 200, bytes: readFileSync(f), contentType: 'application/x-protobuf' };
    }
    return { status: 404 };
  }
  const s = /^\/v1\/maps\/sprites\/(v\d+)\/([a-z0-9_@-]+\.(json|png))$/.exec(path);
  if (s) {
    const f = join(DIR, 'sprites', s[1] ?? '', s[2] ?? '');
    if (!existsSync(f)) return { status: 404 };
    return {
      status: 200,
      bytes: readFileSync(f),
      contentType: s[3] === 'json' ? 'application/json' : 'image/png',
    };
  }
  return null;
}

/** A few places around Interlaken for the gazetteer routes. */
export const PLACES = [
  {
    name: 'Interlaken',
    kind: 'place=town',
    lat: 46.6863,
    lon: 7.8632,
    population: 5700,
    wikidata: 'Q68968',
    wikipedia: 'en:Interlaken',
    osm: 'node/1',
  },
  {
    name: 'Unterseen',
    kind: 'place=town',
    lat: 46.6854,
    lon: 7.8486,
    population: 5900,
    wikidata: 'Q69083',
    wikipedia: 'en:Unterseen',
    osm: 'node/2',
  },
  {
    name: 'Harder Kulm',
    kind: 'natural=peak',
    lat: 46.6977,
    lon: 7.8513,
    population: null,
    wikidata: 'Q1585880',
    wikipedia: null,
    osm: 'node/3',
  },
  {
    name: 'Jungfrau',
    kind: 'natural=peak',
    lat: 46.5375,
    lon: 7.9617,
    population: null,
    wikidata: 'Q15283',
    wikipedia: 'en:Jungfrau',
    osm: 'node/4',
  },
];

function haversine(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const r = Math.PI / 180;
  const a =
    Math.sin(((lat2 - lat1) * r) / 2) ** 2 +
    Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(((lon2 - lon1) * r) / 2) ** 2;
  return 2 * 6371008.8 * Math.asin(Math.min(1, Math.sqrt(a)));
}

export function places(params: URLSearchParams): unknown {
  const q = (params.get('q') ?? '').trim().toLowerCase();
  return {
    places: PLACES.filter((p) => q && p.name.toLowerCase().startsWith(q)).map((p) => ({
      ...p,
      distance_m: null,
    })),
  };
}

export function nearby(params: URLSearchParams): unknown {
  const lat = Number(params.get('lat'));
  const lon = Number(params.get('lon'));
  const radius = Number(params.get('radius') ?? 5000);
  return {
    places: PLACES.map((p) => ({ ...p, distance_m: Math.round(haversine(lat, lon, p.lat, p.lon)) }))
      .filter((p) => p.distance_m <= radius)
      .sort((a, b) => a.distance_m - b.distance_m),
  };
}
