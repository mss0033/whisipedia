/**
 * wsp://weather — forecasts, past weather and station records (docs/components/weather.md). The
 * strip at the top says what each part is: in the library (works offline), a kept copy (with its
 * age), or needing a connection. The map (MapLibre) loads only here, like the Atlas's.
 */
import { ApiRequestError } from '../api/client.ts';
import type { Tilesets, WeatherStatus } from '../api/types.ts';
import { h } from '../core/dom.ts';
import { age, availabilityBadge, errorPage, pageHeading, wspLink } from '../ui/components.ts';
import type { PageContext, PageResult } from './types.ts';

export async function render(ctx: PageContext): Promise<PageResult> {
  if (ctx.route.page !== 'weather') throw new Error('weather page for another route');
  const route = ctx.route.weather;
  let status: WeatherStatus;
  try {
    status = await ctx.app.api.weather(ctx.signal);
  } catch (err) {
    if (err instanceof ApiRequestError && err.isMissingRoute)
      return {
        title: 'Weather',
        body: h(
          'div',
          { class: 'page' },
          pageHeading('Weather'),
          h('p', null, 'This library has no weather service yet.'),
        ),
      };
    return {
      title: 'Weather',
      status: 'error',
      body: errorPage({
        title: 'Weather',
        heading: 'The weather could not be read',
        message: String(err),
        address: ctx.address,
      }),
    };
  }
  let sets: Tilesets | null = null;
  try {
    sets = await ctx.app.api.tilesets(ctx.signal);
  } catch {
    sets = null; // no Atlas yet: the fields are drawn on a plain globe
  }
  const holder = h(
    'div',
    { class: 'wx-holder' },
    h('p', { class: 'atlas-loading' }, 'Unfolding the weather map…'),
  );
  const body = h('div', { class: 'wx-page' }, pageHeading('Weather'), strip(status), holder);
  const view = await import('../weather/weather-view.ts');
  if (ctx.signal.aborted) return { title: 'Weather', body };
  view.mount(holder, {
    api: ctx.app.api,
    storage: ctx.app.storage,
    theme: ctx.app.theme,
    sets,
    status,
    route,
    signal: ctx.signal,
    onAddress: (address) => ctx.app.replaceAddress(address),
    navigate: (address) => ctx.app.navigate(address),
    say: (text) => ctx.app.status(text),
  });
  const title = route.place
    ? `Weather: ${route.place}`
    : route.tab === 'past'
      ? 'Past weather'
      : 'Weather';
  return { title, body, focus: holder.querySelector<HTMLElement>('.wx-tabs button') ?? undefined };
}

/** What is held and how: forecasts (kept copies), the past (in the library), stations, connection. */
function strip(st: WeatherStatus): HTMLElement {
  const runs = st.forecast.filter((m) => m.run);
  const newest = runs
    .map((m) => m.availability)
    .sort((a, b) => (b.as_of ?? '').localeCompare(a.as_of ?? ''))[0];
  const forecast = h(
    'li',
    null,
    h('b', null, 'Forecasts '),
    newest
      ? availabilityBadge(newest)
      : availabilityBadge(st.forecast[0]?.availability ?? { kind: 'kept', label: 'Kept copy' }),
    ' ',
    runs.length
      ? `${runs.map((m) => `${m.name} ${m.run?.slice(8)} UTC ${shortDate(m.run_time)}`).join(' · ')}`
      : 'no run held yet',
  );
  const past = h(
    'li',
    null,
    h('b', null, 'Past weather '),
    availabilityBadge(st.past.availability),
    ' ',
    st.past.years.length
      ? `ERA5 ${st.past.first_day ?? ''} to ${st.past.last_day ?? ''} (${st.past.years.length} years held)`
      : st.past.availability.note,
  );
  const stations = h(
    'li',
    null,
    h('b', null, 'Station records '),
    availabilityBadge(st.stations.availability),
    ' ',
    st.stations.stations
      ? `${st.stations.stations.toLocaleString()} GHCN-Daily stations`
      : st.stations.availability.note,
  );
  const c = st.connection;
  const conn = h(
    'li',
    null,
    h('b', null, 'Connection '),
    c.online === null
      ? 'not tried yet'
      : c.online
        ? h(
            'span',
            { class: 'badge badge-avail badge-avail-online' },
            `online · last answer ${age(c.last_ok)} ago`,
          )
        : h(
            'span',
            { class: 'badge badge-avail badge-avail-stale', title: c.last_error ?? '' },
            c.last_ok ? `no answer since ${age(c.last_ok)} ago` : 'no answer yet',
          ),
  );
  return h(
    'section',
    { class: 'wx-strip', 'aria-label': 'What the weather pages hold and how' },
    h('ul', null, forecast, past, stations, conn),
    h(
      'details',
      { class: 'wx-explain' },
      h('summary', null, 'What works without a connection?'),
      h(
        'p',
        null,
        h('span', { class: 'badge badge-avail badge-avail-offline' }, 'In the library'),
        ' sealed on the NAS: works with no connection, for good. ',
        h('span', { class: 'badge badge-avail badge-avail-kept' }, 'Kept copy'),
        ' a copy of something that changes, fetched every half hour while connected and kept: it works offline and shows its age. Every 00 UTC run is also archived, so the kept forecasts become history. ',
        h('span', { class: 'badge badge-avail badge-avail-live' }, 'Needs a connection'),
        ' asks the outside world at that moment: only the "Check for newer runs" button.',
      ),
      h(
        'p',
        null,
        'Sources: ',
        [...st.forecast.map((m) => m.attribution), st.past.attribution, st.stations.attribution]
          .filter((v, i, a) => a.indexOf(v) === i)
          .join('; '),
        '. See also the ',
        wspLink('wsp://atlas', 'Atlas'),
        '.',
      ),
    ),
  );
}

function shortDate(iso: string | null | undefined): string {
  if (!iso) return '';
  return new Intl.DateTimeFormat(undefined, {
    day: 'numeric',
    month: 'short',
    timeZone: 'UTC',
  }).format(Date.parse(iso));
}
