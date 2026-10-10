/**
 * Tiny hyperscript helper. Builds DOM nodes directly (never innerHTML), so
 * untrusted text is always inert and nothing violates the CSP: no inline
 * style attributes, no inline handlers.
 */

export type Child = Node | string | number | null | undefined | false | Child[];

type Handler = (ev: never) => void;
export type Attrs = {
  [key: string]: string | number | boolean | null | undefined | Handler | Record<string, string>;
};

export function append(parent: Node, ...children: Child[]): void {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(parent, ...c);
    else if (typeof c === 'string' || typeof c === 'number')
      parent.appendChild(document.createTextNode(String(c)));
    else parent.appendChild(c);
  }
}

export function setAttrs(el: Element, attrs: Attrs): void {
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'style') throw new Error('Inline styles are not allowed under the CSP');
    if (k.startsWith('on') && typeof v === 'function') {
      el.addEventListener(k.slice(2).toLowerCase(), v as EventListener);
    } else if (k === 'class') {
      el.setAttribute('class', String(v));
    } else if (k === 'dataset' && typeof v === 'object') {
      for (const [dk, dv] of Object.entries(v)) (el as HTMLElement).dataset[dk] = dv;
    } else if (v === true) {
      el.setAttribute(k, '');
    } else {
      el.setAttribute(k, String(v));
    }
  }
}

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs?: Attrs | null,
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  if (attrs) setAttrs(el, attrs);
  append(el, ...children);
  return el;
}

const SVG_NS = 'http://www.w3.org/2000/svg';

export function s(tag: string, attrs?: Attrs | null, ...children: Child[]): SVGElement {
  const el = document.createElementNS(SVG_NS, tag) as SVGElement;
  if (attrs) setAttrs(el, attrs);
  append(el, ...children);
  return el;
}

export function frag(...children: Child[]): DocumentFragment {
  const f = document.createDocumentFragment();
  append(f, ...children);
  return f;
}

export function clear(el: Element): void {
  el.replaceChildren();
}

let idCounter = 0;
export function uid(prefix = 'wsp'): string {
  idCounter += 1;
  return `${prefix}-${idCounter}`;
}

export function debounce<A extends unknown[]>(fn: (...args: A) => void, ms: number) {
  let t: ReturnType<typeof setTimeout> | undefined;
  const wrapped = (...args: A) => {
    if (t !== undefined) clearTimeout(t);
    t = setTimeout(() => {
      t = undefined;
      fn(...args);
    }, ms);
  };
  wrapped.cancel = () => {
    if (t !== undefined) clearTimeout(t);
    t = undefined;
  };
  return wrapped;
}
