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
});
