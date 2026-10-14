/**
 * Numbered web comics (xkcd in the library) read as comics: the hover text, which carries the last
 * joke, is printed under the strip; the site's Random link (off-site) picks a comic from the
 * library instead; the transcript is folded away; and a bar above the page goes to the previous,
 * next, a random or any numbered comic. The rewrite runs on raw HTML before the sanitiser.
 */
import { makeAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { wspLink } from '../ui/components.ts';

/** A comic page: the strip in `#comic` and previous/next links. Cheap string test first. */
export function looksLikeComic(html: string): boolean {
  return html.includes('id="comic"') && /rel="(prev|next)"/.test(html);
}

export function isComic(root: ParentNode): boolean {
  return (
    root.querySelector('#comic img') !== null &&
    root.querySelector('a[rel="prev"], a[rel="next"]') !== null
  );
}

/** Rewrite a comic page in place (before sanitising). */
export function comicView(root: ParentNode, source: string): void {
  const strip = root.querySelector('#comic');
  const img = strip?.querySelector('img');
  const hover = img?.getAttribute('title')?.trim();
  if (strip && hover) {
    // A <p> with an <i>: kept by the sanitiser, read in order by a screen reader.
    strip.after(h('p', null, h('i', null, hover)));
  }
  const random = makeAddress('random', '', { source });
  for (const a of root.querySelectorAll<HTMLAnchorElement>('a')) {
    const href = a.getAttribute('href') ?? '';
    if (/^random$/i.test(a.textContent?.trim() ?? '') && !href.startsWith('wsp:')) {
      a.setAttribute('href', random); // the sanitiser turns a wsp: address into a library link
    }
  }
  const transcript = root.querySelector('#transcript');
  if (transcript) {
    const text = transcript.textContent?.trim() ?? '';
    if (text) {
      const fold = h('details', null, h('summary', null, 'Transcript'), h('pre', null, text));
      transcript.replaceWith(fold);
    } else {
      transcript.remove();
    }
  }
}

/** The comic's number and how its siblings are addressed: `xkcd.com/936/` -> 936. */
export function comicNumber(nativeId: string): { n: number; at: (k: number) => string } | null {
  const m = /^(.*?)(\d+)(\/?)$/.exec(nativeId);
  if (!m) return null;
  const n = Number(m[2]);
  if (!Number.isSafeInteger(n) || n < 1) return null;
  return { n, at: (k: number) => `${m[1]}${k}${m[3]}` };
}

/** First, previous, random, next and a go-to-number field, for a numbered comic. */
export function comicBar(
  source: string,
  nativeId: string,
  go: (address: string) => void,
): HTMLElement | null {
  const num = comicNumber(nativeId);
  if (!num) return null;
  const here = (k: number) => makeAddress(source, num.at(k));
  const field = h('input', {
    type: 'number',
    min: '1',
    inputmode: 'numeric',
    'aria-label': 'Comic number',
    value: String(num.n),
    class: 'comic-number',
  }) as HTMLInputElement;
  const form = h(
    'form',
    { class: 'comic-go', role: 'search', 'aria-label': 'Go to a comic by number' },
    field,
    h('button', { type: 'submit' }, 'Go'),
  );
  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const k = Number(field.value);
    if (Number.isSafeInteger(k) && k >= 1) go(here(k));
  });
  return h(
    'nav',
    { class: 'comic-bar', 'aria-label': 'Comics' },
    num.n > 1
      ? [wspLink(here(1), '|‹ First'), ' · ', wspLink(here(num.n - 1), '‹ Previous')]
      : null,
    num.n > 1 ? ' · ' : null,
    wspLink(makeAddress('random', '', { source }), 'Random'),
    ' · ',
    wspLink(here(num.n + 1), 'Next ›'),
    ' · Comic #',
    form,
  );
}
