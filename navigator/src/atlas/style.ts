/**
 * The Atlas's map styles: what the library holds (tilesets) drawn in the navigator theme's palette.
 *
 * The basemap is Protomaps' layer set (BSD-3, design CC0) with a colour "flavor" per theme: Navigator
 * '96 a paper road atlas, Mosaic '93 the plain early web, Encarta '95 the encyclopedia's world atlas,
 * System 7 greyscale, the phosphor themes a vector-screen monochrome, and the dark and high-contrast
 * preferences their own palettes. Layers (map, terrain, satellite, hybrid) only offer what is held.
 */
import { BLACK, DARK, type Flavor, GRAYSCALE, LIGHT, layers, WHITE } from '@protomaps/basemaps';
import type { LayerSpecification, SourceSpecification, StyleSpecification } from 'maplibre-gl';
import type { Tileset, Tilesets } from '../api/types.ts';

export type LayerMode = 'map' | 'terrain' | 'satellite' | 'hybrid';

export const LAYER_MODES: readonly { id: LayerMode; label: string; needs: readonly string[] }[] = [
  { id: 'map', label: 'Map', needs: ['basemap'] },
  { id: 'terrain', label: 'Terrain', needs: ['basemap', 'terrain'] },
  { id: 'satellite', label: 'Satellite', needs: ['imagery'] },
  { id: 'hybrid', label: 'Hybrid', needs: ['imagery', 'basemap'] },
];

export interface Look {
  theme: string;
  dark: boolean;
  highContrast: boolean;
}

// --- colours -------------------------------------------------------------------------------------

type Rgb = [number, number, number, number];

function parseColor(c: string): Rgb | null {
  const s = c.trim();
  let m = /^#([0-9a-f]{3})$/i.exec(s);
  if (m?.[1]) {
    const [r, g, b] = [...m[1]].map((x) => Number.parseInt(x + x, 16));
    return [r ?? 0, g ?? 0, b ?? 0, 1];
  }
  m = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(s);
  if (m?.[1]) {
    const v = m[1];
    const alpha = m[2] ? Number.parseInt(m[2], 16) / 255 : 1;
    return [
      Number.parseInt(v.slice(0, 2), 16),
      Number.parseInt(v.slice(2, 4), 16),
      Number.parseInt(v.slice(4, 6), 16),
      alpha,
    ];
  }
  m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:[\s,/]+([\d.]+))?\s*\)$/i.exec(s);
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3]), m[4] === undefined ? 1 : Number(m[4])];
  return null;
}

function toCss([r, g, b, a]: Rgb): string {
  const h = (v: number) =>
    Math.round(Math.min(255, Math.max(0, v)))
      .toString(16)
      .padStart(2, '0');
  return a >= 1
    ? `#${h(r)}${h(g)}${h(b)}`
    : `rgba(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)}, ${a})`;
}

function luminance([r, g, b]: Rgb): number {
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
}

/** Every colour of a flavor through ``fn`` (nested POI and landcover palettes too). */
function mapColors(f: Flavor, fn: (c: Rgb, key: string) => Rgb): Flavor {
  const walk = (obj: Record<string, unknown>, prefix: string): Record<string, unknown> => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(obj)) {
      if (typeof v === 'string') {
        const rgb = parseColor(v);
        out[k] = rgb ? toCss(fn(rgb, prefix + k)) : v;
      } else if (v && typeof v === 'object') {
        out[k] = walk(v as Record<string, unknown>, `${prefix}${k}.`);
      } else {
        out[k] = v;
      }
    }
    return out;
  };
  return walk(f as unknown as Record<string, unknown>, '') as unknown as Flavor;
}

/** Keys of a flavor drawn as lines or text (roads, rails, borders, labels); the rest fill areas. */
const LINE_KEY =
  /(highway|major|minor|link|other|railway|boundaries|label|casing|runway|pier|bridges|tunnel|regular|bold|italic)/;

/** A monochrome vector screen: bright lines and lettering over dark, barely lit areas. */
function phosphor(base: Flavor, hue: Rgb): Flavor {
  return mapColors(base, (c, key) => {
    if (key === 'background' || key === 'earth' || key.includes('halo')) return [0, 0, 0, 1];
    const l = luminance(c);
    const glow = LINE_KEY.test(key) ? 0.55 + 0.45 * l : key === 'water' ? 0.16 : 0.05 + 0.18 * l;
    return [hue[0] * glow, hue[1] * glow, hue[2] * glow, c[3]];
  });
}

