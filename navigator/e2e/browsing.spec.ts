/** Surfing the offline web: directory, Hotlist, History, What's New, System Monitor. */
import { expect, test } from './fixtures.ts';

test('browse the directory down to a document and back up', async ({ page, nav }) => {
  await nav.open('wsp://home');

  await nav.step('the Net Directory button opens the directory', async () => {
    await page
      .getByRole('navigation', { name: 'Directory buttons' })
      .getByRole('link', { name: 'Net Directory' })
      .click();
    await nav.settled();
    await nav.expectAddress(/^wsp:\/\/directory$/);
    await expect(nav.heading).toHaveText('WhisiDir!');
    const crumbs = nav.main.getByRole('navigation', { name: 'Category path' });
    await expect(crumbs).toHaveText('Top');
  });

  const categories = nav.main.locator('.dir-children a');
  const entries = nav.main.locator('.dir-entries a');
  const trail: string[] = [];

  await nav.step('click into categories until there are listings', async () => {
    for (let depth = 0; depth < 5 && (await entries.count()) === 0; depth += 1) {
      expect(
        await categories.count(),
        `categories at ${trail.join(' › ') || 'Top'}`,
      ).toBeGreaterThan(0);
      const cat = categories.first();
      const name = (await cat.innerText()).trim();
      await cat.click();
      await nav.settled();
      trail.push(name);
      const crumbs = nav.main.getByRole('navigation', { name: 'Category path' });
      await expect(crumbs.locator('[aria-current="page"]')).toHaveText(name);
      await expect(page).toHaveTitle(`Directory: ${name} - Whisipedia Navigator`);
    }
    expect(await entries.count()).toBeGreaterThan(0);
  });

  await nav.step('open a listed page', async () => {
    const entry = entries.first();
    const title = (await entry.innerText()).trim();
    await entry.click();
    await nav.settled();
    await expect(page).not.toHaveTitle(/Directory/);
    await expect(nav.main.getByRole('article').or(nav.main.locator('.page-site'))).toBeVisible();
    expect(title.length).toBeGreaterThan(0);
  });

  await nav.step('Back to the category, then the breadcrumb to Top', async () => {
    await page.keyboard.press('Alt+ArrowLeft');
    await nav.settled();
    const crumbs = nav.main.getByRole('navigation', { name: 'Category path' });
    await expect(crumbs.locator('[aria-current="page"]')).toHaveText(trail.at(-1)!);
    await crumbs.getByRole('link', { name: 'Top' }).click();
    await nav.settled();
    await nav.expectAddress(/^wsp:\/\/directory$/);
  });
});

