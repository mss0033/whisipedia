/**
 * Client-side router: maps a `wsp://` address to a page route. Pure and
 * synchronous so it is trivially testable; rendering lives in pages/.
 */
import type { DocKind } from '../api/types.ts';
import { DOC_KINDS } from '../api/types.ts';
import { addressToDocId, parseAddress } from './address.ts';

export type Route =
  | { page: 'home' }
  | { page: 'directory'; path: string }
  | {
      page: 'search';
      query: string;
      kind: DocKind | undefined;
      source: string | undefined;
      pageNo: number;
      lucky: boolean;
    }
  | { page: 'whatsnew' }
  | { page: 'system' }
  | { page: 'history' }
  | { page: 'hotlist' }
  | { page: 'about'; topic: string }
  | { page: 'help'; topic: string }
  | { page: 'random'; source: string | undefined }
  | { page: 'offline'; url: string; from: string | undefined }
  | { page: 'site'; source: string }
  | {
      page: 'document';
      source: string;
      nativeId: string;
      docId: string;
      at: string | undefined;
      diff: boolean;
      fragment: string;
    }
  | {
      page: 'practice';
      /** Native id of the pool document ('' for the hall). */
      pool: string;
      group: string | undefined;
      mode: 'study' | 'exam';
      seed: number | undefined;
      /** Question index within the set. */
      index: number;
    }
  | { page: 'screening' }
  | { page: 'define'; word: string }
  | { page: 'invalid'; input: string };

function toKind(v: string | undefined): DocKind | undefined {
  return v && (DOC_KINDS as readonly string[]).includes(v) ? (v as DocKind) : undefined;
}

function toPage(v: string | undefined): number {
  const n = Number.parseInt(v ?? '1', 10);
  return Number.isFinite(n) && n >= 1 && n <= 1000 ? n : 1;
}

const GENERATION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export function resolveRoute(address: string): Route {
  const a = parseAddress(address);
  if (!a) return { page: 'invalid', input: address };
  const p = a.params;
  switch (a.host) {
    case 'home':
      return { page: 'home' };
    case 'directory':
      return { page: 'directory', path: a.path };
    case 'search':
      return {
        page: 'search',
        query: (p.q ?? '').trim(),
        kind: toKind(p.kind),
        source: p.source || undefined,
        pageNo: toPage(p.page),
        lucky: p.lucky === '1',
      };
    case 'whatsnew':
    case 'system':
    case 'history':
    case 'hotlist':
      return { page: a.host };
    case 'about':
    case 'help':
      return { page: a.host, topic: a.path };
    case 'random':
      return { page: 'random', source: p.source || a.path || undefined };
    case 'offline':
      return { page: 'offline', url: p.url ?? p.doc ?? '', from: p.from };
    case 'screening-room':
      return { page: 'screening' };
    case 'define':
      return { page: 'define', word: a.path };
    case 'practice': {
      const seed = Number.parseInt(p.seed ?? '', 10);
      const index = Number.parseInt(p.n ?? '0', 10);
      return {
        page: 'practice',
        pool: a.path,
        group: /^[A-Z]\d[A-Z]$/.test(p.group ?? '') ? p.group : undefined,
        mode: p.mode === 'exam' ? 'exam' : 'study',
        seed: Number.isFinite(seed) && seed > 0 ? seed : undefined,
        index: Number.isFinite(index) && index >= 0 ? index : 0,
      };
    }
    default: {
      const docId = addressToDocId(a);
      if (!docId) return { page: 'site', source: a.host };
      return {
        page: 'document',
        source: a.host,
        nativeId: a.path,
        docId,
        // Any generation id: dates, Kiwix months ("2026-08"), "pubmed26-u1671", "2026-10-11.nj",
        // refinery set ids ("2026-10-11.ri.set00001"). The API answers 404 for one it does not know.
        at: p.at && GENERATION_ID_RE.test(p.at) ? p.at : undefined,
        diff: p.diff === '1',
        fragment: a.fragment,
      };
    }
  }
}
