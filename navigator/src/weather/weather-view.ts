/**
 * The weather map and its panels (wsp://weather; docs/components/weather.md §5).
 *
 * A MapLibre map (the Atlas's basemap when held) shows one field at a time: a forecast variable at a
 * step of a model's newest run, or an ERA5 daily field on a date. Fields are fetched as PNGs, decoded,
 * coloured and reprojected onto a canvas the map draws under its labels (./field.ts). Clicking a place
 * (or finding it by name) opens its panel: every model's forecast there, or its past from 1940 against
 * the 1991-2020 normal, with the nearest stations' records.
 */
import 'maplibre-gl/dist/maplibre-gl.css';
import '../styles/atlas.css';
import '../styles/weather.css';
import {
  type CanvasSource,
  Map as MapLibreMap,
  Marker,
  type StyleSpecification,
  setWorkerUrl,
} from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import type { WspClient } from '../api/client.ts';
import type {
  ForecastModel,
  PastSeries,
  PointForecast,
  StationSeries,
  Tilesets,
  WeatherStatus,
} from '../api/types.ts';
import { buildStyle, type Look } from '../atlas/style.ts';
import { h, s } from '../core/dom.ts';
import type { SafeStorage } from '../core/storage.ts';
import type { ThemeManager } from '../core/theme.ts';
import { availabilityBadge, wspLink } from '../ui/components.ts';
import { type Series, timeChart } from './charts.ts';
import { CANVAS_CORNERS, type Field, loadField, paint, speedOf, valueAt } from './field.ts';
import { type WeatherRoute, type WeatherTab, weatherAddress } from './route.ts';
import { display, formatValue, legendPosition, legendStops, SCALES, type Units } from './scales.ts';

export interface WeatherOptions {
  api: WspClient;
  storage: SafeStorage;
  theme: ThemeManager;
  sets: Tilesets | null;
  status: WeatherStatus;
  route: WeatherRoute;
  signal: AbortSignal;
  onAddress: (address: string) => void;
  navigate: (address: string) => void;
  say: (text: string) => void;
}

interface Layer {
  id: string;
  label: string;
  kind: string;
  /** Variables fetched to draw it: wind is drawn from its two components. */
  vars: string[];
}

const FORECAST_LAYERS: Layer[] = [
  { id: 't2m', label: 'Temperature', kind: 'temperature', vars: ['t2m'] },
  { id: 'tp', label: 'Precipitation', kind: 'precipitation', vars: ['tp'] },
  { id: 'wind', label: 'Wind', kind: 'speed', vars: ['u10', 'v10'] },
  { id: 'gust', label: 'Gusts', kind: 'speed', vars: ['gust'] },
  { id: 'msl', label: 'Pressure', kind: 'pressure', vars: ['msl'] },
  { id: 'tcc', label: 'Cloud cover', kind: 'percent', vars: ['tcc'] },
  { id: 'd2m', label: 'Dew point', kind: 'temperature', vars: ['d2m'] },
];
const PAST_LAYERS: Layer[] = [
  { id: 't2m_max', label: 'Daily high', kind: 'temperature', vars: ['t2m_max'] },
  { id: 't2m_min', label: 'Daily low', kind: 'temperature', vars: ['t2m_min'] },
  { id: 't2m_mean', label: 'Daily mean temperature', kind: 'temperature', vars: ['t2m_mean'] },
  { id: 'tp_sum', label: 'Precipitation (day)', kind: 'precipitation', vars: ['tp_sum'] },
  { id: 'ws10_mean', label: 'Wind (daily mean)', kind: 'speed', vars: ['ws10_mean'] },
  { id: 'msl_mean', label: 'Pressure (daily mean)', kind: 'pressure', vars: ['msl_mean'] },
  { id: 'tcc_mean', label: 'Cloud cover (daily mean)', kind: 'percent', vars: ['tcc_mean'] },
  { id: 'd2m_mean', label: 'Dew point (daily mean)', kind: 'temperature', vars: ['d2m_mean'] },
];
const MODEL_ORDER = ['ifs', 'gfs', 'aifs'];
const MODEL_CLASS: Record<string, string> = { gfs: 'wx-c-0', ifs: 'wx-c-1', aifs: 'wx-c-2' };
const UNITS_KEY = 'weather.units';
const FIELD_CACHE = 24;
const PLAY_MS = 800;
const SOURCE = 'wx-field';
const DAY = 86_400_000;

// The worker as a bundled asset (same origin, as the CSP requires), like the Atlas's.
setWorkerUrl(workerUrl);

type Range = 'month' | 'year' | 'all' | `y${number}`;

const timeFmt = new Intl.DateTimeFormat(undefined, {
  weekday: 'short',
  day: 'numeric',
  month: 'short',
  hour: 'numeric',
  minute: '2-digit',
});
const dayFmt = new Intl.DateTimeFormat(undefined, {
  weekday: 'short',
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
});

function isUnits(v: unknown): v is Units {
  return v === 'metric' || v === 'us';
}

function latLon(lat: number, lon: number): string {
  return `${Math.abs(lat).toFixed(2)}°${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lon).toFixed(2)}°${lon >= 0 ? 'E' : 'W'}`;
}

function addDays(day: string, n: number): string {
  return new Date(Date.parse(`${day}T00:00:00Z`) + n * DAY).toISOString().slice(0, 10);
}

export class WeatherView {
  private readonly o: WeatherOptions;
  private readonly map: MapLibreMap;
  private readonly canvas = document.createElement('canvas');
  private readonly cache = new Map<string, Promise<Field>>();
  private tab: WeatherTab;
  private model: ForecastModel | undefined;
  private layer: Layer;
  private stepIndex = 0;
  private date: string | undefined;
  private at: { lat: number; lon: number } | undefined;
  private place: string | undefined;
  private units: Units;
  private field: Field | null = null;
  private token = 0;
  private playing: number | null = null;
  private marker: Marker | null = null;
  private panelAbort: AbortController | null = null;
  private range: Range = 'year';
  // DOM
  private readonly root: HTMLElement;
  private readonly tabs: HTMLElement;
  private readonly modelSelect: HTMLSelectElement;
  private readonly modelLabel: HTMLElement;
  private readonly layerSelect: HTMLSelectElement;
  private readonly unitsBox: HTMLElement;
  private readonly refreshBox: HTMLElement;
  private readonly legend: HTMLElement;
  private readonly under: HTMLElement;
  private readonly timeBox: HTMLElement;
  private readonly panel: HTMLElement;
  private readonly findResults: HTMLElement;

