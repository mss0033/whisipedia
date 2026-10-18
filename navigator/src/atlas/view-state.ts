/**
 * The Atlas's address: `wsp://atlas/@46.6863,7.8632,12.5z,30b,45p?layer=terrain&q=Interlaken`.
 * Centre, zoom, then optional bearing (b) and pitch (p); `layer` and `q` (a place to find) are
 * optional. Pure, so the router and the view agree on it and tests need no map.
 */
import type { LayerMode } from './style.ts';

export interface AtlasView {
  lat: number;
  lon: number;
  zoom: number;
  bearing: number;
  pitch: number;
}

export interface AtlasRoute {
  view: AtlasView | undefined;
  layer: LayerMode | undefined;
  query: string;
  /** A point to mark (`?pin=lat,lon`): what a document's "Open in Atlas" shows. */
  pin: { lat: number; lon: number } | undefined;
}

const MODES: readonly LayerMode[] = ['map', 'terrain', 'satellite', 'hybrid'];

export const WORLD_VIEW: AtlasView = { lat: 20, lon: 0, zoom: 1.6, bearing: 0, pitch: 0 };

function num(s: string | undefined): number | undefined {
  if (s === undefined || s === '') return undefined;
  const n = Number(s);
  return Number.isFinite(n) ? n : undefined;
}

export function parseView(path: string): AtlasView | undefined {
  const m =
    /^@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?)z)?(?:,(-?\d+(?:\.\d+)?)b)?(?:,(\d+(?:\.\d+)?)p)?$/.exec(
      path.trim(),
    );
  if (!m) return undefined;
  const lat = num(m[1]);
  const lon = num(m[2]);
  if (lat === undefined || lon === undefined || Math.abs(lat) > 90 || Math.abs(lon) > 180)
    return undefined;
  const zoom = Math.min(22, Math.max(0, num(m[3]) ?? 12));
  const bearing = ((((num(m[4]) ?? 0) % 360) + 540) % 360) - 180;
  const pitch = Math.min(85, Math.max(0, num(m[5]) ?? 0));
  return { lat, lon, zoom, bearing, pitch };
}

export function formatView(v: AtlasView): string {
  const r = (x: number, d: number) => String(Number(x.toFixed(d)));
  let out = `@${r(v.lat, 5)},${r(v.lon, 5)},${r(v.zoom, 2)}z`;
  if (Math.abs(v.bearing) >= 0.5 || v.pitch >= 0.5) out += `,${r(v.bearing, 1)}b`;
  if (v.pitch >= 0.5) out += `,${r(v.pitch, 1)}p`;
  return out;
}

export function parseAtlas(path: string, params: Readonly<Record<string, string>>): AtlasRoute {
  const layer = MODES.find((m) => m === params.layer);
  const pinMatch = /^(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$/.exec(params.pin ?? '');
  const plat = num(pinMatch?.[1]);
  const plon = num(pinMatch?.[2]);
  const pin =
    plat !== undefined && plon !== undefined && Math.abs(plat) <= 90 && Math.abs(plon) <= 180
      ? { lat: plat, lon: plon }
      : undefined;
  return { view: parseView(path), layer, query: (params.q ?? '').trim(), pin };
}

export function atlasAddress(
  view: AtlasView,
  layer: LayerMode,
  extra: { pin?: string } = {},
): string {
  const qs = new URLSearchParams();
  if (layer !== 'map') qs.set('layer', layer);
  if (extra.pin) qs.set('pin', extra.pin);
  const q = qs.toString();
  return `wsp://atlas/${formatView(view)}${q ? `?${q}` : ''}`;
}
