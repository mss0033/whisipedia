/**
 * Reading books the way a person does (2026-10-16 field exam: chapters stopped mid-sentence, a long
 * book was one endless page, and "the illustrated edition" opened another book's author page). Earlier
 * journeys never opened a book: the reader and its links were covered by unit tests on toy texts only.
 *
 * Mock: Frankenstein (5 chapters, an illustrated copy) and a long book with no chapters. Live: Alice
 * read cover to cover, the King James Bible by pages, the illustrated editions of four books, and the
 * Gutenberg cover page's EPUB and author links.
 */
import type { Locator } from '@playwright/test';
import { expect, test } from './fixtures.ts';

const pick = <T>(target: string, mock: T, live: T) => (target === 'live' ? live : mock);

/** The words of a title worth matching: "Moby Dick; Or, The Whale" -> ["moby", "dick", "whale"]. */
const words = (t: string) =>
  t
    .toLowerCase()
    .replace(/[’']/g, '')
    .split(/[^a-z]+/)
    .filter((w) => w.length > 2 && !['the', 'and', 'or'].includes(w));

async function visibleInViewport(el: Locator): Promise<boolean> {
  return el.evaluate((node) => {
    const vp = document.querySelector('main')!.getBoundingClientRect();
    const r = node.getBoundingClientRect();
    return r.top >= vp.top - 2 && r.top < vp.bottom;
  });
}

test('a book is read from its first chapter to its end by Next, with nothing cut off', async ({
  nav,
  target,
}) => {
  const book = pick(target, 'wsp://gutenberg/84', 'wsp://gutenberg/11');
  await nav.open(`${book}?ch=0`);
  const toc = nav.main.locator('.reader-toc li');
  const chapters = await toc.count();
  expect(chapters, 'a table of contents').toBeGreaterThan(3);
  const seen: string[] = [];
  // A title page before the first chapter is the front matter, not "Chapter 1" (2026-10-17: Pride
  // and Prejudice's CHAPTER I read "Chapter 2 of 62").
  let front = false;
  for (let i = 0; i < chapters + 2; i++) {
    const where = await nav.main.locator('.reader-where').innerText();
    if (i === 0 && where.includes('Front matter')) front = true;
    else if (front) expect(where).toContain(`Chapter ${i} of ${chapters - 1}`);
    else expect(where).toContain(`Chapter ${i + 1} of ${chapters}`);
    const title = await nav.main.locator('.reader-chapter h2').innerText();
    const paras = nav.main.locator('.reader-chapter p');
    expect(await paras.count(), `${title}: has text`).toBeGreaterThan(0);
    if (target === 'live' && i > 0) {
      // a chapter ends where a sentence does, not halfway through one
      const last = (await paras.last().innerText()).trim();
      // …or with a closing line in capitals ("THE END", "FINIS")
      expect(last, `${title}: ends a sentence`).toMatch(/[.!?”"’'*)\]_—-]$|^[A-Z][A-Z .]+$/);
    }
    seen.push(title);
    const next = nav.main.getByRole('link', { name: 'Next chapter →' });
    if ((await next.count()) === 0) break;
    await next.click();
    await nav.settled();
  }
  expect(seen).toHaveLength(chapters);
  await expect(nav.main.locator('.reader-nav')).toContainText('The end');
  expect(new Set(seen).size, 'every chapter a different one').toBe(chapters);
});

test('a long book with no chapters is read a page at a time', async ({ nav, target }) => {
  const book = pick(target, 'wsp://gutenberg/9999', 'wsp://gutenberg/10');
  await nav.open(`${book}?ch=0`);
  const where = nav.main.locator('.reader-where');
  await expect(where).toHaveText(/^Page 1 of (\d+)$/);
  const pages = Number(/of (\d+)/.exec(await where.innerText())![1]);
  expect(pages).toBeGreaterThan(1);
  await expect(nav.main.locator('.reader-toc summary')).toHaveText(`Contents (${pages} pages)`);
  await expect(nav.main.locator('.reader-nav')).toHaveText('First page · Next page →');
  const first = await nav.main.locator('.reader-chapter p').last().innerText();

  await nav.step('Next page goes on where the page stopped', async () => {
    await nav.main.getByRole('link', { name: 'Next page →' }).click();
    await nav.settled();
    await expect(where).toHaveText(`Page 2 of ${pages}`);
    await expect(nav.main.locator('.reader-chapter h2')).toHaveText(/\(continued\)$/);
    expect(await nav.main.locator('.reader-chapter p').first().innerText()).not.toBe(first);
  });

  await nav.step('the last page is the end of the book', async () => {
    await nav.open(`${book}?ch=${pages - 1}`);
    await expect(where).toHaveText(`Page ${pages} of ${pages}`);
    await expect(nav.main.locator('.reader-nav')).toContainText('The end');
  });
});

test('"the illustrated edition" opens the same book, and its contents links work', async ({
  nav,
  page,
  target,
}) => {
  const books = pick(
    target,
    ['wsp://gutenberg/84'],
    ['wsp://gutenberg/11', 'wsp://gutenberg/84', 'wsp://gutenberg/2701', 'wsp://gutenberg/1342'],
  );
  for (const book of books) {
    await nav.step(`${book}: the illustrated edition is the same book`, async () => {
      await nav.open(book);
      const title = await nav.heading.innerText();
      const link = nav.main.locator('.copies-notice a', { hasText: 'the illustrated edition' });
      await expect(link).toBeVisible();
      await link.click();
      await nav.settled();
      await nav.expectAddress(/^wsp:\/\/gutenberg-zim\//);
      // the window names the page by its document title (a book's heading may be split in two)
      const opened = (await page.title()).toLowerCase().replace(/[’']/g, '');
      const want = words(title).slice(0, 2);
      for (const w of want) expect(opened, `"${title}" opened "${opened}"`).toContain(w);
      expect(await nav.main.locator('.doc-body').innerText()).not.toMatch(/^\s*$/);
    });
  }

  await nav.step('a contents link scrolls to its chapter in the page', async () => {
    const before = await nav.address();
    const anchor = nav.main.locator('.doc-body a[data-anchor]').nth(1);
    const id = await anchor.getAttribute('data-anchor');
    await anchor.click();
    await expect.poll(() => visibleInViewport(page.locator(`[id="${id}"]`))).toBe(true);
    expect(await nav.address()).toBe(before);
  });
});

test('the Gutenberg cover page: the EPUB is saved and the author is a search', async ({
  nav,
  page,
  target,
}) => {
  test.skip(target !== 'live', 'needs the Gutenberg ZIM (npm run e2e:live)');
  await nav.open("wsp://gutenberg-zim/Alice's%20Adventures%20in%20Wonderland_cover.11");

  await nav.step('the EPUB link is visible and downloads the book', async () => {
    const epub = nav.main.getByRole('link', { name: /EPUB$/ });
    await expect(epub).toBeVisible();
    const download = page.waitForEvent('download');
    await epub.click();
    const file = await download;
    expect(file.suggestedFilename()).toBe("Alice's Adventures in Wonderland.11.epub");
    expect(await file.failure()).toBeNull();
  });

  await nav.step('the author link lists the author’s books', async () => {
    await nav.main.getByRole('link', { name: 'Lewis Carroll' }).click();
    await nav.settled();
    await nav.expectAddress(/^wsp:\/\/search\?q=Lewis\+Carroll&source=gutenberg-zim$/);
    await expect(nav.main.locator('.result, .search-result, li').first()).toBeVisible();
  });
});