  constructor(container: HTMLElement, o: WeatherOptions) {
    this.o = o;
    const r = o.route;
    this.tab = r.tab === 'past' && o.status.past.years.length ? 'past' : r.tab;
    this.units = o.storage.read<Units>(UNITS_KEY, defaultUnits(), isUnits);
    this.at = r.at;
    this.place = r.place;
    this.model = this.pickModel(r.model);
    this.layer = this.pickLayer(r.variable);
    this.date = r.date;

    this.tabs = h('div', {
      class: 'wx-tabs',
      role: 'tablist',
      'aria-label': 'Forecast or past weather',
    });
    this.modelSelect = h('select', { 'aria-label': 'Forecast model' });
    this.modelLabel = h('label', { class: 'wx-field-label' }, 'Model ', this.modelSelect);
    this.layerSelect = h('select', { 'aria-label': 'What the map shows' });
    this.unitsBox = h('div', { class: 'wx-units', role: 'group', 'aria-label': 'Units' });
    this.refreshBox = h('div', { class: 'wx-refresh' });
    const findInput = h('input', {
      type: 'search',
      placeholder: 'Find a place…',
      'aria-label': 'Find a place',
      autocomplete: 'off',
    });
    this.findResults = h('ul', { class: 'wx-find-results', hidden: true });
    const find = h(
      'form',
      { class: 'wx-find', role: 'search' },
      findInput,
      h('button', { type: 'submit' }, 'Find'),
      this.findResults,
    );
    find.addEventListener('submit', (ev) => {
      ev.preventDefault();
      void this.findPlace(findInput.value.trim());
    });
    const controls = h(
      'div',
      { class: 'wx-controls' },
      this.tabs,
      this.modelLabel,
      h('label', { class: 'wx-field-label' }, 'Show ', this.layerSelect),
      this.unitsBox,
      find,
      this.refreshBox,
    );
    const mapEl = h('div', { class: 'wx-map', role: 'region', 'aria-label': 'Weather map' });
    this.legend = h('div', { class: 'wx-legend', 'aria-hidden': 'true' });
    this.under = h(
      'div',
      { class: 'wx-under', 'aria-live': 'off' },
      'Point at the map for a value; click for the forecast there.',
    );
    this.timeBox = h('div', { class: 'wx-time' });
    this.panel = h('aside', {
      class: 'wx-panel',
      hidden: true,
      'aria-label': 'The weather at a place',
    });
    this.root = h(
      'div',
      { class: 'wx-view' },
      controls,
      this.timeBox,
      h('div', { class: 'wx-mapbox' }, mapEl, this.legend, this.under),
      this.panel,
    );
    container.replaceChildren(this.root);

    this.map = new MapLibreMap({
      container: mapEl,
      style: this.style(),
      center: this.at ? [this.at.lon, this.at.lat] : [10, 30],
      zoom: this.at ? 4 : 1.3,
      fadeDuration: 0,
      renderWorldCopies: false,
      attributionControl: { compact: true },
    });
    this.map.on('style.load', () => this.addFieldLayer());
    this.map.on('mousemove', (e) => this.pointAt(e.lngLat.lat, e.lngLat.lng));
    this.map.on('click', (e) => this.choose(e.lngLat.lat, e.lngLat.lng, undefined));
    const offTheme = o.theme.onChange(() => this.map.setStyle(this.style()));
    o.signal.addEventListener(
      'abort',
      () => {
        offTheme();
        this.stop();
        this.panelAbort?.abort();
        this.map.remove();
      },
      { once: true },
    );
    this.renderControls();
    this.renderTime();
    void this.show();
    if (this.at) this.choose(this.at.lat, this.at.lon, this.place, false);
  }

  // --- choices ---------------------------------------------------------------------------------

  private models(): ForecastModel[] {
    return MODEL_ORDER.map((id) => this.o.status.forecast.find((m) => m.id === id)).filter(
      (m): m is ForecastModel => !!m && !!m.run,
    );
  }

  private pickModel(id: string | undefined): ForecastModel | undefined {
    const held = this.models();
    return held.find((m) => m.id === id) ?? held[0];
  }

  private layers(): Layer[] {
    if (this.tab === 'past')
      return PAST_LAYERS.filter((l) => this.o.status.past.variables.includes(l.id));
    const vars = this.model?.variables ?? [];
    return FORECAST_LAYERS.filter((l) => l.vars.every((v) => vars.includes(v)));
  }

  private pickLayer(id: string | undefined): Layer {
    const all = this.layers();
    const fallback = this.tab === 'past' ? PAST_LAYERS[0] : FORECAST_LAYERS[0];
    const first = all.find((l) => l.id === id) ?? all[0] ?? fallback;
    if (!first) throw new Error('no weather layers');
    return first;
  }

  private steps(): number[] {
    return this.model?.steps ?? [];
  }

  private runTime(): number {
    return this.model?.run_time ? Date.parse(this.model.run_time) : Date.now();
  }

  /** The step nearest now (forecasts open on the present). */
  private nowIndex(): number {
    const t0 = this.runTime();
    const steps = this.steps();
    let best = 0;
    steps.forEach((st, i) => {
      if (
        Math.abs(t0 + st * 3_600_000 - Date.now()) <
        Math.abs(t0 + (steps[best] ?? 0) * 3_600_000 - Date.now())
      )
        best = i;
    });
    return best;
  }

