/**
 * TeX in an archived page, as MathML (loaded only for a page that has some: see `typesetSoon`).
 *
 * LibreTexts writes its formulas as `\( … \)` and `\[ … \]` for MathJax to typeset in the browser; no
 * page script runs here, so a Mathematics or Physics page showed about a hundred raw formulas
 * (2026-10-19 audit). Temml turns each into MathML, which the browser draws itself (no fonts, styles
 * or scripts of its own), and the result goes through the same MathML allowlist as a page's own.
 */
import temml from 'temml';
import { sanitizeMath } from './links.ts';

// LibreTexts' own macro: the exercise number its page script filled in (`Exercise \(\PageIndex{55}\)`).
const MACROS: Record<string, string> = { '\\PageIndex': '#1' };
const SKIP = new Set(['pre', 'code', 'kbd', 'samp', 'textarea', 'math', 'script', 'style']);
const DELIMITED = /\\\(([\s\S]+?)\\\)|\\\[([\s\S]+?)\\\]/g;
const MAX_TEX = 4000; // characters of one formula: longer is not a formula

function skipped(t: Text, root: Element): boolean {
  for (let e = t.parentElement; e && e !== root; e = e.parentElement)
    if (SKIP.has(e.localName)) return true;
  return false;
}

/** One formula as sanitized MathML, or null when it is not TeX Temml reads (left as written). */
export function texToMathML(tex: string, display: boolean): Element | null {
  if (!tex.trim() || tex.length > MAX_TEX) return null;
  try {
    const markup = temml.renderToString(tex, {
      displayMode: display,
      macros: { ...MACROS },
      throwOnError: true,
      trust: false,
    });
    // Without its style attributes: the allowlist drops them anyway, and Chrome reports each one as a
    // CSP violation while merely parsing (E2E-1).
    const unstyled = markup.replace(/\sstyle="[^"]*"/g, '');
    const math = new DOMParser().parseFromString(unstyled, 'text/html').querySelector('math');
    const clean = math ? sanitizeMath(math) : null;
    clean?.setAttribute('alttext', tex.trim());
    return clean;
  } catch {
    return null;
  }
}

// Inline elements a formula may run through: LibreTexts wrapped part of one in a <span> (2026-10-19).
const INLINE = new Set([
  'span',
  'em',
  'i',
  'b',
  'strong',
  'sub',
  'sup',
  'font',
  'small',
  'big',
  'u',
]);
const MAX_RUN = 20; // siblings one formula may span

function plainInline(n: ChildNode): boolean {
  if (n.nodeType === Node.TEXT_NODE) return true;
  if (n.nodeType !== Node.ELEMENT_NODE) return false;
  const el = n as Element;
  return INLINE.has(el.localName) && [...el.children].every((c) => plainInline(c));
}

/** A formula whose text runs through inline elements (`\[ … <span>…</span> … \]`): its opening text
 * node, the siblings up to the one that closes it, joined and typeset as one. */
function typesetAcross(root: Element): number {
  let count = 0;
  const opens: Text[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n as Text;
    if (/\\[([]/.test(t.data) && !skipped(t, root)) opens.push(t);
  }
  for (const first of opens) {
    if (!root.contains(first)) continue; // swallowed by an earlier formula
    const at = Math.max(first.data.lastIndexOf('\\('), first.data.lastIndexOf('\\['));
    if (at < 0) continue;
    const display = first.data[at + 1] === '[';
    const close = display ? '\\]' : '\\)';
    if (first.data.indexOf(close, at) >= 0) continue; // closed in its own node: the first pass's
    const run: ChildNode[] = [];
    let tex = first.data.slice(at + 2);
    let end: Text | null = null;
    for (let n = first.nextSibling; n && run.length < MAX_RUN; n = n.nextSibling) {
      if (!plainInline(n)) break;
      const text = n.textContent ?? '';
      const stop = n.nodeType === Node.TEXT_NODE ? text.indexOf(close) : -1;
      if (stop >= 0) {
        tex += text.slice(0, stop);
        end = n as Text;
        break;
      }
      tex += text;
      run.push(n);
    }
    if (!end) continue;
    const math = texToMathML(tex, display);
    if (!math) continue;
    if (display) math.setAttribute('display', 'block');
    const stop = end.data.indexOf(close);
    end.data = end.data.slice(stop + 2);
    for (const n of run) n.remove();
    first.data = first.data.slice(0, at);
    first.after(math);
    count += 1;
  }
  return count;
}

/** Typeset every delimited formula in `root`'s text (not in code); returns how many. */
export function typeset(root: Element): number {
  const texts: Text[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n as Text;
    if ((t.data.includes('\\(') || t.data.includes('\\[')) && !skipped(t, root)) texts.push(t);
  }
  let count = 0;
  for (const t of texts) {
    const text = t.data;
    const out = document.createDocumentFragment();
    let at = 0;
    for (const m of text.matchAll(DELIMITED)) {
      const display = m[2] !== undefined;
      const math = texToMathML(m[1] ?? m[2] ?? '', display);
      if (!math) continue;
      out.append(text.slice(at, m.index));
      if (display) math.setAttribute('display', 'block');
      out.append(math);
      count += 1;
      at = m.index + m[0].length;
    }
    if (at === 0) continue;
    out.append(text.slice(at));
    t.replaceWith(out);
  }
  return count + typesetAcross(root);
}
