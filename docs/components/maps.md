# Maps: the Atlas and its tile library

Owner request (2026-10-18): "expand the maps sections to include up to 4TB of High fidelity map data,
with a proper mix of world data + high res local data for all regions of the world", and "a properly,
style appropriate, fully modern featured map viewer(s)".

Before this, maps were the OpenStreetMap planet PBF (searchable named places, no rendering), scanned
USGS sheets and a coordinate locator box in the navigator.

## 1. What a map is made of

The Atlas draws four kinds of layer. Each is a **tileset**: one or more PMTiles archives (a single
file of tiles addressed by z/x/y, read with range requests) that the API serves as one
`/v1/maps/tiles/<tileset>/{z}/{x}/{y}` source.

| Tileset | Kind | What | Zoom | Licence |
|---|---|---|---|---|
| `basemap` | vector (MVT) | OpenStreetMap: roads, buildings, land use, water, places, POIs (Protomaps daily build) | 0-15, drawn to 22 | ODbL (OSM) |
| `terrain` | raster-dem (Terrarium WebP) | Elevation: Copernicus GLO-30 everywhere, national LiDAR where open (Mapterhorn) | 0-12 world, 13-15 where held, drawn to 22 | per region (Mapterhorn attribution page) |
| `imagery` | raster (WebP) | Satellite: Sentinel-2 10 m cloudless composite (ESA WorldCover S2 RGB) | 0-13, drawn to 18 | CC BY 4.0 |
| `charts`, `aero`, `topo-historic` | raster/vector | NOAA ENC, FAA VFR sectionals, USGS historical topo | per set | US Gov PD |

Hillshade and contour lines are drawn by the browser from `terrain`; 3D terrain uses it too. Labels
come from `basemap` and sit over imagery ("hybrid").

## 2. The 12 TB budget

The owner first set 4 TB, then raised it to 12 TB (2026-10-18). Sizes measured 2026-10-18 (Protomaps
`builds.json`, Mapterhorn `download_urls.json`, ESA S3 listing, `pmtiles extract --dry-run`). The
USGS historical topographic scans (D14, their own lane) are outside this budget.

| Phase | Set | Size | Notes |
|---|---|---|---|
| A | Protomaps planet z0-15 | 139 GB | one archive, daily builds, BLAKE3 + MD5 published |
| A | Fonts and sprites (protomaps/basemaps-assets) | ~6 MB | Noto Sans glyph ranges (OFL), sprites |
| A | Mapterhorn `planet.pmtiles` z0-12 | 356 GB | whole Earth, ~19 m/px at 512 px tiles |
| B | Mapterhorn regional archives cut at z16 | ~2.9 TB | 458 archives (9.6 TB in full); per archive z13-15 is ~7.6 %, z13-16 ~30 % (6-14-26: 264 GB -> 20 GB at z15) |
| B | Sentinel-2 composite, 10 m, rendered to z14 | ~0.4-0.6 TB | ESA WorldCover S2 RGB, 19,359 COGs, 9.2 TB read once and not kept |
| C | Open national orthophotos (sub-metre) | ~4-5 TB | NAIP (US, PD), LINZ (NZ), PDOK (NL), swisstopo, IGN (FR), PNOA (ES), ... by value and size |
| C | Mapterhorn z17-18 where held | rest of budget | finest LiDAR terrain, densest regions first |
| C | Overture buildings/places | ~0.3 TB | ML building footprints where OSM is thin; GeoParquet -> tiles |
| C | NOAA ENC, FAA sectionals | ~20 GB | conversion to tiles |
| | **Total planned** | **~11-12 TB** | each phase lands and is viewable before the next starts |

"High res local data for all regions": the vector basemap is street-level everywhere; terrain is
~1.2 m/px (z16) wherever an open national model exists (Mapterhorn's coverage: N. America 4.6 TB,
Europe 4.4 TB, Asia, Oceania, Africa, S. America smaller) and 19 m/px elsewhere; imagery is 10 m
everywhere and sub-metre where a national orthophoto is open.

