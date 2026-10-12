/**
 * `wsp://` addresses: parsing, formatting, doc_id mapping, and interpreting
 * whatever the user typed into the location bar (address, search, shortcut).
 *
 *   wsp://<host>[/<path>][?<query>][#<fragment>]
 *
 * <host> is either a built-in site (home, search, …) or a source id, in which
 * case <path> is the native id and the address maps 1:1 to a doc_id:
 *   wsp://rfc/9110  <=>  wsp:rfc/9110
 */

import type { DocId } from '../api/types.ts';

export interface WspAddress {
  host: string;
  /** Decoded path without the leading slash ('' for the site root). */
  path: string;
  params: Record<string, string>;
  fragment: string;
}

/** Built-in sites served by the Navigator itself. */
export const BUILTIN_HOSTS = [
  'home',
  'directory',
  'search',
  'whatsnew',
  'system',
  'history',
  'hotlist',
  'about',
  'help',
  'random',
  'offline',
] as const;
export type BuiltinHost = (typeof BUILTIN_HOSTS)[number];

export function isBuiltinHost(host: string): host is BuiltinHost {
  return (BUILTIN_HOSTS as readonly string[]).includes(host);
}

/** Friendly host names accepted in the location bar. */
export const HOST_ALIASES: Readonly<Record<string, string>> = {
  'wikipedia.en': 'wikipedia-en-zim',
  'wikipedia-en': 'wikipedia-en-zim',
  wikipedia: 'wikipedia-en-zim',
  wiki: 'wikipedia-en-zim',
  'wiktionary.en': 'wiktionary-en',
  wiktionary: 'wiktionary-en',
  maps: 'osm-planet',
  osm: 'osm-planet',
  stack: 'stackexchange',
  'search.wsp': 'search',
  start: 'home',
};

export const HOME_ADDRESS = 'wsp://home';
export const SEARCH_ADDRESS = 'wsp://search';

