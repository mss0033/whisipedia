/**
 * End-to-end tests: drive the Navigator in a real Chromium the way a person
 * does (keyboard, mouse, what is on screen). See docs/testing/e2e.md.
 *
 * Two targets, picked by the environment:
 *   mock (default)  `npm run e2e`: builds dist/ and serves it with `vite preview`,
 *                   the CSP header and the built-in mock API. Offline and deterministic.
 *   live            `npm run e2e:live` (WSP_E2E_URL=http://127.0.0.1:8737): the
 *                   running library. Read-only; slow first loads from the NAS.
 */
import { existsSync, readdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { chromium, defineConfig, devices } from '@playwright/test';

const LIVE_URL = process.env.WSP_E2E_URL?.replace(/\/+$/, '');
const MOCK_PORT = Number(process.env.WSP_E2E_PORT ?? 4179);
const MOCK_URL = `http://127.0.0.1:${MOCK_PORT}`;
const CI = !!process.env.CI;

/**
 * The Chromium to run. Order: WSP_E2E_CHROMIUM, then the build this Playwright
 * version expects (CI installs it with `npx playwright install chromium`), then
 * the newest Chromium already in the Playwright cache, so a developer machine
 * never has to download another browser.
 */
function chromiumExecutable(): string | undefined {
  if (process.env.WSP_E2E_CHROMIUM) return process.env.WSP_E2E_CHROMIUM;
  try {
    if (existsSync(chromium.executablePath())) return undefined;
  } catch {
    /* fall through to the cache */
  }
  const cache = process.env.PLAYWRIGHT_BROWSERS_PATH || join(homedir(), '.cache', 'ms-playwright');
  if (!existsSync(cache)) return undefined;
  const candidates = readdirSync(cache)
    .map((d) => /^chromium-(\d+)$/.exec(d))
    .filter((m): m is RegExpExecArray => m !== null)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .map((m) => join(cache, m[0], 'chrome-linux', 'chrome'))
    .filter((p) => existsSync(p));
  return candidates[0];
}

const executablePath = chromiumExecutable();

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/support/warm-up.ts',
  outputDir: './e2e-results',
  // Two workers at most: the owner's PC must stay usable (and the NAS too, for live).
  workers: CI ? 1 : 2,
  fullyParallel: true,
  forbidOnly: CI,
  retries: CI ? 2 : 1,
  timeout: LIVE_URL ? 120_000 : 30_000,
  expect: { timeout: LIVE_URL ? 30_000 : 5_000 },
  reporter: [['list'], ['html', { outputFolder: './e2e-report', open: 'never' }]],
  use: {
    baseURL: LIVE_URL ?? MOCK_URL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: LIVE_URL ? 30_000 : 5_000,
    navigationTimeout: LIVE_URL ? 45_000 : 10_000,
    locale: 'en-GB',
    timezoneId: 'UTC',
    // The app is offline by design: no service workers, nothing to cache across tests.
    serviceWorkers: 'block',
  },
  projects: [
    {
      name: LIVE_URL ? 'live' : 'mock',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1280, height: 900 },
        launchOptions: {
          ...(executablePath ? { executablePath } : {}),
          // WebGL in headless Chromium for the Atlas: software rendering (SwiftShader).
          args: [
            '--use-angle=swiftshader',
            '--enable-unsafe-swiftshader',
            '--ignore-gpu-blocklist',
          ],
        },
      },
    },
  ],
  ...(LIVE_URL
    ? {}
    : {
        webServer: {
          // The production bundle (CSP meta tag + header) with the mock API, not the dev server.
          command: `npm run build && npx vite preview --port ${MOCK_PORT} --strictPort`,
          url: `${MOCK_URL}/v1/sources`,
          reuseExistingServer: !CI,
          timeout: 120_000,
          stdout: 'ignore',
          stderr: 'pipe',
          env: { WSP_MOCK: '1' },
        },
      }),
});
