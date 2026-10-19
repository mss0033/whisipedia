/**
 * The Atlas's map (MapLibre GL, loaded only on map pages). Everything it draws comes from the
 * library (`/v1/maps/...`); nothing is fetched from outside.
 *
 * Toolbar: find a place or coordinates, layers (Map, Terrain, Satellite, Hybrid: only those held),
 * 3D terrain, globe, measure, open a GPX/KML/GeoJSON file, saved places, save the view as a PNG and
 * the coordinate form. A click shows what is there (coordinates in every form, elevation, the
 * feature, nearby places with their articles); the status line follows the cursor. The address bar
 * keeps the view (`wsp://atlas/@lat,lon,zoomz`), so Back, bookmarks and links return to it.
 */
import 'maplibre-gl/dist/maplibre-gl.css';
import '../styles/atlas.css';
import {
  AttributionControl,
  FullscreenControl,
  type GeoJSONSource,
  GeolocateControl,
  type LngLatLike,
  Map as MapLibreMap,
  type MapMouseEvent,
  Marker,
  NavigationControl,
  ScaleControl,
  setWorkerUrl,
} from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import type { WspClient } from '../api/client.ts';
import { ApiRequestError } from '../api/client.ts';
import type { PlaceHit, Tilesets } from '../api/types.ts';
import { h, s } from '../core/dom.ts';
import {
  COORD_FORMATS,
  type CoordFormat,
  distance,
  formatArea,
  formatCoords,
  formatDistance,
  type LatLon,
  parseCoords,
  pathLength,
  polygonArea,
  terrarium,
} from '../core/geo.ts';
import type { SafeStorage } from '../core/storage.ts';
import type { ThemeManager } from '../core/theme.ts';
import { type Feature, type Overlay, bounds as overlayBounds, parseOverlay } from './overlay.ts';
import {
  absolute,
  availableModes,
  buildStyle,
  DEM,
  LAYER_MODES,
  type LayerMode,
  type Look,
  markColors,
} from './style.ts';
import { type AtlasRoute, type AtlasView, atlasAddress, WORLD_VIEW } from './view-state.ts';

/** From this zoom on, a place search favours the places around the map's centre. */
const NEAR_SEARCH_ZOOM = 6;

setWorkerUrl(workerUrl);

export interface AtlasOptions {
  api: WspClient;
  storage: SafeStorage;
  theme: ThemeManager;
  sets: Tilesets;
  route: AtlasRoute;
  signal: AbortSignal;
  /** The view changed: keep it in the address bar (no navigation). */
  onAddress(address: string): void;
  navigate(address: string): void;
  status(text: string): void;
}

interface SavedPlace {
  name: string;
  lat: number;
  lon: number;
  zoom: number;
}

const SAVED_KEY = 'atlas.saved';
const PREFS_KEY = 'atlas.prefs';
const MEASURE = 'atlas-measure';
const OVERLAY = 'atlas-overlay';
const PROFILE_SAMPLES = 96;
const ELEVATION_ZOOM = 14;

interface Prefs {
  format: CoordFormat;
  imperial: boolean;
  mode: LayerMode;
}

function isSaved(v: unknown): v is SavedPlace[] {
  return (
    Array.isArray(v) &&
    v.every(
      (p) =>
        p &&
        typeof p === 'object' &&
        typeof (p as SavedPlace).name === 'string' &&
        Number.isFinite((p as SavedPlace).lat) &&
        Number.isFinite((p as SavedPlace).lon),
    )
  );
}

/** Zoom to show a place of this kind. */
function zoomFor(kind: string): number {
  const k = kind.replace(/^place=/, '');
  return (
    {
      country: 5,
      state: 6.5,
      region: 7,
      province: 7,
      archipelago: 7,
      county: 9,
      district: 10,
      city: 11,
      municipality: 11.5,
      town: 12.5,
      island: 11,
      borough: 13,
      suburb: 13.5,
      village: 14,
      quarter: 14.5,
      hamlet: 15,
      neighbourhood: 15,
      locality: 15,
    }[k] ?? 15.5
  );
}

function kindLabel(kind: string): string {
  const [key, value = ''] = kind.split('=');
  const v = value.replace(/_/g, ' ');
  return key === 'place' ? v : `${v} (${(key ?? '').replace(/_/g, ' ')})`;
}

// --- elevation from the terrain tiles -------------------------------------------------------------

class Elevation {
  private readonly cache = new Map<string, Promise<ImageData | null>>();
  constructor(
    private readonly template: string | undefined,
    private readonly maxZoom: number,
  ) {}

  get available(): boolean {
    return this.template !== undefined;
  }

  private tile(z: number, x: number, y: number): Promise<ImageData | null> {
    const key = `${z}/${x}/${y}`;
    let hit = this.cache.get(key);
    if (!hit) {
      const url = (this.template ?? '')
        .replace('{z}', String(z))
        .replace('{x}', String(x))
        .replace('{y}', String(y));
      hit = fetch(url)
        .then(async (res) => {
          if (!res.ok || res.status === 204) return null;
          const bmp = await createImageBitmap(await res.blob());
          const canvas = new OffscreenCanvas(bmp.width, bmp.height);
          const ctx = canvas.getContext('2d', { willReadFrequently: true });
          if (!ctx) return null;
          ctx.drawImage(bmp, 0, 0);
          return ctx.getImageData(0, 0, bmp.width, bmp.height);
        })
        .catch(() => null);
      this.cache.set(key, hit);
      if (this.cache.size > 64) {
        const first = this.cache.keys().next().value;
        if (first !== undefined) this.cache.delete(first);
      }
    }
    return hit;
  }

