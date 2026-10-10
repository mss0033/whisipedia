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
  | { page: 'offline'; url: string }
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
  | { page: 'invalid'; input: string };

function toKind(v: string | undefined): DocKind | undefined {
  return v && (DOC_KINDS as readonly string[]).includes(v) ? (v as DocKind) : undefined;
}

function toPage(v: string | undefined): number {
  const n = Number.parseInt(v ?? '1', 10);
  return Number.isFinite(n) && n >= 1 && n <= 1000 ? n : 1;
}

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
      return { page: 'offline', url: p.url ?? p.doc ?? '' };
    default: {
      const docId = addressToDocId(a);
      if (!docId) return { page: 'site', source: a.host };
      return {
        page: 'document',
        source: a.host,
        nativeId: a.path,
        docId,
        at: p.at && /^\d{4}-\d{2}-\d{2}$/.test(p.at) ? p.at : undefined,
        diff: p.diff === '1',
        fragment: a.fragment,
      };
    }
  }
}
