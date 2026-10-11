/**
 * Drives the Navigator's chrome like a person: the location bar, the toolbar,
 * the menu bar, the status bar. Locators are by role and label, so they read
 * the page the way a screen reader (and a person) does.
 */
import { expect, type Locator, type Page, test } from '@playwright/test';

export type Target = 'mock' | 'live';

export class Navigator {
  constructor(
    readonly page: Page,
    readonly target: Target,
  ) {}

  get isLive(): boolean {
    return this.target === 'live';
  }

  /** The location bar. Its label reads Netsite:, Location: or Go to:. */
  get location(): Locator {
    return this.page.getByRole('combobox', { name: /^(Netsite|Location|Go to):/ });
  }

  /** The page viewport. */
  get main(): Locator {
    return this.page.getByRole('main', { name: 'Page' });
  }

  get heading(): Locator {
    return this.main.getByRole('heading', { level: 1 });
  }

  /** The text cell of the status bar at the bottom of the window. */
  get status(): Locator {
    return this.page.locator('.statusbar .status-text');
  }

  get toolbar(): Locator {
    return this.page.getByRole('toolbar', { name: 'Navigation' });
  }

  button(name: string | RegExp): Locator {
    return this.toolbar.getByRole('button', { name });
  }

  /** Open a page by address, as if typed into a fresh window. */
  async open(address: string): Promise<void> {
    await this.page.goto(`/#${address}`);
    await this.settled();
  }

  /** Wait until the throbber stops and the page has its heading. */
  async settled(): Promise<void> {
    await expect(this.page.locator('#app')).not.toHaveAttribute('aria-busy', 'true');
    await expect(this.main.locator('h1').first()).toBeAttached();
  }

  /** Ctrl+L, type, Enter: what a person does with the location bar. */
  async go(text: string): Promise<void> {
    await this.page.keyboard.press('Control+L');
    await expect(this.location).toBeFocused();
    await this.page.keyboard.type(text, { delay: 15 });
    await this.page.keyboard.press('Enter');
    await this.settled();
  }

  /** Current wsp:// address, from the URL fragment. */
  async address(): Promise<string> {
    return decodeURIComponent(new URL(this.page.url()).hash.slice(1));
  }

  async expectAddress(re: RegExp): Promise<void> {
    await expect.poll(() => this.address()).toMatch(re);
  }

  /** Pick a menu item with the mouse: View > High Contrast. */
  async menu(menu: string, item: string | RegExp): Promise<void> {
    await this.page
      .getByRole('menubar', { name: 'Main menu' })
      .getByRole('menuitem', { name: menu })
      .click();
    const popup = this.page.getByRole('menu', { name: menu });
    await expect(popup).toBeVisible();
    // Items are menuitem, menuitemradio (themes) or menuitemcheckbox (toggles).
    await popup.locator('[role^="menuitem"]', { hasText: item }).first().click();
    await expect(popup).toBeHidden();
  }

  /**
   * A named step that saves a full-page screenshot when it fails, so the
   * report shows what a person would have seen at that moment.
   */
  async step<T>(name: string, fn: () => Promise<T>): Promise<T> {
    return test.step(name, async () => {
      try {
        return await fn();
      } catch (err) {
        if (!this.page.isClosed()) {
          const info = test.info();
          const slug = name.replace(/[^a-z0-9]+/gi, '-').toLowerCase();
          const body = await this.page.screenshot({ fullPage: true }).catch(() => null);
          if (body)
            await info.attach(`failed-step-${slug}.png`, { body, contentType: 'image/png' });
        }
        throw err;
      }
    });
  }

  /**
   * Press Tab (like a keyboard user) until `target` has focus. Returns the
   * number of presses; fails after `max`.
   */
  async tabTo(target: Locator, max = 40): Promise<number> {
    for (let i = 1; i <= max; i += 1) {
      await this.page.keyboard.press('Tab');
      if (await target.evaluate((el) => el === document.activeElement).catch(() => false)) return i;
    }
    throw new Error(`Tab never reached ${target} in ${max} presses`);
  }

  /** Screenshot of the window, kept in the report (not only on failure). */
  async snapshot(name: string): Promise<void> {
    const body = await this.page.screenshot();
    await test.info().attach(`${name}.png`, { body, contentType: 'image/png' });
  }
}
