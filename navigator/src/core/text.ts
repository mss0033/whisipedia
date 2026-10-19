/**
 * Plain-text documents (RFCs, Gutenberg books): DOM rendering with
 * cross-source autolinks ("RFC 9110", "wsp:gutenberg/1342", URLs).
 */

import { isReadableHost, makeAddress, normalizeAddress } from './address.ts';
import { interwikiAddress } from './interwiki.ts';

const TOKEN_RE =
  /\b(RFC)[ -]?(\d{1,5})\b|\b(wsp:(?:\/\/)?[a-z0-9.-]+\/[^\s<>"')\]]+)|\b((?:https?|ftp):\/\/[^\s<>"')\]]+)/gi;

/** A URL a PDF's line (or table cell) cut short: it ends at the line's end on a path part of one or two
 * characters. FEMA's IS-288.a cites https://en.wikipedia.org/wiki/H|urricane_Harvey across a page
 * break, and the cut link opened the article on the letter H (2026-10-19 audit). Left as text. */
export function cutAtLineEnd(url: string, text: string, at: number): boolean {
  const after = text.slice(at + url.length, at + url.length + 2);
  if (!/^\r?\n/.test(after)) return false;
  const last = url.replace(/\/+$/, '').split('/').pop() ?? '';
  return url.split('/').length > 3 && last.length > 0 && last.length <= 2;
}

export function linkify(text: string): DocumentFragment {
  const f = document.createDocumentFragment();
  let pos = 0;
  for (const m of text.matchAll(TOKEN_RE)) {
    const i = m.index ?? 0;
    if (i > pos) f.appendChild(document.createTextNode(text.slice(pos, i)));
    if ((m[2] && !isReadableHost('rfc')) || (m[4] && cutAtLineEnd(m[4], text, i))) {
      // The library holds no RFCs: plain text, not a 404. A URL cut by the line: plain text too.
      f.appendChild(document.createTextNode(m[0]));
      pos = i + m[0].length;
      continue;
    }
    const a = document.createElement('a');
    a.textContent = m[0];
    if (m[2]) {
      const addr = makeAddress('rfc', String(Number(m[2])));
      a.href = `#${addr}`;
      a.dataset.wsp = addr;
    } else if (m[3]) {
      const addr = normalizeAddress(m[3]) ?? makeAddress('offline', '', { url: m[3] });
      a.href = `#${addr}`;
      a.dataset.wsp = addr;
    } else if (interwikiAddress(m[4] ?? m[0])) {
      const addr = interwikiAddress(m[4] ?? m[0]) as string; // a Wikimedia page the library holds
      a.href = `#${addr}`;
      a.dataset.wsp = addr;
    } else {
      const addr = makeAddress('offline', '', { url: m[4] ?? m[0] });
      a.href = `#${addr}`;
      a.dataset.wsp = addr;
      a.dataset.missing = m[0];
      a.className = 'broken-link';
      a.title = `Not in library: ${m[0]}`;
    }
    f.appendChild(a);
    pos = i + m[0].length;
  }
  if (pos < text.length) f.appendChild(document.createTextNode(text.slice(pos)));
  return f;
}

/** Fixed-width sources keep their layout; prose is reflowed into paragraphs. */
export function renderPlainText(text: string, preformatted: boolean): HTMLElement {
  if (preformatted) {
    const pre = document.createElement('pre');
    pre.className = 'doc-text';
    pre.appendChild(linkify(text));
    return pre;
  }
  const div = document.createElement('div');
  div.className = 'doc-prose';
  for (const para of text.split(/\n\s*\n/)) {
    const t = para.replace(/\s*\n\s*/g, ' ').trim();
    if (!t) continue;
    const isHeading = t.length < 60 && t === t.toUpperCase() && /[A-Z]/.test(t);
    const el = document.createElement(isHeading ? 'h2' : 'p');
    el.appendChild(linkify(t));
    div.appendChild(el);
  }
  return div;
}
