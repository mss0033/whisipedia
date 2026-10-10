/**
 * Plain-text documents (RFCs, Gutenberg books): DOM rendering with
 * cross-source autolinks ("RFC 9110", "wsp:gutenberg/1342", URLs).
 */
import { makeAddress, normalizeAddress } from './address.ts';

const TOKEN_RE =
  /\b(RFC)[ -]?(\d{1,5})\b|\b(wsp:(?:\/\/)?[a-z0-9.-]+\/[^\s<>"')\]]+)|\b((?:https?|ftp):\/\/[^\s<>"')\]]+)/gi;

export function linkify(text: string): DocumentFragment {
  const f = document.createDocumentFragment();
  let pos = 0;
  for (const m of text.matchAll(TOKEN_RE)) {
    const i = m.index ?? 0;
    if (i > pos) f.appendChild(document.createTextNode(text.slice(pos, i)));
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
