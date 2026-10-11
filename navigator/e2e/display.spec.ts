/** Look and feel: themes, high contrast, text size and motion (PLAN §10.4). */
import { expect, test } from './fixtures.ts';

const THEMES = [
  { id: 'navigator96', name: "Navigator '96" },
  { id: 'mosaic93', name: "Mosaic '93" },
  { id: 'encarta95', name: "Encarta '95" },
  { id: 'phosphor-amber', name: 'Phosphor (Amber)' },
  { id: 'phosphor-green', name: 'Phosphor (Green)' },
  { id: 'system7', name: 'System 7' },
];

test('every theme can be picked from the View menu (screenshot each)', async ({ page, nav }) => {
  await nav.open('wsp://home');
  const html = page.locator('html');
  const looks = new Set<string>();
  // Start from a non-default theme so picking the default is a real change.
  for (const theme of [...THEMES.slice(1), THEMES[0]!]) {
    await nav.step(`View › ${theme.name}`, async () => {
      await nav.menu('View', theme.name);
      await expect(html).toHaveAttribute('data-theme', theme.id);
      const look = await page.evaluate(() => {
        const s = getComputedStyle(document.querySelector('.window') ?? document.body);
        const m = getComputedStyle(document.querySelector('main')!);
        return `${s.backgroundColor}|${m.backgroundColor}|${m.color}|${m.fontFamily}`;
      });
      looks.add(look);
      await nav.snapshot(`theme-${theme.id}`);
      // The menu shows the current theme as the checked radio item.
      await page
        .getByRole('menubar', { name: 'Main menu' })
        .getByRole('menuitem', { name: 'View' })
        .click();
      await expect(
        page.getByRole('menu', { name: 'View' }).getByRole('menuitemradio', { name: theme.name }),
      ).toHaveAttribute('aria-checked', 'true');
      await page.keyboard.press('Escape');
    });
  }
  expect(looks.size, 'each theme looks different').toBe(THEMES.length);

  await nav.step('the choice survives a reload', async () => {
    await nav.menu('View', 'System 7');
    await page.reload();
    await nav.settled();
    await expect(html).toHaveAttribute('data-theme', 'system7');
  });
});

test('High Contrast toggles on and off over a theme', async ({ page, nav }) => {
  await nav.open('wsp://home');
  const html = page.locator('html');
  await expect(html).toHaveAttribute('data-contrast', 'normal');
  const before = await nav.main.evaluate(
    (m) => getComputedStyle(m).color + getComputedStyle(m).backgroundColor,
  );
  await nav.menu('View', 'High Contrast');
  await expect(html).toHaveAttribute('data-contrast', 'high');
  const after = await nav.main.evaluate(
    (m) => getComputedStyle(m).color + getComputedStyle(m).backgroundColor,
  );
  expect(after).not.toBe(before);
  await nav.snapshot('high-contrast');
  await nav.menu('View', 'High Contrast');
  await expect(html).toHaveAttribute('data-contrast', 'normal');
});

test('text size grows with Ctrl+] and the menu, and resets with Ctrl+0', async ({ page, nav }) => {
  await nav.open('wsp://home');
  const size = () =>
    nav.main
      .locator('p')
      .first()
      .evaluate((p) => Number.parseFloat(getComputedStyle(p).fontSize));
  const base = await size();
  await nav.main.focus();
  await page.keyboard.press('Control+BracketRight');
  await expect.poll(size).toBeGreaterThan(base);
  const bigger = await size();
  await nav.menu('View', 'Larger Text');
  await expect.poll(size).toBeGreaterThan(bigger);
  await nav.snapshot('larger-text');
  await nav.menu('View', 'Smaller Text');
  await expect.poll(size).toBe(bigger);
  await nav.main.focus();
  await page.keyboard.press('Control+0');
  await expect.poll(size).toBe(base);
});

test.describe('motion', () => {
  /** The blinking NEW! badge on the Start Page is the canary. */
  const blink = (page: import('@playwright/test').Page) =>
    page
      .locator('.new-badge')
      .first()
      .evaluate((el) => getComputedStyle(el).animationName);

  test('animates by default', async ({ page, nav }) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await nav.open('wsp://home');
    expect(await blink(page)).not.toBe('none');
  });

  test('honours the OS reduced-motion setting', async ({ page, nav }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await nav.open('wsp://home');
    await expect(page.locator('html')).toHaveAttribute('data-motion', 'system');
    expect(await blink(page)).toBe('none');
  });

  test('Options › Reduce Motion stops animation, Full Animation overrides the OS', async ({
    page,
    nav,
  }) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await nav.open('wsp://home');
    await nav.menu('Options', 'Reduce Motion');
    await expect(page.locator('html')).toHaveAttribute('data-motion', 'reduce');
    expect(await blink(page)).toBe('none');
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await nav.menu('Options', 'Full Animation');
    await expect(page.locator('html')).toHaveAttribute('data-motion', 'full');
    expect(await blink(page)).not.toBe('none');
  });
});
