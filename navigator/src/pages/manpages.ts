/**
 * Man pages (ManKier) read as references: the site's menu (burger, search box, footer) goes, its
 * list of sections becomes a folded "Sections" box at the top of the page, and every section heading
 * gets a link back to it, so a 60-section page such as rsync(1) is two jumps from any option. The
 * rewrite runs on raw HTML before the sanitiser (which drops classes, keeps prefixed ids).
 */
import { h } from '../core/dom.ts';

export const SECTIONS_ID = 'man-sections';
const CHROME = ['.burger', '#ta', '.menu-heading', 'footer', 'script'];

/** Cheap string test first: only pages with ManKier's section menu are parsed again. */
export function looksLikeManPage(html: string): boolean {
  return html.includes('class="nav-h2"') && html.includes('<section id=');
}

export function isManPage(root: ParentNode): boolean {
  return root.querySelector('ul.nav-h2') !== null && root.querySelector('section[id] h2') !== null;
}

/** Rewrite a man page in place; returns the number of sections listed. */
export function manPageView(root: ParentNode): number {
  for (const el of root.querySelectorAll(CHROME.join(','))) el.remove();
  const list = root.querySelector('ul.nav-h2');
  const first = root.querySelector('section[id]');
  if (!list || !first) return 0;
  const count = list.querySelectorAll(':scope > li').length;
  const box = h(
    'details',
    { id: SECTIONS_ID, open: '' },
    h('summary', null, `Sections (${count})`),
    list,
  );
  first.before(box);
  for (const heading of root.querySelectorAll('section[id] > a > h2, section[id] > h2')) {
    const anchor = heading.parentElement?.tagName === 'A' ? heading.parentElement : heading;
    anchor.after(h('p', null, h('small', null, h('a', { href: `#${SECTIONS_ID}` }, '↑ Sections'))));
  }
  return count;
}