test('Hotlist: Ctrl+D adds the page, the Hotlist shows it, Delete removes it', async ({
  page,
  nav,
  data,
}) => {
  await nav.open(data.doc);
  const title = (await nav.heading.innerText()).trim();
  expect(title).toMatch(data.docTitle);

  await nav.step('Ctrl+D bookmarks the page', async () => {
    await page.keyboard.press('Control+D');
    await expect(nav.status).toHaveText(/^Added “.+” to the Hotlist\.$/);
    await page.keyboard.press('Control+D');
    await expect(nav.status).toHaveText(/is already in the Hotlist\.$/);
  });

  await nav.step('the Bookmarks menu lists it', async () => {
    await page
      .getByRole('menubar', { name: 'Main menu' })
      .getByRole('menuitem', { name: 'Bookmarks' })
      .click();
    const menu = page.getByRole('menu', { name: 'Bookmarks' });
    await expect(menu.getByRole('menuitem', { name: data.docTitle })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
  });

  await nav.step('Ctrl+B opens the Hotlist with the bookmark', async () => {
    await nav.main.focus();
    await page.keyboard.press('Control+B');
    await nav.settled();
    await expect(nav.heading).toHaveText('Hotlist');
    await expect(nav.main.getByRole('link', { name: data.docTitle })).toBeVisible();
  });

  await nav.step('Delete removes it', async () => {
    await nav.main.getByRole('button', { name: /^Delete / }).click();
    await expect(nav.main.getByRole('link', { name: data.docTitle })).toHaveCount(0);
    await expect(nav.main).toContainText('Your Hotlist is empty.');
  });

  await nav.step('the bookmark is gone after a reload too', async () => {
    await page.reload();
    await nav.settled();
    await expect(nav.main).toContainText('Your Hotlist is empty.');
  });
});

test('History lists visited pages, filters them and removes one', async ({ page, nav, data }) => {
  await nav.open(data.doc);
  await nav.go(data.doc2);
  await expect(nav.heading).toContainText(data.doc2Title);

  await nav.step('Ctrl+H opens History with both pages', async () => {
    await page.keyboard.press('Control+H');
    await nav.settled();
    await expect(nav.heading).toHaveText('History');
    await expect(nav.main.getByRole('link', { name: data.docTitle })).toBeVisible();
    await expect(nav.main.getByRole('link', { name: data.doc2Title })).toBeVisible();
  });

  await nav.step('typing in Find filters the list', async () => {
    const find = nav.main.getByRole('searchbox', { name: 'Find:' });
    await find.fill('');
    const title2 = await nav.main.getByRole('link', { name: data.doc2Title }).first().innerText();
    await find.pressSequentially(title2.trim());
    await expect(nav.main.getByRole('link', { name: data.doc2Title })).toBeVisible();
    await expect(nav.main.getByRole('link', { name: data.docTitle })).toHaveCount(0);
    await find.fill('');
  });

  await nav.step('Remove takes one page out of History', async () => {
    const row = nav.main
      .getByRole('row')
      .filter({ has: page.getByRole('link', { name: data.docTitle }) });
    await row.getByRole('button', { name: /^Remove .* from history$/ }).click();
    await expect(nav.main.getByRole('link', { name: data.docTitle })).toHaveCount(0);
    await expect(nav.main.getByRole('link', { name: data.doc2Title })).toBeVisible();
  });

  await nav.step('a history entry opens its page', async () => {
    await nav.main.getByRole('link', { name: data.doc2Title }).first().click();
    await nav.settled();
    await expect(nav.heading).toContainText(data.doc2Title);
  });
});

test("What's New lists recent changes with readable titles", async ({ nav }) => {
  await nav.open('wsp://home');
  await nav.main
    .getByRole('navigation', { name: 'Quick links' })
    .getByRole('link', { name: "What's New!" })
    .click();
  await nav.settled();
  await expect(nav.heading).toHaveText("What's New!");
  const items = nav.main.getByRole('listitem');
  expect(await items.count()).toBeGreaterThan(0);
  for (const text of (await items.allInnerTexts()).slice(0, 30)) {
    expect(text).not.toMatch(/^\s*wsp:/);
  }
});

test('the System Monitor loads and refreshes without errors', async ({ page, nav, target }) => {
  // E2E-2 (docs/testing/e2e-findings.md): live, GET /v1/system takes 30-120 s,
  // so the page does not finish loading. Remove test.fail() once it is fast.
  test.fail(target === 'live', 'E2E-2');
  await nav.open('wsp://home');
  await page
    .getByRole('menubar', { name: 'Main menu' })
    .getByRole('menuitem', { name: 'Go' })
    .click();
  await page
    .getByRole('menu', { name: 'Go' })
    .getByRole('menuitem', { name: 'System Monitor' })
    .click();
  await nav.settled();
  await expect(nav.heading).toHaveText('System Monitor');
  for (const box of ['Library', 'Freshness']) {
    await expect(nav.main.getByRole('region', { name: box })).toBeVisible();
  }
  await expect(nav.main.getByRole('region', { name: 'Library' })).toContainText(/API version/);
  await expect(nav.main).not.toContainText(/Unable to locate the server|Error \d{3}/);
  // It refreshes every 5 s; wait through one refresh (the guards catch any error).
  const refresh = page.waitForResponse((r) => r.url().includes('/v1/system'), { timeout: 15_000 });
  await refresh;
  await expect(nav.heading).toHaveText('System Monitor');
});