/** High contrast: black ground, dark areas, white roads and lettering on black halos, yellow highways. */
function highContrast(base: Flavor): Flavor {
  return mapColors(base, (c, key) => {
    if (key === 'background' || key === 'earth' || key.includes('halo')) return [0, 0, 0, 1];
    if (key === 'water') return [0, 0, 110, 1];
    if (!LINE_KEY.test(key)) {
      const v = 25 + 40 * luminance(c);
      return [v, v, v, c[3]];
    }
    if (/highway|major/.test(key) && !key.includes('label')) return [255, 230, 0, c[3]];
    return [255, 255, 255, c[3]];
  });
}

/** The Encarta '95 world atlas: deep blue sea, warm land, red highways, gold main roads. */
const ENCARTA: Partial<Flavor> = {
  background: '#1f4f8f',
  earth: '#e8dcae',
  water: '#2c64ad',
  park_a: '#b9d397',
  park_b: '#a9c886',
  wood_a: '#a6c487',
  wood_b: '#96b978',
  scrub_a: '#cfd9a3',
  scrub_b: '#c4d195',
  glacier: '#f4f8fb',
  sand: '#ead9a0',
  beach: '#efe0aa',
  buildings: '#d5c79a',
  highway: '#d2452b',
  highway_casing_early: '#7a2416',
  highway_casing_late: '#7a2416',
  major: '#f2b23a',
  major_casing_early: '#8a5a10',
  major_casing_late: '#8a5a10',
  minor_a: '#fffaf0',
  minor_b: '#fffaf0',
  boundaries: '#7e3f8c',
  ocean_label: '#d7e7ff',
  city_label: '#2b1d0e',
  city_label_halo: '#f3ead0',
  country_label: '#5a2d63',
  state_label: '#6b4a73',
  state_label_halo: '#f3ead0',
};

/** Navigator '96: a paper road atlas. */
const PAPER: Partial<Flavor> = {
  background: '#f2efe4',
  earth: '#f2efe4',
  water: '#9cc3e4',
  highway: '#e0553c',
  major: '#f6c75a',
};

export function flavorFor(look: Look): Flavor {
  if (look.theme === 'phosphor-green') return phosphor(BLACK, [51, 255, 102, 1]);
  if (look.theme === 'phosphor-amber') return phosphor(BLACK, [255, 176, 0, 1]);
  if (look.highContrast) return highContrast(look.dark ? BLACK : DARK);
  if (look.theme === 'system7') return look.dark ? BLACK : GRAYSCALE;
  if (look.dark) return DARK;
  if (look.theme === 'encarta95') return { ...LIGHT, ...ENCARTA };
  if (look.theme === 'mosaic93') return WHITE;
  return { ...LIGHT, ...PAPER };
}

/** Colours the Atlas draws its own marks with (measure lines, pins), per look. */
export function markColors(look: Look): { line: string; fill: string; halo: string } {
  if (look.theme === 'phosphor-green')
    return { line: '#33ff66', fill: 'rgba(51,255,102,0.15)', halo: '#000' };
  if (look.theme === 'phosphor-amber')
    return { line: '#ffb000', fill: 'rgba(255,176,0,0.15)', halo: '#000' };
  if (look.highContrast) return { line: '#ffff00', fill: 'rgba(255,255,0,0.2)', halo: '#000' };
  if (look.theme === 'encarta95')
    return { line: '#7e3f8c', fill: 'rgba(126,63,140,0.18)', halo: '#fff' };
  return { line: '#c00000', fill: 'rgba(192,0,0,0.15)', halo: '#fff' };
}

// --- the style ------------------------------------------------------------------------------------

export function availableModes(sets: Tilesets): LayerMode[] {
  const held = new Set(sets.tilesets.map((t) => t.id));
  return LAYER_MODES.filter((m) => m.needs.every((n) => held.has(n))).map((m) => m.id);
}

/** An absolute URL for MapLibre (its workers resolve no relative ones), with `{z}`, `{fontstack}`
 * and the other placeholders left as they are: `new URL()` would percent-encode the braces. */
