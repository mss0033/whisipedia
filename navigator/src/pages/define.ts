/**
 * wsp://define/<word> — straight to the dictionary entry. The library's Wiktionary copies are keyed
 * by headword (case matters: "lighthouse", "Paris"), so the word is tried as typed, in lower case
 * and capitalised, in the HTML Wiktionary first and the dump-derived one second; a word none of them
 * has falls back to a dictionary-only search. The location bar accepts "define word", "def word"
 * and "d word".
 */
import { ApiRequestError } from '../api/client.ts';
import { docIdToAddress, searchAddress } from '../core/address.ts';
import type { Route } from '../core/router.ts';
import { rememberDocument } from './document.ts';
import type { PageContext, PageResult } from './types.ts';

/** Dictionary sites, best copy first. */
export const DICTIONARY_SOURCES = [
  'kiwix-wiktionary-en-all',
  'wiktionary-en',
  'kiwix-wiktionary-en-simple-all',
];

/** The headword spellings worth trying, as typed first. */
export function spellings(word: string): string[] {
  const w = word.trim().replace(/\s+/g, '_');
  if (!w) return [];
  const lower = w.toLowerCase();
  const capital = lower.charAt(0).toUpperCase() + lower.slice(1);
  return [...new Set([w, lower, capital])];
}

export async function render(ctx: PageContext): Promise<PageResult> {
  const { word } = ctx.route as Extract<Route, { page: 'define' }>;
  const query = word.replace(/_/g, ' ').trim();
  if (!query) return { redirect: searchAddress('', { kind: 'dictionary' }) };
  // The source list falls back to [] when the library is slow to answer it: then try every
  // dictionary rather than none (an unknown source answers 404 at once).
  const known = (await ctx.app.sources()).map((s) => s.id);
  const held = known.length ? new Set(known) : new Set(DICTIONARY_SOURCES);
  for (const source of DICTIONARY_SOURCES) {
    if (!held.has(source)) continue;
    for (const spelling of spellings(word)) {
      const id = `wsp:${source}/${spelling}`;
      try {
        const doc = await ctx.app.api.document(id, {}, ctx.signal);
        rememberDocument(id, doc); // the entry page shows it without fetching it again
        return { redirect: docIdToAddress(id) };
      } catch (err) {
        if (err instanceof ApiRequestError && err.status === 404) continue;
        throw err;
      }
    }
  }
  return { redirect: searchAddress(query, { kind: 'dictionary' }) };
}
