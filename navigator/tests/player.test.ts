// The Screening Room's players in jsdom: chrome, state and keys around the browser's own element.
import { describe, expect, it, vi } from 'vitest';
import { audioDeck, videoPlayer } from '../src/ui/player.ts';

function stubPlayback(media: HTMLMediaElement) {
  // jsdom has no media pipeline: play/pause flip the paused flag and fire the events.
  let paused = true;
  Object.defineProperty(media, 'paused', { get: () => paused, configurable: true });
  Object.defineProperty(media, 'duration', { get: () => 120, configurable: true });
  media.play = vi.fn(async () => {
    paused = false;
    media.dispatchEvent(new Event('play'));
  });
  media.pause = vi.fn(() => {
    paused = true;
    media.dispatchEvent(new Event('pause'));
  });
}

describe('video player', () => {
  const make = (skin: 'theater' | 'tube' = 'theater') =>
    videoPlayer({
      src: 'http://lib.test/v1/files/x',
      title: 'Duck and Cover',
      mediaType: 'video/webm',
      fileName: 'duck.webm',
      skin,
      startAt: 42,
    });

  it('wraps a <video> with the file, the format badge and the chosen skin', () => {
    const { el, media } = make('tube');
    expect(media.tagName).toBe('VIDEO');
    expect(media.getAttribute('src')).toBe('http://lib.test/v1/files/x');
    expect(media.getAttribute('preload')).toBe('metadata');
    expect(el.classList.contains('sk-tube')).toBe(true);
    expect(el.querySelector('.pl-badge')?.textContent).toBe('WEBM');
    expect(el.querySelector('.pl-full')).not.toBeNull();
    expect(el.querySelector('.pl-controls select')?.querySelectorAll('option')).toHaveLength(4);
    expect(el.getAttribute('aria-label')).toContain('Duck and Cover');
  });

  it('plays, pauses, resumes at the start position and reports progress', () => {
    const { el, media } = make();
    stubPlayback(media);
    const reports: Array<[number, number]> = [];
    const player = videoPlayer({
      src: 'x',
      title: 't',
      mediaType: 'video/mp4',
      fileName: 'a.mp4',
      skin: 'desk',
      startAt: 42,
      onProgress: (at, d) => reports.push([at, d]),
    });
    stubPlayback(player.media);
    const play = el.querySelector<HTMLButtonElement>('.pl-play')!;
    play.click();
    expect(media.play).toHaveBeenCalled();
    expect(play.getAttribute('aria-label')).toBe('Pause');
    expect(el.classList.contains('is-playing')).toBe(true);
    play.click();
    expect(media.pause).toHaveBeenCalled();
    expect(play.getAttribute('aria-label')).toBe('Play');
    player.media.dispatchEvent(new Event('loadedmetadata'));
    expect(player.media.currentTime).toBe(42);
    player.el.querySelector<HTMLButtonElement>('.pl-play')!.click();
    player.el.querySelector<HTMLButtonElement>('.pl-play')!.click(); // pause reports
    expect(reports.at(-1)).toEqual([42, 120]);
    expect(player.el.querySelector('.pl-time')?.textContent).toBe('0:42 / 2:00');
  });

  it('answers the keys and switches skins from the menu', () => {
    const { el, media } = make();
    stubPlayback(media);
    media.dispatchEvent(new Event('loadedmetadata'));
    const key = (k: string) =>
      el.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
    key('l');
    expect(media.currentTime).toBe(52);
    key('ArrowLeft');
    expect(media.currentTime).toBe(47);
    key('5');
    expect(media.currentTime).toBe(60);
    key('m');
    expect(media.muted).toBe(true);
    key(' ');
    expect(media.play).toHaveBeenCalled();
    // Space on a focused button is that button's: Mute, not Play/Pause.
    const plays = vi.mocked(media.play).mock.calls.length;
    el.querySelector<HTMLButtonElement>('.pl-controls button')!.dispatchEvent(
      new KeyboardEvent('keydown', { key: ' ', bubbles: true }),
    );
    expect(vi.mocked(media.play).mock.calls.length).toBe(plays);
    key('k');
    const menu = el.querySelector<HTMLSelectElement>('.pl-skin')!;
    menu.value = 'steel';
    menu.dispatchEvent(new Event('change'));
    expect(el.classList.contains('sk-steel')).toBe(true);
    expect(el.classList.contains('sk-theater')).toBe(false);
  });

  it('explains a file the browser cannot play and offers the download', () => {
    const { el, media } = videoPlayer({
      src: 'x',
      title: 't',
      mediaType: 'video/x-msvideo',
      fileName: 'old.avi',
      skin: 'theater',
      downloadHref: 'http://lib.test/v1/files/old',
    });
    media.dispatchEvent(new Event('error'));
    const notice = el.querySelector<HTMLElement>('.pl-notice')!;
    expect(notice.hidden).toBe(false);
    expect(notice.textContent).toContain('cannot play this file');
    expect(notice.querySelector('a[download="old.avi"]')?.getAttribute('href')).toBe(
      'http://lib.test/v1/files/old',
    );
  });
});

describe('audio deck', () => {
  it('wraps an <audio> with the LCD and spectrum and no fullscreen button', () => {
    const { el, media } = audioDeck({
      src: 'http://lib.test/v1/files/a',
      title: 'Loomings',
      mediaType: 'audio/ogg',
      fileName: 'ch1.ogg',
      skin: 'theater',
    });
    expect(media.tagName).toBe('AUDIO');
    expect(el.querySelector('.pl-lcd-title')?.textContent).toBe('Loomings');
    expect(el.querySelector('.pl-lcd-time')?.textContent).toBe('0:00');
    expect(el.querySelector('canvas.pl-scope')).not.toBeNull();
    expect(el.querySelector('.pl-full')).toBeNull();
    expect(el.querySelector('.pl-badge')?.textContent).toBe('OGG');
  });
});
