/**
 * Live library only: real ZIM archives (Wikipedia, Stack Exchange) served
 * from the NAS. First loads can take several seconds.
 */
import { expect, test } from './fixtures.ts';

test.beforeEach(({ target }) => {
  test.skip(target !== 'live', 'needs the real library (npm run e2e:live)');
});

test('a Wikipedia article from the ZIM renders with an image that actually loads', async ({
  page,
  nav,
}) => {
  await nav.open('wsp://wikipedia-en-zim/Alan_Turing');
  await expect(nav.heading).toHaveText(/Alan Turing/);
  await expect(nav.main.locator('h1')).toHaveCount(1);
  const body = nav.main.locator('.doc-body');
  await expect(body).toContainText(/mathematician/i);

  await nav.step('an image loads from the library', async () => {
    const imgs = body.locator('img');
    expect(await imgs.count(), 'images kept by the sanitizer').toBeGreaterThan(0);
    const img = imgs.first();
    await img.scrollIntoViewIfNeeded();
    await expect
      .poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth), {
        timeout: 20_000,
      })
      .toBeGreaterThan(0);
    expect(await img.getAttribute('src')).toMatch(/^\/v1\/assets\//);
    await nav.snapshot('alan-turing');
  });

  await nav.step('no visible raw wikitext or script text', async () => {
    const text = await body.innerText();
    expect(text).not.toMatch(/RLCONF|document\.documentElement\.classList/);
  });

  await nav.step('an internal link opens another article', async () => {
    const link = body.locator('p a[data-wsp]:not(.broken-link):visible').first();
    const to = await link.getAttribute('data-wsp');
    await link.click();
    await nav.settled();
    expect(await nav.address()).toBe(to);
    await expect(nav.main.locator('h1')).toHaveCount(1);
    await page.goBack();
    await nav.settled();
    await expect(nav.heading).toHaveText(/Alan Turing/);
  });
});

test('Stack Exchange: site page, Enter site, the book list, a book front page', async ({
  page,
  nav,
}) => {
  await nav.open('wsp://stackexchange');

  await nav.step('the site front page', async () => {
    await expect(nav.heading).toHaveText(/^Welcome to /);
    await expect(
      page.getByText('Netsite:', { exact: true }).or(page.getByText('Location:', { exact: true })),
    ).toBeVisible();
  });

  await nav.step('Enter site shows the list of books', async () => {
    await nav.main.getByRole('link', { name: 'Enter site' }).click();
    await nav.settled();
    await expect(nav.heading).toHaveText(/Stack Exchange/);
    expect(await nav.main.locator('.doc-body li a').count()).toBeGreaterThan(5);
  });

  await nav.step('a book opens its front page', async () => {
    const book = nav.main.getByRole('link', { name: 'askubuntu.com en all', exact: true });
    const to = await book.getAttribute('data-wsp');
    await book.click();
    await nav.settled();
    expect(await nav.address()).toBe(to);
    await expect(nav.main).not.toContainText(/Not Found|Unable to locate/);
    await expect(nav.main.locator('h1')).toHaveCount(1);
    await nav.snapshot('stackexchange-book');
  });
});

test.describe('fixed bug E2E-4', () => {
  test.use({ tolerate: [] });

  test('a question on a Stack Exchange book front page opens', async ({ nav }) => {
    // E2E-4 (docs/testing/e2e-findings.md): links and images on a book front page
    // used to resolve without the book prefix (wsp://stackexchange/questions/…).
    await nav.open('wsp://stackexchange/askubuntu.com_en_all');
    const q = nav.main
      .locator('.doc-body a[data-wsp]:not(.broken-link):visible', { hasText: /\?\s*$/ })
      .first();
    const title = (await q.innerText()).trim();
    await q.click();
    await nav.settled();
    await expect(nav.heading).not.toHaveText('Not Found');
    await expect(nav.heading).toContainText(title.slice(0, 15));
  });
});

test.describe('fixed bug E2E-3', () => {
  test.use({ allowConsole: [/status of 404/] });

  test('the first book in the Stack Exchange list opens', async ({ nav }) => {
    // E2E-3 (docs/testing/e2e-findings.md): the book list used to link
    // 3dprinting.stackexchange.com_en_all_2026-07a (a re-release), which was 404.
    await nav.open('wsp://stackexchange/_books');
    await nav.main.locator('.doc-body li a').first().click();
    await nav.settled();
    await expect(nav.heading).not.toHaveText('Not Found');
  });
});
