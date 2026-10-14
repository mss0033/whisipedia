/** Built-in informational pages: about, help, offline, random, invalid. */
import { ApiRequestError } from '../api/client.ts';
import { docIdToAddress, makeAddress, searchAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { THEMES } from '../core/theme.ts';
import { ENGINE_NAME, errorPage, pageHeading, wspLink } from '../ui/components.ts';
import type { PageContext, PageResult } from './types.ts';

export const VERSION = '0.1.0';

export const SHORTCUTS: Array<[string, string]> = [
  ['Alt+Left / Alt+Right', 'Back / Forward'],
  ['Alt+Home', 'Start Page'],
  ['Ctrl+L, Alt+D or F6', 'Focus the Location bar'],
  ['/', 'Focus the search box on the page (or the Location bar)'],
  ['Ctrl+R or F5', 'Reload'],
  ['Esc', 'Stop loading, close menus and dialogs'],
  ['Ctrl+D', 'Add this page to the Hotlist'],
  ['Ctrl+B', 'Open the Hotlist'],
  ['Ctrl+H', 'Open History'],
  ['Ctrl+U', 'View Source Data for this document'],
  ['Ctrl+] / Ctrl+[', 'Larger / smaller text'],
  ['Ctrl+0', 'Reset text size'],
  ['F10', 'Focus the menu bar (then arrow keys)'],
  ['Ctrl+P', 'Print'],
];

export async function renderAbout(_ctx: PageContext): Promise<PageResult> {
  const body = h(
    'div',
    { class: 'page page-about' },
    pageHeading('About Whisipedia Navigator'),
    h(
      'p',
      { class: 'about-version' },
      h('b', null, `Whisipedia Navigator ${VERSION}`),
      ' — a web browser for an offline web.',
    ),
    h(
      'p',
      null,
      'Everything you see here comes from the library on this computer. The Navigator makes no requests to the Internet: a strict Content Security Policy (',
      h('code', null, "default-src 'self'"),
      ') makes any outbound request fail.',
    ),
    h('h2', null, 'Themes'),
    h('ul', null, ...THEMES.map((t) => h('li', null, h('b', null, t.name), ` — ${t.hint}`))),
    h(
      'p',
      null,
      'Change the theme from the View menu. Light or Dark there gives the light themes their night colours (following the system setting unless you choose); a high-contrast variant, text sizes and reduced motion live there too.',
    ),
    h('h2', null, 'Credits'),
    h(
      'p',
      null,
      `The search engine is called ${ENGINE_NAME}. Content belongs to its publishers and is shown under each source’s license, printed on every page.`,
    ),
    h('p', null, wspLink(makeAddress('help'), 'Help and keyboard shortcuts')),
  );
  return { title: 'About Whisipedia Navigator', body };
}

export async function renderHelp(ctx: PageContext): Promise<PageResult> {
  const body = h(
    'div',
    { class: 'page page-help' },
    pageHeading('Navigator Handbook'),
    h('h2', null, 'Getting around'),
    h(
      'p',
      null,
      'Every source is a site with a ',
      h('code', null, 'wsp://'),
      ' address, like ',
      wspLink('wsp://mdwiki/Diabetes'),
      ' or ',
      wspLink('wsp://wikipedia-en-zim/Alan_Turing'),
      '. Type an address into the Location bar, or type anything else to search. Shortcuts such as ',
      h('kbd', null, 'gutenberg 84'),
      ', ',
      h('kbd', null, 'mdwiki diabetes'),
      ' and ',
      h('kbd', null, 'wiktionary library'),
      ' jump straight to the page.',
    ),
    h(
      'p',
      null,
      'Links to things the library does not hold are drawn as ',
      h('a', { class: 'broken-link', href: '#wsp://offline' }, 'broken links'),
      '.',
    ),
    h('h2', { id: 'help-practice', tabindex: '-1' }, 'Exam Practice Hall'),
    h(
      'p',
      null,
      wspLink(makeAddress('practice'), 'wsp://practice'),
      ' turns the library’s question pools (the amateur radio licence pools) into a drill: pick a group to study it one question at a time with the answer and rule shown, or sit a mock exam that draws one question from every group, as the real one does, and grades it at 74%. Keys 1–4 or A–D answer, N or Enter moves on. What you have answered stays in this browser only.',
    ),
    h('h2', { id: 'help-screening', tabindex: '-1' }, 'The Screening Room'),
    h(
      'p',
      null,
      wspLink(makeAddress('screening-room'), 'wsp://screening-room'),
      ' is where the library’s film, lectures, recorded radio and music play. A document whose original file is video or audio shows a player in place of the file panel: four skins (Theater 98, Brushed Steel, Desktop 95, Tube 2008) chosen from the player’s menu, resume where you left off, and keys: Space or K play/pause, J and L skip ten seconds, arrows seek and change the volume, M mutes, F fills the screen, 0–9 jump to a tenth. The room lists what to continue and which sites hold media, and its projection test builds a short reel in the browser to try the players before any film is on the shelves.',
    ),
    h('h2', { id: 'help-search', tabindex: '-1' }, 'Search tips'),
    h(
      'ul',
      null,
      h(
        'li',
        null,
        'Write in plain language: ',
        wspLink(
          searchAddress('how do vaccines train the immune system'),
          'how do vaccines train the immune system',
        ),
        '.',
      ),
      h(
        'li',
        null,
        'Start with “define” for a dictionary box: ',
        wspLink(searchAddress('define sesquipedalian'), 'define sesquipedalian'),
        '.',
      ),
      h('li', null, "I'm Feeling Lucky opens the first result directly."),
    ),
    h('h2', null, 'Keyboard shortcuts'),
    h(
      'table',
      { class: 'grid-table' },
      h('thead', null, h('tr', null, h('th', null, 'Keys'), h('th', null, 'Action'))),
      h(
        'tbody',
        null,
        ...SHORTCUTS.map(([k, a]) =>
          h('tr', null, h('td', null, h('kbd', null, k)), h('td', null, a)),
        ),
      ),
    ),
  );
  const topic = (ctx.route as { topic: string }).topic;
  return { title: 'Navigator Handbook', body, scrollTo: topic ? `help-${topic}` : undefined };
}

export async function renderOffline(ctx: PageContext): Promise<PageResult> {
  const url = (ctx.route as { url: string }).url;
  const from = (ctx.route as { from: string | undefined }).from;
  // "The missing link has been noted": report it to the gap miner (PLAN §5.3).
  if (url) void ctx.app.api.reportGap({ href: url, from_doc_id: from ?? ctx.app.lastDocId() });
  let words = '';
  try {
    const u = new URL(/^[a-z]+:/i.test(url) ? url : `http://${url}`);
    words =
      `${u.hostname.replace(/^www\./, '').split('.')[0] ?? ''} ${decodeURIComponent(u.pathname).replace(/[/_\-.]+/g, ' ')}`.trim();
  } catch {
    words = url;
  }
  return {
    title: 'Not in library',
    body: errorPage({
      title: 'Not in library',
      heading: 'This site is not in the library',
      message:
        'Whisipedia is an offline web. The page you asked for lives on the Internet and has not been mirrored. Nothing was downloaded. The missing link has been noted so the library can consider it in a future update.',
      address: url || undefined,
      suggestions: [
        words ? wspLink(searchAddress(words), `Search the library for “${words}”`) : null,
        wspLink(makeAddress('home'), 'Go to the Start Page'),
      ].filter((x) => x !== null),
    }),
  };
}

export async function renderRandom(ctx: PageContext): Promise<PageResult> {
  const source = (ctx.route as { source: string | undefined }).source;
  try {
    const r = await ctx.app.api.random(source, ctx.signal);
    return { redirect: docIdToAddress(r.doc_id) };
  } catch (err) {
    // Busy or failing: the app retries. Only an older daemon (no /v1/random) or an empty site fall back.
    if (!(err instanceof ApiRequestError) || !(err.isMissingRoute || err.code === 'empty'))
      throw err;
    // Older daemon without /v1/random: a random recent change.
    const ch = await ctx.app.api
      .recentChanges(50, source, ctx.signal)
      .catch(() => ({ items: [], cursor: null }));
    const pool = ch.items.filter((c) => c.kind !== 'removed');
    const pick = pool[Math.floor(Math.random() * pool.length)];
    if (pick) return { redirect: docIdToAddress(pick.doc_id) };
    return {
      title: 'No random page',
      body: errorPage({
        title: 'Empty',
        heading: 'Nothing to pick from',
        message: 'This site has no pages yet.',
      }),
    };
  }
}

export async function renderInvalid(ctx: PageContext): Promise<PageResult> {
  return {
    title: 'Bad address',
    body: errorPage({
      title: 'Bad address',
      heading: 'Bad address',
      message: 'That does not look like a wsp:// address.',
      address: ctx.address,
      suggestions: [wspLink(searchAddress(ctx.address), 'Search for it instead')],
    }),
  };
}