const HOST_RE = /^[a-z0-9][a-z0-9.-]*$/;

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/** Encode only what would break parsing; keep addresses human-readable. */
export function encodePath(path: string): string {
  return path.replace(/[%?#\s]/g, (c) => encodeURIComponent(c));
}

export function parseAddress(input: string): WspAddress | null {
  const raw = input.trim();
  const m = /^wsp:(\/\/)?(.*)$/i.exec(raw);
  if (!m) return null;
  let rest = m[2] ?? '';
  let fragment = '';
  const hashAt = rest.indexOf('#');
  if (hashAt !== -1) {
    fragment = safeDecode(rest.slice(hashAt + 1));
    rest = rest.slice(0, hashAt);
  }
  const params: Record<string, string> = {};
  const qAt = rest.indexOf('?');
  if (qAt !== -1) {
    for (const [k, v] of new URLSearchParams(rest.slice(qAt + 1))) params[k] = v;
    rest = rest.slice(0, qAt);
  }
  const slash = rest.indexOf('/');
  let host = (slash === -1 ? rest : rest.slice(0, slash)).toLowerCase();
  let path = slash === -1 ? '' : rest.slice(slash + 1);
  path = safeDecode(path.replace(/\/+$/, ''));
  if (host === '') host = 'home';
  host = HOST_ALIASES[host] ?? host;
  if (!HOST_RE.test(host)) return null;
  return { host, path, params, fragment };
}

export function formatAddress(a: WspAddress): string {
  let s = `wsp://${a.host}`;
  if (a.path) s += `/${encodePath(a.path)}`;
  const qs = new URLSearchParams(a.params).toString();
  if (qs) s += `?${qs}`;
  if (a.fragment) s += `#${encodeURIComponent(a.fragment)}`;
  return s;
}

/** Canonical string form, or null when not a wsp address. */
export function normalizeAddress(input: string): string | null {
  const a = parseAddress(input);
  return a ? formatAddress(a) : null;
}

export function makeAddress(
  host: string,
  path = '',
  params: Record<string, string | number | undefined> = {},
): string {
  const clean: Record<string, string> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') clean[k] = String(v);
  }
  return formatAddress({ host, path, params: clean, fragment: '' });
}

export function docIdToAddress(id: DocId): string {
  const m = /^wsp:([^/]+)\/(.+)$/.exec(id);
  if (!m) return makeAddress('offline', '', { doc: id });
  return makeAddress(m[1]!, m[2]!);
}

export function addressToDocId(a: WspAddress): DocId | null {
  if (isBuiltinHost(a.host) || !a.path) return null;
  return `wsp:${a.host}/${a.path}`;
}

export function searchAddress(q: string, extra: Record<string, string | number | undefined> = {}) {
  return makeAddress('search', '', { q, ...extra });
}

/**
 * Sites with something to read, once the library's site list has arrived (null until then). Shortcuts
 * and autolinks into a site the library does not hold would only lead to a 404 and a false gap report.
 */
let readableHosts: ReadonlySet<string> | null = null;

export function setReadableHosts(hosts: Iterable<string> | null): void {
  readableHosts = hosts === null ? null : new Set(hosts);
}

/** True while the list is unknown (the server answers for itself), else whether `host` is readable. */
export function isReadableHost(host: string): boolean {
  return readableHosts === null || readableHosts.has(host);
}

/** Instant client-side navigational shortcuts ("rfc 9110", "Q42"). */
const SHORTCUTS: Array<[RegExp, (m: RegExpExecArray) => string]> = [
  [/^rfc\s*-?\s*(\d{1,5})$/i, (m) => makeAddress('rfc', String(Number(m[1])))],
  [/^(?:gutenberg|pg)\s*#?\s*(\d+)$/i, (m) => makeAddress('gutenberg', m[1]!)],
  [/^arxiv:?\s*(\d{4}\.\d{4,5}(?:v\d+)?)$/i, (m) => makeAddress('arxiv', m[1]!)],
  [/^(Q[1-9]\d*)$/, (m) => makeAddress('wikidata', m[1]!)],
  [/^pmid:?\s*(\d+)$/i, (m) => makeAddress('pubmed', m[1]!)],
];

export function shortcutFor(text: string): string | null {
  const t = text.trim();
  for (const [re, fn] of SHORTCUTS) {
    const m = re.exec(t);
    if (m) return fn(m);
  }
  return null;
}

export type LocationInput =
  | { type: 'address'; address: string }
  | { type: 'search'; query: string }
  | { type: 'external'; url: string; address: string };

const EXTERNAL_RE = /^(?:https?|ftp|gopher|news|telnet|wais|mailto):/i;
const BARE_DOMAIN_RE =
  /^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|edu|gov|io|int)(?:\/\S*)?$/i;

/** Decide what the location bar should do with free text. */
export function interpretInput(text: string): LocationInput | null {
  const t = text.trim();
  if (!t) return null;
  if (/^wsp:/i.test(t)) {
    const n = normalizeAddress(t);
    if (n) return { type: 'address', address: n };
    return { type: 'search', query: t };
  }
  if (/^about:/i.test(t)) {
    return { type: 'address', address: makeAddress('about', t.slice(6)) };
  }
  if (EXTERNAL_RE.test(t) || BARE_DOMAIN_RE.test(t)) {
    return { type: 'external', url: t, address: makeAddress('offline', '', { url: t }) };
  }
  const shortcut = shortcutFor(t);
  if (shortcut && isReadableHost(parseAddress(shortcut)?.host ?? '')) {
    return { type: 'address', address: shortcut };
  }
  return { type: 'search', query: t };
}

/** Address to put in the page URL fragment so reloads and new tabs work. */
export function addressToHash(address: string): string {
  return `#${address}`;
}

export function hashToAddress(hash: string): string | null {
  const h = hash.replace(/^#/, '');
  if (!h) return null;
  return normalizeAddress(h) ?? normalizeAddress(safeDecode(h));
}
