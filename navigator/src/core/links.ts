/**
 * Document HTML → safe DOM inside the offline web.
 *
 * 1. Sanitize with an allowlist (no scripts, no styles, no event handlers,
 *    no remote resources), so arbitrary upstream HTML (ZIM, EPUB) can never
 *    execute, phone home, or trip the CSP.
 * 2. Rewrite every link to a `wsp://` address. Links the library cannot
 *    satisfy become period "broken links" pointing at wsp://offline.
 */
import type { DocumentLink } from '../api/types.ts';
import { docIdToAddress, encodePath, makeAddress, normalizeAddress } from './address.ts';
import { interwikiAddress } from './interwiki.ts';

export type LinkTarget =
  | { kind: 'local'; address: string }
  | { kind: 'anchor'; id: string }
  | { kind: 'missing'; original: string; address: string };

export interface LinkContext {
  sourceId: string;
  nativeId: string;
  /** The document's id, carried into "not in library" addresses for the gap report. */
  docId?: string | undefined;
  /** Resolution table from the API (`DocumentResponse.links`). */
  links?: readonly DocumentLink[] | undefined;
}

const SCHEME_RE = /^([a-z][a-z0-9+.-]*):/i;
const ANCHOR_PREFIX = 'doc-';

function missing(original: string, ctx?: LinkContext): LinkTarget {
  const address = makeAddress('offline', '', { url: original, from: ctx?.docId });
  return { kind: 'missing', original, address };
}

export function anchorId(id: string): string {
  return ANCHOR_PREFIX + id;
}