  private address(): string {
    const route: WeatherRoute = {
      tab: this.tab,
      model: this.model?.id,
      variable: this.layer.id,
      step: this.tab === 'forecast' ? this.steps()[this.stepIndex] : undefined,
      date: this.tab === 'past' ? this.date : undefined,
      at: this.at,
      place: this.place,
    };
    return weatherAddress(route);
  }

  private remember(): void {
    this.o.onAddress(this.address());
  }

  // --- controls --------------------------------------------------------------------------------

  private renderControls(): void {
    const tab = (id: WeatherTab, label: string, enabled: boolean): HTMLButtonElement => {
      const b = h(
        'button',
        {
          type: 'button',
          role: 'tab',
          'aria-selected': String(this.tab === id),
          disabled: !enabled,
          title: enabled ? '' : 'Not held yet',
        },
        label,
      );
      b.addEventListener('click', () => this.setTab(id));
      return b;
    };
    this.tabs.replaceChildren(
      tab('forecast', 'Forecast', this.models().length > 0),
      tab('past', 'Past weather', this.o.status.past.years.length > 0),
    );
    this.modelLabel.hidden = this.tab !== 'forecast';
    this.modelSelect.replaceChildren(
      ...this.models().map((m) =>
        h(
          'option',
          { value: m.id, selected: m.id === this.model?.id },
          `${m.name} · ${m.run?.slice(8)} UTC`,
        ),
      ),
    );
    this.modelSelect.onchange = () => {
      const keepStep = this.steps()[this.stepIndex];
      this.model = this.pickModel(this.modelSelect.value);
      this.layer = this.pickLayer(this.layer.id);
      const i = keepStep === undefined ? -1 : this.steps().indexOf(keepStep);
      this.stepIndex = i >= 0 ? i : this.nowIndex();
      this.renderControls();
      this.renderTime();
      void this.show();
    };
    this.layerSelect.replaceChildren(
      ...this.layers().map((l) =>
        h('option', { value: l.id, selected: l.id === this.layer.id }, l.label),
      ),
    );
    this.layerSelect.onchange = () => {
      this.layer = this.pickLayer(this.layerSelect.value);
      void this.show();
    };
    const unit = (u: Units, label: string): HTMLButtonElement => {
      const b = h('button', { type: 'button', 'aria-pressed': String(this.units === u) }, label);
      b.addEventListener('click', () => {
        this.units = u;
        this.o.storage.write(UNITS_KEY, u);
        this.renderControls();
        this.renderLegend();
        if (this.at) this.choose(this.at.lat, this.at.lon, this.place, false);
      });
      return b;
    };
    this.unitsBox.replaceChildren(unit('metric', '°C, mm, km/h'), unit('us', '°F, in, mph'));
    this.refreshBox.hidden = this.tab !== 'forecast';
    const refresh = h('button', { type: 'button' }, 'Check for newer runs');
    refresh.addEventListener('click', async () => {
      refresh.disabled = true;
      try {
        const r = await this.o.api.refreshWeather();
        this.o.say(
          r.started
            ? 'Asked the providers for newer runs: they appear here within minutes.'
            : `Could not ask: ${r.message}`,
        );
      } catch (err) {
        this.o.say(`Could not ask: ${String(err)}`);
      }
      window.setTimeout(() => {
        refresh.disabled = false;
      }, 60_000);
    });
    this.refreshBox.replaceChildren(refresh, ' ', availabilityBadge(this.o.status.refresh));
  }

  private setTab(tab: WeatherTab): void {
    if (tab === this.tab) return;
    this.stop();
    this.tab = tab;
    this.layer = this.pickLayer(undefined);
    this.renderControls();
    this.renderTime();
    void this.show();
    if (this.at) this.choose(this.at.lat, this.at.lon, this.place, false);
  }

  private renderTime(): void {
    if (this.tab === 'forecast') this.renderForecastTime();
    else this.renderPastTime();
  }

  private renderForecastTime(): void {
    const steps = this.steps();
    if (!steps.length) {
      this.timeBox.replaceChildren(h('p', null, 'No forecast run is held yet.'));
      return;
    }
    const want = this.o.route.step;
    const fromRoute = want === undefined ? -1 : steps.indexOf(want);
    if (this.stepIndex === 0 && fromRoute >= 0) this.stepIndex = fromRoute;
    else if (this.stepIndex === 0) this.stepIndex = this.nowIndex();
    const slider = h('input', {
      type: 'range',
      min: 0,
      max: steps.length - 1,
      value: this.stepIndex,
      'aria-label': 'Forecast time',
    });
    const label = h('output', { class: 'wx-when' });
    const sync = (): void => {
      const st = steps[this.stepIndex] ?? 0;
      const t = this.runTime() + st * 3_600_000;
      const text = `${timeFmt.format(t)} · +${st} h`;
      label.textContent = text;
      slider.value = String(this.stepIndex);
      slider.setAttribute('aria-valuetext', text);
    };
    slider.addEventListener('input', () => {
      this.stepIndex = Number(slider.value);
      sync();
      void this.show();
    });
    const go = (d: number): void => {
      this.stepIndex = (this.stepIndex + d + steps.length) % steps.length;
      sync();
      void this.show();
    };
    const play = this.playButton(() => go(1));
    this.timeBox.replaceChildren(
      h('button', { type: 'button', 'aria-label': 'Earlier', onclick: () => go(-1) }, '◀'),
      play,
      h('button', { type: 'button', 'aria-label': 'Later', onclick: () => go(1) }, '▶'),
      slider,
      label,
      h(
        'span',
        { class: 'wx-run' },
        this.model ? availabilityBadge(this.model.availability) : '',
        this.model ? ` ${this.model.name}, run of ${this.model.run?.slice(8)} UTC` : '',
      ),
    );
    sync();
  }

