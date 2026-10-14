/**
 * The Screening Room's players: a video player in four period skins and an audio deck with a live
 * spectrum. Both wrap the browser's own <video> and <audio> (the file streams from the library's
 * original-file route with range requests); the chrome is ours. No scripts from elsewhere, no
 * analytics, nothing generated: the library plays what it holds.
 *
 * Keys on a focused player: Space or K play/pause, J and L skip 10 s, arrows seek 5 s and change the
 * volume, M mutes, F fills the screen (video), 0-9 jump to a tenth of the file.
 */
import { h } from '../core/dom.ts';
import { clock, formatLabel, type MediaKind, SKINS, type SkinId } from '../core/media.ts';

export interface PlayerOptions {
  /** Where the file streams from (the original-file route, or a blob for the projection test). */
  src: string;
  title: string;
  mediaType: string;
  fileName: string;
  /** Resume here when given (seconds). */
  startAt?: number | undefined;
  skin: SkinId;
  /** Called when the viewer picks another skin from the player's menu. */
  onSkin?: ((skin: SkinId) => void) | undefined;
  /** Called every few seconds of playback and on pause with the position and length. */
  onProgress?: ((at: number, duration: number) => void) | undefined;
  /** A download link for files the browser cannot play. */
  downloadHref?: string | undefined;
  /** Captions and chapters, when the library has them. */
  tracks?:
    | Array<{ src: string; label: string; lang: string; kind?: 'captions' | 'chapters' }>
    | undefined;
}

export interface Player {
  el: HTMLElement;
  media: HTMLMediaElement;
}

const SKIP = 10;
const STEP = 5;
const PROGRESS_EVERY = 5; // seconds between onProgress calls while playing

export function videoPlayer(opts: PlayerOptions): Player {
  return build('video', opts);
}

export function audioDeck(opts: PlayerOptions): Player {
  return build('audio', opts);
}

function build(kind: MediaKind, opts: PlayerOptions): Player {
  const media = (kind === 'video' ? h('video') : h('audio')) as HTMLMediaElement;
  media.preload = 'metadata';
  media.src = opts.src;
  media.setAttribute('aria-label', opts.title);
  if (kind === 'video') media.setAttribute('playsinline', '');
  for (const t of opts.tracks ?? []) {
    media.append(
      h('track', { kind: t.kind ?? 'captions', src: t.src, label: t.label, srclang: t.lang }),
    );
  }

  const play = h('button', { type: 'button', class: 'pl-play', 'aria-label': 'Play' }, '▶');
  const stop = h('button', { type: 'button', class: 'pl-stop', 'aria-label': 'Stop' }, '■');
  const seek = h('input', {
    type: 'range',
    class: 'pl-seek',
    min: '0',
    max: '1000',
    value: '0',
    'aria-label': 'Position',
  });
  const time = h('span', { class: 'pl-time' }, '0:00 / 0:00');
  const mute = h('button', { type: 'button', class: 'pl-mute', 'aria-label': 'Mute' }, '🔊');
  const volume = h('input', {
    type: 'range',
    class: 'pl-volume',
    min: '0',
    max: '100',
    value: '80',
    'aria-label': 'Volume',
  });
  const format = h(
    'span',
    { class: 'pl-format', title: opts.mediaType },
    formatLabel(opts.mediaType, opts.fileName),
  );
  const full =
    kind === 'video'
      ? h('button', { type: 'button', class: 'pl-full', 'aria-label': 'Fill the screen' }, '⛶')
      : null;
  const skinMenu = h(
    'select',
    { class: 'pl-skin', 'aria-label': 'Player skin' },
    SKINS.map((s) =>
      h(
        'option',
        { value: s.id, selected: s.id === opts.skin ? '' : undefined, title: s.hint },
        s.name,
      ),
    ),
  );
  const notice = h('p', { class: 'pl-notice', hidden: '' });
  const scope =
    kind === 'audio'
      ? h('canvas', { class: 'pl-scope', width: '480', height: '120', 'aria-hidden': 'true' })
      : null;
  const lcd =
    kind === 'audio'
      ? h(
          'div',
          { class: 'pl-lcd' },
          h('span', { class: 'pl-lcd-title' }, opts.title),
          h('b', { class: 'pl-lcd-time' }, '0:00'),
        )
      : null;

  const el = h(
    'div',
    {
      class: `player pl-${kind} sk-${opts.skin}`,
      tabindex: '0',
      role: 'group',
      'aria-label': `${kind === 'video' ? 'Video' : 'Audio'} player: ${opts.title}`,
    },
    h(
      'div',
      { class: 'pl-title' },
      h('span', null, opts.title),
      h('span', { class: 'pl-badge' }, format),
    ),
    kind === 'video' ? h('div', { class: 'pl-screen' }, media) : [lcd, scope, media],
    h('div', { class: 'pl-controls' }, play, stop, seek, time, mute, volume, full, skinMenu),
    notice,
  );

  const total = () => (Number.isFinite(media.duration) ? media.duration : 0);
  const showTime = () => {
    time.textContent = `${clock(media.currentTime)} / ${clock(total())}`;
    if (lcd) lcd.querySelector('.pl-lcd-time')!.textContent = clock(media.currentTime);
    if (!seeking)
      seek.value = total() ? String(Math.round((1000 * media.currentTime) / total())) : '0';
  };
  const setPlaying = (on: boolean) => {
    play.textContent = on ? '❚❚' : '▶';
    play.setAttribute('aria-label', on ? 'Pause' : 'Play');
    el.classList.toggle('is-playing', on);
  };
  let seeking = false;
  let lastReport = 0;
  const report = () => {
    opts.onProgress?.(media.currentTime, total());
    lastReport = media.currentTime;
  };
  const toggle = () => {
    if (media.paused) void media.play()?.catch(() => undefined);
    else media.pause();
  };

  play.addEventListener('click', toggle);
  stop.addEventListener('click', () => {
    media.pause();
    media.currentTime = 0;
    showTime();
    report();
  });
  seek.addEventListener('pointerdown', () => {
    seeking = true;
  });
  seek.addEventListener('change', () => {
    seeking = false;
    if (total()) media.currentTime = (Number(seek.value) / 1000) * total();
  });
  seek.addEventListener('input', () => {
    if (total())
      time.textContent = `${clock((Number(seek.value) / 1000) * total())} / ${clock(total())}`;
  });
  volume.addEventListener('input', () => {
    media.volume = Number(volume.value) / 100;
    media.muted = false;
  });
  mute.addEventListener('click', () => {
    media.muted = !media.muted;
  });
  full?.addEventListener('click', () => {
    const screen = el.querySelector('.pl-screen') ?? el;
    if (document.fullscreenElement) void document.exitFullscreen?.();
    else void screen.requestFullscreen?.();
  });
  skinMenu.addEventListener('change', () => {
    const next = skinMenu.value as SkinId;
    for (const s of SKINS) el.classList.toggle(`sk-${s.id}`, s.id === next);
    opts.onSkin?.(next);
  });
  media.addEventListener('loadedmetadata', () => {
    if (opts.startAt && opts.startAt < total()) media.currentTime = opts.startAt;
    showTime();
  });
  media.addEventListener('timeupdate', () => {
    showTime();
    if (!media.paused && media.currentTime - lastReport >= PROGRESS_EVERY) report();
  });
  media.addEventListener('play', () => setPlaying(true));
  media.addEventListener('pause', () => {
    setPlaying(false);
    report();
  });
  media.addEventListener('ended', () => {
    setPlaying(false);
    report();
  });
  media.addEventListener('volumechange', () => {
    mute.textContent = media.muted || media.volume === 0 ? '🔇' : '🔊';
    mute.setAttribute('aria-label', media.muted ? 'Unmute' : 'Mute');
  });
  media.addEventListener('error', () => {
    notice.hidden = false;
    notice.replaceChildren('Your browser cannot play this file here');
    if (opts.downloadHref) {
      notice.append(
        ': ',
        h('a', { href: opts.downloadHref, download: opts.fileName }, 'download it'),
        ' to open it in a player of your own.',
      );
    } else {
      notice.append('.');
    }
  });
  el.addEventListener('keydown', (ev) => {
    if (ev.target instanceof HTMLSelectElement || ev.target instanceof HTMLInputElement) return;
    const k = ev.key.toLowerCase();
    let handled = true;
    if (k === ' ' || k === 'k') toggle();
    else if (k === 'j' || k === 'arrowleft')
      media.currentTime = Math.max(0, media.currentTime - (k === 'j' ? SKIP : STEP));
    else if (k === 'l' || k === 'arrowright')
      media.currentTime = Math.min(
        total() || Infinity,
        media.currentTime + (k === 'l' ? SKIP : STEP),
      );
    else if (k === 'arrowup') media.volume = Math.min(1, media.volume + 0.1);
    else if (k === 'arrowdown') media.volume = Math.max(0, media.volume - 0.1);
    else if (k === 'm') media.muted = !media.muted;
    else if (k === 'f' && full) full.click();
    else if (/^[0-9]$/.test(k) && total()) media.currentTime = (Number(k) / 10) * total();
    else handled = false;
    if (handled) ev.preventDefault();
  });

  if (scope) spectrum(media, scope);
  showTime();
  return { el, media };
}

