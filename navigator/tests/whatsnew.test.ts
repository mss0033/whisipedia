import { describe, expect, it } from 'vitest';
import type { Change, DocumentResponse } from '../src/api/types.ts';
import { replayStubNotice } from '../src/pages/document.ts';
import { groupByUpdate, updateHeading } from '../src/pages/whatsnew.ts';

const change = (source_id: string, generation_id: string, added_at: string | null): Change =>
  ({
    doc_id: `wsp:${source_id}/x`,
    kind: 'added',
    generation_id,
    source_id,
    title: 'x',
    address: `wsp://${source_id}/x`,
    added_at,
  }) as Change;

describe("What's New", () => {
  it('groups by site edition, not by edition id alone', () => {
    const items = [
      change('a', '2024-06', null),
      change('b', '2024-06', null),
      change('a', '2024-06', null),
    ];
    const groups = [...groupByUpdate(items).values()];
    expect(groups.map((g) => g.length)).toEqual([2, 1]);
  });

  it('dates each update by when the library took it in', () => {
    const c = change('kiwix-booksdash', '2024-06', '2026-10-11T03:00:00Z');
    expect(updateHeading(c, 'Bookdash')).toMatch(/^Bookdash · edition 2024-06 · added .*2026/);
    expect(updateHeading(change('a', '2024-06', null), 'A')).toBe('A · edition 2024-06');
  });

  it('explains an uncaptured video player page', () => {
    const stub = {
      native_id: 'youtube.fuzzy.replayweb.page/embed/w4mlQoAVurk',
    } as DocumentResponse;
    expect(replayStubNotice(stub)?.textContent).toContain('video itself is not in the library');
    expect(replayStubNotice({ native_id: 'A/Video' } as DocumentResponse)).toBeNull();
  });
});
