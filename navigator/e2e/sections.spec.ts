/**
 * Places in a page and the way back to them (trawl 2026-11-10, batch F1): an address with a
 * `#section` opens at the section, a link inside the page lands on its target however the id is
 * written, Back returns to where the reader was, and a navigation that is stopped leaves the page on
 * screen working. Mock target only: the pages are patched into the mock library's answers.
 */
import type { Page } from '@playwright/test';
import { expect, test } from './fixtures.ts';

test.skip(({ target }) => target === 'live', 'patched pages: mock only');

const FRANK = 'wsp://gutenberg-zim/Frankenstein%3B%20Or%2C%20The%20Modern%20Prometheus.84';
const ARTICLE = 'wsp://wikipedia-en-zim/Alan_Turing';

const FILLER = '<p>Filler paragraph, long enough to take a line or two of the page.</p>\n'.repeat(
  60,
);

/** The mock library's answer for one article, with this HTML as the page. */
async function patchArticle(page: Page, html: string): Promise<void> {
  await page.route('**/v1/documents/**Alan_Turing**', async (route) => {
    if (/\/history(\?|$)/.test(route.request().url())) return route.fallback();
    const res = await route.fetch();
    const doc = (await res.json()) as Record<string, unknown>;
    await route.fulfill({ response: res, json: { ...doc, html, section: null } });
  });
}

/** How far below the top of the viewport (the scrolling `main`) an element starts. */
async function offsetInMain(page: Page, selector: string): Promise<number> {
  return page.evaluate((sel) => {
    const main = document.querySelector('main')!;
    const el = main.querySelector(sel)!;
    return el.getBoundingClientRect().top - main.getBoundingClientRect().top;
  }, selector);
}

const scrollTop = (page: Page) => page.evaluate(() => document.querySelector('main')!.scrollTop);

test('an address with a #section opens at the section, with focus there', async ({ page, nav }) => {
  // Text after the section too, so it can reach the top of the window.
  await patchArticle(
    page,
    `<h1>Sections</h1>${FILLER}<h2 id="Legacy">Legacy</h2>${FILLER}<p><a name="notes"></a>Notes.</p>${FILLER}`,
  );
  await nav.open(`${ARTICLE}#Legacy`);
  const heading = nav.main.getByRole('heading', { name: 'Legacy' });
  await expect(heading).toBeInViewport();
  expect(await scrollTop(page)).toBeGreaterThan(0);
  expect(Math.abs(await offsetInMain(page, '#pg-Legacy'))).toBeLessThan(40);
  await expect(heading).toBeFocused();

  // An <a name> target takes focus too, though a link without href takes none by itself.
  await nav.go(`${ARTICLE}#notes`);
  expect(Math.abs(await offsetInMain(page, '#pg-notes'))).toBeLessThan(40);
  await expect(nav.main.locator('#pg-notes')).toBeFocused();
});

