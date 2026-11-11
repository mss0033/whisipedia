/**
 * Reading in every palette and at every width (trawl 2026-11-10, batch F2): formula images are
 * visible on dark pages and print as they are, a right-to-left page runs right to left, and a phone
 * never scrolls the page sideways. Mock target only: the pages are patched into the mock's answers.
 */
import type { Page } from '@playwright/test';
import { expect, test } from './fixtures.ts';

test.skip(({ target }) => target === 'live', 'patched pages: mock only');

const ARTICLE = 'wsp://wikipedia-en-zim/Alan_Turing';
const SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="30"><text x="2" y="22" font-size="20">E = mc²</text></svg>';
const HEBREW = 'בראשית ברא אלהים את השמים ואת הארץ. והארץ היתה תהו ובהו וחשך על פני תהום.';

async function patchArticle(page: Page, html: string, more: Record<string, unknown> = {}) {
  await page.route('**/v1/documents/**Alan_Turing**', async (route) => {
    if (/\/history(\?|$)/.test(route.request().url())) return route.fallback();
    const res = await route.fetch();
    const doc = (await res.json()) as Record<string, unknown>;
    await route.fulfill({ response: res, json: { ...doc, html, section: null, ...more } });
  });
  await page.route('**/v1/assets/wikipedia-en-zim/1/*.svg', (route) =>
    route.fulfill({ contentType: 'image/svg+xml', body: SVG }),
  );
}

const FORMULA = `<p>Mass and energy: <span class="mwe-math-element"><span class="mwe-math-mathml-inline mwe-math-mathml-a11y"><math xmlns="http://www.w3.org/1998/Math/MathML"><semantics><mrow><mi>E</mi><mo>=</mo><mi>m</mi><msup><mi>c</mi><mn>2</mn></msup></mrow><annotation encoding="application/x-tex">E=mc^2</annotation></semantics></math></span><img src="/v1/assets/wikipedia-en-zim/1/formula.svg" class="mwe-math-fallback-image-inline mw-invert skin-invert" alt="E=mc^2" width="120" height="30"></span></p>
<figure class="skin-invert-image"><img src="/v1/assets/wikipedia-en-zim/1/diagram.svg" alt="A line diagram" width="120" height="30"></figure>
<p><img src="/v1/assets/wikipedia-en-zim/1/photo.svg" alt="A photograph" width="120" height="30"></p>`;

const filterOf = (page: Page, alt: string) =>
  page.evaluate(
    (a) => getComputedStyle(document.querySelector(`main img[alt="${a}"]`)!).filter,
    alt,
  );

const LOOKS: Array<{
  name: string;
  theme: string;
  scheme?: 'dark';
  contrast?: boolean;
  dark: boolean;
}> = [
  { name: "Navigator '96", theme: "Navigator '96", dark: false },
  { name: "Mosaic '93", theme: "Mosaic '93", dark: false },
  { name: 'System 7', theme: 'System 7', dark: false },
  { name: "Encarta '95", theme: "Encarta '95", dark: true },
  { name: 'Phosphor (Amber)', theme: 'Phosphor (Amber)', dark: true },
  { name: 'Phosphor (Green)', theme: 'Phosphor (Green)', dark: true },
  { name: "Navigator '96, dark", theme: "Navigator '96", scheme: 'dark', dark: true },
  { name: "Mosaic '93, dark", theme: "Mosaic '93", scheme: 'dark', dark: true },
  { name: 'System 7, dark', theme: 'System 7', scheme: 'dark', dark: true },
  { name: 'High Contrast', theme: "Navigator '96", contrast: true, dark: true },
];

for (const look of LOOKS) {
  test(`formula images in ${look.name}: ${look.dark ? 'drawn in the page’s ink' : 'as they are'}`, async ({
    page,
    nav,
  }) => {
    await page.emulateMedia({ colorScheme: look.scheme ?? 'light' });
    await patchArticle(page, FORMULA);
    await nav.open(ARTICLE);
    await nav.menu('View', look.theme);
    if (look.contrast) await nav.menu('View', /High Contrast/);
    const html = page.locator('html');
    if (look.scheme) await expect(html).toHaveAttribute('data-dark', 'on');
    if (look.contrast) await expect(html).toHaveAttribute('data-contrast', 'high');

    const formula = nav.main.getByRole('img', { name: 'E=mc^2' });
    await expect(formula).toBeVisible();
    await expect(formula).toHaveAttribute('data-wsp-role', 'ink');
    const inverted = 'invert(1) hue-rotate(180deg)';
    expect(await filterOf(page, 'E=mc^2')).toBe(look.dark ? inverted : 'none');
    // Marked on a wrapper, as Wikipedia mostly does.
    expect(await filterOf(page, 'A line diagram')).toBe(look.dark ? inverted : 'none');
    // A photograph is never inverted.
    expect(await filterOf(page, 'A photograph')).toBe('none');
    // The hidden MathML copy is not shown beside its image.
    await expect(nav.main.locator('.doc-body math')).toHaveCount(0);
    await nav.snapshot(`formula-${look.name.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}`);

    await nav.step('on paper nothing is inverted', async () => {
      await page.emulateMedia({ media: 'print', colorScheme: look.scheme ?? 'light' });
      expect(await filterOf(page, 'E=mc^2')).toBe('none');
      expect(await filterOf(page, 'A line diagram')).toBe('none');
    });
  });
}

