/**
 * `wsp://weather` addresses: what the weather page shows, kept in the query so a reload, a bookmark
 * or a new tab opens the same view. Light on purpose: the router imports it, not the map.
 *
 *   wsp://weather                                   forecast, temperature, now
 *   wsp://weather?var=tp&model=ifs&step=48&at=51.5,-0.12
 *   wsp://weather/past?var=t2m_max&date=2021-07-14&at=51.5,-0.12
 */

export type WeatherTab = 'forecast' | 'past';

export interface WeatherRoute {
  tab: WeatherTab;
  model: string | undefined;
  variable: string | undefined;
  step: number | undefined;
  date: string | undefined;
  at: { lat: number; lon: number } | undefined;
  /** A place name for `at` (from a search), shown as the panel's heading. */
  place: string | undefined;
}

const AT = /^(-?\d{1,2}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)$/;

export function parseWeather(path: string, p: Readonly<Record<string, string>>): WeatherRoute {
  const m = AT.exec(p.at ?? '');
  const lat = m ? Number(m[1]) : Number.NaN;
  const lon = m ? Number(m[2]) : Number.NaN;
  const step = Number.parseInt(p.step ?? '', 10);
  return {
    tab: path.startsWith('past') ? 'past' : 'forecast',
    model: /^[a-z]{2,8}$/.test(p.model ?? '') ? p.model : undefined,
    variable: /^[a-z0-9_]{2,12}$/.test(p.var ?? '') ? p.var : undefined,
    step: Number.isFinite(step) && step >= 0 && step <= 400 ? step : undefined,
    date: /^\d{4}-\d{2}-\d{2}$/.test(p.date ?? '') ? p.date : undefined,
    at:
      Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180
        ? { lat, lon }
        : undefined,
    place: p.place ? p.place.slice(0, 120) : undefined,
  };
}

export function weatherAddress(r: WeatherRoute): string {
  const qs = new URLSearchParams();
  if (r.variable) qs.set('var', r.variable);
  if (r.tab === 'forecast') {
    if (r.model) qs.set('model', r.model);
    if (r.step !== undefined) qs.set('step', String(r.step));
  } else if (r.date) qs.set('date', r.date);
  if (r.at) qs.set('at', `${r.at.lat.toFixed(3)},${r.at.lon.toFixed(3)}`);
  if (r.at && r.place) qs.set('place', r.place);
  const q = qs.toString().replaceAll('%2C', ',');
  return `wsp://weather${r.tab === 'past' ? '/past' : ''}${q ? `?${q}` : ''}`;
}
