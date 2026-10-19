/** WhizBang!: searching from the location bar and the search page. */
import type { Locator } from '@playwright/test';
import { expect, test } from './fixtures.ts';

/** "Results 1 - 10 of about 37 for …" → { first, last, total }. */
async function resultsBar(main: Locator) {
  const bar = main.locator('.results-bar');
  await expect(bar).toContainText(/Results \d+ - \d+ of/);
  const text = (await bar.innerText()).replace(/,/g, '');
  const m = /Results (\d+) - (\d+) of (?:about|more than) (\d+)/.exec(text);
  expect(m, `results bar: ${text}`).not.toBeNull();
  return { first: Number(m![1]), last: Number(m![2]), total: Number(m![3]) };
}

test('a plain-language query in the location bar: results, filters, paging, back and forward', async ({
  page,
  nav,
  data,
}) => {
  await nav.open('wsp://home');
  const results = nav.main.getByRole('list', { name: 'Search results' });

  await nav.step('Ctrl+L, type a question, Enter', async () => {
    await nav.go(data.question);
    await nav.expectAddress(/^wsp:\/\/search\?q=how/);
    await expect(page).toHaveTitle(`WhizBang! Search: ${data.question} - Whisipedia Navigator`);
    await expect(nav.location).toHaveValue(/^wsp:\/\/search\?q=/);
  });

  await nav.step('ten blue links with clean, highlighted snippets', async () => {
    await expect(results.getByRole('listitem')).toHaveCount(10);
    const first = results.getByRole('listitem').first();
    await expect(first.getByRole('link').first()).toBeVisible();
    for (const snippet of await results.locator('.result-snippet').allInnerTexts()) {
      expect(snippet, 'snippet must not show raw HTML').not.toMatch(/<\/?[a-z][^>]*>/i);
      expect(snippet).not.toMatch(/&(?:lt|gt|amp|quot|nbsp);/);
    }
    // Query terms are highlighted (bold) in at least one snippet.
    await expect(results.locator('.result-snippet b').first()).toBeVisible();
    const hl = (await results.locator('.result-snippet b').allInnerTexts()).join(' ').toLowerCase();
    expect(hl).toMatch(/vaccin|immun|train|system/);
    const bar = await resultsBar(nav.main);
    expect(bar).toMatchObject({ first: 1, last: 10 });
    expect(bar.total).toBeGreaterThan(10);
  });

  const before = await resultsBar(nav.main);

  await nav.step('a kind filter applies, and Back removes it', async () => {
    const filters = nav.main.getByRole('navigation', { name: 'Filter by kind' });
    await expect(filters.getByText('All', { exact: true })).toHaveAttribute('aria-current', 'page');
    const link = filters.getByRole('link').first();
    const label = (await link.innerText()).trim();
    await link.click();
    await nav.settled();
    await nav.expectAddress(/[?&]kind=/);
    // The kind's count is the filtered search's own (or none when not exact; 2026-10-17): match its name.
    const kindName = label.replace(/\s*\([\d,]+\+?\)$/, '');
    const current = filters.locator('[aria-current="page"]');
    await expect(current).toHaveText(
      new RegExp(`^${kindName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(\\s*\\(|$)`),
    );
    await expect(results.getByRole('listitem').first()).toBeVisible();
    await page.getByRole('button', { name: /^Back\b/ }).click();
    await nav.settled();
    expect(await resultsBar(nav.main)).toMatchObject(before);
  });

  await nav.step('Next goes to page 2', async () => {
    const pager = nav.main.getByRole('navigation', { name: 'Result pages' });
    await pager.getByRole('link', { name: 'Next' }).click();
    await nav.settled();
    await nav.expectAddress(/[?&]page=2/);
    expect(await resultsBar(nav.main)).toMatchObject({ first: 11 });
    await expect(pager.getByText('2', { exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(pager.getByRole('link', { name: 'Previous' })).toBeVisible();
  });

  await nav.step('Back returns to page 1, Forward to page 2', async () => {
    await nav.button(/^Back\b/).click();
    await nav.settled();
    expect(await resultsBar(nav.main)).toMatchObject({ first: 1, last: 10 });
    await expect(nav.button(/^Forward\b/)).toBeEnabled();
    await page.keyboard.press('Alt+ArrowRight');
    await nav.settled();
    expect(await resultsBar(nav.main)).toMatchObject({ first: 11 });
    await page.goBack();
    await nav.settled();
    expect(await resultsBar(nav.main)).toMatchObject({ first: 1 });
  });
});

test("a kind filter narrows the results to that kind's count", async ({ nav, data }) => {
  // E2E-5 (docs/testing/e2e-findings.md, fixed): facet counts and filtered totals agree. Counts are
  // shown only when the list holds every match; a filter then finds exactly that many (2026-10-17).
  await nav.open(`wsp://search?q=${encodeURIComponent(data.question)}`);
  const before = await resultsBar(nav.main);
  const filters = nav.main.getByRole('navigation', { name: 'Filter by kind' });
  const links = filters.getByRole('link');
  const labels = await links.allInnerTexts();
  const counts = labels.map((l) => Number(/\(([\d,]+)\)/.exec(l)?.[1]?.replace(/,/g, '') ?? -1));
  const counted = counts.every((n) => n >= 0);
  // The smallest facet, so the narrowing is visible.
  const pick = counted ? counts.indexOf(Math.min(...counts)) : labels.length - 1;
  await links.nth(pick).click();
  await nav.settled();
  const after = await resultsBar(nav.main);
  expect(after.total).toBeGreaterThan(0);
  if (counted) {
    expect(after.total).toBeLessThan(before.total);
    expect(after.total).toBe(counts[pick]);
  }
  // The last page the pager offers has results.
  const pager = nav.main.getByRole('navigation', { name: 'Result pages' });
  if (await pager.count()) {
    await pager.getByRole('link', { name: /^\d+$/ }).last().click();
    await nav.settled();
    await expect(
      nav.main.getByRole('list', { name: 'Search results' }).getByRole('listitem').first(),
    ).toBeVisible();
  }
});

test("I'm Feeling Lucky goes straight to a document", async ({ page, nav }) => {
  await nav.open('wsp://home');
  await nav.button(/^Search\b/).click();
  await nav.settled();
  const box = nav.main.getByRole('combobox', { name: 'Search for:' });
  // Opening the search site puts the cursor in its box.
  await expect(box).toBeFocused();
  await box.pressSequentially('alan turing', { delay: 20 });
  // Escape dismisses the suggestions, and those still on their way: one arriving after Escape opened
  // the list again over the buttons (the test clicked a suggestion half the time).
  await box.press('Escape');
  await page.waitForTimeout(600); // past the suggest debounce: nothing may open now
  await expect(nav.main.locator('.suggest-item:visible')).toHaveCount(0);
  await nav.main.getByRole('button', { name: "I'm Feeling Lucky" }).click();
  await nav.settled();
  await nav.expectAddress(/^wsp:\/\/[a-z0-9.-]+\/[^?]+/);
  expect(await nav.address()).not.toMatch(/^wsp:\/\/search/);
  await expect(nav.main.getByRole('article')).toBeVisible();
  await expect(nav.main.locator('h1')).toHaveCount(1);
  await expect(page.getByText('Location:', { exact: true })).toBeVisible();
});

test("I'm Feeling Lucky opens the page that tops the results", async ({ nav }) => {
  // E2E-6 (docs/testing/e2e-findings.md, fixed): the top hit must not depend on the page size.
  await nav.open('wsp://search?q=alan%20turing');
  const first = nav.main
    .getByRole('list', { name: 'Search results' })
    .getByRole('listitem')
    .first()
    .getByRole('link')
    .first();
  const title = ((await first.textContent()) ?? '').trim();
  await nav.main.getByRole('button', { name: "I'm Feeling Lucky" }).first().click();
  await nav.settled();
  await expect(nav.heading).toContainText(title);
});

test('search-as-you-type suggestions are keyboard-selectable', async ({ page, nav, data }) => {
  await nav.open('wsp://search');
  const box = nav.main.getByRole('combobox', { name: 'Search for:' });
  await box.pressSequentially(data.suggest, { delay: 40 });
  const list = nav.main.getByRole('listbox', { name: 'Suggestions' });
  await expect(list).toBeVisible();
  await expect(box).toHaveAttribute('aria-expanded', 'true');
  const options = list.getByRole('option');
  expect(await options.count()).toBeGreaterThan(0);

  await page.keyboard.press('ArrowDown');
  await expect(options.first()).toHaveAttribute('aria-selected', 'true');
  const firstId = await options.first().getAttribute('id');
  await expect(box).toHaveAttribute('aria-activedescendant', firstId!);
  if ((await options.count()) > 1) {
    await page.keyboard.press('ArrowDown');
    await expect(options.nth(1)).toHaveAttribute('aria-selected', 'true');
    await page.keyboard.press('ArrowUp');
  }
  await expect(options.first()).toHaveAttribute('aria-selected', 'true');
  const picked = (await options.first().locator('.suggest-text').innerText()).trim();

  await page.keyboard.press('Enter');
  await nav.settled();
  await expect(list).toBeHidden();
  // A suggestion is a title: it searches for it (or opens it).
  await expect(nav.main).toContainText(picked);
  await expect(page).not.toHaveTitle(/^WhizBang! Search$/);
});

test('Escape closes the suggestions and keeps the text', async ({ page, nav }) => {
  await nav.open('wsp://search');
  const box = nav.main.getByRole('combobox', { name: 'Search for:' });
  await box.pressSequentially('turing', { delay: 40 });
  const list = nav.main.getByRole('listbox', { name: 'Suggestions' });
  await expect(list).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(list).toBeHidden();
  await expect(box).toHaveValue('turing');
});

test('a shortcut in the location bar opens the page directly', async ({ page, nav, target }) => {
  test.skip(target === 'live', 'the live library holds no RFC site yet');
  await nav.open('wsp://home');
  await nav.go('rfc 9110');
  await nav.expectAddress(/^wsp:\/\/rfc\/9110$/);
  await expect(nav.heading).toContainText('HTTP Semantics');
  await expect(page).toHaveTitle(/RFC 9110/);
});

test('a navigational query ("wikipedia Alan Turing") jumps straight to the article', async ({
  nav,
}) => {
  // E2E-7 (docs/testing/e2e-findings.md, fixed): pages served straight from a ZIM count.
  await nav.open('wsp://home');
  await nav.go('wikipedia Alan Turing');
  await nav.expectAddress(/^wsp:\/\/wikipedia[^/]*\/Alan_Turing$/);
  await expect(nav.heading).toHaveText(/Alan Turing/);
});

test('a misspelled query offers "Did you mean", and following it searches the fix', async ({
  page,
  nav,
  data,
}) => {
  // E2E-10 (docs/testing/e2e-findings.md) is fixed: the live library offers the correction.
  await nav.open('wsp://home');
  await nav.go(data.misspelled);
  const line = nav.main.locator('.did-you-mean');
  await expect(line).toContainText('Did you mean:');
  const fix = line.getByRole('link');
  const corrected = (await fix.innerText()).trim();
  expect(corrected).not.toBe(data.misspelled);
  expect(corrected.length).toBeGreaterThan(2);
  await fix.click();
  await nav.settled();
  await expect(page).toHaveTitle(`WhizBang! Search: ${corrected} - Whisipedia Navigator`);
  await expect(
    nav.main.getByRole('list', { name: 'Search results' }).getByRole('listitem').first(),
  ).toBeVisible();
  // The corrected query needs no further correction.
  await expect(nav.main.locator('.did-you-mean')).toHaveCount(0);
});