  private renderPastTime(): void {
    const p = this.o.status.past;
    if (!p.last_day || !p.first_day) {
      this.timeBox.replaceChildren(h('p', null, p.availability.note));
      return;
    }
    const first = p.first_day;
    const last = p.last_day;
    if (!this.date || this.date < first || this.date > last) this.date = last;
    const input = h('input', {
      type: 'date',
      min: first,
      max: last,
      value: this.date,
      'aria-label': 'Day',
    });
    const label = h('output', { class: 'wx-when' });
    const sync = (): void => {
      input.value = this.date ?? last;
      label.textContent = this.date ? dayFmt.format(Date.parse(`${this.date}T00:00:00Z`)) : '';
    };
    input.addEventListener('change', () => {
      if (input.value >= first && input.value <= last) {
        this.date = input.value;
        sync();
        void this.show();
      }
    });
    const go = (d: number): void => {
      const next = addDays(this.date ?? last, d);
      if (next < first || next > last) return;
      this.date = next;
      sync();
      void this.show();
    };
    this.timeBox.replaceChildren(
      h('button', { type: 'button', 'aria-label': 'Day before', onclick: () => go(-1) }, '◀'),
      this.playButton(() => go(1)),
      h('button', { type: 'button', 'aria-label': 'Day after', onclick: () => go(1) }, '▶'),
      input,
      label,
      h(
        'span',
        { class: 'wx-run' },
        availabilityBadge(p.availability),
        ` ERA5 reanalysis, ${p.years.length} years held`,
      ),
    );
    sync();
  }

  private playButton(step: () => void): HTMLButtonElement {
    const b = h('button', { type: 'button', class: 'wx-play', 'aria-pressed': 'false' }, 'Play');
    b.addEventListener('click', () => {
      if (this.playing !== null) {
        this.stop();
        return;
      }
      b.textContent = 'Pause';
      b.setAttribute('aria-pressed', 'true');
      this.playing = window.setInterval(step, PLAY_MS);
    });
    this.stopButton = () => {
      b.textContent = 'Play';
      b.setAttribute('aria-pressed', 'false');
    };
    return b;
  }

  private stopButton: () => void = () => {};

  private stop(): void {
    if (this.playing !== null) window.clearInterval(this.playing);
    this.playing = null;
    this.stopButton();
  }

  // --- the map ---------------------------------------------------------------------------------

  private look(): Look {
    const p = this.o.theme.current;
    return { theme: p.theme, dark: this.o.theme.dark(), highContrast: p.highContrast };
  }

  private style(): StyleSpecification {
    const sets = this.o.sets;
    if (sets?.tilesets.some((t) => t.id === 'basemap')) return buildStyle(sets, 'map', this.look());
    return {
      version: 8,
      sources: {},
      layers: [
        {
          id: 'background',
          type: 'background',
          paint: { 'background-color': this.look().dark ? '#0b1320' : '#d9e3ec' },
        },
      ],
    };
  }

  private addFieldLayer(): void {
    if (!this.map.getSource(SOURCE))
      this.map.addSource(SOURCE, {
        type: 'canvas',
        canvas: this.canvas,
        coordinates: CANVAS_CORNERS,
        animate: false,
      });
    const labels = this.map.getStyle().layers.find((l) => l.type === 'symbol');
    if (!this.map.getLayer(SOURCE))
      this.map.addLayer(
        {
          id: SOURCE,
          type: 'raster',
          source: SOURCE,
          paint: {
            'raster-opacity': 0.72,
            'raster-resampling': 'linear',
            'raster-fade-duration': 0,
          },
        },
        labels?.id,
      );
    this.redraw();
  }

  private redraw(): void {
    const src = this.map.getSource(SOURCE) as CanvasSource | undefined;
    if (!src) return;
    src.play();
    requestAnimationFrame(() => src.pause());
  }

  private coding(v: string): { offset: number; scale: number } {
    const info = this.o.status.variables.find((x) => x.id === v);
    return { offset: info?.offset ?? 0, scale: info?.scale ?? 1 };
  }

  private urls(layer: Layer, stepIndex: number, date: string | undefined): string[] | null {
    if (this.tab === 'forecast') {
      const m = this.model;
      const st = this.steps()[stepIndex];
      if (!m?.run || st === undefined) return null;
      return layer.vars.map((v) => this.o.api.forecastFieldUrl(m.id, m.run ?? '', v, st));
    }
    if (!date) return null;
    return layer.vars.map((v) => this.o.api.pastFieldUrl(v, date));
  }

  private fieldFor(layer: Layer, urls: string[]): Promise<Field> {
    const key = urls.join('|');
    const hit = this.cache.get(key);
    if (hit) {
      this.cache.delete(key);
      this.cache.set(key, hit);
      return hit;
    }
    const p = Promise.all(urls.map((u, i) => loadField(u, this.coding(layer.vars[i] ?? '')))).then(
      (fs) => {
        const [a, b] = fs;
        if (!a) throw new Error('no field');
        return b ? speedOf(a, b) : a;
      },
    );
    p.catch(() => this.cache.delete(key));
    this.cache.set(key, p);
    while (this.cache.size > FIELD_CACHE) {
      const oldest = this.cache.keys().next().value;
      if (oldest === undefined) break;
      this.cache.delete(oldest);
    }
    return p;
  }

  private async show(): Promise<void> {
    const mine = ++this.token;
    this.remember();
    this.renderLegend();
    const urls = this.urls(this.layer, this.stepIndex, this.date);
    if (!urls) return;
    try {
      const field = await this.fieldFor(this.layer, urls);
      if (mine !== this.token || this.o.signal.aborted) return;
      this.field = field;
      paint(this.canvas, field, this.layer.kind);
      this.redraw();
      this.prefetch();
    } catch (err) {
      if (mine === this.token)
        this.o.say(`The ${this.layer.label.toLowerCase()} map could not be read: ${String(err)}`);
    }
  }

  /** The next frame of a forecast or the next day, so playing does not wait. */
  private prefetch(): void {
    if (this.tab === 'forecast') {
      const next = (this.stepIndex + 1) % Math.max(1, this.steps().length);
      const urls = this.urls(this.layer, next, undefined);
      if (urls) void this.fieldFor(this.layer, urls).catch(() => undefined);
    } else if (
      this.date &&
      this.o.status.past.last_day &&
      this.date < this.o.status.past.last_day
    ) {
      const urls = this.urls(this.layer, 0, addDays(this.date, 1));
      if (urls) void this.fieldFor(this.layer, urls).catch(() => undefined);
    }
  }