## 3. Acquisition (`map-tiles` adapter)

A registry source per tileset part, adapter `map-tiles`, `index: false` (tiles are not searched; a
catalog document per archive is). Discovery kinds:

* `protomaps-builds`: newest entry of `build-metadata.protomaps.dev/builds.json` (MD5 checked).
* `pmtiles-list`: a JSON list of archives with MD5 and bounds (Mapterhorn `download_urls.json`),
  optionally filtered by bounds/name and **cut at a zoom** (`max_zoom`): clustered PMTiles store tiles
  in tile-id order, lowest zoom first, so the cut is `pmtiles extract --maxzoom` (go-pmtiles) and
  costs only the bytes kept.
* `files`: fixed URLs (fonts and sprites zip).

Archives land in the vault like any upstream file; nothing on the NAS is written by hand.

## 4. Serving (`wsp_api.maps`)

* `wsp_core.pmtiles`: a PMTiles v3 reader (header, varint directories, leaf directories, Hilbert tile
  ids) on any random-access file. Root directories stay in memory; leaf directories in a bounded LRU.
* A tileset is the current generations of its sources, highest zoom first. A tile comes from the
  first archive that covers it; a terrain or imagery tile that no archive holds at that zoom is cut from
  its nearest held ancestor (terrain decoded to metres, interpolated, re-encoded losslessly), so the
  browser sees one seamless source and never a hole.
* Reads run in their own small pool: a slow NAS read never holds a document read's slot.
* `GET /v1/maps/tilesets` (what is held: kind, zooms, bounds, attribution, URL template),
  `/v1/maps/tiles/...`, `/v1/maps/glyphs/{fontstack}/{range}.pbf`, `/v1/maps/sprites/...`.

## 5. The Atlas (`wsp://maps`)

MapLibre GL (BSD-3) with Protomaps' basemap layers (BSD-3), loaded only on map pages (its own bundle
budget), CSP build with a same-origin worker, no remote URL anywhere.

**Style that fits the navigator.** Each navigator theme has its own map palette, built as a Protomaps
"flavor": Navigator 96 a paper road atlas, Encarta 95 the encyclopedia atlas (warm land, deep blue
sea), System 7 greyscale, Phosphor green/amber a monochrome vector-screen map, dark mode a night map,
high contrast a high-contrast map. The chrome (toolbar, layer box, panels) uses the theme's own
widgets.

**Features.**
1. Layers: Map, Terrain (hillshade + contours), Satellite, Hybrid, Topo; 3D terrain with tilt;
   overlays (charts, sectionals, historic sheets) as they arrive. Only layers the library holds are
   offered.
2. Find a place: the library's own place index (OSM places, Wikidata coordinates), coordinates in
   any common form (decimal, DMS, UTM, MGRS), and a fit to the result.
3. What's here: a click shows the place's coordinates (decimal, DMS, UTM, MGRS), elevation, the named
   feature under it, and nearby library articles (Wikipedia, Wikivoyage) with links.
4. Cursor read-out of coordinates and elevation; scale bar; north arrow that resets bearing.
5. Measure distance (great circle) and area, with an elevation profile along a path.
6. Pins and saved places (per browser), GPX/GeoJSON/KML opened from disk as an overlay.
7. Address bar permalinks: `wsp://maps/@lat,lon,zoom[,bearing,pitch]?layer=terrain`.
8. Save the view as a PNG; print-friendly layout.
9. Keyboard: arrows pan, +/- zoom, Shift+arrows rotate/tilt, `/` focuses search.
10. Small maps inside documents that carry coordinates (articles, Factbook, places), with "Open in
    Atlas".

**Not in this round:** turn-by-turn routing (Valhalla tiles, ~100 GB; proposed for later).
