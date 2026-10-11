/** The Start Page (wsp://home): what a person sees when the Navigator opens. */
import { expect, test } from './fixtures.ts';

test('the start page shows the portal: search box, sites, what is new, stats', async ({
  page,
  nav,
}) => {
  await nav.step('open the Navigator', async () => {
    await page.goto('/');
    await nav.settled();
    await expect(page).toHaveTitle('Whisipedia Start Page - Whisipedia Navigator');
    await expect(nav.heading).toHaveText(/Whisipedia\s*Start Page/);
    await expect(nav.location).toHaveValue('wsp://home');
    await expect(page.getByText('Netsite:', { exact: true })).toBeVisible();
  });

  await nav.step('the WhizBang! search box is there', async () => {
    await expect(nav.main.getByRole('img', { name: 'WhizBang!' })).toBeVisible();
    const box = nav.main.getByRole('combobox', { name: 'WhizBang! the library:' });
    await expect(box).toBeVisible();
    await expect(nav.main.getByRole('button', { name: "I'm Feeling Lucky" })).toBeVisible();
  });

  await nav.step('every site is listed, grouped by kind', async () => {
    const sites = nav.main.getByRole('region', { name: 'Sites in the Library' });
    await expect(sites).toBeVisible();
    await expect(sites.getByRole('heading', { level: 3 }).first()).toBeVisible();
    const links = sites.getByRole('link');
    expect(await links.count()).toBeGreaterThan(3);
    for (const text of await links.allInnerTexts()) expect(text.trim()).not.toBe('');
  });

  await nav.step("What's New! items have readable titles", async () => {
    const box = nav.main.getByRole('region', { name: "What's New!" });
    await expect(box).toBeVisible();
    const items = box.getByRole('listitem');
    expect(await items.count()).toBeGreaterThan(0);
    for (const text of await items.allInnerTexts()) {
      // A title a person can read: not a raw doc id, a wsp:// address or a file name.
      expect(text, 'What’s New item').not.toMatch(/^\s*wsp:/);
      expect(text).not.toMatch(/\.(html?|zim|json)\b/i);
      expect(text.replace(/NEW!|updated|removed/g, '').trim().length).toBeGreaterThan(1);
    }
    await expect(box.getByRole('link', { name: 'More new stuff…' })).toBeVisible();
  });

  await nav.step('Library Stats counts documents and sites', async () => {
    const stats = nav.main.getByRole('region', { name: 'Library Stats' });
    await expect(stats).toContainText(/[\d,]+ documents in \d+ sites\./);
  });

  await nav.step('the chrome is complete', async () => {
    for (const name of ['Back', 'Forward', 'Home', 'Reload', 'Search', 'Hotlist']) {
      await expect(nav.button(new RegExp(`^${name}\\b`))).toBeVisible();
    }
    await expect(nav.button(/^Back\b/)).toBeDisabled();
    await expect(page.getByRole('menubar', { name: 'Main menu' })).toBeVisible();
    await expect(page.locator('.statusbar')).toContainText('Offline');
    await expect(nav.status).toHaveText(/Document: Done/);
  });
});
