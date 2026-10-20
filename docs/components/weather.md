# Weather: forecasts, past weather and station records

Owner request (2026-10-20): a forecast page and a historical weather viewer, "and perhaps we can
introduce some sort of methodology of delineating between the 'offline' functionality where documents
are available via the NAS and live functionality where as much as possible is stored cold, but some
live data may be required for accurate functionality."

## 1. Offline, kept copy, live: how the library says what works without a connection

Every view that shows data says which of three kinds it is, with the same words and the same badge
everywhere (`Availability` in the API, `availabilityBadge` in the Navigator):

| Kind | Badge | Meaning | Without a connection |
|---|---|---|---|
| `offline` | **In the library** | Held in the vault on the NAS: sealed, checksummed, kept. | Works, forever. |
| `kept` | **Kept copy · as of …** | A copy of something that changes (a forecast), fetched while connected and kept. Every copy is also archived cold, so the archive grows into history. | Works, showing the newest copy and its age; says it is out of date once newer data would exist. |
| `live` | **Needs a connection** | Asked of the outside world at the moment it is used (a "check for a newer forecast now" button). | Says so; nothing else on the page depends on it. |

Rules that follow from it:

* **Nothing is fetched by the browser.** A page reads only the library's API; the library fetches. A
  page with no connection behind it still opens and shows what is kept.
* **Kept beats live.** A feed is fetched on a timer into a local hot copy and archived cold; the page
  reads the copy. `live` is reserved for an explicit action the reader takes.
* **Age is always shown** for a kept copy, with what "out of date" means for it (a forecast run is
  superseded every 6 hours; a 3-day-old run is still a forecast, but a worse one).
* **The connection is reported, not guessed**: the last successful and failed fetch of each feed
  (`connection` in `GET /v1/weather`), so "no newer forecast" and "no connection" read differently.

## 2. What is held

| Product | Source (registry) | Kind | Licence | Size |
|---|---|---|---|---|
| GFS 0.25° forecast, 0-240 h every 3 h | `weather-gfs` (NOAA via AWS Open Data) | kept | US Gov public domain | ~550 MB a run |
| ECMWF IFS 0.25° forecast, 0-144 h / 3 h, to 240 h / 6 h | `weather-ecmwf-ifs` (ECMWF open data) | kept | CC BY 4.0 | ~330 MB a run |
| ECMWF AIFS 0.25° (machine-learned) forecast, 0-240 h / 6 h | `weather-ecmwf-aifs` | kept | CC BY 4.0 | ~160 MB a run |
| ERA5 reanalysis, daily, 0.25°, 1940 to about a week ago | `era5-daily` (ECMWF/Copernicus, ARCO-ERA5 copy on Google Cloud, no account) | offline | Copernicus licence (CC BY 4.0 terms) | ~6 GB a year, ~0.5 TB in all |
| GHCN-Daily station records, 1763 to yesterday, ~129,000 stations | `ghcn-daily` (NOAA NCEI) | offline | US Gov public domain | 3.8 GB a snapshot |

Variables (forecast and ERA5): 2 m temperature (ERA5: daily mean, minimum, maximum), 2 m dew point,
10 m wind (forecast: components; ERA5: daily mean speed), gusts (GFS, IFS), mean sea-level pressure,
precipitation (forecast: per interval; ERA5: daily total), total cloud cover.

**Archive policy.** Every run is fetched into the hot copy; the 00 UTC run of each model is archived in
full (`[weather] archive_runs`, default `["00"]`): about 1 GB a day, 0.4 TB a year. The hot copy keeps
the newest two runs of each model on the local disk (~7 GB).

**Not held, and why.** ERA5 through the Copernicus API needs an account; the ARCO-ERA5 copy is the same
data without one. Weather radar and satellite loops are live-only feeds with no free global archive;
they are not planned.

## 3. How it flows

* **Forecasts.** `whisipedia-weather.timer` runs `wsp weather pull` every 30 minutes: for each model,
  the newest run whose last wanted step is published, fetched field by field with HTTP range requests
  from the provider's index files (`.idx` for GFS, `.index` for ECMWF), decoded with ecCodes,
  normalised to one grid (0.25°, 90°N to 90°S, 0° to 359.75°E) and units, and written to the hot copy
  as one memory-mapped `uint16` array per variable (`<state>/weather/hot/<model>/<run>/`). The GRIB
  bytes of an archived run go to `_incoming/<source>/<day>.<run>/`; `wsp weather archive` writes the
  ingest plan and the vault seals the day (a partition per run, ADR-0010).
* **ERA5.** `wsp weather era5 --year Y` reads ERA5's hourly fields (t2m, tp hourly; others 3-hourly),
  makes the day's eight daily fields, and packs a year as two files per variable: the year's maps
  (a zip of one PNG per day) and its point series (an `.npz` of 40×40-point tiles × days). Newest years
  first; the current year is re-cut monthly. A lane backfills to 1940 at the NAS write budget.
* **Stations.** `ghcn-daily` is a document-collection source (the tarball and station lists, `index:
  false`); `wsp weather stations` reads the sealed tarball (paced) into a local SQLite file of
  per-station, per-element, per-year values.

**Encoding.** A value is `offset + scale × n` for `n` in 0..65534 (65535: missing), per variable
(temperature 0.003 K, pressure 0.5 Pa, precipitation 0.01 mm, wind 0.004 m/s, cloud 0.002 %). A map
field travels to the browser as a PNG whose red and green bytes are `n`'s high and low bytes; the
browser colours it, reprojects it to the map and reads values under the cursor from it.

## 4. Serving (`wsp_api.weather`)

* `GET /v1/weather`: what is held, its availability, the variables (units, scale, offset) and the
  connection state of each feed.
* `GET /v1/weather/forecast/{model}/{run}/{var}/{step}.png`, `GET /v1/weather/forecast/point?lat&lon`
  (every model's newest run at a point).
* `GET /v1/weather/past/{var}/{date}.png`, `GET /v1/weather/past/point?lat&lon&from&to` (daily series
  and the 1991-2020 normal for each day of the year).
* `GET /v1/weather/stations?lat&lon`, `GET /v1/weather/stations/{id}?from&to`.
* `POST /v1/weather/refresh` (loopback only): starts the pull now (the one `live` action).

## 5. The page (`wsp://weather`)

A map (the Atlas's basemap when held) with the chosen field drawn over it, a legend, the value under the
cursor, a time slider with play, model and variable choices, °C/°F. **Forecast** and **Past weather**
tabs. Clicking a place (or searching the gazetteer) opens its panel: every model's forecast together
(temperature, precipitation, wind, pressure, cloud), or the past at that point (any range from 1940,
against its normal), with the nearest stations' records. The availability strip at the top says what
each part is, how old it is and whether the library is connected.