/** Resolve one href from a document into the offline web. */
export function resolveHref(rawHref: string, ctx: LinkContext): LinkTarget {
  const href = rawHref.trim();
  if (!href) return missing(rawHref, ctx);

  const known = ctx.links?.find((l) => l.href === href);
  if (known)
    return known.target
      ? { kind: 'local', address: docIdToAddress(known.target) }
      : missing(href, ctx);

  if (href.startsWith('#')) {
    const id = href.slice(1);
    return id ? { kind: 'anchor', id } : missing(href, ctx);
  }

  const scheme = SCHEME_RE.exec(href)?.[1]?.toLowerCase();
  if (scheme === 'wsp') {
    const n = normalizeAddress(href);
    return n ? { kind: 'local', address: n } : missing(href, ctx);
  }
  if (scheme === 'http' || scheme === 'https') {
    const mirrored = interwikiAddress(href); // a Wikimedia page the library holds
    if (mirrored) return { kind: 'local', address: mirrored };
  }
  if (scheme) return missing(href, ctx); // http, mailto, javascript, data, ...
  if (href.startsWith('//')) return missing(href, ctx); // protocol-relative = remote

  // Relative link inside the same source "site" (ZIM-style ./Article).
  const base = new URL(`wsp-rel://site/${encodePath(ctx.nativeId)}`);
  let resolved: URL;
  try {
    resolved = new URL(href, base);
  } catch {
    return missing(href, ctx);
  }
  let path: string;
  try {
    path = decodeURIComponent(resolved.pathname.replace(/^\/+/, ''));
  } catch {
    path = resolved.pathname.replace(/^\/+/, '');
  }
  if (!path) return missing(href, ctx);
  const fragment = resolved.hash.replace(/^#/, '');
  const addr = makeAddress(ctx.sourceId, path);
  return { kind: 'local', address: fragment ? `${addr}#${fragment}` : addr };
}

/* --------------------------- sanitizer ----------------------------- */

/** Elements dropped together with their contents. */
const DROP = new Set([
  'script',
  'style',
  'link',
  'meta',
  'base',
  'iframe',
  'frame',
  'frameset',
  'object',
  'embed',
  'applet',
  'form',
  'input',
  'button',
  'select',
  'textarea',
  'noscript',
  'template',
  'svg',
  'math',
  'video',
  'audio',
  'source',
  'track',
  'canvas',
  'head',
  'title',
]);

/** Elements kept (with filtered attributes). Others are unwrapped. */
const KEEP = new Set([
  'a',
  'abbr',
  'address',
  'article',
  'aside',
  'b',
  'bdi',
  'bdo',
  'blockquote',
  'br',
  'caption',
  'cite',
  'code',
  'col',
  'colgroup',
  'dd',
  'del',
  'details',
  'dfn',
  'div',
  'dl',
  'dt',
  'em',
  'figcaption',
  'figure',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'hr',
  'i',
  'img',
  'ins',
  'kbd',
  'li',
  'mark',
  'ol',
  'p',
  'pre',
  'q',
  'rp',
  'rt',
  'ruby',
  's',
  'samp',
  'section',
  'small',
  'span',
  'strong',
  'sub',
  'summary',
  'sup',
  'table',
  'tbody',
  'td',
  'tfoot',
  'th',
  'thead',
  'time',
  'tr',
  'u',
  'ul',
  'var',
  'wbr',
]);

const GLOBAL_ATTRS = new Set(['id', 'title', 'lang', 'dir']);
const TAG_ATTRS: Record<string, Set<string>> = {
  a: new Set(['href']),
  img: new Set(['src', 'alt', 'width', 'height']),
  td: new Set(['colspan', 'rowspan']),
  th: new Set(['colspan', 'rowspan', 'scope']),
  ol: new Set(['start', 'reversed', 'type']),
  li: new Set(['value']),
  time: new Set(['datetime']),
  abbr: new Set(['title']),
  blockquote: new Set(['cite']),
  details: new Set(['open']),
};

/** Only same-origin API assets may load (CSP would block anything else anyway). */
/**
 * Images come only from the library's asset and file routes. Any other `/v1/` path would let an
 * upstream page make every viewer's browser run a search or a random pick per image (review
 * 2026-10-14 #9).
 */
export function isAllowedImageSrc(src: string): boolean {
  return allowedImagePath(src) !== null;
}

/**
 * The path an image source resolves to when it is one of the library's assets, else null. Checked
 * after resolving: `/v1/assets/../search` or `%2e%2e` would pass a check of the raw string and then
 * load `/v1/search` (review 2026-10-14 B). Sanitised pages never need `/v1/files/` (whole originals).
 */
export function allowedImagePath(src: string): string | null {
  if (!src.startsWith('/') || src.startsWith('//') || /[\s\\]/.test(src)) return null;
  let url: URL;
  let origin: string;
  try {
    url = new URL(src, document.baseURI);
    origin = new URL(document.baseURI).origin;
  } catch {
    return null;
  }
  if (url.origin !== origin) return null;
  if (!url.pathname.startsWith('/v1/assets/') || /%2e|%2f|%5c/i.test(url.pathname)) return null;
  return url.pathname + url.search;
}

export interface SanitizeStats {
  links: number;
  broken: number;
  removed: number;
}

export function sanitizeDocumentHtml(
  html: string,
  ctx: LinkContext,
): { fragment: DocumentFragment; stats: SanitizeStats } {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  const out = document.createDocumentFragment();
  const stats: SanitizeStats = { links: 0, broken: 0, removed: 0 };

  const walk = (src: Node, dest: Node): void => {
    for (const child of Array.from(src.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE) {
        dest.appendChild(document.createTextNode(child.textContent ?? ''));
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) continue; // comments, PIs
      const el = child as Element;
      const tag = el.tagName.toLowerCase();
      if (DROP.has(tag)) {
        stats.removed += 1;
        continue;
      }
      if (!KEEP.has(tag)) {
        walk(el, dest); // unwrap unknown wrappers (font, center, custom tags, ...)
        continue;
      }
      if (tag === 'img') {
        dest.appendChild(sanitizeImage(el, stats));
        continue;
      }
      const copy = document.createElement(tag);
      const allowed = TAG_ATTRS[tag];
      for (const attr of Array.from(el.attributes)) {
        const name = attr.name.toLowerCase();
        if (name === 'href') continue;
        if (!GLOBAL_ATTRS.has(name) && !allowed?.has(name)) continue;
        if (name === 'id') copy.setAttribute('id', anchorId(attr.value));
        else copy.setAttribute(name, attr.value);
      }
      if (tag === 'a') {
        const href = el.getAttribute('href');
        if (href !== null) rewriteAnchor(copy as HTMLAnchorElement, href, ctx, stats);
      }
      walk(el, copy);
      dest.appendChild(copy);
    }
  };
  walk(parsed.body, out);
  return { fragment: out, stats };
}

function sanitizeImage(el: Element, stats: SanitizeStats): Node {
  const src = el.getAttribute('src') ?? '';
  const alt = el.getAttribute('alt') ?? '';
  const path = allowedImagePath(src);
  if (path !== null) {
    const img = document.createElement('img');
    img.src = path;
    img.alt = alt;
    img.loading = 'lazy';
    img.decoding = 'async';
    for (const a of ['width', 'height']) {
      const v = el.getAttribute(a);
      if (v && /^\d{1,4}$/.test(v)) img.setAttribute(a, v);
    }
    const title = el.getAttribute('title');
    if (title) img.title = title; // plain text; a comic's hover text lives here
    // High-density variants, each checked like src: `a.png 2x, b.png 3x`.
    const srcset = (el.getAttribute('srcset') ?? '')
      .split(',')
      .map((item) => item.trim().split(/\s+/))
      .filter(([u, d]) => u && (!d || /^\d+(\.\d+)?[xw]$/.test(d)))
      .map(([u, d]) => [allowedImagePath(u!), d] as const)
      .filter(([u]) => u !== null)
      .map(([u, d]) => (d ? `${u} ${d}` : u!));
    if (srcset.length) img.setAttribute('srcset', srcset.join(', '));
    return img;
  }
  stats.removed += 1;
  const span = document.createElement('span');
  span.className = 'broken-image';
  span.setAttribute('role', 'img');
  span.setAttribute('aria-label', alt ? `Image not in library: ${alt}` : 'Image not in library');
  span.textContent = alt ? `[IMAGE: ${alt}]` : '[IMAGE]';
  return span;
}

export function rewriteAnchor(
  a: HTMLAnchorElement,
  href: string,
  ctx: LinkContext,
  stats?: SanitizeStats,
): LinkTarget {
  const target = resolveHref(href, ctx);
  if (stats) stats.links += 1;
  switch (target.kind) {
    case 'local':
      a.setAttribute('href', `#${target.address}`);
      a.dataset.wsp = target.address;
      break;
    case 'anchor':
      a.setAttribute('href', `#${anchorId(target.id)}`);
      a.dataset.anchor = anchorId(target.id);
      break;
    case 'missing':
      if (stats) stats.broken += 1;
      a.setAttribute('href', `#${target.address}`);
      a.dataset.wsp = target.address;
      a.dataset.missing = target.original;
      a.classList.add('broken-link');
      a.setAttribute('title', `Not in library: ${target.original}`);
      break;
  }
  return target;
}
