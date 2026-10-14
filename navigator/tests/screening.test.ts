// The Screening Room and media documents in jsdom against the mock API.
import { beforeEach, describe, expect, it } from 'vitest';
import { WspClient } from '../src/api/client.ts';
import { App } from '../src/app.ts';
import { remember } from '../src/core/media.ts';
import { MemoryStore, SafeStorage } from '../src/core/storage.ts';
import { mockFetch, until } from './helpers.ts';

function boot() {
  document.body.innerHTML = '<div id="app"></div>';
  history.replaceState(null, '', '/');
  const root = document.getElementById('app')!;
  const storage = new SafeStorage(new MemoryStore());
  const app = new App(root, { api: new WspClient({ fetch: mockFetch() }), storage });
  app.start();
  const main = () => document.querySelector('main')!;
  const settled = (pred: () => boolean) =>
    until(() => !root.classList.contains('is-loading') && pred());
  return { app, main, settled, storage };
}

describe('The Screening Room', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-theme');
  });

  it('lists the video and audio sites, what to continue, and offers the projection test', async () => {
    const { app, main, settled, storage } = boot();
    remember(storage, {
      docId: 'wsp:prelinger-archives/DuckandC1951',
      address: 'wsp://prelinger-archives/DuckandC1951',
      title: 'Duck and Cover (1951)',
      kind: 'video',
      at: 200,
      duration: 540,
    });
    app.navigate('wsp://cinema'); // an alias of the room
    await settled(() => !!main().querySelector('.page-screening'));
    expect(main().querySelector('h1')?.textContent).toBe('The Screening Room');
    const sites = [...main().querySelectorAll('.screening-sites a')].map((a) => a.textContent);
    expect(sites).toEqual(['Prelinger Archives', 'LibriVox']);
    const cont = main().querySelector('.screening-continue li')!;
    expect(cont.textContent).toContain('Duck and Cover (1951)');
    expect(cont.textContent).toContain('at 3:20 of 9:00');
    expect(main().querySelector('#screening-test')).not.toBeNull();
    cont.querySelector('button')!.click();
    expect(main().querySelector('.screening-continue')).toBeNull();
  });

  it('plays a film document in the video player and a recording in the deck', async () => {
    const { app, main, settled } = boot();
    app.navigate('wsp://prelinger-archives/DuckandC1951');
    await settled(() => !!main().querySelector('.page-document'));
    const video = main().querySelector<HTMLVideoElement>('.player.pl-video video')!;
    expect(video).not.toBeNull();
    expect(video.getAttribute('src')).toContain(
      '/v1/files/wsp%3Aprelinger-archives%2FDuckandC1951',
    );
    expect(main().querySelector('.original-media .original-links')?.textContent).toContain(
      'WEBM video',
    );
    expect(main().querySelector('iframe')).toBeNull();
    expect(main().querySelector('.badge-kind')?.textContent).toBe('Video');

    app.navigate('wsp://librivox/moby-dick-01');
    await settled(() => !!main().querySelector('.player.pl-audio'));
    expect(main().querySelector('.player.pl-audio audio')?.getAttribute('src')).toContain(
      'moby-dick-01',
    );
    expect(main().querySelector('.pl-lcd-title')?.textContent).toBe(
      'Moby Dick, chapter 1: Loomings',
    );
  });
});
