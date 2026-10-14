/**
 * wsp://screening-room — the library's video and audio: what to continue, which sites hold media,
 * and a projection test that builds a short reel in the browser and plays it through the real
 * players, so the room can be tried before any film is on the shelves.
 */
import { makeAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { formatDate } from '../core/format.ts';
import { clock, forget, readSkin, type SkinId, unfinished, writeSkin } from '../core/media.ts';
import { errorPage, pageHeading, sourceSiteLink, wspLink } from '../ui/components.ts';
import { audioDeck, type Player, videoPlayer } from '../ui/player.ts';
import type { AppServices, PageContext, PageResult } from './types.ts';

export const MEDIA_KINDS = ['video', 'audio'] as const;

export async function render(ctx: PageContext): Promise<PageResult> {
  const sources = await ctx.app.sources();
  const video = sources.filter((s) => s.kind === 'video');
  const audio = sources.filter((s) => s.kind === 'audio');
  const body = h(
    'div',
    { class: 'page page-screening' },
    pageHeading('The Screening Room'),
    h(
      'p',
      null,
      'Film, lectures, recorded radio and music the library holds, played in period players with range-request streaming from the vault. Nothing is fetched from outside; what you see came in through the same review desk as everything else.',
    ),
    continueList(ctx),
    sites(
      'Video',
      video,
      'No film or lecture collections are on the shelves yet; the programme (archival film, NASA, university lectures) is planned and waits for the owner’s go.',
    ),
    sites(
      'Audio',
      audio,
      'No audiobook, radio or music collections are on the shelves yet; the programme (LibriVox, old-time radio, public-domain recordings) is planned and waits for the owner’s go.',
    ),
    projectionTest(ctx.app),
  );
  return { title: 'The Screening Room', body };
}

function continueList(ctx: PageContext): HTMLElement | null {
  const items = unfinished(ctx.app.storage);
  if (items.length === 0) return null;
  const list = h('ul', { class: 'screening-continue' });
  for (const p of items) {
    const row = h(
      'li',
      null,
      wspLink(p.address, p.title),
      ` · ${p.kind} · at ${clock(p.at)}${p.duration ? ` of ${clock(p.duration)}` : ''} · ${formatDate(p.updated)} `,
      h(
        'button',
        { type: 'button', class: 'linkish', 'aria-label': `Forget ${p.title}` },
        'forget',
      ),
    );
    row.querySelector('button')!.addEventListener('click', () => {
      forget(ctx.app.storage, p.docId);
      row.remove();
      if (!list.children.length) list.parentElement?.remove();
    });
    list.append(row);
  }
  return h(
    'section',
    { 'aria-labelledby': 'screening-continue' },
    h('h2', { id: 'screening-continue' }, 'Continue'),
    list,
  );
}

function sites(
  label: string,
  list: Array<Parameters<typeof sourceSiteLink>[0]>,
  empty: string,
): HTMLElement {
  return h(
    'section',
    null,
    h('h2', null, label),
    list.length
      ? h(
          'ul',
          { class: 'screening-sites' },
          list.map((s) => h('li', null, sourceSiteLink(s), ' — ', s.description)),
        )
      : h(
          'p',
          { class: 'notice' },
          empty,
          ' ',
          wspLink(makeAddress('directory'), 'See what the library holds'),
          '.',
        ),
  );
}

/* ------------------------------ projection test ------------------------------ */

const REEL_SECONDS = 8;

/**
 * A test reel made here: colour bars and a countdown drawn on a canvas, a tone from Web Audio,
 * recorded with the browser's own recorder into a WebM the players stream like any file.
 */
function projectionTest(app: AppServices): HTMLElement {
  const stage = h('div', { class: 'screening-stage' });
  const status = h('p', { class: 'fineprint', 'aria-live': 'polite' });
  const run = h('button', { type: 'button', class: 'btn-like' }, 'Run the projection test');
  const section = h(
    'section',
    { 'aria-labelledby': 'screening-test' },
    h('h2', { id: 'screening-test' }, 'Projection test'),
    h(
      'p',
      null,
      `Builds an ${REEL_SECONDS}-second reel in this browser (colour bars, a countdown and a test tone), then plays it through the video player and the audio deck exactly as a file from the vault would play. Nothing is downloaded.`,
    ),
    h('p', null, run),
    status,
    stage,
  );
  run.addEventListener('click', async () => {
    run.disabled = true;
    status.textContent = 'Recording the reel…';
    try {
      const { video, audio } = await makeReel();
      stage.replaceChildren();
      const skin = readSkin(app.storage, app.theme.current.theme);
      const mount = (p: Player, note: string) => {
        stage.append(
          h('div', { class: 'screening-player' }, p.el, h('p', { class: 'fineprint' }, note)),
        );
      };
      const onSkin = (s: SkinId) => writeSkin(app.storage, s);
      mount(
        videoPlayer({
          src: video.url,
          title: 'Test reel (video)',
          mediaType: video.type,
          fileName: 'test-reel.webm',
          skin,
          onSkin,
        }),
        `Video: ${video.type}, ${Math.round(video.bytes / 1024)} KB.`,
      );
      mount(
        audioDeck({
          src: audio.url,
          title: 'Test tone (audio)',
          mediaType: audio.type,
          fileName: 'test-tone.webm',
          skin,
          onSkin,
        }),
        `Audio: ${audio.type}, ${Math.round(audio.bytes / 1024)} KB.`,
      );
      status.textContent =
        'Reel ready. Press play on either player; the skin menu changes the chrome.';
    } catch (err) {
      status.replaceChildren(
        errorPage({
          title: 'Projection test',
          heading: 'The projector could not be loaded',
          message: err instanceof Error ? err.message : String(err),
          address: makeAddress('screening-room'),
          suggestions: [],
        }),
      );
    } finally {
      run.disabled = false;
    }
  });
  return section;
}

interface Reel {
  url: string;
  type: string;
  bytes: number;
}

async function makeReel(): Promise<{ video: Reel; audio: Reel }> {
  const Recorder = (globalThis as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder;
  const Ctx = (globalThis as { AudioContext?: typeof AudioContext }).AudioContext;
  const canvas = h('canvas', { width: '640', height: '480' });
  const g = canvas.getContext('2d');
  if (!Recorder || !Ctx || !g || typeof canvas.captureStream !== 'function') {
    throw new Error(
      'This browser cannot record a test reel (no MediaRecorder, Web Audio or canvas capture).',
    );
  }
  const ctx = new Ctx();
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  gain.gain.value = 0.2;
  const dest = ctx.createMediaStreamDestination();
  osc.connect(gain).connect(dest);
  osc.frequency.value = 440;
  osc.start();
  const videoStream = canvas.captureStream(30);
  const tracks = [...videoStream.getVideoTracks(), ...dest.stream.getAudioTracks()];
  const videoRec = record(Recorder, new MediaStream(tracks), [
    'video/webm;codecs=vp9,opus',
    'video/webm;codecs=vp8,opus',
    'video/webm',
  ]);
  const audioRec = record(Recorder, dest.stream, ['audio/webm;codecs=opus', 'audio/webm']);
  const start = performance.now();
  await new Promise<void>((resolve) => {
    const frame = () => {
      const t = (performance.now() - start) / 1000;
      drawFrame(g, canvas.width, canvas.height, t);
      osc.frequency.value = t < REEL_SECONDS / 2 ? 440 : 880;
      if (t < REEL_SECONDS) requestAnimationFrame(frame);
      else resolve();
    };
    frame();
  });
  const [video, audio] = await Promise.all([videoRec.stop(), audioRec.stop()]);
  osc.stop();
  await ctx.close();
  return { video, audio };
}

function record(
  Recorder: typeof MediaRecorder,
  stream: MediaStream,
  types: string[],
): { stop: () => Promise<Reel> } {
  const type = types.find((t) => Recorder.isTypeSupported?.(t)) ?? '';
  const rec = type ? new Recorder(stream, { mimeType: type }) : new Recorder(stream);
  const chunks: Blob[] = [];
  rec.addEventListener('dataavailable', (e) => {
    if (e.data.size) chunks.push(e.data);
  });
  rec.start(250);
  return {
    stop: () =>
      new Promise<Reel>((resolve) => {
        rec.addEventListener('stop', () => {
          const blob = new Blob(chunks, { type: rec.mimeType || type || 'video/webm' });
          resolve({ url: URL.createObjectURL(blob), type: blob.type, bytes: blob.size });
        });
        rec.stop();
      }),
  };
}

const BARS = ['#c0c0c0', '#c0c000', '#00c0c0', '#00c000', '#c000c0', '#c00000', '#0000c0'];

function drawFrame(g: CanvasRenderingContext2D, W: number, H: number, t: number): void {
  g.fillStyle = '#000';
  g.fillRect(0, 0, W, H);
  if (t < REEL_SECONDS / 2) {
    BARS.forEach((col, i) => {
      g.fillStyle = col;
      g.fillRect((i * W) / 7, 0, W / 7 + 1, H * 0.7);
    });
  } else {
    g.fillStyle = '#d8cfbf';
    g.fillRect(0, 0, W, H);
    g.strokeStyle = '#222';
    g.lineWidth = 4;
    g.beginPath();
    g.arc(W / 2, H / 2, H * 0.38, 0, Math.PI * 2);
    g.stroke();
    g.fillStyle = '#111';
    g.font = `bold ${Math.round(H * 0.4)}px Georgia, serif`;
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText(String(Math.max(1, Math.ceil(REEL_SECONDS - t))), W / 2, H / 2 + 4);
  }
  g.fillStyle = '#000a';
  g.fillRect(0, H - 40, W, 40);
  g.fillStyle = '#f3ead9';
  g.font = '18px monospace';
  g.textAlign = 'left';
  g.textBaseline = 'middle';
  g.fillText('WHISIPEDIA · TEST REEL', 14, H - 20);
  g.textAlign = 'right';
  g.fillText(clock(t), W - 14, H - 20);
}