  /** Metres above sea level at a point, or null where the library holds no terrain. */
  async at(p: LatLon): Promise<number | null> {
    if (!this.template) return null;
    const z = Math.min(ELEVATION_ZOOM, this.maxZoom);
    const n = 2 ** z;
    const fx = ((p.lon + 180) / 360) * n;
    const lat = (p.lat * Math.PI) / 180;
    const fy = ((1 - Math.asinh(Math.tan(lat)) / Math.PI) / 2) * n;
    const x = Math.floor(fx);
    const y = Math.floor(fy);
    if (y < 0 || y >= n) return null;
    const img = await this.tile(z, ((x % n) + n) % n, y);
    if (!img) return null;
    const px = Math.min(img.width - 1, Math.floor((fx - x) * img.width));
    const py = Math.min(img.height - 1, Math.floor((fy - y) * img.height));
    const i = (py * img.width + px) * 4;
    return terrarium(img.data[i] ?? 0, img.data[i + 1] ?? 0, img.data[i + 2] ?? 0);
  }
}

// --- the view -------------------------------------------------------------------------------------

export class Atlas {
  readonly root: HTMLElement;
  private readonly map: MapLibreMap;
  private readonly opts: AtlasOptions;
  private readonly modes: LayerMode[];
  private prefs: Prefs;
  private mode: LayerMode;
  private terrain3d = false;
  private globe = false;
  private pin: Marker | null = null;
  private pinAt: LatLon | null = null;
  private measuring: 'distance' | 'area' | null = null;
  private measurePoints: LatLon[] = [];
  private overlays: Overlay[] = [];
  private readonly elevation: Elevation;
  private readonly panel: HTMLElement;
  private readonly panelTitle: HTMLElement;
  private readonly panelBody: HTMLElement;
  private readonly statusCoords: HTMLElement;
  private readonly statusExtra: HTMLElement;
  private readonly search: HTMLInputElement;
  private readonly modeButtons = new Map<LayerMode, HTMLButtonElement>();
  private readonly toolButtons = new Map<string, HTMLButtonElement>();
  private addressTimer: ReturnType<typeof setTimeout> | undefined;
  private readonly cleanups: (() => void)[] = [];

