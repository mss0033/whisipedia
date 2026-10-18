/**
 * Tracks and places a reader opens from disk, drawn over the Atlas: GPX (tracks, routes, waypoints),
 * KML (placemarks with points, lines and polygons) and GeoJSON. Parsed in the browser; nothing leaves
 * it. Returns plain GeoJSON, or an error message.
 */

export interface Feature {
  type: 'Feature';
  geometry:
    | { type: 'Point'; coordinates: [number, number] }
    | { type: 'LineString'; coordinates: [number, number][] }
    | { type: 'Polygon'; coordinates: [number, number][][] };
  properties: { name: string };
}

export interface Overlay {
  name: string;
  features: Feature[];
}

const MAX_BYTES = 50 * 1024 * 1024;

function coordList(text: string): [number, number][] {
  return text
    .trim()
    .split(/\s+/)
    .map((t) => t.split(',').map(Number))
    .filter((c) => c.length >= 2 && Number.isFinite(c[0]) && Number.isFinite(c[1]))
    .map((c) => [c[0] as number, c[1] as number]);
}

function textOf(el: Element | null | undefined): string {
  return el?.textContent?.trim() ?? '';
}

function child(el: Element, name: string): Element | null {
  for (const c of Array.from(el.children)) if (c.localName === name) return c;
  return null;
}

function parseGpx(doc: Document): Feature[] {
  const out: Feature[] = [];
  const pt = (e: Element): [number, number] | null => {
    const lat = Number(e.getAttribute('lat'));
    const lon = Number(e.getAttribute('lon'));
    return Number.isFinite(lat) && Number.isFinite(lon) ? [lon, lat] : null;
  };
  for (const w of Array.from(doc.getElementsByTagNameNS('*', 'wpt'))) {
    const p = pt(w);
    if (p)
      out.push({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: p },
        properties: { name: textOf(child(w, 'name')) },
      });
  }
  const lines = (parents: string, points: string) => {
    for (const t of Array.from(doc.getElementsByTagNameNS('*', parents))) {
      const name = textOf(child(t, 'name'));
      const segs = parents === 'trk' ? Array.from(t.getElementsByTagNameNS('*', 'trkseg')) : [t];
      for (const seg of segs) {
        const coords = Array.from(seg.getElementsByTagNameNS('*', points))
          .map(pt)
          .filter((c): c is [number, number] => c !== null);
        if (coords.length >= 2)
          out.push({
            type: 'Feature',
            geometry: { type: 'LineString', coordinates: coords },
            properties: { name },
          });
      }
    }
  };
  lines('trk', 'trkpt');
  lines('rte', 'rtept');
  return out;
}

function parseKml(doc: Document): Feature[] {
  const out: Feature[] = [];
  for (const pm of Array.from(doc.getElementsByTagNameNS('*', 'Placemark'))) {
    const name = textOf(child(pm, 'name'));
    for (const p of Array.from(pm.getElementsByTagNameNS('*', 'Point'))) {
      const [c] = coordList(textOf(child(p, 'coordinates')));
      if (c)
        out.push({
          type: 'Feature',
          geometry: { type: 'Point', coordinates: c },
          properties: { name },
        });
    }
    for (const l of Array.from(pm.getElementsByTagNameNS('*', 'LineString'))) {
      const cs = coordList(textOf(child(l, 'coordinates')));
      if (cs.length >= 2)
        out.push({
          type: 'Feature',
          geometry: { type: 'LineString', coordinates: cs },
          properties: { name },
        });
    }
    for (const poly of Array.from(pm.getElementsByTagNameNS('*', 'Polygon'))) {
      const ring = poly.getElementsByTagNameNS('*', 'coordinates')[0];
      const cs = coordList(textOf(ring));
      if (cs.length >= 4)
        out.push({
          type: 'Feature',
          geometry: { type: 'Polygon', coordinates: [cs] },
          properties: { name },
        });
    }
  }
  return out;
}

