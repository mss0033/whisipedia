/** Formulas written as TeX in a page's text are typeset once the page is shown, by a module loaded
 * only for such a page (`texrender.ts`, with Temml). `data-typesetting` is set meanwhile. */
export function hasTex(html: string): boolean {
  return html.includes('\\(') || html.includes('\\[');
}

export function typesetSoon(root: HTMLElement): void {
  root.dataset.typesetting = '1';
  void import('./texrender.ts')
    .then((m) => {
      root.dataset.formulas = String(m.typeset(root));
    })
    .catch(() => undefined)
    .finally(() => {
      delete root.dataset.typesetting;
    });
}
