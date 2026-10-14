/**
 * Question-and-answer threads (Stack Exchange ZIMs) read as threads: the site's own chrome (top
 * bars, site header, sidebars, footer) goes, the accepted answer comes first and the rest follow by
 * score, each post carries its score as a badge, and the tags search the library instead of
 * pointing at tag pages the library does not hold. Pure DOM work on the sanitised fragment.
 */
import { searchAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';

const CHROME = [
  'header',
  'nav',
  'footer',
  '#left-sidebar',
  '#sidebar',
  '.js-top-bar',
  '.kiwix-topbar-fill',
  '.site-header',
  '.js-voting-container',
  '.votecell',
  '.js-post-menu',
  '.post-menu',
  '.bottom-notice',
  '.js-share-link',
];

export interface ThreadStats {
  answers: number;
  accepted: boolean;
  tags: number;
}

/** Whether a fragment is a question page (a question post and an answers block). */
export function isThread(root: ParentNode): boolean {
  return root.querySelector('#question[data-score], .question[data-questionid]') !== null;
}

function score(el: Element): number {
  const n = Number(el.getAttribute('data-score'));
  return Number.isFinite(n) ? n : 0;
}

function accepted(el: Element): boolean {
  return (
    el.classList.contains('accepted-answer') || el.getAttribute('itemprop') === 'acceptedAnswer'
  );
}

function badge(el: Element, text: string, cls: string): void {
  // <small> with a title survives the sanitiser (classes do not); the title is the styling hook.
  const body = el.querySelector('.postcell, .answercell, .js-post-body, .post-text') ?? el;
  body.prepend(h('small', { class: `thread-badge ${cls}`, title: cls }, text), ' ');
}

/** Rewrite a thread in place and say what was done. */
export function threadView(root: ParentNode, source: string): ThreadStats {
  for (const el of root.querySelectorAll(CHROME.join(','))) el.remove();
  const question = root.querySelector<HTMLElement>('#question, .question[data-questionid]');
  if (question) badge(question, `▲ ${score(question)}`, 'thread-score');
  const answers = [
    ...root.querySelectorAll<HTMLElement>('.answer[data-answerid], .answer[data-score]'),
  ];
  answers.sort((a, b) => Number(accepted(b)) - Number(accepted(a)) || score(b) - score(a));
  const block = root.querySelector('#answers') ?? answers[0]?.parentElement ?? null;
  for (const a of answers) {
    if (accepted(a)) badge(a, '✓ accepted', 'thread-accepted');
    badge(a, `▲ ${score(a)}`, 'thread-score');
    // In the new order, after the answers header; a rule between answers (it survives the
    // sanitiser, where a class would not).
    block?.append(h('hr', null), a);
  }
  let tags = 0;
  for (const tag of root.querySelectorAll<HTMLAnchorElement>('a.post-tag, a[rel="tag"]')) {
    const name = tag.textContent?.trim() ?? '';
    if (!name) continue;
    tags++;
    const address = searchAddress(name, { source });
    tag.setAttribute('href', address); // the sanitiser turns a wsp: address into a library link
    tag.dataset.wsp = address;
    tag.classList.remove('broken-link');
    tag.title = `Search ${source} for “${name}”`;
  }
  return { answers: answers.length, accepted: answers.some(accepted), tags };
}