export function absolute(url: string): string {
  if (/^https?:\/\//.test(url)) return url;
  const origin = globalThis.location?.origin ?? 'http://127.0.0.1';
  return `${origin}${url.startsWith('/') ? '' : '/'}${url}`;
}

function source(t: Tileset, kind: 'vector' | 'raster' | 'raster-dem'): SourceSpecification {
  const tiles = t.tiles.map(absolute);
  const bounds = t.bounds as [number, number, number, number];
  if (kind === 'vector') {
    return {
      type: 'vector',
      tiles,
      minzoom: t.min_zoom,
      maxzoom: t.held_max_zoom,
      attribution: t.attribution,
      bounds,
    };
  }
  if (kind === 'raster-dem') {
    return {
      type: 'raster-dem',
      tiles,
      tileSize: t.tile_size,
      minzoom: t.min_zoom,
      maxzoom: t.max_zoom,
      encoding: t.encoding === 'mapbox' ? 'mapbox' : 'terrarium',
      attribution: t.attribution,
    };
  }
  return {
    type: 'raster',
    tiles,
    tileSize: t.tile_size,
    minzoom: t.min_zoom,
    maxzoom: t.max_zoom,
    attribution: t.attribution,
  };
}

export const BASEMAP = 'protomaps';
export const DEM = 'terrain-dem';
export const HILLSHADE_DEM = 'hillshade-dem';
export const IMAGERY = 'imagery';

export function buildStyle(
  sets: Tilesets,
  mode: LayerMode,
  look: Look,
  lang = 'en',
): StyleSpecification {
  const by = new Map(sets.tilesets.map((t) => [t.id, t]));
  const flavor = flavorFor(look);
  const sources: Record<string, SourceSpecification> = {};
  const out: LayerSpecification[] = [];
  const basemap = by.get('basemap');
  const terrain = by.get('terrain');
  const imagery = by.get('imagery');
  if (basemap) sources[BASEMAP] = source(basemap, 'vector');
  if (terrain) {
    // Separate sources for 3D terrain and the hillshade: MapLibre advises against sharing one.
    sources[DEM] = source(terrain, 'raster-dem');
    sources[HILLSHADE_DEM] = source(terrain, 'raster-dem');
  }
  if (imagery) sources[IMAGERY] = source(imagery, 'raster');

  const satellite = (mode === 'satellite' || mode === 'hybrid') && imagery;
  if (satellite) {
    out.push({ id: 'background', type: 'background', paint: { 'background-color': '#000' } });
    out.push({
      id: 'imagery',
      type: 'raster',
      source: IMAGERY,
      paint: { 'raster-fade-duration': 150 },
    });
    if (mode === 'hybrid' && basemap)
      out.push(...layers(BASEMAP, flavor, { labelsOnly: true, lang }));
  } else if (basemap) {
    const base = layers(BASEMAP, flavor, { lang });
    if (mode === 'terrain' && terrain) {
      // Relief goes over the land cover and under water, roads and labels.
      const at = base.findIndex((l) => l.id.startsWith('water'));
      const shade: LayerSpecification = {
        id: 'hillshade',
        type: 'hillshade',
        source: HILLSHADE_DEM,
        paint: {
          'hillshade-exaggeration': look.theme.startsWith('phosphor') ? 0.35 : 0.5,
          'hillshade-shadow-color':
            look.dark || look.theme.startsWith('phosphor') ? '#000' : '#473b24',
          'hillshade-highlight-color': look.theme.startsWith('phosphor')
            ? flavor.boundaries
            : '#ffffff',
          'hillshade-accent-color': look.dark ? '#000' : '#5a4d36',
        },
      };
      base.splice(at < 0 ? base.length : at, 0, shade);
    }
    out.push(...base);
  }
  const fonts = sets.fonts.length ? sets.fonts : ['Noto Sans Regular'];
  const style: StyleSpecification = {
    version: 8,
    name: `Whisipedia Atlas (${look.theme}${look.dark ? ', dark' : ''})`,
    sources,
    layers: out,
    ...(sets.glyphs ? { glyphs: absolute(sets.glyphs) } : {}),
    ...(sets.sprite
      ? {
          sprite: absolute(
            `${sets.sprite}/${look.dark || look.theme.startsWith('phosphor') ? 'dark' : 'light'}`,
          ),
        }
      : {}),
  };
  void fonts;
  return style;
}