  private pointAt(lat: number, lon: number): void {
    if (!this.field) return;
    const v = valueAt(this.field, lat, lon);
    this.under.textContent = `${formatValue(this.layer.kind, v, this.units)} at ${latLon(lat, lon)}`;
  }

  private renderLegend(): void {
    const sc = SCALES[this.layer.kind];
    if (!sc) return;
    const w = 280;
    const id = `wx-grad-${this.layer.kind}`;
    const stops = legendStops(this.layer.kind).map((st) =>
      s('stop', { offset: `${(st.offset * 100).toFixed(1)}%`, 'stop-color': st.color }),
    );
    const x = (v: number): number => legendPosition(this.layer.kind, v) * (w - 20) + 10;
    const ticks = sc.ticks.map((v) =>
      s(
        'text',
        { x: x(v), y: 32, class: 'wx-legend-tick' },
        display(this.layer.kind, v, this.units).value.toFixed(
          this.layer.kind === 'precipitation' && this.units === 'us' ? 2 : 0,
        ),
      ),
    );
    const unit = display(this.layer.kind, sc.min, this.units).unit;
    const per =
      this.layer.kind === 'precipitation'
        ? this.tab === 'past'
          ? '/day' // "in a day" read as words with inches (2026-10-21)
          : ' since the step before'
        : '';
    this.legend.replaceChildren(
      h('span', { class: 'wx-legend-title' }, `${this.layer.label} (${unit}${per})`),
      s(
        'svg',
        { viewBox: `0 0 ${w} 38`, width: w, height: 38 },
        s('defs', null, s('linearGradient', { id, x1: '0', x2: '1', y1: '0', y2: '0' }, ...stops)),
        s('rect', {
          x: 10,
          y: 4,
          width: w - 20,
          height: 14,
          fill: `url(#${id})`,
          class: 'wx-legend-bar',
        }),
        ...ticks,
      ),
    );
  }

  // --- places ----------------------------------------------------------------------------------

  private async findPlace(q: string): Promise<void> {
    this.findResults.replaceChildren();
    this.findResults.hidden = true;
    if (!q) return;
    try {
      const found = await this.o.api.places(q, 6, this.o.signal);
      if (!found.places.length) {
        this.o.say(`No place called "${q}" in the gazetteer.`);
        return;
      }
      this.findResults.replaceChildren(
        ...found.places.map((p) => {
          const b = h(
            'button',
            { type: 'button' },
            p.name,
            h('small', null, ` ${p.kind} · ${latLon(p.lat, p.lon)}`),
          );
          b.addEventListener('click', () => {
            this.findResults.hidden = true;
            this.map.flyTo({ center: [p.lon, p.lat], zoom: 6 });
            this.choose(p.lat, p.lon, p.name);
          });
          return h('li', null, b);
        }),
      );
      this.findResults.hidden = false;
      this.findResults.querySelector('button')?.focus();
    } catch (err) {
      this.o.say(`Places cannot be looked up: ${String(err)}`);
    }
  }

  private choose(lat: number, lon: number, place: string | undefined, fly = false): void {
    this.at = { lat, lon };
    this.place = place;
    if (!this.marker) this.marker = new Marker({ color: '#c0392b', scale: 0.8 });
    this.marker.setLngLat([lon, lat]).addTo(this.map);
    if (fly) this.map.flyTo({ center: [lon, lat], zoom: 6 });
    this.remember();
    void this.openPanel();
  }

  // --- the panel -------------------------------------------------------------------------------

  private async openPanel(): Promise<void> {
    const at = this.at;
    if (!at) return;
    this.panelAbort?.abort();
    const ctl = new AbortController();
    this.panelAbort = ctl;
    this.o.signal.addEventListener('abort', () => ctl.abort(), { once: true });
    const close = h('button', { type: 'button', class: 'wx-close', 'aria-label': 'Close' }, '×');
    close.addEventListener('click', () => {
      this.panel.hidden = true;
      this.at = undefined;
      this.place = undefined;
      this.marker?.remove();
      this.remember();
    });
    const other = this.tab === 'forecast' ? 'past' : 'forecast';
    const switcher = h(
      'button',
      { type: 'button', class: 'wx-link' },
      other === 'past' ? 'Past weather here' : 'Forecast here',
    );
    switcher.disabled = other === 'past' ? !this.o.status.past.years.length : !this.models().length;
    switcher.addEventListener('click', () => this.setTab(other));
    const heading = h('h2', null, this.place ?? latLon(at.lat, at.lon));
    const body = h(
      'div',
      { class: 'wx-panel-body' },
      h('p', { class: 'atlas-loading' }, 'Reading…'),
    );
    this.panel.replaceChildren(
      h(
        'header',
        null,
        heading,
        h(
          'p',
          { class: 'wx-panel-tools' },
          this.place ? `${latLon(at.lat, at.lon)} · ` : '',
          switcher,
          ' · ',
          wspLink(
            `wsp://atlas/@${at.lat.toFixed(4)},${at.lon.toFixed(4)},9z?pin=${at.lat.toFixed(4)},${at.lon.toFixed(4)}`,
            'In the Atlas',
          ),
        ),
        close,
      ),
      body,
    );
    this.panel.hidden = false;
    try {
      if (this.tab === 'forecast') body.replaceChildren(await this.forecastPanel(at, ctl.signal));
      else body.replaceChildren(await this.pastPanel(at, ctl.signal));
    } catch (err) {
      if (ctl.signal.aborted) return;
      body.replaceChildren(
        h('p', { class: 'wx-chart-empty' }, `Nothing to show here: ${String(err)}`),
      );
    }
  }