/** A live spectrum on the deck's canvas, drawn only while playing; silent where Web Audio is absent. */
function spectrum(media: HTMLMediaElement, canvas: HTMLCanvasElement): void {
  const Ctx = (globalThis as { AudioContext?: typeof AudioContext }).AudioContext;
  const g = canvas.getContext?.('2d');
  if (!Ctx || !g) return;
  let analyser: AnalyserNode | null = null;
  let frame = 0;
  const bars = 32;
  const draw = () => {
    const W = canvas.width;
    const H = canvas.height;
    g.clearRect(0, 0, W, H);
    const data = analyser ? new Uint8Array(analyser.frequencyBinCount) : null;
    if (data && analyser) analyser.getByteFrequencyData(data);
    for (let i = 0; i < bars; i++) {
      const v = data ? data[Math.floor((i / bars) ** 1.6 * data.length * 0.5)]! / 255 : 0.08;
      const lit = Math.round(v * 10);
      for (let s = 0; s < 10; s++) {
        g.fillStyle = s < lit ? (s > 7 ? '#d6a64b' : '#9bd36a') : '#2a2f24';
        g.fillRect(4 + (i * (W - 8)) / bars, H - 6 - s * (H / 11), (W - 8) / bars - 3, H / 11 - 3);
      }
    }
    if (!media.paused) frame = requestAnimationFrame(draw);
  };
  media.addEventListener('play', () => {
    try {
      if (!analyser) {
        const ctx = new Ctx();
        const source = ctx.createMediaElementSource(media);
        analyser = ctx.createAnalyser();
        analyser.fftSize = 512;
        source.connect(analyser).connect(ctx.destination);
      }
    } catch {
      analyser = null; // a cross-origin source or a browser without the API: the deck still plays
    }
    cancelAnimationFrame(frame);
    draw();
  });
  media.addEventListener('pause', () => cancelAnimationFrame(frame));
  draw();
}
