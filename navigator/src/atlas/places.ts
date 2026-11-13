/**
 * What a place-search row says about its place, and how map credits are written: the parts of the
 * Atlas that are words, kept apart from the map so they can be tested without one.
 */
import type { PlaceHit } from '../api/types.ts';
import { LOCALE } from '../core/format.ts';
import { type CoordFormat, formatCoords, formatDistance } from '../core/geo.ts';

/** OpenStreetMap's element types: how a place is stored, which tells a reader nothing. */
const ELEMENT_TYPES = new Set(['node', 'way', 'relation', 'area']);

/**
 * `place=town` as "town", `natural=peak` as "peak (natural)". A kind with no value, or one that only
 * names the element type ("node"), has nothing to say: the empty string, which callers leave out.
 */
export function kindLabel(kind: string): string {
  const [rawKey = '', rawValue = ''] = kind.split('=');
  const key = rawKey.trim().replace(/_/g, ' ');
  const value = rawValue.trim().replace(/_/g, ' ');
  const said = (word: string) => word !== '' && !ELEMENT_TYPES.has(word);
  if (!said(value)) return said(key) && key !== 'place' ? key : '';
  return key === 'place' || !said(key) ? value : `${value} (${key})`;
}

/**
 * The facts after a place's name: its kind, population, distance and where it is. Two places of one
 * name ("Springfield") can then be told apart by their coordinates until the index holds regions.
 */
export function placeFacts(
  pl: Pick<PlaceHit, 'kind' | 'population' | 'distance_m' | 'lat' | 'lon'>,
  opts: { format: CoordFormat; imperial: boolean; withDistance: boolean },
): string[] {
  const facts: string[] = [];
  const kind = kindLabel(pl.kind ?? '');
  if (kind) facts.push(kind);
  if (pl.population) facts.push(`pop. ${pl.population.toLocaleString(LOCALE)}`);
  if (opts.withDistance && pl.distance_m !== null && pl.distance_m !== undefined)
    facts.push(formatDistance(pl.distance_m, opts.imperial));
  facts.push(formatCoords({ lat: pl.lat, lon: pl.lon }, opts.format));
  return facts;
}

/** Below this width the map's credits start folded (a button), not as a band across the map. */
export const NARROW_MAP = 640;
