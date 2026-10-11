/** Keyboard only: a person who never touches the mouse can do everything. */
import { expect, test } from './fixtures.ts';

test('Tab reaches the skip link, the location bar, the search box and the results', async ({
  page,
  nav,
  data,
}) => {
  await nav.open('wsp://home');
  await page.locator('body').focus();

  await nav.step('the first Tab shows the skip link', async () => {
    await page.keyboard.press('Tab');
    const skip = page.getByRole('link', { name: 'Skip to page content' });
    await expect(skip).toBeFocused();
    await expect(skip).toBeInViewport();
  });

  await nav.step('Tab reaches the location bar through the chrome', async () => {
    const presses = await nav.tabTo(nav.location, 25);
    expect(presses).toBeLessThan(25);
  });

  const box = nav.main.getByRole('combobox', { name: 'WhizBang! the library:' });
  await nav.step('Tab reaches the WhizBang! box, type and Enter searches', async () => {
    await nav.tabTo(box, 40);
    await page.keyboard.type(data.question);
    await page.keyboard.press('Enter');
    await nav.settled();
    await nav.expectAddress(/^wsp:\/\/search\?q=/);
  });

  await nav.step('Tab from the search box reaches the first result, Enter opens it', async () => {
    const results = nav.main.getByRole('list', { name: 'Search results' });
    const first = results.getByRole('listitem').first().getByRole('link').first();
    const title = (await first.innerText()).trim();
    await nav.main.getByRole('combobox', { name: 'Search for:' }).first().focus();
    await nav.tabTo(first, 40);
    await expect(first).toBeFocused();
    // A visible focus ring in this theme.
    const ring = await first.evaluate((a) => {
      const s = getComputedStyle(a);
      return s.outlineStyle !== 'none' && s.outlineWidth !== '0px';
    });
    expect(ring, 'focused link shows a focus ring').toBe(true);
    await page.keyboard.press('Enter');
    await nav.settled();
    await expect(nav.heading).toContainText(title.slice(0, 20));
  });

  await nav.step('Alt+Left goes back to the results', async () => {
    await page.keyboard.press('Alt+ArrowLeft');
    await nav.settled();
    await expect(nav.main.getByRole('list', { name: 'Search results' })).toBeVisible();
  });
});

test('the skip link moves focus to the page', async ({ page, nav }) => {
  await nav.open('wsp://home');
  await page.locator('body').focus();
  await page.keyboard.press('Tab');
  await page.keyboard.press('Enter');
  await expect(nav.main).toBeFocused();
});

test('F10 opens the menus; arrows, Enter and Escape work them', async ({ page, nav }) => {
  await nav.open('wsp://home');
  await page.keyboard.press('F10');
  const bar = page.getByRole('menubar', { name: 'Main menu' });
  await expect(bar.getByRole('menuitem', { name: 'File' })).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  await expect(bar.getByRole('menuitem', { name: 'Go' })).toBeFocused();
  await page.keyboard.press('Enter');
  const go = page.getByRole('menu', { name: 'Go' });
  await expect(go).toBeVisible();
  // Typeahead: "D" jumps to Directory.
  await page.keyboard.press('d');
  await expect(go.getByRole('menuitem', { name: 'Directory' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(go).toBeHidden();
  await expect(bar.getByRole('menuitem', { name: 'Go' })).toBeFocused();
  await page.keyboard.press('Enter');
  await page.keyboard.press('d');
  await page.keyboard.press('Enter');
  await nav.settled();
  await expect(nav.heading).toHaveText('WhisiDir!');
});

test('"/" jumps to the search box from anywhere on a page', async ({ page, nav }) => {
  await nav.open('wsp://home');
  await nav.main.focus();
  await page.keyboard.press('/');
  await expect(nav.main.getByRole('combobox', { name: 'WhizBang! the library:' })).toBeFocused();
});
