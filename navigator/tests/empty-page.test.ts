import { describe, expect, it } from 'vitest';
import { sanitizeDocumentHtml } from '../src/core/links.ts';
import { emptyPageNotice } from '../src/pages/document.ts';

describe('a page empty in its archive is said to be (2026-10-19 audit)', () => {
  it('names a bot check saved in place of the page, with the address it came from', () => {
    const html = `<html><head><script src="x.token.awswaf.com/challenge.js"></script></head>
      <body><div id="challenge-container"></div><script>AwsWafIntegration.getToken()</script></body></html>`;
    const note = emptyPageNotice(
      html,
      'sfhandbook pt1',
      'https://irp.fas.org/doddir/milmed/sfhandbook-pt1.pdf',
    );
    expect(note?.textContent).toContain('bot check');
    expect(note?.textContent).toContain('https://irp.fas.org/doddir/milmed/sfhandbook-pt1.pdf');
  });

  it('names a page whose content a script loaded, and stays quiet on a page with words', () => {
    expect(
      emptyPageNotice('<h1>9.10: Exercises</h1><div>\n\t </div>', '9.10: Exercises', null)
        ?.textContent,
    ).toContain('loaded by a script');
    const words =
      '<p>Heat stroke is a medical emergency in which the body overheats past forty degrees.</p>';
    expect(emptyPageNotice(words, 'Heat stroke', null)).toBeNull();
    expect(
      emptyPageNotice('<p><img src="/v1/assets/s/g/map.png" alt=""></p>', 'A map', null),
    ).toBeNull();
    // A picture the reader does not show (a data: splash logo, Instagram's app shell) is not content.
    expect(
      emptyPageNotice(
        '<div id="splash"><img src="data:image/png;base64,iVBOR"></div>',
        'Instagram',
        null,
      )?.textContent,
    ).toContain('loaded by a script');
  });

  it('names an app shell whose only element is its loading animation (2026-10-21 audit)', () => {
    const html =
      '<div id="ember-app"><div class="gh-loading-content"><video autoplay muted>' +
      '<source src="logo-loader.mp4" type="video/mp4"></video></div></div><script src="ghost.js"></script>';
    expect(emptyPageNotice(html, 'Ghost', null)?.textContent).toContain('loaded by a script');
  });

  it('names a page that is only a canvas program', () => {
    const html = '<div class="mini"><canvas id="c"></canvas></div><script src="game.js"></script>';
    expect(emptyPageNotice(html, 'mini_checkerboard', null)?.textContent).toContain(
      'interactive program',
    );
  });
});

describe('the notice from the sanitiser’s own count: the page is not parsed twice (navigator-code-12)', () => {
  const PAGES: Array<[string, string]> = [
    [
      `<html><head><script src="x.token.awswaf.com/challenge.js"></script></head>
      <body><div id="challenge-container"></div><script>AwsWafIntegration.getToken()</script></body></html>`,
      'sfhandbook pt1',
    ],
    ['<h1>9.10: Exercises</h1><div>\n\t </div>', '9.10: Exercises'],
    [
      '<p>Heat stroke is a medical emergency in which the body overheats past forty degrees.</p>',
      'Heat stroke',
    ],
    ['<p><img src="/v1/assets/s/g/map.png" alt=""></p>', 'A map'],
    ['<div id="splash"><img src="data:image/png;base64,iVBOR"></div>', 'Instagram'],
    [
      '<div id="ember-app"><div class="gh-loading-content"><video autoplay muted>' +
        '<source src="logo-loader.mp4" type="video/mp4"></video></div></div><script src="ghost.js"></script>',
      'Ghost',
    ],
    ['<div class="mini"><canvas id="c"></canvas></div><script src="game.js"></script>', 'mini'],
    // What the sanitiser adds is not the page's text: a frame's link, a placeholder's alt text.
    [
      '<iframe src="/v1/assets/s/g/inner.html" title="The inner page of this frameset"></iframe>',
      'Frames',
    ],
    [
      '<p><img src="https://cdn.example/a.png" alt="A long description of a picture the library does not hold"></p>',
      'Pic',
    ],
    // Text the reader drops still counted, as before; a script's never.
    ['<button>Subscribe to our newsletter and never miss a single story again</button>', 'News'],
    [
      '<script>var words = "long enough to pass for a paragraph of forty characters";</script>',
      'App',
    ],
    ['<h1>Only a title that is long enough to pass for a paragraph of text</h1>', 'T'],
    ['<p>Short Title</p><p>and thirty-nine more characters, no more</p>', 'Short Title'],
    // Pretty-printed markup: its indentation is not words (34 characters here, under the 40).
    [
      '<div>\n  <p>\n    Loading\n  </p>\n  <p>\n    Please wait\n  </p>\n  <p>\n    One moment\n  </p>\n  <p>\n    Ok\n  </p>\n</div>',
      'Shell',
    ],
    // A title in two pieces is still the title.
    [
      '<p><b>Short </b><i> Title</i></p><p>and thirty-nine more characters, no more</p>',
      'Short Title',
    ],
    [
      '<noscript><p>Please enable JavaScript to view this page, it needs it badly.</p></noscript>',
      'JS',
    ],
  ];
  const ctx = { sourceId: 's', nativeId: 'n' };

  it('says the same as parsing the page did, for every kind of page', () => {
    for (const [html, title] of PAGES) {
      const parsed = emptyPageNotice(html, title, 'https://example.org/x');
      const counted = emptyPageNotice(
        html,
        title,
        'https://example.org/x',
        sanitizeDocumentHtml(html, ctx).seen,
      );
      expect(counted?.textContent ?? null, html).toBe(parsed?.textContent ?? null);
    }
  });

  it('counts the page’s own words, pictures, frames and canvases in one walk', () => {
    const seen = sanitizeDocumentHtml(
      `<h1>Title words</h1><p>Body   words <b>here</b>.</p><script>ignored()</script>
       <iframe src="/v1/assets/s/g/inner.html" title="Inner"></iframe><iframe src="https://x.example/"></iframe>
       <img src="/v1/assets/s/g/a.png" alt="held"><img src="https://x.example/b.png" alt="not held">
       <canvas></canvas><svg><text>drawn</text></svg>`,
      ctx,
    ).seen;
    expect(seen.text).toContain('Body words here.');
    expect(seen.text).not.toMatch(/Title words|ignored|Frame:|IMAGE|Inner|held/);
    expect(seen.text).toContain('drawn');
    expect(seen).toMatchObject({ images: 1, frames: 2, canvas: 1 });
  });

  it('does not call the parser again when the count is given', () => {
    const html = '<p>Heat stroke is a medical emergency in which the body overheats badly.</p>';
    const seen = sanitizeDocumentHtml(html, ctx).seen;
    const parse = DOMParser.prototype.parseFromString;
    let calls = 0;
    DOMParser.prototype.parseFromString = function (...args) {
      calls += 1;
      return parse.apply(this, args);
    };
    try {
      expect(emptyPageNotice(html, 'Heat stroke', null, seen)).toBeNull();
      expect(calls).toBe(0);
      emptyPageNotice(html, 'Heat stroke', null);
      expect(calls).toBe(1);
    } finally {
      DOMParser.prototype.parseFromString = parse;
    }
  });
});
