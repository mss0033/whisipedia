/**
 * Shared Playwright fixtures for the Navigator journeys.
 *
 * Every test gets three guards for free (auto fixtures):
 *  - Offline guarantee: any request that leaves the app's origin fails the
 *    test, and is aborted so nothing actually goes out (PLAN §10.5).
 *  - CSP: Content-Security-Policy violations fail the test (PLAN §10.1).
 *  - Clean console: console errors and uncaught exceptions fail the test.
 *    A test that expects an error (a deliberate 404) lists it in `allowConsole`.
 *
 * And a `nav` helper that drives the browser chrome the way a person does.
 */
import { test as base, expect, type Page } from '@playwright/test';
import { DATA, type TargetData } from './support/data.ts';
import { Navigator, type Target } from './support/navigator.ts';

export type { Target };

export interface Fixtures {
  /** Which library the suite runs against. */
  target: Target;
  /** Console error texts this test expects (regexes). */
  allowConsole: RegExp[];
  /** KNOWN_ISSUES ids the guards tolerate. Pinning tests set this to []. */
  tolerate: string[];
  guards: undefined;
  nav: Navigator;
  /** Queries and pages for the current target. */
  data: TargetData;
}

/**
 * Known app bugs the guards tolerate so every other journey can still run.
 * Each has a dedicated `test.fail()` test that pins it; when the bug is fixed
 * that test starts passing "unexpectedly" and fails the suite, which is the
 * cue to delete the entry here. See docs/testing/e2e-findings.md.
 */
export const KNOWN_ISSUES: Array<{ id: string; console: RegExp }> = [
  {
    // E2E-1: parsing document HTML with DOMParser reports a CSP violation for
    // every inline style attribute, although the sanitizer drops them.
    id: 'E2E-1',
    console: /Refused to apply inline style|CSP violation: style-src-(attr|elem) blocked inline/,
  },
  {
    // E2E-4: in multi-book ZIMs (Stack Exchange) the API resolves a book front
    // page's relative images against the source root, dropping the book: 404s.
    id: 'E2E-4',
    console: /status of 404 .*\/v1\/assets\/stackexchange\/[^/]+\/(images|static)\//,
  },
];

export const test = base.extend<Fixtures>({
  target: [process.env.WSP_E2E_URL ? 'live' : 'mock', { option: true }],
  allowConsole: [[], { option: true }],
  tolerate: [KNOWN_ISSUES.map((k) => k.id), { option: true }],
  guards: [
    async ({ context, baseURL, allowConsole, tolerate }, use) => {
      const problems: string[] = [];
      const origin = new URL(baseURL!).origin;
      const local = (url: string) => {
        if (/^(data|blob|about):/.test(url)) return true;
        try {
          return new URL(url).origin === origin;
        } catch {
          return false;
        }
      };

      await context.route(
        (url) => !local(url.href),
        (route) => {
          problems.push(`off-origin request: ${route.request().method()} ${route.request().url()}`);
          return route.abort('blockedbyclient');
        },
      );

      // CSP violations surface as console errors with a recognisable prefix.
      await context.addInitScript(() => {
        document.addEventListener('securitypolicyviolation', (e) => {
          console.error(
            `CSP violation: ${e.violatedDirective} blocked ${e.blockedURI || '(inline)'} in ${e.sourceFile}:${e.lineNumber}`,
          );
        });
      });

      const watch = (page: Page) => {
        page.on('console', (msg) => {
          if (msg.type() !== 'error') return;
          const text = msg.text();
          const where = msg.location().url;
          if (allowConsole.some((re) => re.test(text) || re.test(where))) return;
          const line = `${text} (${where})`;
          if (KNOWN_ISSUES.some((k) => tolerate.includes(k.id) && k.console.test(line))) return;
          problems.push(`console error: ${text}${where ? ` (${where})` : ''}`);
        });
        page.on('pageerror', (err) => {
          problems.push(`uncaught exception: ${err.message}`);
        });
      };
      context.on('page', watch);
      for (const p of context.pages()) watch(p);

      await use(undefined);

      expect(problems, 'offline / CSP / console guard').toEqual([]);
    },
    { auto: true },
  ],

  nav: async ({ page, target }, use) => {
    await use(new Navigator(page, target));
  },
  data: async ({ target }, use) => {
    await use(DATA[target]);
  },
});

export { expect };
