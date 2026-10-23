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
  | { kind: 'missing'; original: string; address: string }
  /** A link that goes nowhere upstream either (`href="#"`, `javascript:void(0)`): plain text here. */
  | { kind: 'none' }
  /** A file of the archive to save, not a page (a Gutenberg book's EPUB): `/v1/assets/…`. */
  | { kind: 'download'; path: string; name: string; view: boolean };

/** Upstream placeholders for "no link": the xkcd front page's "Next >" is `href="#"` (field exam 2026-10-14). */
const NOWHERE = /^(?:#|javascript:\s*(?:void\s*\(\s*0\s*\)\s*;?)?)$/i;

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

/** Files a browser shows by itself (the asset route serves these inline). */
const VIEWABLE = /\.(png|jpe?g|gif|webp|avif|bmp|svg|ico)$/i;

export function anchorId(id: string): string {
  return ANCHOR_PREFIX + id;
}

/**
 * The Atlas address of a `geo:` URI (RFC 5870: `geo:46.6833,7.85`, `geo:46.68,7.85,560;u=30`; the
 * common `?z=12` zoom too), pinned, or null when it holds no coordinates.
 */
export function geoAddress(uri: string): string | null {
  const m =
    /^geo:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)(?:,-?\d+(?:\.\d+)?)?(?:;[^?]*)?(?:\?(.*))?$/i.exec(
      uri.trim(),
    );
  if (!m) return null;
  const lat = Number(m[1]);
  const lon = Number(m[2]);
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180)
    return null;
  if (lat === 0 && lon === 0 && m[3]) return null; // geo:0,0?q=place: a search, not a point
  const z = Number(new URLSearchParams(m[3] ?? '').get('z'));
  const zoom = Number.isFinite(z) && z >= 1 && z <= 21 ? z : 13;
  const r = (v: number) => String(Number(v.toFixed(6)));
  return `wsp://atlas/@${r(lat)},${r(lon)},${zoom}z?pin=${r(lat)},${r(lon)}`;
}

