import { describe, expect, it } from 'vitest';
import type { Tilesets } from '../src/api/types.ts';
import { parseOverlay } from '../src/atlas/overlay.ts';
import { absolute, availableModes, buildStyle, flavorFor } from '../src/atlas/style.ts';
import { atlasAddress, formatView, parseAtlas, parseView } from '../src/atlas/view-state.ts';
import { resolveRoute } from '../src/core/router.ts';

const SETS: Tilesets = {
  tilesets: [
    {
      id: 'basemap',
      kind: 'vector',
      encoding: '',
      tile_type: 'mvt',
      tile_size: 512,
      min_zoom: 0,
      max_zoom: 15,
      held_max_zoom: 15,
      bounds: [-180, -85, 180, 85],
      archives: 1,
      attribution: '© OpenStreetMap',
      tiles: ['/v1/maps/tiles/basemap/{z}/{x}/{y}'],
    },
    {
      id: 'terrain',
      kind: 'raster-dem',
      encoding: 'terrarium',
      tile_type: 'webp',
      tile_size: 512,
      min_zoom: 0,
      max_zoom: 16,
      held_max_zoom: 12,
      bounds: [-180, -85, 180, 85],
      archives: 6,
      attribution: '© Mapterhorn',
      tiles: ['/v1/maps/tiles/terrain/{z}/{x}/{y}'],
    },
  ],
  glyphs: '/v1/maps/glyphs/{fontstack}/{range}.pbf',
  sprite: '/v1/maps/sprites/v4',
  fonts: ['Noto Sans Regular'],
};

describe('the Atlas address', () => {
  it('round-trips a view', () => {
    const v = parseView('@46.6863,7.8632,12.5z,30b,45p');
    expect(v).toEqual({ lat: 46.6863, lon: 7.8632, zoom: 12.5, bearing: 30, pitch: 45 });
    expect(formatView(v ?? { lat: 0, lon: 0, zoom: 0, bearing: 0, pitch: 0 })).toBe(
      '@46.6863,7.8632,12.5z,30b,45p',
    );
    expect(formatView({ lat: 1.123456789, lon: 2, zoom: 3, bearing: 0, pitch: 0 })).toBe(
      '@1.12346,2,3z',
    );
    expect(parseView('@95,0,3z')).toBeUndefined();
    expect(parseView('Interlaken')).toBeUndefined();
  });
  it('reads layer, query and pin, and is a route of its own', () => {
    const r = parseAtlas('@46.6,7.8,9z', { layer: 'terrain', q: ' Bern ', pin: '46.9,7.4' });
    expect(r.layer).toBe('terrain');
    expect(r.query).toBe('Bern');
    expect(r.pin).toEqual({ lat: 46.9, lon: 7.4 });
    expect(parseAtlas('', { layer: 'lava' }).layer).toBeUndefined();
    expect(resolveRoute('wsp://maps')).toMatchObject({ page: 'atlas' });
    expect(resolveRoute('wsp://atlas/@1,2,3z?layer=hybrid')).toMatchObject({
      page: 'atlas',
      atlas: { layer: 'hybrid' },
    });
    expect(resolveRoute('wsp://osm/node/1')).toMatchObject({
      page: 'document',
      source: 'osm-planet',
    });
    expect(atlasAddress({ lat: 1, lon: 2, zoom: 3, bearing: 0, pitch: 0 }, 'terrain')).toBe(
      'wsp://atlas/@1,2,3z?layer=terrain',
    );
  });
});

describe('the Atlas styles', () => {
  it('offers only the layers the library holds', () => {
    expect(availableModes(SETS)).toEqual(['map', 'terrain']);
    expect(availableModes({ ...SETS, tilesets: [] })).toEqual([]);
  });
  it('keeps URL placeholders intact', () => {
    expect(absolute('/v1/maps/glyphs/{fontstack}/{range}.pbf')).toMatch(
      /\/v1\/maps\/glyphs\/\{fontstack\}\/\{range\}\.pbf$/,
    );
    const style = buildStyle(SETS, 'terrain', {
      theme: 'navigator96',
      dark: false,
      highContrast: false,
    });
    expect(style.glyphs).toMatch(/\{fontstack\}\/\{range\}/);
    const basemap = style.sources.protomaps as { tiles: string[] };
    expect(basemap.tiles[0]).toMatch(/\/\{z\}\/\{x\}\/\{y\}$/);
    expect(style.layers.some((l) => l.type === 'hillshade')).toBe(true);
    // Relief lies under the water and the roads.
    const ids = style.layers.map((l) => l.id);
    expect(ids.indexOf('hillshade')).toBeLessThan(ids.findIndex((id) => id.startsWith('water')));
    expect(String(style.sprite)).toMatch(/sprites\/v4\/light$/);
  });
  it('gives each theme its own palette', () => {
    const looks = [
      'navigator96',
      'mosaic93',
      'encarta95',
      'phosphor-amber',
      'phosphor-green',
      'system7',
    ].map((theme) => JSON.stringify(flavorFor({ theme, dark: false, highContrast: false })));
    expect(new Set(looks).size).toBe(6);
    const green = flavorFor({ theme: 'phosphor-green', dark: false, highContrast: false });
    expect(green.background).toBe('#000000');
    // A vector screen: roads glow brighter than the land around them.
    const lum = (hex: string) => Number.parseInt(hex.slice(3, 5), 16);
    expect(lum(green.highway)).toBeGreaterThan(lum(green.park_a));
    const hc = flavorFor({ theme: 'navigator96', dark: false, highContrast: true });
    expect(hc.earth).toBe('#000000');
    expect(hc.city_label).toBe('#ffffff');
  });
});

