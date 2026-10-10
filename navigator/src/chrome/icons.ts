/** Chunky toolbar icons drawn as inline SVG (no image requests, CSP-safe). */
import { s } from '../core/dom.ts';

export type IconName =
  | 'back'
  | 'forward'
  | 'home'
  | 'reload'
  | 'search'
  | 'print'
  | 'stop'
  | 'bookmark';

const PATHS: Record<IconName, string[]> = {
  back: ['M14 4 L4 12 L14 20 L14 15 L21 15 L21 9 L14 9 Z'],
  forward: ['M10 4 L20 12 L10 20 L10 15 L3 15 L3 9 L10 9 Z'],
  home: ['M3 12 L12 3 L21 12 L18 12 L18 21 L14 21 L14 15 L10 15 L10 21 L6 21 L6 12 Z'],
  reload: ['M12 4 A8 8 0 1 0 20 12 L17 12 A5 5 0 1 1 12 7 L12 10 L17 5.5 L12 1 Z'],
  search: [
    'M10 3 A7 7 0 1 0 10 17 A7 7 0 1 0 10 3 Z M10 6 A4 4 0 1 1 10 14 A4 4 0 1 1 10 6 Z',
    'M14.5 15.5 L17 13 L22 18 L19.5 20.5 Z',
  ],
  print: [
    'M6 2 L18 2 L18 8 L6 8 Z',
    'M2 9 L22 9 L22 17 L18 17 L18 14 L6 14 L6 17 L2 17 Z',
    'M7 15 L17 15 L17 22 L7 22 Z',
  ],
  stop: ['M8 2 L16 2 L22 8 L22 16 L16 22 L8 22 L2 16 L2 8 Z'],
  bookmark: ['M6 2 L18 2 L18 22 L12 17 L6 22 Z'],
};

export function icon(name: IconName): SVGElement {
  return s(
    'svg',
    {
      viewBox: '0 0 24 24',
      width: 24,
      height: 24,
      class: `icon icon-${name}`,
      'aria-hidden': 'true',
      focusable: 'false',
    },
    ...PATHS[name].map((d) => s('path', { d, 'fill-rule': 'evenodd' })),
  );
}

/** The throbber: a "W" over a starfield with a passing comet. */
export function throbberSvg(): SVGElement {
  const stars: SVGElement[] = [];
  const pts: Array<[number, number]> = [
    [6, 8],
    [30, 6],
    [40, 14],
    [10, 32],
    [36, 36],
    [22, 4],
    [4, 22],
    [42, 26],
    [16, 40],
  ];
  pts.forEach(([x, y], i) => {
    stars.push(
      s('circle', { cx: x, cy: y, r: i % 3 === 0 ? 1.2 : 0.8, class: `star star-${i % 3}` }),
    );
  });
  return s(
    'svg',
    {
      viewBox: '0 0 48 48',
      width: 40,
      height: 40,
      class: 'throbber-svg',
      'aria-hidden': 'true',
      focusable: 'false',
    },
    s('rect', { x: 0, y: 0, width: 48, height: 48, class: 'throbber-sky' }),
    ...stars,
    s(
      'g',
      { class: 'comet' },
      s('line', { x1: -14, y1: -6, x2: 0, y2: 0, class: 'comet-tail' }),
      s('circle', { cx: 0, cy: 0, r: 2.2, class: 'comet-head' }),
    ),
    s('text', { x: 24, y: 33, 'text-anchor': 'middle', class: 'throbber-letter' }, 'W'),
  );
}