test('Back to an address with a #section returns to where the reader was, not to the section', async ({
  page,
  nav,
}) => {
  await nav.open('wsp://home');
  await nav.go(`${FRANK}#chap01`);
  const atSection = await scrollTop(page);
  expect(atSection).toBeGreaterThan(300);
  // The reader scrolls back up the page, then goes elsewhere.
  await page.evaluate(() => {
    document.querySelector('main')!.scrollTop = 250;
  });
  await nav.go('wsp://about');
  await expect(nav.heading).toContainText(/About/);
  await page.keyboard.press('Alt+ArrowLeft');
  await nav.settled();
  await nav.expectAddress(/#chap01$/);
  await expect.poll(() => scrollTop(page)).toBe(250);

  await nav.step('Forward and Back again keep each page its own place', async () => {
    await page.keyboard.press('Alt+ArrowRight');
    await nav.settled();
    await expect(nav.heading).toContainText(/About/);
    await page.keyboard.press('Alt+ArrowLeft');
    await nav.settled();
    await expect.poll(() => scrollTop(page)).toBe(250);
  });
});

test('links inside a page land on their target: encoded ids, <a name>', async ({ page, nav }) => {
  await patchArticle(
    page,
    `<h1>Places</h1>
     <p><a href="#Caf%C3%A9">To the café</a> · <a href="#notes">To the notes</a> · <a href="#gone">Nowhere</a></p>
     ${FILLER}<h2 id="Café">Café</h2><p>Coffee.</p>
     ${FILLER}<p><a name="notes"></a>The notes.</p>${FILLER}`,
  );
  await nav.open(ARTICLE);

  await nav.main.getByRole('link', { name: 'To the café' }).click();
  await expect(nav.main.getByRole('heading', { name: 'Café' })).toBeInViewport();
  expect(Math.abs(await offsetInMain(page, '[id="pg-Café"]'))).toBeLessThan(40);
  await expect(nav.main.getByRole('heading', { name: 'Café' })).toBeFocused();

  await page.evaluate(() => {
    document.querySelector('main')!.scrollTop = 0;
  });
  await nav.main.getByRole('link', { name: 'To the notes' }).click();
  expect(Math.abs(await offsetInMain(page, '#pg-notes'))).toBeLessThan(40);

  // A target the page does not have is plain text, not a dead link.
  await expect(nav.main.getByRole('link', { name: 'Nowhere' })).toHaveCount(0);
  await expect(nav.main.getByText('Nowhere')).toBeVisible();

  await nav.step('the same places by address', async () => {
    await nav.go(`${ARTICLE}#Café`);
    expect(Math.abs(await offsetInMain(page, '[id="pg-Café"]'))).toBeLessThan(40);
    await nav.go(`${ARTICLE}#notes`);
    expect(Math.abs(await offsetInMain(page, '#pg-notes'))).toBeLessThan(40);
  });
});

test('the section stays in place when a picture above it arrives late', async ({ page, nav }) => {
  // A tall picture with no width or height in the markup, above the section, answered after the
  // page has been shown and scrolled.
  let release: () => void = () => undefined;
  const held = new Promise<void>((r) => {
    release = r;
  });
  await page.route('**/v1/assets/wikipedia-en-zim/1/late.svg', async (route) => {
    await held;
    await route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="900"><rect width="400" height="900" fill="gray"/></svg>',
    });
  });
  await patchArticle(
    page,
    `<h1>Late picture</h1><p>Lead.</p>
     <p><img src="/v1/assets/wikipedia-en-zim/1/late.svg" alt="A tall picture" loading="eager"></p>
     ${FILLER}<h2 id="Legacy">Legacy</h2>${FILLER}`,
  );
  await page.goto(`/#${ARTICLE}#Legacy`);
  await expect(nav.main.getByRole('heading', { name: 'Legacy' })).toBeInViewport();
  const before = await scrollTop(page);
  // The reader's own sanitiser loads pictures lazily: bring it into loading range, then back.
  await page.evaluate(() => document.querySelector('main img')?.setAttribute('loading', 'eager'));
  release();
  await expect
    .poll(() =>
      page.evaluate(() => (document.querySelector('main img') as HTMLImageElement).naturalHeight),
    )
    .toBe(900);
  await expect.poll(() => scrollTop(page)).toBeGreaterThan(before + 500); // the page grew above
  expect(Math.abs(await offsetInMain(page, '#pg-Legacy'))).toBeLessThan(40);
});

test('a navigation that is stopped leaves the Atlas on screen alive', async ({ page, nav }) => {
  test.slow();
  await nav.open('wsp://atlas/@46.6875,7.86,14z');
  const canvas = page.locator('.atlas-map canvas');
  await expect(canvas).toBeVisible();

  // A page that never answers: the reader follows it, waits, presses Stop.
  let release: () => void = () => undefined;
  const held = new Promise<void>((r) => {
    release = r;
  });
  await page.route('**/v1/documents/**Alan_Turing**', async (route) => {
    await held;
    await route.abort();
  });
  await page.keyboard.press('Control+L');
  await page.keyboard.type(ARTICLE);
  await page.keyboard.press('Enter');
  await expect(page.locator('#app')).toHaveAttribute('aria-busy', 'true');
  await nav.button('Stop').click();
  await expect(page.locator('#app')).not.toHaveAttribute('aria-busy', 'true');
  await expect(nav.status).toHaveText('Transfer interrupted!');
  release();

  // Before: the load's signal was the page's only signal, and Stop removed the map under the reader.
  await expect(canvas).toBeVisible();
  const zoomIn = page.getByRole('button', { name: 'Zoom in' }).first();
  await zoomIn.click();
  await expect.poll(async () => /,(\d+(?:\.\d+)?)z/.exec(await nav.address())?.[1]).not.toBe('14');
});
