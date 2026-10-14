import { describe, expect, it } from 'vitest';
import {
  clock,
  defaultSkinFor,
  forget,
  formatLabel,
  mediaKindOf,
  POSITIONS_KEPT,
  positions,
  readSkin,
  remember,
  resumeAt,
  unfinished,
  writeSkin,
} from '../src/core/media.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';

describe('media kinds and labels', () => {
  it('tells video from audio by media type, then by file name', () => {
    expect(mediaKindOf('video/webm')).toBe('video');
    expect(mediaKindOf('video/mp4; codecs="avc1"')).toBe('video');
    expect(mediaKindOf('audio/ogg')).toBe('audio');
    expect(mediaKindOf('application/ogg')).toBe('audio');
    expect(mediaKindOf('application/octet-stream', 'reel.mkv')).toBe('video');
    expect(mediaKindOf('application/octet-stream', 'talk.FLAC')).toBe('audio');
    expect(mediaKindOf('application/pdf', 'doc.pdf')).toBeNull();
    expect(mediaKindOf('image/png', 'map.png')).toBeNull();
  });

  it('labels formats and formats clocks', () => {
    expect(formatLabel('video/webm', 'reel.webm')).toBe('WEBM');
    expect(formatLabel('audio/x-flac')).toBe('FLAC');
    expect(formatLabel('', '')).toBe('MEDIA');
    expect(clock(0)).toBe('0:00');
    expect(clock(65)).toBe('1:05');
    expect(clock(3723)).toBe('1:02:03');
    expect(clock(Number.NaN)).toBe('0:00');
  });

  it('picks a skin from the theme unless the viewer chose one', () => {
    const storage = new SafeStorage(new MemoryStore());
    expect(defaultSkinFor('navigator96')).toBe('desk');
    expect(defaultSkinFor('mosaic93')).toBe('steel');
    expect(defaultSkinFor('encarta95')).toBe('theater');
    expect(readSkin(storage, 'navigator96')).toBe('desk');
    writeSkin(storage, 'tube');
    expect(readSkin(storage, 'navigator96')).toBe('tube');
    storage.write('media:skin', 'bogus');
    expect(readSkin(storage, 'encarta95')).toBe('theater');
  });
});

describe('playback memory', () => {
  const entry = (docId: string, at: number, duration = 600) => ({
    docId,
    address: `wsp://prelinger-archives/${docId}`,
    title: docId,
    kind: 'video' as const,
    at,
    duration,
  });

  it('remembers the newest position per document and offers resumes only mid-way', () => {
    const storage = new SafeStorage(new MemoryStore());
    remember(storage, entry('a', 120), new Date('2026-10-13T20:00:00Z'));
    remember(storage, entry('b', 5), new Date('2026-10-13T20:01:00Z')); // barely started
    remember(storage, entry('c', 590), new Date('2026-10-13T20:02:00Z')); // as good as finished
    remember(storage, entry('a', 240), new Date('2026-10-13T20:03:00Z')); // moved on
    expect(positions(storage).map((p) => [p.docId, p.at])).toEqual([
      ['a', 240],
      ['c', 590],
      ['b', 5],
    ]);
    expect(resumeAt(storage, 'a')).toBe(240);
    expect(resumeAt(storage, 'b')).toBeNull();
    expect(resumeAt(storage, 'c')).toBeNull();
    expect(resumeAt(storage, 'zzz')).toBeNull();
    expect(unfinished(storage).map((p) => p.docId)).toEqual(['a']);
    forget(storage, 'a');
    expect(unfinished(storage)).toEqual([]);
  });

  it('keeps the list short and ignores stored junk', () => {
    const storage = new SafeStorage(new MemoryStore());
    for (let i = 0; i < POSITIONS_KEPT + 5; i++) remember(storage, entry(`d${i}`, 60));
    expect(positions(storage)).toHaveLength(POSITIONS_KEPT);
    expect(positions(storage)[0]!.docId).toBe(`d${POSITIONS_KEPT + 4}`);
    storage.write('media:positions', [{ docId: 1 }]);
    expect(positions(storage)).toEqual([]);
  });
});
