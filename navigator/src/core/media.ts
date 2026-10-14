/**
 * The Screening Room's pure parts: which original files are playable media, player skins and the
 * viewer's own playback memory (resume positions). Rendering lives in ui/player.ts and
 * pages/screening.ts.
 */
import type { SafeStorage } from './storage.ts';
import type { ThemeId } from './theme.ts';

export type MediaKind = 'video' | 'audio';

const VIDEO_EXT = new Set(['mp4', 'm4v', 'webm', 'mkv', 'ogv', 'mov', 'avi', 'mpg', 'mpeg']);
const AUDIO_EXT = new Set([
  'mp3',
  'ogg',
  'oga',
  'opus',
  'flac',
  'wav',
  'm4a',
  'aac',
  'wma',
  'aiff',
]);

/** Whether an original file is video or audio, by media type first and file name second. */
export function mediaKindOf(mediaType: string, name = ''): MediaKind | null {
  const type = mediaType.toLowerCase().split(';')[0]!.trim();
  if (type.startsWith('video/')) return 'video';
  if (type.startsWith('audio/')) return 'audio';
  if (type === 'application/ogg') return 'audio';
  const ext = name.includes('.') ? name.slice(name.lastIndexOf('.') + 1).toLowerCase() : '';
  if (VIDEO_EXT.has(ext)) return 'video';
  if (AUDIO_EXT.has(ext)) return 'audio';
  return null;
}

/** A short label for the format: "WEBM", "MP3", "OGG". */
export function formatLabel(mediaType: string, name = ''): string {
  const ext = name.includes('.') ? name.slice(name.lastIndexOf('.') + 1) : '';
  if (ext) return ext.toUpperCase();
  const sub = mediaType.split(';')[0]!.split('/')[1] ?? '';
  return (sub.replace(/^x-/, '') || 'media').toUpperCase();
}

/** 0:05, 12:34, 1:02:03. */
export function clock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const t = Math.floor(seconds);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return `${h ? `${h}:` : ''}${mm}:${String(s).padStart(2, '0')}`;
}

/* ----------------------------------- skins ----------------------------------- */

export const SKINS = [
  { id: 'theater', name: 'Theater 98', hint: 'Bevelled charcoal, gold glyphs' },
  { id: 'steel', name: 'Brushed Steel', hint: 'Metal case, lozenge buttons' },
  { id: 'desk', name: 'Desktop 95', hint: 'Grey bevels, title bar, LED timecode' },
  { id: 'tube', name: 'Tube 2008', hint: 'Glossy grey bar, red progress, round play button' },
] as const;
export type SkinId = (typeof SKINS)[number]['id'];

export function isSkin(v: unknown): v is SkinId {
  return typeof v === 'string' && SKINS.some((s) => s.id === v);
}

/** The skin a theme suggests when the viewer has not chosen one. */
export function defaultSkinFor(theme: ThemeId | string): SkinId {
  switch (theme) {
    case 'navigator96':
    case 'system7':
      return 'desk';
    case 'mosaic93':
      return 'steel';
    default:
      return 'theater';
  }
}

const SKIN_KEY = 'media:skin';

export function readSkin(storage: SafeStorage, theme: string): SkinId {
  return storage.read<SkinId>(SKIN_KEY, defaultSkinFor(theme), isSkin);
}

export function writeSkin(storage: SafeStorage, skin: SkinId): void {
  storage.write(SKIN_KEY, skin);
}

/* -------------------------------- resume positions -------------------------------- */

export interface Position {
  docId: string;
  address: string;
  title: string;
  kind: MediaKind;
  /** Seconds into the file, and its length when known. */
  at: number;
  duration: number;
  /** ISO time of the last update. */
  updated: string;
}

const POSITIONS_KEY = 'media:positions';
export const POSITIONS_KEPT = 24;
/** Beyond this share of the file a viewing counts as finished: no resume offered. */
const FINISHED = 0.95;
/** Under this many seconds nothing is worth resuming. */
const TOO_EARLY = 10;

function isPosition(v: unknown): v is Position {
  if (typeof v !== 'object' || v === null) return false;
  const p = v as Record<string, unknown>;
  return (
    typeof p.docId === 'string' &&
    typeof p.address === 'string' &&
    typeof p.title === 'string' &&
    (p.kind === 'video' || p.kind === 'audio') &&
    typeof p.at === 'number' &&
    typeof p.duration === 'number' &&
    typeof p.updated === 'string'
  );
}

function isPositions(v: unknown): v is Position[] {
  return Array.isArray(v) && v.every(isPosition);
}

/** Every remembered position, newest first. */
export function positions(storage: SafeStorage): Position[] {
  return storage.read<Position[]>(POSITIONS_KEY, [], isPositions);
}

/** Where to resume a document, or null when it was never played, barely started or finished. */
export function resumeAt(storage: SafeStorage, docId: string): number | null {
  const p = positions(storage).find((x) => x.docId === docId);
  if (!p || p.at < TOO_EARLY) return null;
  if (p.duration > 0 && p.at / p.duration >= FINISHED) return null;
  return p.at;
}

/** Remember a position; the newest entries win and the list stays short. */
export function remember(
  storage: SafeStorage,
  position: Omit<Position, 'updated'>,
  now = new Date(),
): void {
  const rest = positions(storage).filter((x) => x.docId !== position.docId);
  storage.write(
    POSITIONS_KEY,
    [{ ...position, updated: now.toISOString() }, ...rest].slice(0, POSITIONS_KEPT),
  );
}

/** Positions worth offering to continue: started, not finished, newest first. */
export function unfinished(storage: SafeStorage): Position[] {
  return positions(storage).filter(
    (p) => p.at >= TOO_EARLY && !(p.duration > 0 && p.at / p.duration >= FINISHED),
  );
}

export function forget(storage: SafeStorage, docId: string): void {
  storage.write(
    POSITIONS_KEY,
    positions(storage).filter((x) => x.docId !== docId),
  );
}
