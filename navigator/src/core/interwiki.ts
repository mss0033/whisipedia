/**
 * Links to Wikimedia projects the library mirrors stay inside the offline web: an article cited as
 * https://en.wiktionary.org/wiki/calli opens the library's Wiktionary, not a "not in library" page.
 */
import { makeAddress } from './address.ts';

/** Wikipedia languages held by wikipedia-top10 (registry `languages`); English reads the ZIM edition. */
const WIKIPEDIA_LANGS: ReadonlySet<string> = new Set([
  'ceb',
  'de',
  'fr',
  'sv',
  'nl',
  'es',
  'ru',
  'it',
  'pl',
]);

function wikiTitle(url: URL): string | null {
  let title: string | null = null;
  if (url.pathname.startsWith('/wiki/')) title = url.pathname.slice('/wiki/'.length);
  else if (url.pathname === '/w/index.php') title = url.searchParams.get('title');
  if (!title) return null;
  try {
    title = decodeURIComponent(title);
  } catch {
    // keep the raw title
  }
  title = title.replace(/ /g, '_');
  return title && !title.includes(':') ? title : null; // Special:, Talk:, File: … are not mirrored
}

/** The library address for a Wikimedia URL it mirrors, or null. */
export function interwikiAddress(href: string): string | null {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
  const host = url.hostname.toLowerCase().replace(/^www\./, '');
  const title = wikiTitle(url);
  if (!title) return null;
  const fragment = url.hash.replace(/^#/, '');
  const withFragment = (a: string): string => (fragment ? `${a}#${fragment}` : a);
  const m = /^([a-z-]+)\.(?:m\.)?(wikipedia|wiktionary)\.org$/.exec(host);
  if (m) {
    const [, lang, project] = m;
    if (project === 'wiktionary')
      return lang === 'en' ? withFragment(makeAddress('wiktionary-en', title)) : null;
    if (lang === 'en') return withFragment(makeAddress('wikipedia-en-zim', title));
    if (lang && WIKIPEDIA_LANGS.has(lang))
      return withFragment(makeAddress('wikipedia-top10', `${lang}/${title}`));
    return null;
  }
  if (host === 'mdwiki.org') return withFragment(makeAddress('mdwiki', title));
  return null;
}
