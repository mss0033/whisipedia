// Every dark palette draws one-ink pictures (formula images) inverted, every light one as they are,
// and print never inverts: the token is read from the stylesheets, so a new dark theme that forgets
// it fails here (review of plan B, F2 item 6).
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const css = (name: string) =>
  readFileSync(join(process.cwd(), 'src', 'styles', name), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

/** Top-level rules of a stylesheet that set `--page-bg`: selector and declarations. */
function palettes(text: string): Array<{ selector: string; body: string }> {
  return [...text.matchAll(/([^{}]+)\{([^{}]*--page-bg:[^{}]*)\}/g)].map((m) => ({
    selector: m[1]!.trim().replace(/\s+/g, ' '),
    body: m[2]!,
  }));
}

function isDark(body: string): boolean {
  const hex = /--page-bg:\s*#([0-9a-f]{3,6})\b/i.exec(body)?.[1];
  if (!hex) throw new Error(`no hex --page-bg in: ${body.slice(0, 80)}`);
  const full = hex.length === 3 ? [...hex].map((c) => c + c).join('') : hex;
  const [r, g, b] = [0, 2, 4].map((i) => Number.parseInt(full.slice(i, i + 2), 16));
  return (0.2126 * r! + 0.7152 * g! + 0.0722 * b!) / 255 < 0.4;
}

describe('--ink-filter', () => {
  const blocks = palettes(css('themes.css'));

  it('finds the palettes', () => {
    expect(blocks.length).toBeGreaterThanOrEqual(8);
    expect(blocks.filter((b) => isDark(b.body)).length).toBeGreaterThanOrEqual(4);
  });

  it('is an inversion in every dark palette and none in the light ones', () => {
    // The night palettes of Mosaic and System 7 refine the shared night block, which sets it.
    for (const b of blocks) {
      const own = /--ink-filter:\s*([^;]+);/.exec(b.body)?.[1]?.trim();
      const refinesNight =
        /^:root\[data-dark="on"\]:where\(\[data-theme="(mosaic93|system7)"\]\)$/.test(b.selector);
      if (isDark(b.body)) {
        if (refinesNight) expect(own, b.selector).toBeUndefined();
        else expect(own, b.selector).toBe('invert(1) hue-rotate(180deg)');
      } else if (b.selector.startsWith(':root,')) expect(own, b.selector).toBe('none');
      else expect(own, b.selector).toBeUndefined(); // light themes take the default
    }
  });

  it('is what an ink picture is drawn with, on screen only', () => {
    const pages = css('pages.css');
    expect(pages).toMatch(
      /\.doc-body img\[data-wsp-role="ink"\]\s*\{\s*filter:\s*var\(--ink-filter, none\);\s*\}/,
    );
    expect(pages).toMatch(
      /@media print\s*\{\s*\.doc-body img\[data-wsp-role="ink"\]\s*\{\s*filter:\s*none;\s*\}/,
    );
    // No other rule inverts document pictures: photographs are never marked.
    expect(pages.match(/filter:\s*(?:var\(--ink-filter|invert)/g)?.length).toBe(1);
  });
});
