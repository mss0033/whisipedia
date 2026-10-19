import { describe, expect, it } from 'vitest';
import { cutAtLineEnd, linkify } from '../src/core/text.ts';

describe('a URL a PDF line cut short (2026-10-19 audit, FEMA IS-288.a)', () => {
  it('is left as text, not linked to the article on the letter H', () => {
    const text =
      'Harvey\r\n(https://en.wikipedia.org/wiki/H\r\n2017\r\n(https://en.wikipedia.org/wiki/2 $125';
    const div = document.createElement('div');
    div.append(linkify(text));
    expect([...div.querySelectorAll('a')].map((a) => a.textContent)).toEqual([
      'https://en.wikipedia.org/wiki/2',
    ]);
    expect(
      cutAtLineEnd('https://en.wikipedia.org/wiki/H', 'https://en.wikipedia.org/wiki/H\nx', 0),
    ).toBe(true);
  });

  it('links a whole URL at a line end, and a short one inside a line', () => {
    expect(cutAtLineEnd('https://xkcd.com/936', 'https://xkcd.com/936\n', 0)).toBe(false);
    expect(cutAtLineEnd('https://xkcd.com/', 'https://xkcd.com/\n', 0)).toBe(false);
    expect(cutAtLineEnd('https://ex.org/a/b2', 'see https://ex.org/a/b2 here', 4)).toBe(false);
  });
});
