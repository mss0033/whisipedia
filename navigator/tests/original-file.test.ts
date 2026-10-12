import { describe, expect, it } from 'vitest';
import { originalPanel } from '../src/pages/document.ts';

const base = (path: string) => `http://lib.test${path}`;

describe('original file panel', () => {
  it('frames a PDF and offers open and download', () => {
    const el = originalPanel(
      {
        title: 'Coast Pilot 1',
        kind: 'other',
        original: {
          url: '/v1/files/wsp%3Anoaa%2Fcp1',
          name: 'CP1.pdf',
          media_type: 'application/pdf',
          inline: true,
          preview_url: null,
        },
      },
      base,
    )!;
    const frame = el.querySelector('iframe')!;
    expect(frame.getAttribute('src')).toBe('http://lib.test/v1/files/wsp%3Anoaa%2Fcp1');
    expect(frame.getAttribute('title')).toContain('Coast Pilot 1');
    expect(el.textContent).toContain('PDF document');
    expect(el.querySelector('a[download="CP1.pdf"]')).not.toBeNull();
    expect(el.querySelector('a[target="_blank"]')).not.toBeNull();
  });

  it('shows a map scan through its preview', () => {
    const el = originalPanel({
      title: 'Boston South 1:24,000',
      kind: 'maps',
      original: {
        url: '/v1/files/x',
        name: 'MA_Boston_South.tif',
        media_type: 'image/tiff',
        inline: false,
        preview_url: '/v1/files/x/preview',
      },
    })!;
    expect(el.querySelector('iframe')).toBeNull();
    expect(el.querySelector('img')!.getAttribute('src')).toBe('/v1/files/x/preview');
    expect(el.querySelector('a.original-preview')!.getAttribute('href')).toBe('/v1/files/x');
    expect(el.querySelector('img')!.getAttribute('alt')).toContain('Boston South');
    expect(el.textContent).toContain('TIFF image');
  });

  it('offers only a download for other files, and nothing without one', () => {
    const el = originalPanel({
      title: 'Data',
      kind: 'data',
      original: {
        url: '/v1/files/z',
        name: 'data.zip',
        media_type: 'application/zip',
        inline: false,
        preview_url: null,
      },
    })!;
    expect(el.querySelector('a[target="_blank"]')).toBeNull();
    expect(el.textContent).toContain('download it to open it');
    expect(originalPanel({ title: 'x', kind: 'other', original: null })).toBeNull();
  });

  it('shows a map PDF as its rendered sheet, not a viewer', () => {
    const el = originalPanel({
      title: 'RI East Greenwich 1942',
      kind: 'maps',
      original: {
        url: '/v1/files/m',
        name: 'East-Greenwich.pdf',
        media_type: 'application/pdf',
        inline: true,
        preview_url: '/v1/files/m/preview',
      },
    })!;
    expect(el.querySelector('iframe')).toBeNull();
    expect(el.querySelector('img')!.getAttribute('src')).toBe('/v1/files/m/preview');
  });
});