test('a right-to-left page runs right to left: by its language, or by its own dir', async ({
  page,
  nav,
}) => {
  const body = `<h1>בראשית</h1><p>${HEBREW}</p><pre>ls -la /usr/share</pre><p>קוד: <code>x = 1</code></p>`;
  await patchArticle(page, body, { lang: 'he' });
  await nav.open(ARTICLE);
  const direction = (sel: string) =>
    page.evaluate((s) => getComputedStyle(document.querySelector(s)!).direction, sel);
  expect(await direction('main .doc-body')).toBe('rtl');
  expect(await direction('main .doc-body p')).toBe('rtl');
  // Code still runs left to right, from the left.
  expect(await direction('main .doc-body pre')).toBe('ltr');
  expect(await direction('main .doc-body code')).toBe('ltr');
  // The Navigator's own words around the page do not turn.
  expect(await direction('main .doc-footer')).toBe('ltr');
  expect(await direction('.statusbar')).toBe('ltr');

  await nav.step('a page tagged with its source’s default language says so itself', async () => {
    await page.unroute('**/v1/documents/**Alan_Turing**');
    await patchArticle(page, `<html dir="rtl"><body>${body}</body></html>`, { lang: 'en' });
    await nav.button('Reload').click();
    await nav.settled();
    expect(await direction('main .doc-body')).toBe('rtl');
  });

  await nav.step('and no page is forced left to right by its tag', async () => {
    await page.unroute('**/v1/documents/**Alan_Turing**');
    await patchArticle(page, body, { lang: 'en' });
    await nav.button('Reload').click();
    await nav.settled();
    await expect(nav.main.locator('.doc-body')).not.toHaveAttribute('dir', /.*/);
    await expect(nav.main.locator('.doc-body [dir="ltr"]')).toHaveCount(0);
  });
});

test.describe('at phone width', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  const WIDE = `<h1>Wide things</h1><p>Lead paragraph.</p>
<table><caption>Longest rivers</caption><tr>${'<th>Column heading</th>'.repeat(14)}</tr><tr>${'<td>1234567890</td>'.repeat(14)}</tr></table>
<p>Between.</p><table><tr><td>small</td></tr></table>
<pre>${'a_long_line_of_code_with_no_break_in_it;'.repeat(6)}</pre>
<p>End.</p>`;

  test('a wide table, a long line of code and a long address do not push the page sideways', async ({
    page,
    nav,
  }) => {
    await patchArticle(page, WIDE, {
      url_native: `https://en.wikipedia.org/wiki/${'A_very_long_page_name_'.repeat(8)}`,
    });
    await nav.open(ARTICLE);
    const sideways = () =>
      page.evaluate(() => {
        const main = document.querySelector('main')!;
        return {
          main: main.scrollWidth - main.clientWidth,
          doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        };
      });
    expect(await sideways()).toEqual({ main: 0, doc: 0 });

    const boxes = nav.main.locator('[data-wsp-role="table-scroll"]');
    await expect(boxes).toHaveCount(2);
    const wide = nav.main.getByRole('region', { name: 'Table: Longest rivers' });
    // The wide table scrolls in its own box, which is a named tab stop while it overflows.
    await expect(wide).toHaveAttribute('tabindex', '0');
    expect(await wide.evaluate((b) => b.scrollWidth > b.clientWidth)).toBe(true);
    await wide.focus();
    await page.keyboard.press('ArrowRight');
    await expect.poll(() => wide.evaluate((b) => b.scrollLeft)).toBeGreaterThan(0);
    // The table that fits adds no tab stop and no region.
    await expect(boxes.nth(1)).not.toHaveAttribute('tabindex', /.*/);
    await expect(nav.main.getByRole('region')).toHaveCount(1);
  });
});
