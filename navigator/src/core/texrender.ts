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
  return count;
}
