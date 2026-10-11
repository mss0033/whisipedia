/**
 * The guards guard themselves: each test here breaks a rule on purpose and is
 * marked test.fail(), so the suite goes red if a guard ever stops catching it.
 */
import { expect, test } from './fixtures.ts';

test('the offline guard fails a test that reaches off the computer', async ({ page, nav }) => {
  test.fail();
  await nav.open('wsp://about');
  // The CSP should already block this; the guard must notice either way.
  await page.evaluate(() => fetch('https://example.invalid/beacon').catch(() => undefined));
  await expect(nav.heading).toBeVisible();
});

test('the console guard fails a test with an uncaught exception', async ({ page, nav }) => {
  test.fail();
  await nav.open('wsp://about');
  await page.evaluate(() => {
    setTimeout(() => {
      throw new Error('deliberate e2e guard check');
    });
  });
  await page.waitForTimeout(100);
});
