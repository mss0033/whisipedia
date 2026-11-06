# Equal-area projections for the Weather page and the Atlas world view (2026-11-05)

The owner asked whether the weather map and the Atlas could show other projections, with Gall–Peters as
a possible default (2026-11-05). For now the library keeps to what MapLibre supports; this note records
what was checked and how it could be done later. Nothing in it has been done.

## What MapLibre can do

The Navigator draws both maps with MapLibre GL JS 6.11.2 (`navigator/package.json`). Its style
specification offers two projections only: `mercator` and `vertical-perspective` (the 3-D globe the
Atlas already has). There is no equal-area or pseudo-cylindrical projection, so Gall–Peters, Equal Earth
or Mollweide cannot be a MapLibre setting; they need a renderer of our own for the views that use them.

## Where an equal-area projection is worth it

- **Weather.** The page is viewed at world and continent scale, and areas matter there: a rain band over
  Canada should not look larger than one over Brazil. The field is already a regular latitude/longitude
  grid (`navigator/src/weather/weather-view.ts`, drawn into a canvas), so reprojecting it is cheap.
- **Atlas, zoomed out only.** At city and street zoom every projection looks the same, and the vector and
  terrain tiles are Mercator by construction. An equal-area world view helps below about zoom 4; closer
  in, the normal MapLibre map takes over. The globe stays as the distortion-free option.

## How

1. Add `d3-geo` and `d3-geo-projection` to the Navigator (bundled, so it works offline; pinned versions).
   Projections offered: Gall–Peters (`geoCylindricalEqualArea().parallel(45)`), Equal Earth
   (`geoEqualEarth`), Mollweide (`geoMollweide`), plus Mercator and the globe through MapLibre.
2. A 2-D canvas renderer for the Weather page: the field reprojected per pixel (inverse projection into
   the lat/lon grid, nearest or bilinear), then coastlines (`earth`) and country borders
   (`boundaries_country`) drawn from the library's own Protomaps basemap at zoom 0-4, decoded from the
   PMTiles archive the API already serves. Pan and zoom with `d3-zoom`; hover values and the timeline
   reuse the existing code.
3. A projection menu on both pages; the choice is remembered per viewer (localStorage, guarded) and the
   default is a constant the owner picks.
4. Atlas: the equal-area world view below a zoom threshold, handing off to MapLibre at the same centre
   when zooming in, and back when zooming out.

## Choosing the default

Gall–Peters keeps areas exact but stretches shapes strongly (the tropics tall and thin, high latitudes
flattened). Equal Earth (Šavrič, Patterson and Jenny, 2018) is also equal-area with much less shape
distortion and is what many atlases now use. The plan offers both; the owner chooses the default after
seeing them side by side.

## Effort and checks

Medium: the weather renderer is the bulk; the Atlas world view reuses it. Checks: the Navigator's
typecheck, biome and Playwright tests; screenshots of each projection with every weather field; first
paint and pan speed on the weather page at least as fast as today.