  private async forecastPanel(
    at: { lat: number; lon: number },
    signal: AbortSignal,
  ): Promise<Node> {
    const pf: PointForecast = await this.o.api.forecastPoint(at.lat, at.lon, signal);
    const u = this.units;
    const conv = (kind: string, vals: (number | null)[]): (number | null)[] =>
      vals.map((v) => (v === null ? null : display(kind, v, u).value));
    const models = pf.models
      .slice()
      .sort((a, b) => MODEL_ORDER.indexOf(a.model) - MODEL_ORDER.indexOf(b.model));
    const name = (id: string): string =>
      this.o.status.forecast.find((m) => m.id === id)?.name ?? id;
    const times = (pm: (typeof models)[number]): number[] => pm.times.map((t) => Date.parse(t));
    const lines = (v: string, kind: string, extra: Partial<Series> = {}): Series[] =>
      models
        .filter((pm) => pm.values[v])
        .map((pm) => ({
          label: name(pm.model),
          cls: MODEL_CLASS[pm.model] ?? 'wx-c-3',
          kind: 'line' as const,
          times: times(pm),
          values: conv(kind, pm.values[v] ?? []),
          ...extra,
        }));
    const speed = models.map((pm) => {
      const uu = pm.values.u10 ?? [];
      const vv = pm.values.v10 ?? [];
      return {
        label: name(pm.model),
        cls: MODEL_CLASS[pm.model] ?? 'wx-c-3',
        kind: 'line' as const,
        times: times(pm),
        values: conv(
          'speed',
          uu.map((x, i) => {
            const y = vv[i];
            return x === null || y === null || y === undefined ? null : Math.hypot(x, y);
          }),
        ),
      };
    });
    const cumulative = models
      .filter((pm) => pm.values.tp)
      .map((pm) => {
        let total = 0;
        return {
          label: name(pm.model),
          cls: MODEL_CLASS[pm.model] ?? 'wx-c-3',
          kind: 'line' as const,
          times: times(pm),
          values: conv(
            'precipitation',
            (pm.values.tp ?? []).map((v) => {
              total += v ?? 0;
              return total;
            }),
          ),
        };
      });
    const first = models[0];
    const now = Date.now();
    const label = (ms: number): string => timeFmt.format(ms);
    const unitOf = (kind: string): string => display(kind, 0, u).unit;
    const charts: HTMLElement[] = [
      timeChart({
        title: 'Temperature',
        unit: unitOf('temperature'),
        series: lines('t2m', 'temperature'),
        now,
        timeLabel: label,
      }),
      timeChart({
        title: 'Precipitation, running total',
        unit: unitOf('precipitation'),
        series: [
          ...cumulative,
          ...(first?.values.tp
            ? [
                {
                  label: `${name(first.model)}, each step`,
                  cls: `${MODEL_CLASS[first.model] ?? 'wx-c-3'} wx-light`,
                  kind: 'bar' as const,
                  times: times(first),
                  values: conv('precipitation', first.values.tp),
                },
              ]
            : []),
        ],
        zero: true,
        now,
        digits: u === 'us' ? 2 : 1,
        timeLabel: label,
      }),
      timeChart({
        title: 'Wind',
        unit: unitOf('speed'),
        series: [
          ...speed,
          ...lines('gust', 'speed', { dashed: true }).map((se) => ({
            ...se,
            label: `${se.label} gusts`,
          })),
        ],
        zero: true,
        now,
        timeLabel: label,
      }),
      timeChart({
        title: 'Pressure',
        unit: 'hPa',
        series: lines('msl', 'pressure'),
        now,
        digits: 0,
        timeLabel: label,
      }),
      timeChart({
        title: 'Cloud cover',
        unit: '%',
        series: lines('tcc', 'percent'),
        zero: true,
        now,
        digits: 0,
        timeLabel: label,
      }),
      timeChart({
        title: 'Dew point',
        unit: unitOf('temperature'),
        series: lines('d2m', 'temperature'),
        now,
        timeLabel: label,
      }),
    ];
    return h(
      'div',
      null,
      h(
        'p',
        { class: 'wx-panel-note' },
        availabilityBadge(pf.availability),
        ` Grid point ${latLon(pf.grid_lat, pf.grid_lon)} (0.25°, about 25 km). Times are local; each model's newest run.`,
      ),
      ...charts,
    );
  }

  private rangeDates(): { from: string; to: string } | null {
    const p = this.o.status.past;
    if (!p.last_day || !p.first_day) return null;
    const last = p.last_day;
    if (this.range === 'month') return { from: addDays(last, -29), to: last };
    if (this.range === 'year') return { from: addDays(last, -364), to: last };
    if (this.range === 'all') return { from: p.first_day, to: last };
    const y = Number(this.range.slice(1));
    return { from: `${y}-01-01`, to: `${y}-12-31` < last ? `${y}-12-31` : last };
  }

  private async pastPanel(at: { lat: number; lon: number }, signal: AbortSignal): Promise<Node> {
    const box = h('div', null);
    const years = this.o.status.past.years;
    const picker = h('select', { 'aria-label': 'Period' });
    const opts: [Range, string][] = [
      ['month', 'Last 30 days'],
      ['year', 'Last 365 days'],
      ...years
        .slice()
        .reverse()
        .map((y): [Range, string] => [`y${y}`, String(y)]),
      ['all', `Every year held (${years.length})`],
    ];
    picker.replaceChildren(
      ...opts.map(([v, label]) => h('option', { value: v, selected: v === this.range }, label)),
    );
    picker.addEventListener('change', () => {
      this.range = picker.value as Range;
      void this.openPanel();
    });
    const range = this.rangeDates();
    box.append(h('p', { class: 'wx-panel-tools' }, h('label', null, 'Period ', picker)));
    if (!range) {
      box.append(h('p', null, this.o.status.past.availability.note));
    } else {
      const ps: PastSeries = await this.o.api.pastPoint(at.lat, at.lon, range, signal);
      box.append(this.pastCharts(ps));
    }
    box.append(await this.stationsBlock(at, range, signal));
    return box;
  }

