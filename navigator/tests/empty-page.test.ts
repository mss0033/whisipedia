import { describe, expect, it } from 'vitest';
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
    expect(emptyPageNotice('<p><img src="map.png" alt=""></p>', 'A map', null)).toBeNull();
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
