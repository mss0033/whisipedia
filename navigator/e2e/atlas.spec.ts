/**
 * The Atlas (wsp://atlas): the map draws from the library's tiles, follows the theme, and its tools
 * work with the mouse and keyboard. Mock target: a few streets of Interlaken and terrain at z13
 * (mock/fixtures/maps).
 */
import { writeFileSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { expect, test } from './fixtures.ts';

// Live too since 2026-10-18 (the planet basemap and the world terrain are in the library): the live
// run found the casino listed before Interlaken, "unter" answered from Germany and no "where is" map.

// The mock holds no copy of the places' Wikipedia articles: the lookups answer 404.
test.use({ allowConsole: [/status of 404/] });

const START = 'wsp://atlas/@46.6875,7.86,14z';

async function mapReady(page: Page): Promise<void> {
  await expect(page.locator('.atlas-map canvas')).toBeVisible();
  // The style and the first tiles: MapLibre marks the canvas container when it is idle.
  await page.waitForFunction(
    () => document.querySelector('.atlas-map canvas')?.getAttribute('aria-label') !== null,
  );
  await page.waitForTimeout(1500);
}

async function clickMap(
  page: Page,
  x: number,
  y: number,
  opts: { double?: boolean } = {},
): Promise<void> {
  const box = await page.locator('.atlas-map').boundingBox();
  if (!box) throw new Error('no map');
  if (opts.double) await page.mouse.dblclick(box.x + x, box.y + y);
  else await page.mouse.click(box.x + x, box.y + y);
}

test('maps opens the Atlas, which draws the library’s map and keeps the view in the address', async ({
  page,
  nav,
}) => {
  await nav.open('wsp://home');
  await nav.go('wsp://maps');
  await nav.expectAddress(/^wsp:\/\/atlas/);
  await nav.open(START);
  await mapReady(page);
  const layers = page.getByRole('group', { name: 'Layers' });
  await expect(layers.getByRole('button', { name: 'Map' })).toHaveAttribute('aria-pressed', 'true');
  await expect(layers.getByRole('button', { name: 'Terrain' })).toBeVisible();
  await expect(layers.getByRole('button', { name: 'Satellite' })).toHaveCount(0); // no imagery held
  // Drag the map: the address follows (replaced, not a new history entry).
  const box = await page.locator('.atlas-map').boundingBox();
  if (!box) throw new Error('no map');
  await page.mouse.move(box.x + 600, box.y + 300);
  await page.mouse.down();
  await page.mouse.move(box.x + 520, box.y + 260, { steps: 6 });
  await page.mouse.up();
  await nav.expectAddress(/^wsp:\/\/atlas\/@46\.\d+,7\.\d+,14z$/);
  await nav.snapshot('atlas-map');
});

test('a click says what is there: coordinates in every form, elevation, nearby places', async ({
  page,
  nav,
}) => {
  await nav.open(START);
  await mapReady(page);
  await clickMap(page, 640, 300);
  const panel = page.locator('.atlas-panel');
  await expect(panel.locator('.atlas-panel-title')).toHaveText("What's here");
  for (const form of ['Decimal degrees', 'Degrees, minutes, seconds', 'UTM', 'MGRS']) {
    await expect(panel.getByRole('rowheader', { name: form })).toBeVisible();
  }
  await expect(panel.locator('.atlas-coords code').nth(3)).toHaveText(/^32TMS \d{5} \d{5}$/);
  await expect(panel.locator('.atlas-elevation')).toHaveText(/Elevation: 5\d\d m/); // Interlaken, ~566 m
  await expect(panel.locator('.atlas-nearby li').first()).toContainText('Interlaken');
  await expect(page.locator('.atlas-status-coords')).toHaveText(/^46\.\d{5}, 7\.\d{5}$/);
  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();
});

test('find a place by name, and a point by MGRS', async ({ page, nav }) => {
  await nav.open(START);
  await mapReady(page);
  await page.keyboard.press('/');
  await expect(page.getByRole('searchbox', { name: 'Find a place or coordinates' })).toBeFocused();
  await page.keyboard.type('unter');
  await page.keyboard.press('Enter');
  await expect(
    page.locator('.atlas-panel .atlas-places').getByRole('button', { name: 'Unterseen' }),
  ).toBeVisible();
  await expect(page.locator('.atlas-library-search a')).toHaveAttribute(
    'href',
    '#wsp://search?q=unter',
  );
  const search = page.getByRole('searchbox', { name: 'Find a place or coordinates' });
  await search.fill('32TMS1306970932');
  await search.press('Enter');
  await expect(page.locator('.atlas-panel-title')).toHaveText("What's here");
  await expect(page.locator('.atlas-coords code').first()).toHaveText(/^46\.686\d\d, 7\.863\d\d$/);
});

test('measure a distance and an area; the profile reads the terrain', async ({ page, nav }) => {
  await nav.open(START);
  await mapReady(page);
  const tools = page.getByRole('group', { name: 'Tools' });
  await tools.getByRole('button', { name: 'Measure' }).click();
  await clickMap(page, 400, 200);
  await clickMap(page, 600, 350);
  await clickMap(page, 800, 300, { double: true });
  const body = page.locator('.atlas-panel-body');
  await expect(body).toContainText(/\d+(\.\d+)? (k?m) · 3 points/);
  await expect(page.locator('.atlas-profile')).toContainText(/Elevation \d+–\d+ m · climb \d+ m/);
  await expect(page.locator('.atlas-profile-chart')).toBeVisible();
  await body.getByRole('button', { name: 'Imperial' }).click();
  await expect(body).toContainText(/(ft|mi) · 3 points/);
  await tools.getByRole('button', { name: 'Area' }).click();
  await clickMap(page, 400, 200);
  await clickMap(page, 600, 200);
  await clickMap(page, 600, 400, { double: true });
  await expect(body).toContainText(/(acres|sq mi) · perimeter/);
});

test('a GPX file from disk is drawn and summarised; places are saved per browser', async ({
  page,
  nav,
}, info) => {
  await nav.open(START);
  await mapReady(page);
  const gpx = info.outputPath('walk.gpx');
  writeFileSync(
    gpx,
    '<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><wpt lat="46.687" lon="7.862"><name>Start</name></wpt><trk><name>Walk</name><trkseg><trkpt lat="46.687" lon="7.862"/><trkpt lat="46.690" lon="7.858"/><trkpt lat="46.692" lon="7.852"/></trkseg></trk></gpx>',
  );
  await page.setInputFiles('input[type=file]', gpx);
  await expect(page.locator('.atlas-panel-title')).toHaveText('Your files');
  await expect(page.locator('.atlas-panel-body')).toContainText(
    'walk.gpx · 1 points, 1 tracks (961 m)',
  );
  await clickMap(page, 300, 300);
  await page.locator('.atlas-panel').getByRole('button', { name: 'Save place' }).click();
  await page.getByRole('group', { name: 'Tools' }).getByRole('button', { name: 'Places' }).click();
  await expect(page.locator('.atlas-panel .atlas-places li')).toHaveCount(1);
});

test('the map follows the theme without errors', async ({ page, nav }) => {
  await nav.open(START);
  await mapReady(page);
  for (const theme of ['Phosphor (Green)', "Encarta '95", 'System 7', "Navigator '96"]) {
    await nav.menu('View', theme);
    await page.waitForTimeout(800);
    await expect(page.locator('.atlas-map canvas')).toBeVisible();
  }
  await page
    .getByRole('group', { name: 'Layers' })
    .getByRole('button', { name: 'Terrain' })
    .click();
  await page.getByRole('group', { name: 'Tools' }).getByRole('button', { name: '3D' }).click();
  await expect(
    page.getByRole('group', { name: 'Tools' }).getByRole('button', { name: '3D' }),
  ).toHaveAttribute('aria-pressed', 'true');
  await nav.expectAddress(/layer=terrain/);
});

test('a "where is" answer shows a live map and opens the Atlas pinned', async ({ page, nav }) => {
  await nav.open('wsp://search?q=where+is+lisbon');
  const answer = page.getByRole('complementary', { name: /^Map of Lisbon/ }); // the mock adds ", Portugal"
  await expect(answer.locator('.minimap-live canvas')).toBeVisible();
  const open = answer.getByRole('link', { name: 'Open in the Atlas »' });
  await expect(open).toHaveAttribute(
    'href',
    /#wsp:\/\/atlas\/@38\.7\d*,-9\.1\d*,1[12]z\?pin=38\.7\d*,-9\.1\d*$/,
  );
  await open.click();
  await nav.expectAddress(/^wsp:\/\/atlas\/@38\.7\d*,-9\.1\d*,1[12]z/);
  await expect(page.locator('.atlas-panel-title')).toHaveText("What's here");
});