describe('files drawn over the map', () => {
  it('reads GPX tracks and waypoints, KML placemarks and GeoJSON', () => {
    const gpx = parseOverlay(
      'walk.gpx',
      '<gpx xmlns="http://www.topografix.com/GPX/1/1"><wpt lat="1" lon="2"><name>A</name></wpt><trk><trkseg><trkpt lat="1" lon="2"/><trkpt lat="1.1" lon="2.1"/></trkseg></trk></gpx>',
    );
    expect(typeof gpx === 'string' ? gpx : gpx.features.map((f) => f.geometry.type)).toEqual([
      'Point',
      'LineString',
    ]);
    const kml = parseOverlay(
      'p.kml',
      '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>P</name><Point><coordinates>7.86,46.68,0</coordinates></Point></Placemark></kml>',
    );
    expect(typeof kml === 'string' ? kml : kml.features[0]?.geometry).toEqual({
      type: 'Point',
      coordinates: [7.86, 46.68],
    });
    const gj = parseOverlay(
      'x.geojson',
      JSON.stringify({
        type: 'MultiPoint',
        coordinates: [
          [1, 2],
          [3, 4],
        ],
      }),
    );
    expect(typeof gj === 'string' ? gj : gj.features.length).toBe(2);
  });
  it('says what is wrong with a file it cannot draw', () => {
    expect(parseOverlay('a.gpx', '<gpx')).toMatch(/well-formed/);
    expect(parseOverlay('a.xml', '<svg xmlns="http://www.w3.org/2000/svg"/>')).toMatch(/Not a GPX/);
    expect(parseOverlay('a.json', '{"type":"FeatureCollection","features":[]}')).toMatch(
      /no points/,
    );
  });
});

describe('geo: links', () => {
  it('open the Atlas at the point, pinned', async () => {
    const { geoAddress, resolveHref } = await import('../src/core/links.ts');
    expect(geoAddress('geo:46.68333333333333,7.85')).toBe(
      'wsp://atlas/@46.683333,7.85,13z?pin=46.683333,7.85',
    );
    expect(geoAddress('geo:-33.8568,151.2153,58;u=35?z=16')).toBe(
      'wsp://atlas/@-33.8568,151.2153,16z?pin=-33.8568,151.2153',
    );
    expect(geoAddress('geo:0,0?q=Interlaken')).toBeNull();
    expect(geoAddress('geo:95,0')).toBeNull();
    expect(
      resolveHref('geo:46.68,7.85', { sourceId: 'wikipedia-top10', nativeId: 'en/Interlaken' }),
    ).toEqual({
      kind: 'local',
      address: 'wsp://atlas/@46.68,7.85,13z?pin=46.68,7.85',
    });
    const r = parseAtlas('@46.68,7.85,13z', { pin: '46.68,7.85' });
    expect(r.pin).toEqual({ lat: 46.68, lon: 7.85 });
  });
});

describe('coordinates in articles', () => {
  it("a Wikipedia article's Coordinates link survives sanitizing and opens the Atlas", async () => {
    const { sanitizeDocumentHtml } = await import('../src/core/links.ts');
    const html =
      '<p>Coordinates: <a rel="mw:ExtLink" href="geo:46.68333333333333,7.85" class="external text"><span class="geo">46.683; 7.850</span></a></p>';
    const { fragment } = sanitizeDocumentHtml(html, {
      sourceId: 'wikipedia-top10',
      nativeId: 'en/Interlaken',
    });
    const a = fragment.querySelector('a');
    expect(a?.getAttribute('href')).toBe('#wsp://atlas/@46.683333,7.85,13z?pin=46.683333,7.85');
    expect(a?.classList.contains('broken')).toBe(false);
  });
});
