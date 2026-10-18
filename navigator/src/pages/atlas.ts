/**
 * wsp://atlas — the library's maps. The map itself (MapLibre, ~250 KB gzipped) loads only here.
 * Before any tileset is held the page says what is on its way instead.
 */
import { ApiRequestError } from '../api/client.ts';
import type { Tilesets } from '../api/types.ts';
import { h } from '../core/dom.ts';
import { errorPage, pageHeading, wspLink } from '../ui/components.ts';
import type { PageContext, PageResult } from './types.ts';

export async function render(ctx: PageContext): Promise<PageResult> {
  if (ctx.route.page !== 'atlas') throw new Error('atlas page for another route');
  const route = ctx.route.atlas;
  let sets: Tilesets;
  try {
    sets = await ctx.app.api.tilesets(ctx.signal);
  } catch (err) {
    if (err instanceof ApiRequestError && err.isMissingRoute)
      return { title: 'Atlas', body: notYet() };
    return {
      title: 'Atlas',
      status: 'error',
      body: errorPage({
        title: 'Atlas',
        heading: 'The maps could not be read',
        message: String(err),
        address: ctx.address,
      }),
    };
  }
  if (!sets.tilesets.length) return { title: 'Atlas', body: notYet() };
  const heading = h('h1', { class: 'visually-hidden' }, 'Atlas');
  const holder = h(
    'div',
    { class: 'atlas-holder' },
    h('p', { class: 'atlas-loading' }, 'Unfolding the map…'),
  );
  const view = await import('../atlas/atlas-map.ts');
  if (ctx.signal.aborted) return { title: 'Atlas', body: holder };
  view.mount(holder, {
    api: ctx.app.api,
    storage: ctx.app.storage,
    theme: ctx.app.theme,
    sets,
    route,
    signal: ctx.signal,
    onAddress: (address) => ctx.app.replaceAddress(address),
    navigate: (address) => ctx.app.navigate(address),
    status: (text) => ctx.app.status(text),
  });
  return {
    title: route.query ? `Atlas: ${route.query}` : 'Atlas',
    body: h('div', { class: 'atlas-page' }, heading, holder),
    focus: holder.querySelector<HTMLElement>('.atlas-map') ?? undefined,
  };
}

function notYet(): HTMLElement {
  return h(
    'div',
    { class: 'page page-atlas-empty' },
    pageHeading('Atlas'),
    h(
      'p',
      null,
      'The map library is still arriving: the OpenStreetMap basemap of the whole planet, world terrain, high-resolution regional terrain and satellite imagery are being downloaded and sealed into the vault. The Atlas opens here by itself as soon as the first of them is on the shelves.',
    ),
    h(
      'p',
      null,
      'Progress is on the ',
      wspLink('wsp://system', 'System Monitor'),
      '. Meanwhile, places can be looked up in ',
      wspLink('wsp://osm-planet', 'OpenStreetMap'),
      ' and the encyclopedias.',
    ),
  );
}

/** `wsp://atlas/@lat,lon,zoomz?pin=lat,lon`: a point shown and marked in the Atlas. */
export function atlasPin(lat: number, lon: number, zoom = 13): string {
  const r = (v: number) => String(Number(v.toFixed(6)));
  return `wsp://atlas/@${r(lat)},${r(lon)},${Math.round(zoom * 10) / 10}z?pin=${r(lat)},${r(lon)}`;
}

/**
 * Small live maps (`.minimap[data-lat][data-lon][data-zoom]`) where a page shows a place: drawn from
 * the library's basemap once it holds one; until then the box keeps what it shows. The map module
 * loads only when there is a map to draw.
 */
export async function upgradeMiniMaps(root: HTMLElement, ctx: PageContext): Promise<void> {
  const boxes = [...root.querySelectorAll<HTMLElement>('.minimap[data-lat][data-lon]')];
  if (!boxes.length) return;
  let sets: Tilesets;
  try {
    sets = await ctx.app.api.tilesets(ctx.signal);
  } catch {
    return;
  }
  if (!sets.tilesets.some((t) => t.id === 'basemap') || ctx.signal.aborted) return;
  const { miniMap } = await import('../atlas/atlas-map.ts');
  for (const box of boxes) {
    const lat = Number(box.dataset.lat);
    const lon = Number(box.dataset.lon);
    const zoom = Number(box.dataset.zoom ?? 11);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    miniMap(box, {
      sets,
      theme: ctx.app.theme,
      signal: ctx.signal,
      lat,
      lon,
      zoom: Number.isFinite(zoom) ? zoom : 11,
    });
  }
}