  constructor(container: HTMLElement, opts: AtlasOptions) {
    this.opts = opts;
    this.modes = availableModes(opts.sets);
    this.prefs = opts.storage.read<Prefs>(PREFS_KEY, {
      format: 'decimal',
      imperial: false,
      mode: 'map',
    });
    const wanted = opts.route.layer ?? this.prefs.mode;
    this.mode = this.modes.includes(wanted) ? wanted : (this.modes[0] ?? 'map');
    const terrain = opts.sets.tilesets.find((t) => t.id === 'terrain');
    this.elevation = new Elevation(
      terrain ? absolute(terrain.tiles[0] ?? '') : undefined,
      terrain?.max_zoom ?? 0,
    );

    this.search = h('input', {
      type: 'search',
      class: 'search-input atlas-search',
      placeholder: 'Place, or coordinates (46.68, 7.86 / 32TMS1306970932)',
      'aria-label': 'Find a place or coordinates',
      // The navigator's own "/" shortcut focuses the page's search box.
      'data-search-box': '',
      autocomplete: 'off',
      size: 32,
    });
    const form = h(
      'form',
      {
        class: 'atlas-find',
        role: 'search',
        onsubmit: (e: Event) => {
          e.preventDefault();
          void this.find(this.search.value);
        },
      },
      this.search,
      h('button', { type: 'submit', class: 'btn btn-small' }, 'Find'),
    );
    const layers = h('div', { class: 'atlas-group', role: 'group', 'aria-label': 'Layers' });
    for (const m of LAYER_MODES) {
      if (!this.modes.includes(m.id)) continue;
      const b = h(
        'button',
        {
          type: 'button',
          class: 'btn btn-small',
          'aria-pressed': String(m.id === this.mode),
          onclick: () => this.setMode(m.id),
        },
        m.label,
      );
      this.modeButtons.set(m.id, b);
      layers.append(b);
    }
    const tool = (id: string, label: string, title: string, fn: () => void, pressable = false) => {
      const b = h(
        'button',
        {
          type: 'button',
          class: 'btn btn-small',
          title,
          ...(pressable ? { 'aria-pressed': 'false' } : {}),
          onclick: fn,
        },
        label,
      );
      this.toolButtons.set(id, b);
      return b;
    };
    const fileInput = h('input', {
      type: 'file',
      accept: '.gpx,.kml,.geojson,.json',
      class: 'visually-hidden',
      'aria-label': 'Open a GPX, KML or GeoJSON file',
      onchange: () => void this.openFiles(fileInput),
    });
    const format = h(
      'select',
      {
        class: 'atlas-format',
        'aria-label': 'Coordinate form',
        onchange: () => this.setFormat(format.value as CoordFormat),
      },
      ...COORD_FORMATS.map((f) =>
        h('option', { value: f.id, selected: f.id === this.prefs.format }, f.label),
      ),
    );
    const tools = h(
      'div',
      { class: 'atlas-group', role: 'group', 'aria-label': 'Tools' },
      terrain
        ? tool('3d', '3D', 'Tilt the map over the terrain', () => this.toggle3d(), true)
        : null,
      tool(
        'globe',
        'Globe',
        'Show the Earth as a globe when zoomed out',
        () => this.toggleGlobe(),
        true,
      ),
      tool(
        'distance',
        'Measure',
        'Measure a distance (click points; double-click to finish)',
        () => this.startMeasure('distance'),
        true,
      ),
      tool(
        'area',
        'Area',
        'Measure an area (click corners; double-click to finish)',
        () => this.startMeasure('area'),
        true,
      ),
      tool('open', 'Open…', 'Draw a GPX, KML or GeoJSON file from your disk', () =>
        fileInput.click(),
      ),
      tool('saved', 'Places', 'Your saved places', () => this.showSaved()),
      tool('image', 'Save image', 'Save the view as a PNG', () => this.saveImage()),
      fileInput,
    );
    const bar = h(
      'div',
      { class: 'atlas-bar' },
      form,
      layers,
      tools,
      h('label', { class: 'atlas-format-label' }, 'Coordinates ', format),
    );

    this.panelTitle = h('span', { class: 'atlas-panel-title' });
    this.panelBody = h('div', { class: 'atlas-panel-body' });
    this.panel = h(
      'aside',
      { class: 'atlas-panel', hidden: true, 'aria-live': 'polite' },
      h(
        'div',
        { class: 'atlas-panel-titlebar' },
        this.panelTitle,
        h(
          'button',
          {
            type: 'button',
            class: 'btn btn-small',
            'aria-label': 'Close panel',
            onclick: () => this.closePanel(),
          },
          '×',
        ),
      ),
      this.panelBody,
    );
    const mapEl = h('div', { class: 'atlas-map', tabindex: '0', 'aria-label': 'Map' });
    this.statusCoords = h('span', { class: 'atlas-status-coords' }, 'Point at the map');
    this.statusExtra = h('span', { class: 'atlas-status-extra' });
    const status = h('div', { class: 'atlas-status' }, this.statusCoords, this.statusExtra);
    this.root = h(
      'div',
      { class: 'atlas' },
      bar,
      h('div', { class: 'atlas-body' }, mapEl, this.panel),
      status,
    );
    container.replaceChildren(this.root);

    const view: AtlasView =
      opts.route.view ??
      (opts.route.pin ? { ...opts.route.pin, zoom: 13, bearing: 0, pitch: 0 } : WORLD_VIEW);
    const motion = !opts.theme.reducedMotion();
    this.map = new MapLibreMap({
      container: mapEl,
      style: buildStyle(opts.sets, this.mode, this.look()),
      center: [view.lon, view.lat],
      zoom: view.zoom,
      bearing: view.bearing,
      pitch: view.pitch,
      maxPitch: 85,
      attributionControl: false,
      fadeDuration: motion ? 300 : 0,
      cooperativeGestures: false,
    });
    this.map.addControl(new NavigationControl({ visualizePitch: true }), 'top-right');
    this.map.addControl(new FullscreenControl({ container: this.root }), 'top-right');
    this.map.addControl(new GeolocateControl({ trackUserLocation: false }), 'top-right');
    this.map.addControl(
      new ScaleControl({ unit: this.prefs.imperial ? 'imperial' : 'metric', maxWidth: 140 }),
      'bottom-left',
    );
    // The default attribution links to maplibre.org, outside the library: credit the sources only.
    this.map.addControl(
      new AttributionControl({ compact: true, customAttribution: 'Whisipedia Atlas' }),
      'bottom-right',
    );

    this.map.on('style.load', () => this.restoreOwnLayers());
    this.map.on('mousemove', (e) => this.onMove(e));
    this.map.on('click', (e) => void this.onClick(e));
    this.map.on('dblclick', (e) => {
      if (this.measuring) {
        e.preventDefault();
        this.finishMeasure();
      }
    });
    this.map.on('moveend', () => this.keepAddress());
    this.map.on('zoom', () => this.showZoom());
    this.showZoom();

    const keys = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (this.measuring) this.finishMeasure();
      else this.closePanel();
    };
    this.root.addEventListener('keydown', keys);
    this.cleanups.push(opts.theme.onChange(() => this.restyle()));
    opts.signal.addEventListener('abort', () => this.destroy(), { once: true });

