/** Reading documents: open a result, follow links, Back and Forward. */
import { expect, test } from './fixtures.ts';

test('open a search result, follow an internal link, then Back and Forward', async ({
  page,
  nav,
  data,
}) => {
  const dialogs: string[] = [];
  page.on('dialog', (d) => {
    dialogs.push(d.message());
    void d.dismiss();
  });

  await nav.open(`wsp://search?q=${encodeURIComponent(data.docQuery)}`);
  const results = nav.main.getByRole('list', { name: 'Search results' });
  let title = '';

  await nav.step('click a result', async () => {
    const hit = results.getByRole('link', { name: data.result }).first();
    title = ((await hit.textContent()) ?? '').trim();
    await hit.click();
    await nav.settled();
    await nav.expectAddress(data.docAddress);
    await expect(page).toHaveTitle(`${title} - Whisipedia Navigator`);
    await expect(page.getByText('Location:', { exact: true })).toBeVisible();
  });

  const article = nav.main.getByRole('article');
  const body = article.locator('.doc-body');

  await nav.step('the original page renders, with exactly one page heading', async () => {
    await expect(article).toBeVisible();
    await expect(body.locator('p:visible').first()).toBeVisible(); // Wikipedia leads with a hidden mw-empty-elt <p>
    await expect(nav.main.locator('h1')).toHaveCount(1);
    await expect(nav.heading).toContainText(title);
    await expect(nav.heading).toBeFocused();
    // Sanitized: no script, no inline style or handlers survive.
    await expect(body.locator('script, style, iframe, [style], [onclick]')).toHaveCount(0);
    expect(dialogs).toEqual([]);
    // Provenance footer.
    await expect(article.getByRole('button', { name: 'View Source Data' })).toBeVisible();
  });

  // The first visible link to another document in the library, once the library has said which of
  // the page's links lead nowhere (they are marked broken).
  await expect(nav.main.locator('[data-links-checked]')).toHaveCount(1);
  const link = body.locator('p a[data-wsp]:not(.broken-link):visible').first();
  let target = '';

  await nav.step('hovering a link shows its target in the status bar', async () => {
    target = (await link.getAttribute('data-wsp')) ?? '';
    expect(target).toMatch(/^wsp:\/\/[^/]+\/.+/);
    await link.hover();
    await expect(nav.status).toHaveText(target);
    await page.mouse.move(5, 5);
    await expect(nav.status).not.toHaveText(target);
  });

  let linkedTitle = '';
  await nav.step('clicking it opens the linked document', async () => {
    await link.click();
    await nav.settled();
    expect(await nav.address()).toBe(target);
    await expect(nav.main.locator('h1')).toHaveCount(1);
    linkedTitle = (await nav.heading.innerText()).trim();
    expect(linkedTitle).not.toBe(title);
  });

  await nav.step('Back returns to the first page, Forward to the linked one', async () => {
    await nav.button(/^Back\b/).click();
    await nav.settled();
    await expect(nav.heading).toContainText(title);
    await nav.expectAddress(data.docAddress);
    await nav.button(/^Forward\b/).click();
    await nav.settled();
    await expect(nav.heading).toHaveText(linkedTitle);
    await page.goBack();
    await nav.settled();
    await page.goBack();
    await nav.settled();
    await expect(nav.heading).toHaveText(`Search results for ${data.docQuery}`);
    await expect(results.getByRole('listitem').first()).toBeVisible();
  });
});