  private pastCharts(ps: PastSeries): Node {
    const u = this.units;
    const monthly = ps.days.length > 3 * 366;
    const days = ps.days.map((d) => Date.parse(`${d}T12:00:00Z`));
    const group = (
      vals: (number | null)[],
      how: 'mean' | 'sum',
    ): { times: number[]; values: (number | null)[] } => {
      if (!monthly) return { times: days, values: vals };
      const keys = new Map<string, { t: number; sum: number; n: number }>();
      ps.days.forEach((d, i) => {
        const k = d.slice(0, 7);
        const v = vals[i];
        const e = keys.get(k) ?? { t: Date.parse(`${k}-15T00:00:00Z`), sum: 0, n: 0 };
        if (v !== null && v !== undefined) {
          e.sum += v;
          e.n += 1;
        }
        keys.set(k, e);
      });
      const out = [...keys.values()];
      return {
        times: out.map((e) => e.t),
        values: out.map((e) => (e.n ? (how === 'sum' ? e.sum : e.sum / e.n) : null)),
      };
    };
    const conv = (kind: string, vals: (number | null)[]): (number | null)[] =>
      vals.map((v) => (v === null ? null : display(kind, v, u).value));
    const label = (ms: number): string =>
      monthly
        ? new Intl.DateTimeFormat(undefined, {
            month: 'short',
            year: 'numeric',
            timeZone: 'UTC',
          }).format(ms)
        : dayFmt.format(ms);
    const v = ps.values;
    const n = ps.normals ?? {};
    const normalYears = ps.normal_years ?? [];
    const hi = group(conv('temperature', v.t2m_max ?? []), 'mean');
    const lo = group(conv('temperature', v.t2m_min ?? []), 'mean');
    const mean = group(conv('temperature', v.t2m_mean ?? []), 'mean');
    const normalMean = group(conv('temperature', n.t2m_mean ?? []), 'mean');
    const tUnit = display('temperature', 0, u).unit;
    const pUnit = display('precipitation', 0, u).unit;
    const rain = group(conv('precipitation', v.tp_sum ?? []), 'sum');
    const normalRain = group(conv('precipitation', n.tp_sum ?? []), 'sum');
    const running = (vals: (number | null)[]): (number | null)[] => {
      let t = 0;
      return vals.map((x) => {
        t += x ?? 0;
        return t;
      });
    };
    const normalLabel = normalYears.length
      ? `Normal (${normalYears[0]}–${normalYears[normalYears.length - 1]}, ${normalYears.length} years)`
      : '';
    const normalSeries = (vals: { times: number[]; values: (number | null)[] }): Series[] =>
      normalLabel && vals.values.some((x) => x !== null)
        ? [{ label: normalLabel, cls: 'wx-normal', kind: 'line', dashed: true, ...vals }]
        : [];
    const summary = this.pastSummary(ps);
    const charts: HTMLElement[] = [
      timeChart({
        title: monthly ? 'Temperature, monthly' : 'Temperature',
        unit: tUnit,
        series: [
          {
            label: 'Low to high',
            cls: 'wx-c-1',
            kind: 'band',
            times: hi.times,
            values: hi.values,
            low: lo.values,
          },
          { label: 'Mean', cls: 'wx-c-0', kind: 'line', ...mean },
          ...normalSeries(normalMean),
        ],
        timeLabel: label,
      }),
      timeChart({
        title: monthly ? 'Precipitation, monthly totals' : 'Precipitation',
        unit: pUnit,
        series: [{ label: monthly ? 'Month' : 'Day', cls: 'wx-c-1', kind: 'bar', ...rain }],
        zero: true,
        digits: u === 'us' ? 2 : 1,
        timeLabel: label,
      }),
    ];
    if (!monthly)
      charts.push(
        timeChart({
          title: 'Precipitation, running total',
          unit: pUnit,
          series: [
            {
              label: 'This period',
              cls: 'wx-c-1',
              kind: 'line',
              times: rain.times,
              values: running(rain.values),
            },
            ...normalSeries({ times: normalRain.times, values: running(normalRain.values) }),
          ],
          zero: true,
          digits: u === 'us' ? 2 : 0,
          timeLabel: label,
        }),
      );
    const simple = (id: string, title: string, kind: string, zero = false): HTMLElement => {
      const g = group(conv(kind, v[id] ?? []), 'mean');
      return timeChart({
        title,
        unit: display(kind, 0, u).unit,
        series: [
          { label: 'Daily mean', cls: 'wx-c-0', kind: 'line', ...g },
          ...normalSeries(group(conv(kind, n[id] ?? []), 'mean')),
        ],
        zero,
        digits: kind === 'pressure' || kind === 'percent' ? 0 : 1,
        timeLabel: label,
      });
    };
    charts.push(
      simple('ws10_mean', 'Wind', 'speed', true),
      simple('msl_mean', 'Pressure', 'pressure'),
      simple('tcc_mean', 'Cloud cover', 'percent', true),
      simple('d2m_mean', 'Dew point', 'temperature'),
    );
    return h(
      'div',
      null,
      h(
        'p',
        { class: 'wx-panel-note' },
        availabilityBadge(ps.availability),
        ` ERA5 at ${latLon(ps.grid_lat, ps.grid_lon)} (0.25°, about 25 km), ${ps.days[0] ?? ''} to ${ps.days[ps.days.length - 1] ?? ''}.`,
      ),
      summary,
      ...charts,
    );
  }

  private pastSummary(ps: PastSeries): HTMLElement {
    const u = this.units;
    const best = (
      vals: (number | null)[] | undefined,
      pick: (a: number, b: number) => boolean,
    ): [number, string] | null => {
      let out: [number, string] | null = null;
      (vals ?? []).forEach((v, i) => {
        if (v === null) return;
        if (!out || pick(v, out[0])) out = [v, ps.days[i] ?? ''];
      });
      return out;
    };
    const hot = best(ps.values.t2m_max, (a, b) => a > b);
    const cold = best(ps.values.t2m_min, (a, b) => a < b);
    const total = (ps.values.tp_sum ?? []).reduce<number>((t, v) => t + (v ?? 0), 0);
    const normal = (ps.normals?.tp_sum ?? []).reduce<number>((t, v) => t + (v ?? 0), 0);
    const items: string[] = [];
    if (hot) items.push(`Hottest: ${formatValue('temperature', hot[0], u)} on ${hot[1]}`);
    if (cold) items.push(`coldest: ${formatValue('temperature', cold[0], u)} on ${cold[1]}`);
    items.push(
      `precipitation ${formatValue('precipitation', total, u)}${normal > 0 ? ` (${Math.round((total / normal) * 100)}% of normal)` : ''}`,
    );
    return h('p', { class: 'wx-summary' }, `${items.join('; ')}.`);
  }