    if (opts.route.pin)
      this.map.once('load', () => void this.whatsHere(opts.route.pin as LatLon, false));
    if (opts.route.query) {
      this.search.value = opts.route.query;
      this.map.once('load', () => void this.find(opts.route.query));
    }
  }

  private look(): Look {
    const p = this.opts.theme.current;
    return { theme: p.theme, dark: this.opts.theme.dark(), highContrast: p.highContrast };
  }

  private savePrefs(): void {
    this.opts.storage.write(PREFS_KEY, this.prefs);
  }

  destroy(): void {
    clearTimeout(this.addressTimer);
    for (const c of this.cleanups) c();
    this.map.remove();
  }

  // --- layers and looks ---

  private setMode(mode: LayerMode): void {
    if (mode === this.mode) return;
    this.mode = mode;
    this.prefs.mode = mode;
    this.savePrefs();
    for (const [m, b] of this.modeButtons) b.setAttribute('aria-pressed', String(m === mode));
    this.restyle();
    this.keepAddress();
  }

  private restyle(): void {
    this.map.setStyle(buildStyle(this.opts.sets, this.mode, this.look()), { diff: false });
  }

  /** Our own sources and layers (pin, measure, overlays, terrain, projection) after a new style. */
  private restoreOwnLayers(): void {
    const colors = markColors(this.look());
    const map = this.map;
    map.addSource(MEASURE, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    map.addLayer({
      id: `${MEASURE}-fill`,
      type: 'fill',
      source: MEASURE,
      filter: ['==', '$type', 'Polygon'],
      paint: { 'fill-color': colors.fill },
    });
    map.addLayer({
      id: `${MEASURE}-line`,
      type: 'line',
      source: MEASURE,
      filter: ['!=', '$type', 'Point'],
      paint: { 'line-color': colors.line, 'line-width': 2.5, 'line-dasharray': [2, 1] },
    });
    map.addLayer({
      id: `${MEASURE}-points`,
      type: 'circle',
      source: MEASURE,
      filter: ['==', '$type', 'Point'],
      paint: {
        'circle-radius': 4,
        'circle-color': colors.line,
        'circle-stroke-color': colors.halo,
        'circle-stroke-width': 1.5,
      },
    });
    this.drawMeasure();
    this.overlays.forEach((o, i) => {
      this.drawOverlay(o, i);
    });
    if (this.terrain3d && map.getSource(DEM)) map.setTerrain({ source: DEM, exaggeration: 1.3 });
    map.setProjection({ type: this.globe ? 'globe' : 'mercator' });
  }

  private toggle3d(): void {
    this.terrain3d = !this.terrain3d;
    this.toolButtons.get('3d')?.setAttribute('aria-pressed', String(this.terrain3d));
    if (this.terrain3d) {
      if (this.mode === 'map' && this.modes.includes('terrain')) this.setMode('terrain');
      if (this.map.getSource(DEM)) this.map.setTerrain({ source: DEM, exaggeration: 1.3 });
      if (this.map.getPitch() < 30)
        this.map.easeTo({ pitch: 60, duration: this.opts.theme.reducedMotion() ? 0 : 800 });
    } else {
      this.map.setTerrain(null);
      this.map.easeTo({ pitch: 0, duration: this.opts.theme.reducedMotion() ? 0 : 500 });
    }
  }

  private toggleGlobe(): void {
    this.globe = !this.globe;
    this.toolButtons.get('globe')?.setAttribute('aria-pressed', String(this.globe));
    this.map.setProjection({ type: this.globe ? 'globe' : 'mercator' });
  }

  private setFormat(f: CoordFormat): void {
    this.prefs.format = f;
    this.savePrefs();
    if (this.pinAt && !this.panel.hidden && this.panelTitle.textContent === "What's here")
      void this.whatsHere(this.pinAt, false);
  }

  // --- address and status ---

  private currentView(): AtlasView {
    const c = this.map.getCenter();
    return {
      lat: c.lat,
      lon: c.lng,
      zoom: this.map.getZoom(),
      bearing: this.map.getBearing(),
      pitch: this.map.getPitch(),
    };
  }

  private keepAddress(): void {
    clearTimeout(this.addressTimer);
    this.addressTimer = setTimeout(() => {
      const pin = this.pinAt
        ? `${this.pinAt.lat.toFixed(6)},${this.pinAt.lon.toFixed(6)}`
        : undefined;
      this.opts.onAddress(atlasAddress(this.currentView(), this.mode, pin ? { pin } : {}));
    }, 400);
  }

  private showZoom(): void {
    this.statusExtra.textContent = `Zoom ${this.map.getZoom().toFixed(1)}`;
  }

  private onMove(e: MapMouseEvent): void {
    const p = { lat: e.lngLat.lat, lon: e.lngLat.wrap().lng };
    let text = formatCoords(p, this.prefs.format);
    if (this.measuring && this.measurePoints.length) {
      const last = this.measurePoints[this.measurePoints.length - 1];
      if (last)
        text += ` · ${formatDistance(pathLength(this.measurePoints) + distance(last, p), this.prefs.imperial)} so far`;
    }
    this.statusCoords.textContent = text;
  }

  // --- clicks: what is here, or a measure point ---

  private async onClick(e: MapMouseEvent): Promise<void> {
    const p = { lat: e.lngLat.lat, lon: e.lngLat.wrap().lng };
    if (this.measuring) {
      // A double-click (finish) also sends its clicks: its second click adds no point.
      if (e.originalEvent.detail >= 2) return;
      this.measurePoints.push(p);
      this.drawMeasure();
      this.showMeasure();
      return;
    }
    const features = this.map.queryRenderedFeatures(e.point).filter((f) => f.properties?.name);
    await this.whatsHere(
      p,
      true,
      features.map((f) => ({
        name: String(f.properties?.name),
        kind: String(f.properties?.kind ?? f.sourceLayer ?? ''),
      })),
    );
  }

  private setPin(p: LatLon): void {
    this.pinAt = p;
    if (!this.pin) this.pin = new Marker({ color: markColors(this.look()).line });
    this.pin.setLngLat([p.lon, p.lat]).addTo(this.map);
    this.keepAddress();
  }

  private async whatsHere(
    p: LatLon,
    move: boolean,
    features: { name: string; kind: string }[] = [],
  ): Promise<void> {
    this.setPin(p);
    if (move && this.map.getZoom() < 4)
      this.map.easeTo({ center: [p.lon, p.lat] as LngLatLike, zoom: 6 });
    const rows = h('table', { class: 'atlas-coords' });
    for (const f of COORD_FORMATS) {
      const value = formatCoords(p, f.id);
      rows.append(
        h(
          'tr',
          null,
          h('th', { scope: 'row' }, f.label),
          h('td', null, h('code', null, value)),
          h(
            'td',
            null,
            h(
              'button',
              { type: 'button', class: 'btn btn-small', onclick: () => void this.copy(value) },
              'Copy',
            ),
          ),
        ),
      );
    }
    const elevation = h(
      'p',
      { class: 'atlas-elevation' },
      this.elevation.available ? 'Elevation: reading…' : '',
    );
    const seen = new Set<string>();
    const here = features.filter((f) => !seen.has(f.name) && seen.add(f.name)).slice(0, 4);
    const nearby = h('div', { class: 'atlas-nearby' }, h('p', null, 'Looking for places nearby…'));
    const link = atlasAddress(this.currentView(), this.mode, {
      pin: `${p.lat.toFixed(6)},${p.lon.toFixed(6)}`,
    });
    this.openPanel(
      "What's here",
      here.length
        ? h(
            'p',
            { class: 'atlas-here' },
            ...here.flatMap((f, i) => [
              i ? ', ' : '',
              h('strong', null, f.name),
              f.kind ? ` (${f.kind.replace(/_/g, ' ')})` : '',
            ]),
          )
        : null,
      rows,
      elevation,
      h(
        'p',
        { class: 'atlas-actions' },
        h(
          'button',
          {
            type: 'button',
            class: 'btn btn-small',
            onclick: () => this.savePlace(here[0]?.name ?? formatCoords(p, 'decimal'), p),
          },
          'Save place',
        ),
        ' ',
        h(
          'button',
          { type: 'button', class: 'btn btn-small', onclick: () => void this.copy(link) },
          'Copy link',
        ),
      ),
      h('h3', null, 'Nearby'),
      nearby,
    );
    if (this.elevation.available) {
      const m = await this.elevation.at(p);
      elevation.textContent =
        m === null
          ? 'Elevation: no terrain held here'
          : `Elevation: ${Math.round(m)} m (${Math.round(m / 0.3048)} ft)`;
    }
    try {
      const found = await this.opts.api.nearby(p.lat, p.lon, 10000, 8, this.opts.signal);
      nearby.replaceChildren(
        found.places.length
          ? this.placeList(found.places, true)
          : h('p', null, 'No named places within 10 km in the place index.'),
      );
    } catch (err) {
      nearby.replaceChildren(h('p', null, this.placeIndexProblem(err)));
    }
  }

  private placeIndexProblem(err: unknown): string {
    if (err instanceof ApiRequestError && err.isBusy)
      return 'The place index is not built yet; places appear here once it is.';
    return 'The place index could not be read.';
  }

  private placeList(places: PlaceHit[], withDistance: boolean): HTMLElement {
    const list = h('ol', { class: 'atlas-places' });
    for (const pl of places) {
      const go = h(
        'button',
        {
          type: 'button',
          class: 'linkish',
          onclick: () => {
            this.flyTo({ lat: pl.lat, lon: pl.lon }, zoomFor(pl.kind));
            void this.whatsHere({ lat: pl.lat, lon: pl.lon }, false);
          },
        },
        pl.name,
      );
      const facts = [kindLabel(pl.kind)];
      if (pl.population) facts.push(`pop. ${pl.population.toLocaleString()}`);
      if (withDistance && pl.distance_m !== null)
        facts.push(formatDistance(pl.distance_m, this.prefs.imperial));
      const article = h('span', { class: 'atlas-article' });
      const item = h('li', null, go, ` · ${facts.join(' · ')} `, article);
      if (pl.wikipedia) void this.articleLink(pl.wikipedia, article);
      list.append(item);
    }
    return list;
  }

  /** The library's copy of a place's Wikipedia article (OSM's `wikipedia=en:Title`). */
  private async articleLink(tag: string, into: HTMLElement): Promise<void> {
    const [lang, ...rest] = tag.split(':');
    const title = rest.join(':').trim();
    if (!lang || !title) return;
    const url = `https://${lang}.wikipedia.org/wiki/${encodeURIComponent(title.replace(/ /g, '_'))}`;
    try {
      const copy = await this.opts.api.outsideCopy(url, this.opts.signal);
      if (copy?.address) {
        into.replaceChildren(
          h(
            'a',
            {
              href: `#${copy.address}`,
              onclick: (e: Event) => {
                e.preventDefault();
                this.opts.navigate(copy.address);
              },
            },
            'article',
          ),
        );
      }
    } catch {
      /* no article: nothing to link */
    }
  }

  private flyTo(p: LatLon, zoom: number): void {
    const target = { center: [p.lon, p.lat] as LngLatLike, zoom };
    if (this.opts.theme.reducedMotion()) this.map.jumpTo(target);
    else this.map.flyTo({ ...target, speed: 1.6 });
  }

  // --- find ---

  private async find(text: string): Promise<void> {
    const q = text.trim();
    if (!q) return;
    const point = parseCoords(q);
    if (point) {
      this.flyTo(point, Math.max(this.map.getZoom(), 13));
      await this.whatsHere(point, false);
      return;
    }
    const results = h('div', null, h('p', null, `Looking for “${q}”…`));
    const library = h(
      'p',
      { class: 'atlas-library-search' },
      h('a', { href: `#wsp://search?q=${encodeURIComponent(q)}` }, `Search the library for “${q}”`),
    );
    this.openPanel('Places', results, library);
    try {
      // Seen from a region (not the whole world), the places around it first: "unter" from
      // Interlaken is Unterseen next door (2026-10-18).
      const c = this.map.getCenter();
      const near = this.map.getZoom() >= NEAR_SEARCH_ZOOM ? { lat: c.lat, lon: c.lng } : undefined;
      const found = await this.opts.api.places(q, 15, this.opts.signal, near);
      if (!found.places.length) {
        results.replaceChildren(h('p', null, `No place named “${q}” in the place index.`));
        return;
      }
      results.replaceChildren(this.placeList(found.places, false));
      const best = found.places[0];
      if (best) this.flyTo({ lat: best.lat, lon: best.lon }, zoomFor(best.kind));
    } catch (err) {
      results.replaceChildren(h('p', null, this.placeIndexProblem(err)));
    }
  }

  // --- measuring ---

  private startMeasure(kind: 'distance' | 'area'): void {
    if (this.measuring === kind) {
      this.finishMeasure();
      return;
    }
    this.measuring = kind;
    this.measurePoints = [];
    this.map.doubleClickZoom.disable();
    this.map.getCanvas().classList.add('atlas-crosshair');
    for (const id of ['distance', 'area'])
      this.toolButtons.get(id)?.setAttribute('aria-pressed', String(id === kind));
    this.drawMeasure();
    this.openPanel(
      kind === 'distance' ? 'Measure distance' : 'Measure area',
      h('p', null, 'Click points on the map; double-click or press Esc to finish.'),
    );
  }

  private finishMeasure(): void {
    this.measuring = null;
    this.map.getCanvas().classList.remove('atlas-crosshair');
    for (const id of ['distance', 'area'])
      this.toolButtons.get(id)?.setAttribute('aria-pressed', 'false');
    setTimeout(() => this.map.doubleClickZoom.enable(), 0);
    this.showMeasure(true);
  }

  private drawMeasure(): void {
    const src = this.map.getSource(MEASURE) as GeoJSONSource | undefined;
    if (!src) return;
    const coords = this.measurePoints.map((p) => [p.lon, p.lat] as [number, number]);
    const features: Feature[] = coords.map((c) => ({
      type: 'Feature',
      properties: { name: '' },
      geometry: { type: 'Point', coordinates: c },
    }));
    const area =
      this.measuring === 'area' || (this.measuring === null && this.measureKind === 'area');
    if (coords.length >= 2) {
      features.push({
        type: 'Feature',
        properties: { name: '' },
        geometry:
          area && coords.length >= 3
            ? { type: 'Polygon', coordinates: [[...coords, coords[0] as [number, number]]] }
            : { type: 'LineString', coordinates: coords },
      });
    }
    src.setData({ type: 'FeatureCollection', features });
  }

  private measureKind: 'distance' | 'area' = 'distance';

  private showMeasure(done = false): void {
    if (this.measuring) this.measureKind = this.measuring;
    const pts = this.measurePoints;
    const imperial = this.prefs.imperial;
    const parts: (HTMLElement | null)[] = [];
    if (this.measureKind === 'area') {
      const perim = pts.length >= 3 ? pathLength([...pts, pts[0] as LatLon]) : pathLength(pts);
      parts.push(
        h(
          'p',
          null,
          h('strong', null, formatArea(polygonArea(pts), imperial)),
          ` · perimeter ${formatDistance(perim, imperial)} · ${pts.length} corners`,
        ),
      );
    } else {
      parts.push(
        h(
          'p',
          null,
          h('strong', null, formatDistance(pathLength(pts), imperial)),
          ` · ${pts.length} points`,
        ),
      );
    }
    const units = h(
      'button',
      { type: 'button', class: 'btn btn-small', onclick: () => this.toggleUnits() },
      imperial ? 'Metric' : 'Imperial',
    );
    const clear = h(
      'button',
      { type: 'button', class: 'btn btn-small', onclick: () => this.clearMeasure() },
      'Clear',
    );
    parts.push(h('p', { class: 'atlas-actions' }, units, ' ', clear));
    const profile =
      done && this.measureKind === 'distance' && pts.length >= 2 && this.elevation.available
        ? h('div', { class: 'atlas-profile' }, 'Elevation profile: reading…')
        : null;
    parts.push(profile);
    this.openPanel(this.measureKind === 'area' ? 'Area' : 'Distance', ...parts);
    if (profile) void this.drawProfile(profile, pts);
  }

  private toggleUnits(): void {
    this.prefs.imperial = !this.prefs.imperial;
    this.savePrefs();
    this.showMeasure(!this.measuring);
  }

  private clearMeasure(): void {
    this.measurePoints = [];
    this.drawMeasure();
    this.closePanel();
  }

  /** Elevation along a path: samples spaced evenly by distance, drawn as an SVG profile. */
  private async drawProfile(into: HTMLElement, pts: LatLon[]): Promise<void> {
    const total = pathLength(pts);
    if (total <= 0) return;
    const samples: { d: number; p: LatLon }[] = [];
    for (let i = 0; i < PROFILE_SAMPLES; i++) {
      const want = (total * i) / (PROFILE_SAMPLES - 1);
      let acc = 0;
      for (let j = 1; j < pts.length; j++) {
        const a = pts[j - 1] as LatLon;
        const b = pts[j] as LatLon;
        const seg = distance(a, b);
        if (acc + seg >= want || j === pts.length - 1) {
          const t = seg > 0 ? Math.min(1, (want - acc) / seg) : 0;
          samples.push({
            d: want,
            p: { lat: a.lat + (b.lat - a.lat) * t, lon: a.lon + (b.lon - a.lon) * t },
          });
          break;
        }
        acc += seg;
      }
    }
    const heights = await Promise.all(samples.map((s0) => this.elevation.at(s0.p)));
    const known = heights.filter((v): v is number => v !== null);
    if (known.length < 2) {
      into.textContent = 'Elevation profile: no terrain held along this path.';
      return;
    }
    const lo = Math.min(...known);
    const hi = Math.max(...known);
    const span = Math.max(1, hi - lo);
    const W = 300;
    const H = 90;
    let path = '';
    samples.forEach((s0, i) => {
      const v = heights[i];
      if (v === null || v === undefined) return;
      const x = (s0.d / total) * W;
      const y = H - ((v - lo) / span) * (H - 8) - 4;
      path += `${path ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
    });
    let climb = 0;
    let descent = 0;
    for (let i = 1; i < heights.length; i++) {
      const a = heights[i - 1];
      const b = heights[i];
      if (a === null || a === undefined || b === null || b === undefined) continue;
      if (b > a) climb += b - a;
      else descent += a - b;
    }
    const svg = s(
      'svg',
      {
        class: 'atlas-profile-chart',
        viewBox: `0 0 ${W} ${H}`,
        role: 'img',
        'aria-label': `Elevation from ${Math.round(lo)} to ${Math.round(hi)} metres`,
      },
      s('path', { d: `${path}L${W},${H}L0,${H}Z`, class: 'atlas-profile-fill' }),
      s('path', { d: path, class: 'atlas-profile-line' }),
    );
    into.replaceChildren(
      h(
        'p',
        null,
        `Elevation ${Math.round(lo)}–${Math.round(hi)} m · climb ${Math.round(climb)} m · descent ${Math.round(descent)} m`,
      ),
      svg,
    );
  }

  // --- saved places ---

  private saved(): SavedPlace[] {
    return this.opts.storage.read<SavedPlace[]>(SAVED_KEY, [], isSaved);
  }

  private savePlace(name: string, p: LatLon): void {
    const list = this.saved().filter(
      (x) => !(Math.abs(x.lat - p.lat) < 1e-6 && Math.abs(x.lon - p.lon) < 1e-6),
    );
    list.unshift({ name, lat: p.lat, lon: p.lon, zoom: this.map.getZoom() });
    this.opts.storage.write(SAVED_KEY, list.slice(0, 200));
    this.opts.status(`Saved “${name}” to your places`);
  }

  private showSaved(): void {
    const list = this.saved();
    if (!list.length) {
      this.openPanel(
        'Your places',
        h(
          'p',
          null,
          'No saved places yet. Click the map, then “Save place”. They are kept in this browser only.',
        ),
      );
      return;
    }
    const ol = h('ol', { class: 'atlas-places' });
    list.forEach((pl, i) => {
      ol.append(
        h(
          'li',
          null,
          h(
            'button',
            {
              type: 'button',
              class: 'linkish',
              onclick: () => {
                this.flyTo(pl, pl.zoom);
                void this.whatsHere(pl, false);
              },
            },
            pl.name,
          ),
          ` · ${formatCoords(pl, this.prefs.format)} `,
          h(
            'button',
            {
              type: 'button',
              class: 'btn btn-small',
              'aria-label': `Forget ${pl.name}`,
              onclick: () => {
                const next = this.saved();
                next.splice(i, 1);
                this.opts.storage.write(SAVED_KEY, next);
                this.showSaved();
              },
            },
            'Forget',
          ),
        ),
      );
    });
    this.openPanel(
      'Your places',
      ol,
      h('p', { class: 'atlas-note' }, 'Kept in this browser only.'),
    );
  }

  // --- files ---

  private async openFiles(input: HTMLInputElement): Promise<void> {
    for (const file of Array.from(input.files ?? [])) {
      const parsed = parseOverlay(file.name, await file.text());
      if (typeof parsed === 'string') {
        this.openPanel('Open a file', h('p', null, `${file.name}: ${parsed}`));
        continue;
      }
      this.overlays.push(parsed);
      this.drawOverlay(parsed, this.overlays.length - 1);
      const [w, s0, e, n] = overlayBounds(parsed);
      this.map.fitBounds([w, s0, e, n], {
        padding: 60,
        maxZoom: 15,
        duration: this.opts.theme.reducedMotion() ? 0 : 900,
      });
      this.showOverlays();
    }
    input.value = '';
  }

  private drawOverlay(o: Overlay, i: number): void {
    const id = `${OVERLAY}-${i}`;
    if (this.map.getSource(id)) return;
    const c = markColors(this.look());
    this.map.addSource(id, {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: o.features },
    });
    this.map.addLayer({
      id: `${id}-fill`,
      type: 'fill',
      source: id,
      filter: ['==', '$type', 'Polygon'],
      paint: { 'fill-color': c.fill },
    });
    this.map.addLayer({
      id: `${id}-line`,
      type: 'line',
      source: id,
      filter: ['!=', '$type', 'Point'],
      paint: { 'line-color': c.line, 'line-width': 3 },
    });
    this.map.addLayer({
      id: `${id}-points`,
      type: 'circle',
      source: id,
      filter: ['==', '$type', 'Point'],
      paint: {
        'circle-radius': 5,
        'circle-color': c.line,
        'circle-stroke-color': c.halo,
        'circle-stroke-width': 1.5,
      },
    });
  }

  private showOverlays(): void {
    const ul = h('ul', null);
    for (const o of this.overlays) {
      const points = o.features.filter((f) => f.geometry.type === 'Point').length;
      const lines = o.features.filter((f) => f.geometry.type === 'LineString');
      const km = lines.reduce(
        (sum, f) =>
          sum +
          pathLength(
            (f.geometry.coordinates as [number, number][]).map(([lon, lat]) => ({ lat, lon })),
          ),
        0,
      );
      ul.append(
        h(
          'li',
          null,
          h('strong', null, o.name),
          ` · ${points} points, ${lines.length} tracks${km ? ` (${formatDistance(km, this.prefs.imperial)})` : ''}`,
        ),
      );
    }
    this.openPanel(
      'Your files',
      ul,
      h('p', { class: 'atlas-note' }, 'Drawn from your disk; nothing is sent anywhere.'),
    );
  }

  // --- image ---

  private saveImage(): void {
    this.map.once('render', () => {
      this.map.getCanvas().toBlob((blob) => {
        if (!blob) return;
        const a = h('a', {
          href: URL.createObjectURL(blob),
          download: `atlas-${new Date().toISOString().slice(0, 10)}.png`,
        });
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      }, 'image/png');
    });
    this.map.triggerRepaint();
  }

  // --- panel ---

  private openPanel(title: string, ...content: (Node | null)[]): void {
    this.panelTitle.textContent = title;
    this.panelBody.replaceChildren(...content.filter((c): c is Node => c !== null));
    this.panel.hidden = false;
  }

  private closePanel(): void {
    this.panel.hidden = true;
  }

  private async copy(text: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      this.opts.status(`Copied ${text}`);
    } catch {
      this.opts.status('Copying is not allowed here; select the text instead');
    }
  }
}

export interface MiniMapOptions {
  sets: Tilesets;
  theme: ThemeManager;
  signal: AbortSignal;
  lat: number;
  lon: number;
  zoom: number;
}

/** A small, still map of one place (search answers, documents): no controls, a pin, the theme's look. */
export function miniMap(box: HTMLElement, opts: MiniMapOptions): void {
  const p = opts.theme.current;
  const look: Look = { theme: p.theme, dark: opts.theme.dark(), highContrast: p.highContrast };
  const el = h('div', {
    class: 'minimap-canvas',
    role: 'img',
    'aria-label': `Map of ${opts.lat.toFixed(4)}, ${opts.lon.toFixed(4)}`,
  });
  box.replaceChildren(el);
  box.classList.add('minimap-live');
  const map = new MapLibreMap({
    container: el,
    style: buildStyle(opts.sets, 'map', look),
    center: [opts.lon, opts.lat],
    zoom: Math.min(opts.zoom, 15),
    interactive: false,
    attributionControl: false,
    fadeDuration: 0,
  });
  new Marker({ color: markColors(look).line, scale: 0.7 })
    .setLngLat([opts.lon, opts.lat])
    .addTo(map);
  const off = opts.theme.onChange(() => {
    const q = opts.theme.current;
    map.setStyle(
      buildStyle(opts.sets, 'map', {
        theme: q.theme,
        dark: opts.theme.dark(),
        highContrast: q.highContrast,
      }),
    );
  });
  opts.signal.addEventListener(
    'abort',
    () => {
      off();
      map.remove();
    },
    { once: true },
  );
}

export function mount(container: HTMLElement, opts: AtlasOptions): Atlas {
  return new Atlas(container, opts);
}