test('a link to a page outside the library is shown broken, with its target on hover', async ({
  nav,
  target,
}) => {
  test.skip(target === 'live', 'mock fixture: Alan Turing links to turing.org.uk');
  await nav.open('wsp://wikipedia-en-zim/Alan_Turing');
  const broken = nav.main.locator('a.broken-link', { hasText: 'Turing Digital Archive' });
  await expect(broken).toBeVisible();
  await broken.hover();
  await expect(nav.status).toHaveText(/^Not in library: https?:\/\//);
  await expect(
    nav.main.getByText(/link\(s\) on this page go to websites the library does not hold offline/),
  ).toBeVisible();
  // Remote images are replaced by a period placeholder, never fetched.
  await expect(nav.main.getByRole('img', { name: /^Image not in library/ })).toBeVisible();
});

test('View Source Data opens with Ctrl+U and closes with Escape', async ({ page, nav, data }) => {
  await nav.open(data.doc);
  await expect(nav.heading).toContainText(data.docTitle);
  await page.keyboard.press('Control+U');
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(/sha256|SHA-256|manifest/i);
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
});

test('the time machine lists versions and shows a diff', async ({ nav, target }) => {
  test.skip(target === 'live', 'needs a document with history; the mock has three Turing versions');
  await nav.open('wsp://wikipedia-en-zim/Alan_Turing');
  await nav.main.getByText('Time Machine: see this page as of an earlier date').click();
  const table = nav.main.locator('.tm-table');
  await expect(table.getByRole('row')).toHaveCount(4);
  await table.getByRole('link', { name: 'diff vs current' }).first().click();
  await nav.settled();
  await nav.expectAddress(/[?&]diff=1/);
  await expect(nav.main.getByRole('note')).toContainText('Time Machine:');
  await expect(nav.main.locator('.diff-stats')).toContainText(/line\(s\) added/);
});

test("a link to a page the site's archive never captured is marked before it is followed", async ({
  nav,
  target,
}) => {
  // 2026-10-19 audit: 71 links on 32 archived sites opened Not Found (a "contact" page, Talk: pages).
  test.skip(target === 'live', 'mock fixture: the mock holds no Turing Award page');
  await nav.open('wsp://wikipedia-en-zim/Alan_Turing');
  const gone = nav.main.locator('p a.uncaptured-link', { hasText: 'Turing Award' });
  await expect(gone).toBeVisible();
  await expect(nav.main.locator('p a.uncaptured-link', { hasText: 'Enigma machine' })).toHaveCount(
    0,
  );
  await gone.hover();
  await expect(nav.status).toHaveText('Not in library: wsp://wikipedia-en-zim/Turing_Award');
  await expect(nav.main.getByText(/this site’s archive did not capture them/)).toBeVisible();
});

test.describe('a missing page', () => {
  test.use({ allowConsole: [/status of 404/] });

  test('shows Not Found and offers a search', async ({ page, nav }) => {
    const gaps = page.waitForRequest((r) => r.url().endsWith('/v1/gaps') && r.method() === 'POST');
    await nav.open('wsp://wikipedia-en-zim/No_Such_Page_E2E');
    await expect(nav.heading).toHaveText('Not Found');
    await gaps; // the gap miner hears about it (POST /v1/gaps is harmless)
    await nav.main.getByRole('link', { name: /Search the library for/ }).click();
    await nav.settled();
    await nav.expectAddress(/^wsp:\/\/search\?q=No\+Such\+Page\+E2E|^wsp:\/\/search\?q=No%20Such/);
  });
});

test.describe('fixed bug E2E-1', () => {
  test.use({ tolerate: [] });

  test('opening a page with inline styles reports no CSP violation', async ({ nav }) => {
    // E2E-1 (docs/testing/e2e-findings.md): the API now drops inline styles, which
    // DOMParser used to report as CSP violations.
    await nav.open('wsp://wikipedia-en-zim/Alan_Turing');
    await expect(nav.heading).toHaveText(/Alan Turing/);
  });
});

test('an outside link to a page the library holds opens the library copy', async ({ nav }) => {
  // 2026-10-17: a link to en.wikipedia.org read "This site is not in the library".
  await nav.open('wsp://offline?url=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FTuring_machine');
  await nav.expectAddress(/^wsp:\/\/wikipedia-en-zim\/Turing_machine$/);
  await expect(nav.heading).toHaveText(/Turing machine/);
});