function parseGeoJson(text: string): Feature[] {
  const data: unknown = JSON.parse(text);
  const feats: unknown[] = [];
  const take = (v: unknown) => {
    if (!v || typeof v !== 'object') return;
    const o = v as {
      type?: string;
      features?: unknown[];
      geometry?: unknown;
      properties?: Record<string, unknown>;
    };
    if (o.type === 'FeatureCollection' && Array.isArray(o.features)) o.features.forEach(take);
    else if (o.type === 'Feature') feats.push(o);
    else if (typeof o.type === 'string')
      feats.push({ type: 'Feature', geometry: o, properties: {} });
  };
  take(data);
  const out: Feature[] = [];
  const pair = (c: unknown): [number, number] | null =>
    Array.isArray(c) && Number.isFinite(c[0]) && Number.isFinite(c[1])
      ? [Number(c[0]), Number(c[1])]
      : null;
  for (const f of feats as {
    geometry?: { type?: string; coordinates?: unknown };
    properties?: Record<string, unknown>;
  }[]) {
    const g = f.geometry;
    const name = String(f.properties?.name ?? f.properties?.title ?? '');
    if (!g?.coordinates) continue;
    const add = (type: string, coords: unknown) => {
      if (type === 'Point') {
        const p = pair(coords);
        if (p)
          out.push({
            type: 'Feature',
            geometry: { type: 'Point', coordinates: p },
            properties: { name },
          });
      } else if (type === 'LineString' && Array.isArray(coords)) {
        const cs = coords.map(pair).filter((c): c is [number, number] => c !== null);
        if (cs.length >= 2)
          out.push({
            type: 'Feature',
            geometry: { type: 'LineString', coordinates: cs },
            properties: { name },
          });
      } else if (type === 'Polygon' && Array.isArray(coords) && Array.isArray(coords[0])) {
        const cs = (coords[0] as unknown[])
          .map(pair)
          .filter((c): c is [number, number] => c !== null);
        if (cs.length >= 4)
          out.push({
            type: 'Feature',
            geometry: { type: 'Polygon', coordinates: [cs] },
            properties: { name },
          });
      }
    };
    const t = g.type ?? '';
    if (t.startsWith('Multi') && Array.isArray(g.coordinates))
      for (const part of g.coordinates) add(t.slice(5), part);
    else add(t, g.coordinates);
  }
  return out;
}

/** Parse a file's text by its name and contents. */
export function parseOverlay(name: string, text: string): Overlay | string {
  if (text.length > MAX_BYTES) return 'The file is larger than 50 MB.';
  const lower = name.toLowerCase();
  let features: Feature[];
  try {
    if (lower.endsWith('.json') || lower.endsWith('.geojson') || text.trimStart().startsWith('{')) {
      features = parseGeoJson(text);
    } else {
      const doc = new DOMParser().parseFromString(text, 'application/xml');
      if (doc.getElementsByTagName('parsererror').length) return 'The file is not well-formed XML.';
      const root = doc.documentElement.localName;
      if (root === 'gpx') features = parseGpx(doc);
      else if (root === 'kml') features = parseKml(doc);
      else return 'Not a GPX, KML or GeoJSON file.';
    }
  } catch {
    return 'The file could not be read as GPX, KML or GeoJSON.';
  }
  if (features.length === 0) return 'The file holds no points, tracks or shapes.';
  return { name, features };
}

/** [west, south, east, north] of every coordinate. */
export function bounds(o: Overlay): [number, number, number, number] {
  let w = 180;
  let s = 90;
  let e = -180;
  let n = -90;
  const see = ([x, y]: [number, number]) => {
    w = Math.min(w, x);
    e = Math.max(e, x);
    s = Math.min(s, y);
    n = Math.max(n, y);
  };
  for (const f of o.features) {
    const g = f.geometry;
    if (g.type === 'Point') see(g.coordinates);
    else if (g.type === 'LineString') g.coordinates.forEach(see);
    else for (const ring of g.coordinates) ring.forEach(see);
  }
  return [w, s, e, n];
}