/** Resolve one href from a document into the offline web. */
export function resolveHref(rawHref: string, ctx: LinkContext): LinkTarget {
  const href = rawHref.trim();
  if (!href || NOWHERE.test(href)) return { kind: 'none' };

  if (href.startsWith('/v1/assets/')) {
    // The server writes a link to a file in an archive (EPUB, PDF, a picture, sheet music) as its
    // asset URL. A picture opens to look at; anything else is saved.
    const path = allowedImagePath(href);
    if (!path) return missing(href, ctx);
    const name = decodeURIComponent(path.split('?')[0]!.split('/').pop() ?? '') || 'download';
    return { kind: 'download', path, name, view: VIEWABLE.test(name) };
  }

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
  if (scheme === 'geo') {
    // Coordinates (RFC 5870): every Wikipedia article's "Coordinates" link. The Atlas shows them.
    const at = geoAddress(href);
    return at ? { kind: 'local', address: at } : missing(href, ctx);
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
  // A <form> is unwrapped, not dropped: its headings and labels are the page (Bootstrap's sign-in
  // example was empty, 2026-10-21; ASP.NET sites wrap whole pages in one). Its controls still go.
  'input',
  'button',
  'select',
  'textarea',
  'noscript',
  'template',
  'svg',
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
/** The one data attribute kept: rewrites mark what they added (classes are dropped). */
export const ROLE_ATTR = 'data-wsp-role';
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
 * The path an image source (or a download link) resolves to when it is one of the library's assets, else null. Checked
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

const MATHML_NS = 'http://www.w3.org/1998/Math/MathML';
/** Presentation MathML kept (rebuilt in the MathML namespace): PlanetMath's formulas are MathML only,
 * and dropping `math` left "Let be a complete metric space" (2026-10-16 field exam). */
const MATH_TAGS = new Set([
  'math',
  'mrow',
  'mi',
  'mn',
  'mo',
  'ms',
  'mtext',
  'mspace',
  'msup',
  'msub',
  'msubsup',
  'mfrac',
  'msqrt',
  'mroot',
  'mover',
  'munder',
  'munderover',
  'mtable',
  'mtr',
  'mtd',
  'mstyle',
  'mpadded',
  'mphantom',
  'menclose',
  'mfenced',
  'semantics',
]);
const MATH_ATTRS = new Set([
  'display',
  'mathvariant',
  'stretchy',
  'fence',
  'separator',
  'lspace',
  'rspace',
  'accent',
  'accentunder',
  'linethickness',
  'columnalign',
  'rowalign',
  'displaystyle',
  'scriptlevel',
  'form',
  'largeop',
  'movablelimits',
  'symmetric',
  'minsize',
  'maxsize',
  'width',
  'height',
  'depth',
  'notation',
  'open',
  'close',
  'separators',
  'alttext',
]);

/** A formula as MathML (allowlisted elements and attributes, no links), or null to drop it. */
export function sanitizeMath(el: Element): Element | null {
  // Wikipedia's hidden accessible copy: its fallback image is the formula on screen.
  if (el.closest('[class*="mwe-math-mathml"]')) return null;
  const copy = (src: Element): Element | null => {
    const tag = src.localName.toLowerCase();
    if (!MATH_TAGS.has(tag)) return null; // annotation, annotation-xml, maction, unknown
    const out = document.createElementNS(MATHML_NS, tag);
    for (const attr of Array.from(src.attributes)) {
      const name = attr.name.toLowerCase();
      if (MATH_ATTRS.has(name)) out.setAttribute(name, attr.value);
    }
    for (const child of Array.from(src.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE)
        out.appendChild(document.createTextNode(child.textContent ?? ''));
      else if (child.nodeType === Node.ELEMENT_NODE) {
        const c = copy(child as Element);
        if (c) out.appendChild(c);
        if (tag === 'semantics') break; // its first child is the presentation; the rest are annotations
      }
    }
    return out;
  };
  return copy(el);
}

/** Notes a wiki's own stylesheet hides, which show once it is gone: Wikipedia's citation maintenance
 * messages ("{{cite journal}}: CS1 maint: unflagged free DOI", 2026-10-19 audit). */
const HIDDEN_CLASSES = ['cs1-maint', 'cs1-hidden-error', 'hiddenStructure'];

/** Wiki template leftovers in a page's text: a parameter never filled in (`{{{name}}}`), and a category
 * or file link with no name (`[[Category:]]`, `[[File:|thumb|]]`, an unsigned comment's `[[User:|]]`
 * and `[[Special:Contributions/|]]` on explain xkcd), a magic word (`{{PAGENAME}}`,
 * Energypedia's file pages), a link to a template (`[[Template:Pub Tag Improved Cookstoves}]]`,
 * Energypedia, 2026-10-21 audit). Never a page's words: Gardenology
 * and Appropedia pages showed them in place of the values their templates lacked (2026-10-19 audit).
 * Text in code blocks is left alone. */
const TEMPLATE_LEFTOVERS =
  /\[\[\s*(?:\{\{\{[^{}]{1,40}\}\}\}|[A-Z][A-Za-z]{1,20}(?:[ _][A-Za-z]{1,20})?\s*:\s*(?:[A-Z][A-Za-z]{1,30}\/)?(?:\|[^[\]|]{0,40})*|Template\s*:[^[\]]{1,80})\s*\]\]|\{\{\{[\w -]{1,40}(?:\|[^{}]{0,40})?\}\}\}|\{\{(?:FULL|BASE|SUB)?PAGENAMEE?\}\}|\{\{(?:SITENAME|NAMESPACE)\}\}/g;
const LEFTOVER_MARK = '\uE000'; // private use: a page's text has it only from an icon font, if ever
const EMPTIED_BRACKETS = /\(\s*\uE000[\s\uE000]*\)/g;
const CODE_TAGS = new Set(['pre', 'code', 'kbd', 'samp', 'textarea']);

/** The page a `<frame>` or `<iframe>` showed: its source rewritten to the archive's asset route
 * (`/v1/assets/<source>/<generation>/<path>.html`), as the address of that page. */
function framePage(el: Element): { address: string; name: string } | null {
  const m = /^\/v1\/assets\/([^/]+)\/[^/]+\/([^?#]+\.html?)$/i.exec(el.getAttribute('src') ?? '');
  if (!m) return null;
  let path: string;
  try {
    path = decodeURIComponent(m[2]!);
  } catch {
    return null;
  }
  const name = (
    el.getAttribute('title') ||
    el.getAttribute('name') ||
    path.split('/').pop() ||
    path
  ).trim();
  return { address: makeAddress(m[1]!, path), name };
}

export function withoutTemplateLeftovers(text: string): string {
  let t = text;
  let marked = false;
  // Again until nothing changes: [[Image:{{{range_map}}}|{{{width}}}|]] is a nameless image link
  // once its parameters are gone (Gardenology, 2026-10-20 audit).
  for (let i = 0; i < 3 && (t.includes('[[') || t.includes('{{')); i++) {
    const next = t
      // A removed link leaves a mark, so the brackets it alone filled go too (below).
      .replace(TEMPLATE_LEFTOVERS, (m) => {
        if (!m.startsWith('[[')) return '';
        marked = true;
        return LEFTOVER_MARK;
      })
      .replace(BROKEN_TEMPLATE, '$1')
      .replace(UNCLOSED_TEMPLATE, '')
      .replace(PARAM_TEMPLATE, '')
      .replace(CUT_LINK, '')
      .replace(TEMPLATE_OPENER, '');
    if (next === t) break;
    t = next;
  }
  // `([[User talk:{{{1}}}|talk]])` is `()` once its link is gone (explain xkcd's unsigned comments,
  // 2026-10-22 audit); parentheses a page writes itself (`main()`) never held a mark.
  if (marked) t = t.replace(EMPTIED_BRACKETS, '').replaceAll(LEFTOVER_MARK, '');
  if (t.includes('[['))
    t = t.replace(UNLINKED, (_m, title: string, label?: string) => label ?? title);
  return t.includes('&') ? t.replace(ESCAPED_TWICE, (e) => UNESCAPE[e] ?? e) : t;
}

/** A template a translation broke, so the wiki printed it (Gentoo's handbook in Chinese and Japanese:
 * `{{Package|net-wireless/wpa_supplicant} }`): its argument, the words it would have shown. */
const BROKEN_TEMPLATE = /\{\{[A-Z][A-Za-z]{1,20}\|([^{}|]{1,100})\}\s+\}/g;

/** A template the wiki left open and printed, nothing after it: LibrePathology's reference
 * `{{Ref HospAuto|` (2026-10-20 audit). */
const UNCLOSED_TEMPLATE = /\{\{[A-Z][\w ]{0,30}\|\s*$/;

/** A template with named parameters the wiki printed instead of expanding: Gentoo's outdated
 * translations show `{{Talk|open|date=28 mars, 2017}}`, Energypedia's upload summaries
 * `{{Information |Description =  |Source =  }}` (2026-10-21), and the opening of a code box whose end
 * comes lines later, `{{CodeBox|title=Eksempel|lang=text|1=` (2026-10-20 audit). A named parameter
 * (`key=`) marks them: a template a page writes about has none. */
const PARAM_TEMPLATE =
  /\{\{[A-Z][A-Za-z]{1,20} ?(?:\|[^{}|\n]*)*\|\s*[A-Za-z][A-Za-z0-9_ ]{0,30}=[^{}|\n]*(?:\|[^{}|\n]*)*\}\}/g;
/** A category or file link the wiki cut short in a summary ("[[Category:MENA_(Middle_East_and_N..."):
 * Energypedia's file history, 2026-10-21 audit. */
const CUT_LINK = /\[\[(?:Category|File|Image):[^\]\n]{0,200}\.\.\.(?!\])/g;
const TEMPLATE_OPENER = /\{\{[A-Z][A-Za-z]{1,20}\|(?:[a-z0-9_]+=[^|\n{}]*\|)+(?:1=)?/g;

/** A wiki link the wiki never turned into one: `[[Colsterworth]]`, `[[Main Page|home]]` (OpenStreetMap
 * wiki, 2026-10-19 audit), shown as its words. A title starts with a capital and has no comma: a
 * chemical name (`[[2,2′-...]]`) or a list comprehension (`[[y for x in xs]]`) is not one. */
const UNLINKED = /\[\[([A-Z][^[\]|,{}]{0,80})(?:\|([^[\]|{}]{1,80}))?\]\]/g;

/** Entities escaped twice upstream, so the page shows `&quot;` (The World Factbook: "or
 * &quot;Mountain of Mercy,&quot; lies Persepolis", 2026-10-19 audit). Outside code only. */
const ESCAPED_TWICE = /&(?:quot|apos|amp|nbsp|#0*39|#0*34);/g;
const UNESCAPE: Record<string, string> = {
  '&quot;': '"',
  '&apos;': "'",
  '&#39;': "'",
  '&#039;': "'",
  '&#34;': '"',
  '&#034;': '"',
  '&amp;': '&',
  '&nbsp;': '\u00a0',
};

/** Classes of navigation-only blocks: a field's sidebar, the navboxes at the end of an article. */
const NAVIGATION_CLASSES = ['sidebar', 'vertical-navbox', 'navbox'];

function isNavigationBlock(el: Element, tag: string): boolean {
  return (
    (tag === 'table' || tag === 'div') && NAVIGATION_CLASSES.some((c) => el.classList.contains(c))
  );
}

function navigationTitle(el: Element): string {
  const head = el.querySelector('.sidebar-title, .navbox-title, caption, th');
  const text = (head?.textContent ?? '').replace(/\s+/g, ' ').trim();
  return text.length > 0 && text.length <= 80 ? text : 'Navigation';
}

export function sanitizeDocumentHtml(
  html: string,
  ctx: LinkContext,
): { fragment: DocumentFragment; stats: SanitizeStats } {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  const out = document.createDocumentFragment();
  const stats: SanitizeStats = { links: 0, broken: 0, removed: 0 };
  // Navigation blocks, folded: expanded in place, a field's sidebar filled the first screen before
  // the lead (Gibbs free energy, Arrow's theorem: 2026-10-16 field exam). Inner ones stay as they are.
  let inNavigation = 0;
  let inCode = 0;
  let emptied = 0; // text nodes that were only template leftovers

  const walk = (src: Node, dest: Node): void => {
    for (const child of Array.from(src.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE) {
        const text = child.textContent ?? '';
        const clean = inCode ? text : withoutTemplateLeftovers(text);
        if (clean !== text && !clean.trim()) emptied += 1;
        dest.appendChild(document.createTextNode(clean));
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) continue; // comments, PIs
      const el = child as Element;
      const tag = el.tagName.toLowerCase();
      if (tag === 'noframes') {
        // A frameset page's text for browsers without frames, which the parser keeps as raw text
        // (Javadoc's "Frame Alert … Non-frame version", shown as markup: 2026-10-19 audit).
        walk(new DOMParser().parseFromString(el.textContent ?? '', 'text/html').body, dest);
        continue;
      }
      if ((tag === 'frame' || tag === 'iframe') && framePage(el)) {
        // A frame is never loaded here: a link to the page it showed, when the archive holds it.
        const { address, name } = framePage(el)!;
        const p = document.createElement('p');
        const a = document.createElement('a');
        a.setAttribute('href', `#${address}`);
        a.dataset.wsp = address;
        a.textContent = name;
        p.append('Frame: ', a);
        dest.appendChild(p);
        stats.links += 1;
        continue;
      }
      if (DROP.has(tag) || HIDDEN_CLASSES.some((c) => el.classList.contains(c))) {
        stats.removed += 1;
        continue;
      }
      if (tag === 'math') {
        const math = sanitizeMath(el);
        if (math) dest.appendChild(math);
        else stats.removed += 1;
        continue;
      }
      if (inNavigation === 0 && isNavigationBlock(el, tag)) {
        const box = document.createElement('details');
        box.setAttribute(ROLE_ATTR, 'navigation');
        const summary = document.createElement('summary');
        summary.textContent = navigationTitle(el);
        box.appendChild(summary);
        inNavigation += 1;
        walk({ childNodes: [el] } as unknown as Node, box);
        inNavigation -= 1;
        dest.appendChild(box);
        continue;
      }
      if (!KEEP.has(tag)) {
        // Unwrap unknown wrappers (font, center, main, custom tags, ...), keeping an id as a place to
        // land: "Skip to content" and contents links pointed at a <main id> that was gone (2026-10-16).
        const id = el.getAttribute('id');
        if (id)
          dest.appendChild(Object.assign(document.createElement('span'), { id: anchorId(id) }));
        walk(el, dest);
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
        if (name === ROLE_ATTR) {
          // A styling hook set by the page rewrites (threads): a short word, never read aloud.
          if (/^[a-z-]{1,32}$/.test(attr.value)) copy.setAttribute(ROLE_ATTR, attr.value);
          continue;
        }
        if (!GLOBAL_ATTRS.has(name) && !allowed?.has(name)) continue;
        if (name === 'id') copy.setAttribute('id', anchorId(attr.value));
        else copy.setAttribute(name, attr.value);
      }
      if (tag === 'a') {
        const href = el.getAttribute('href');
        if (href !== null) rewriteAnchor(copy as HTMLAnchorElement, href, ctx, stats);
      }
      const code = CODE_TAGS.has(tag);
      if (code) inCode += 1;
      const emptiedBefore = emptied;
      walk(el, copy);
      if (code) inCode -= 1;
      if (
        tag === 'li' &&
        emptied > emptiedBefore &&
        !copy.textContent?.trim() &&
        !copy.querySelector('img, svg, math')
      ) {
        // A list item that held only a template leftover is not an empty bullet (Energypedia's
        // `[[Template:Pub Tag …}]]`, 2026-10-21 audit).
        stats.removed += 1;
        continue;
      }
      if (
        tag === 'a' &&
        copy.hasAttribute('href') &&
        !copy.textContent?.trim() &&
        !copy.querySelector('img')
      ) {
        // An icon-font link (`<i class="fa fa-download">`) is empty once classes go: name it by its
        // title (the Gutenberg cover page's EPUB and HTML links were invisible, 2026-10-16 field exam).
        // A page of the library with no name is named by its address; a link out of the library
        // with no name (a share button, a social icon) is left out: there is nothing to see or follow.
        const label =
          el.getAttribute('title')?.trim() ||
          el.getAttribute('aria-label')?.trim() ||
          copy.getAttribute('download') ||
          (copy.dataset.wsp && !copy.dataset.missing ? addressLabel(copy.dataset.wsp) : '');
        if (!label) {
          stats.removed += 1;
          continue;
        }
        copy.textContent = label;
      }
      dest.appendChild(copy);
    }
  };
  walk(parsed.body, out);
  // A frameset page's <noframes> follows its <frameset>, outside the body the parser gives it.
  if (parsed.body?.localName === 'frameset')
    for (const nf of Array.from(parsed.documentElement.children))
      if (nf.localName === 'noframes') walk({ childNodes: [nf] } as unknown as Node, out);
  // A wiki's blank lines are empty paragraphs (`<p><br></p>`): a run of them is one blank line, not a
  // screen of white (Energypedia: twelve in a row closing an article, 2026-10-21 audit).
  const blank = (n: Node | null): boolean =>
    n instanceof Element &&
    n.localName === 'p' &&
    !n.textContent?.trim() &&
    [...n.children].every((c) => c.localName === 'br');
  for (const p of Array.from(out.querySelectorAll('p'))) {
    let before = p.previousSibling;
    while (before?.nodeType === Node.TEXT_NODE && !before.textContent?.trim())
      before = before.previousSibling;
    if (blank(p) && blank(before)) p.remove();
  }
  // A link to a place in the page that is not there (a dropped navigation bar) is plain text.
  const ids = new Set([...out.querySelectorAll('[id]')].map((e) => e.id));
  for (const a of out.querySelectorAll<HTMLAnchorElement>('a[data-anchor]')) {
    if (!ids.has(a.dataset.anchor!)) {
      a.removeAttribute('href');
      delete a.dataset.anchor;
    }
  }
  return { fragment: out, stats };
}

/** "wsp://site/a/b_c%20d" -> "b c d": a name for a link that has none. */
function addressLabel(address: string): string {
  const tail =
    address
      .replace(/[?#].*$/, '')
      .replace(/\/+$/, '')
      .split('/')
      .pop() ?? '';
  let name = tail;
  try {
    name = decodeURIComponent(tail);
  } catch {
    /* keep it as written */
  }
  return name.replace(/[_]+/g, ' ').trim() || address;
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
  return imagePlaceholder(alt);
}

export function imagePlaceholder(alt: string): HTMLElement {
  const span = document.createElement('span');
  span.className = 'broken-image';
  span.setAttribute('role', 'img');
  span.setAttribute('aria-label', alt ? `Image not in library: ${alt}` : 'Image not in library');
  span.textContent = alt ? `[IMAGE: ${alt}]` : '[IMAGE]';
  return span;
}

const IMAGE_BUSY_TRIES = 4;

/** An image the archive answers with no picture (an HTML error page the crawl saved under a .png
 * name, a file it never got: LibreTexts, 2026-10-19 audit) becomes the placeholder of an image the
 * library does not hold, with its caption, instead of a broken-image icon. A busy library (503) is
 * not an answer: the image is asked for again after the Retry-After the library gave, a few times
 * (explain xkcd's maps were "not in library" while the lanes restarted, 2026-10-21 audit). */
export function replaceBrokenImages(root: ParentNode): void {
  for (const img of root.querySelectorAll('img')) {
    let tries = 0;
    const swap = () => img.replaceWith(imagePlaceholder(img.getAttribute('alt')?.trim() ?? ''));
    const failed = async () => {
      const url = img.currentSrc || img.src;
      let wait = 0;
      if (tries < IMAGE_BUSY_TRIES && url) {
        try {
          const res = await fetch(url, { cache: 'no-store' });
          if (res.status === 503) wait = Number(res.headers.get('Retry-After')) || 5;
          else if (res.ok) wait = 1; // it came this time: the error was the moment, not the file
        } catch {
          wait = 0; // the library itself is gone: nothing to wait for
        }
      }
      if (!img.isConnected) return; // the reader moved on
      if (!wait) {
        img.removeEventListener('error', failed);
        swap();
        return;
      }
      tries += 1;
      setTimeout(() => {
        if (!img.isConnected) return;
        const srcset = img.getAttribute('srcset');
        if (srcset !== null) img.setAttribute('srcset', srcset);
        img.setAttribute('src', img.getAttribute('src') ?? ''); // setting src loads it again
      }, Math.min(wait, 30) * 1000);
    };
    img.addEventListener('error', failed); // listening before the page is shown
  }
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
    case 'none':
      a.removeAttribute('href');
      break;
    case 'download':
      a.setAttribute('href', target.path);
      if (target.view) {
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener');
      } else a.setAttribute('download', target.name);
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
