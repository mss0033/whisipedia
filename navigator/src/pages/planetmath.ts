/**
 * PlanetMath pages (LaTeXML output with NNexus auto-links) print each link's target as visible
 * typewriter text ("a unique http://planetmath.org/node/2777fixed point") and follow auto-linked
 * terms with a cluster of favicon links to guessed concepts, often the wrong ones (field exam
 * 2026-10-14). The URL text goes, the guessed-concept cluster goes, and the term stays as text.
 */
const URL_ONLY = /^\s*https?:\/\/(?:www\.)?planetmath\.org\/\S*\s*$/;

export function looksLikePlanetMath(html: string): boolean {
  return html.includes('nnexus_concept') || html.includes('planetmath.org/node/');
}

/** Clean in place; returns how many pieces were removed. */
export function planetMathView(root: ParentNode): number {
  let removed = 0;
  for (const span of root.querySelectorAll('span.ltx_font_typewriter')) {
    if (URL_ONLY.test(span.textContent ?? '')) {
      span.remove();
      removed++;
    }
  }
  for (const sup of root.querySelectorAll('sup')) {
    if (sup.querySelector('a.nnexus_concept') && !sup.textContent?.trim()) {
      sup.remove();
      removed++;
    }
  }
  for (const a of root.querySelectorAll('a.nnexus_concepts')) {
    a.replaceWith(a.ownerDocument.createTextNode(a.textContent ?? ''));
    removed++;
  }
  return removed;
}