  private async stationsBlock(
    at: { lat: number; lon: number },
    range: { from: string; to: string } | null,
    signal: AbortSignal,
  ): Promise<Node> {
    const box = h('section', { class: 'wx-stations' }, h('h3', null, 'Weather stations nearby'));
    if (!this.o.status.stations.stations) {
      box.append(
        h(
          'p',
          null,
          availabilityBadge(this.o.status.stations.availability),
          ' ',
          this.o.status.stations.availability.note,
        ),
      );
      return box;
    }
    const near = await this.o.api.stationsNear(at.lat, at.lon, 8, signal);
    if (!near.stations.length) {
      box.append(h('p', null, 'No station within about 2,500 km.'));
      return box;
    }
    const chart = h('div', { class: 'wx-station-chart' });
    const list = h(
      'ul',
      { class: 'wx-station-list' },
      ...near.stations.map((st) => {
        const b = h(
          'button',
          { type: 'button' },
          `${st.name}, ${st.country}`,
          h(
            'small',
            null,
            ` ${st.distance_km?.toFixed(0) ?? '?'} km · ${st.first_year ?? '?'}–${st.last_year ?? '?'}`,
          ),
        );
        b.addEventListener('click', async () => {
          chart.replaceChildren(h('p', { class: 'atlas-loading' }, 'Reading the record…'));
          const whole =
            !range || (st.last_year !== null && range.from.slice(0, 4) > String(st.last_year));
          const want = whole
            ? {
                from: `${Math.max(st.first_year ?? 1900, (st.last_year ?? 2000) - 1)}-01-01`,
                to: `${st.last_year ?? 2000}-12-31`,
              }
            : range;
          try {
            let ss = await this.o.api.station(st.id, want, signal);
            let note = '';
            const measured = (x: StationSeries): boolean =>
              Object.values(x.values).some((vals) => vals.some((v) => v !== null));
            if (!measured(ss) && st.last_year) {
              // Nothing measured in the period (the station closed, or reports late): its latest year.
              ss = await this.o.api.station(
                st.id,
                { from: `${st.last_year}-01-01`, to: `${st.last_year}-12-31` },
                signal,
              );
              note = `No measurements from ${want.from} to ${want.to}; its latest year, ${st.last_year}:`;
            }
            chart.replaceChildren(
              ...(note ? [h('p', { class: 'wx-panel-note' }, note)] : []),
              this.stationCharts(ss),
            );
          } catch (err) {
            chart.replaceChildren(h('p', null, `The record could not be read: ${String(err)}`));
          }
        });
        return h('li', null, b);
      }),
    );
    box.append(
      h(
        'p',
        { class: 'wx-panel-note' },
        availabilityBadge(near.availability),
        ' NOAA GHCN-Daily: what the station measured.',
      ),
      list,
      chart,
    );
    return box;
  }

  private stationCharts(ss: StationSeries): Node {
    const u = this.units;
    const times = ss.days.map((d) => Date.parse(`${d}T12:00:00Z`));
    const conv = (kind: string, vals: (number | null)[] | undefined): (number | null)[] =>
      (vals ?? []).map((v) =>
        v === null
          ? null
          : kind === 'temperature'
            ? display(kind, v + 273.15, u).value
            : display(kind, v, u).value,
      );
    const label = (ms: number): string => dayFmt.format(ms);
    const out = h(
      'div',
      null,
      h('h4', null, `${ss.station.name}, ${ss.station.country} (${ss.station.id})`),
    );
    if (ss.values.TMAX || ss.values.TMIN)
      out.append(
        timeChart({
          title: 'Measured temperature',
          unit: display('temperature', 0, u).unit,
          series: [
            {
              label: 'Low to high',
              cls: 'wx-c-1',
              kind: 'band',
              times,
              values: conv('temperature', ss.values.TMAX),
              low: conv('temperature', ss.values.TMIN),
            },
            ...(ss.values.TAVG
              ? [
                  {
                    label: 'Mean',
                    cls: 'wx-c-0',
                    kind: 'line' as const,
                    times,
                    values: conv('temperature', ss.values.TAVG),
                  },
                ]
              : []),
          ],
          timeLabel: label,
        }),
      );
    if (ss.values.PRCP)
      out.append(
        timeChart({
          title: 'Measured precipitation',
          unit: display('precipitation', 0, u).unit,
          series: [
            {
              label: 'Day',
              cls: 'wx-c-1',
              kind: 'bar',
              times,
              values: conv('precipitation', ss.values.PRCP),
            },
          ],
          zero: true,
          digits: u === 'us' ? 2 : 1,
          timeLabel: label,
        }),
      );
    if (ss.values.SNWD)
      out.append(
        timeChart({
          title: 'Snow depth',
          unit: display('precipitation', 0, u).unit,
          series: [
            {
              label: 'Depth',
              cls: 'wx-c-2',
              kind: 'line',
              times,
              values: conv('precipitation', ss.values.SNWD),
            },
          ],
          zero: true,
          timeLabel: label,
        }),
      );
    if (out.childNodes.length === 1)
      out.append(h('p', null, 'This station has no temperature or precipitation in this period.'));
    return out;
  }
}

function defaultUnits(): Units {
  try {
    return /^en-US|^en-LR|^my\b/.test(navigator.language) ? 'us' : 'metric';
  } catch {
    return 'metric';
  }
}

export function mount(container: HTMLElement, opts: WeatherOptions): WeatherView {
  return new WeatherView(container, opts);
}
