import { describe, expect, it } from 'vitest';
import { versionsNotice } from '../src/pages/document.ts';

describe('earlier versions', () => {
  it('points a page with history to its Time Machine, opened by the address fragment', () => {
    const el = versionsNotice({ has_history: true }, 'wsp://wikipedia-top10/en/Tide', {
      at: undefined,
    })!;
    expect(el.textContent).toContain('This page has earlier versions');
    expect(el.querySelector('a')?.getAttribute('href')).toBe(
      '#wsp://wikipedia-top10/en/Tide#time-machine',
    );
  });

  it('says nothing for a single version, or while viewing an older one', () => {
    expect(versionsNotice({ has_history: false }, 'wsp://x/y', { at: undefined })).toBeNull();
    expect(versionsNotice({ has_history: true }, 'wsp://x/y', { at: '2026-01' })).toBeNull();
  });
});
