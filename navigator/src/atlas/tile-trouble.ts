/**
 * What a map says when parts of it cannot be read (Atlas, weather map, small maps).
 *
 * MapLibre reports failed requests through its `error` event; with a listener it no longer prints
 * them. The answers mean different things (API/maps.py): 503 or no answer at all is a library that
 * is busy or an archive that cannot be reached, and the reader should know; 404 is something the
 * library does not hold, which is normal and says nothing. Anything else (another 4xx, a fault of
 * the page) goes to the console as before. In MapLibre 6.11 a tile's 404 and every glyph failure
 * never reach the listener at all (it drops the first and only warns of the second), so the 404
 * rule is a guard for other versions, and a font range that cannot be read is NOT noticed here.
 *
 * MapLibre keeps a failed tile as failed while it stays in view and counts it as done, so the map
 * goes idle with holes in it. The notice therefore promises no retry and offers one ("Try again"
 * reloads the map's sources), and it stays while a tile that failed is still failed and in view:
 * it goes when the map comes to rest with none left (they loaded after a reload, or left the view).
 */
import { h } from '../core/dom.ts';

export type MapErrorKind = 'unreadable' | 'absent' | 'other';

/** Statuses that mean "not now": busy, timed out, too many requests. */
const TRANSIENT = new Set([408, 425, 429]);

/** Sort a MapLibre error by the HTTP status it carries (an AJAXError; status 0 is no answer). */
export function classifyMapError(error: unknown): MapErrorKind {
  if (!error || typeof error !== 'object') return 'other';
  const { status, url } = error as { status?: unknown; url?: unknown };
  if (typeof status !== 'number' || typeof url !== 'string') return 'other';
  if (status === 0 || status >= 500 || TRANSIENT.has(status)) return 'unreadable';
  if (status === 404 || status === 410) return 'absent';
  return 'other';
}

/** A tile MapLibre gave up on that is still on the map: failed, in use, not thrown away. */
function stillFailed(tile: object): boolean {
  const t = tile as { state?: unknown; aborted?: unknown; uses?: unknown };
  return t.state === 'errored' && t.aborted !== true && (typeof t.uses !== 'number' || t.uses > 0);
}

export const TILE_TROUBLE_TEXT =
  'Some of the map could not be read: the library is busy or the map archive is unreachable.';

export interface TileTroubleOptions {
  /** Reload the map's sources. Without it the notice has no button (a still map). */
  retry?: () => void;
  /** The window's status line: told once per spell of trouble, not once per tile. */
  say?: (text: string) => void;
  /** Errors that are not about reading the map (default: the console, as MapLibre does). */
  report?: (error: unknown) => void;
}

export class TileTrouble {
  /** Put it next to the map. It is in the page from the start, empty: a live region is announced
   * when words arrive in it, not when it appears with them. */
  readonly element: HTMLElement;
  private readonly text: HTMLElement;
  private readonly again: HTMLButtonElement | null = null;
  /** MapLibre's own tile objects that failed (from the error event), while they may still be failed. */
  private readonly tiles = new Set<object>();
  /** Something else could not be read (a sprite, a style) since the map last moved or was reloaded. */
  private other = false;
  private shown = false;

  constructor(private readonly opts: TileTroubleOptions = {}) {
    this.text = h('span', { role: 'status' });
    this.element = h('p', { class: 'map-trouble' }, this.text);
    if (opts.retry) {
      this.again = h(
        'button',
        { type: 'button', class: 'btn btn-small', hidden: true },
        'Try again',
      );
      this.again.addEventListener('click', () => {
        this.tiles.clear();
        this.other = false;
        opts.retry?.();
      });
      this.element.append(' ', this.again);
    }
  }

  get visible(): boolean {
    return this.shown;
  }

  /** MapLibre's `error` event. */
  error(ev: { error?: unknown; tile?: unknown } | null | undefined): void {
    const kind = classifyMapError(ev?.error);
    if (kind === 'absent') return;
    if (kind === 'other') {
      (this.opts.report ?? ((e: unknown) => console.error(e)))(ev?.error ?? ev);
      return;
    }
    if (ev?.tile && typeof ev.tile === 'object') this.tiles.add(ev.tile);
    else this.other = true;
    if (this.shown) return;
    this.shown = true;
    this.text.textContent = TILE_TROUBLE_TEXT;
    this.element.classList.add('is-on');
    if (this.again) this.again.hidden = false;
    this.opts.say?.(TILE_TROUBLE_TEXT);
  }

  /** The map started to move or took a new style: a failure that was not a tile's is in the past.
   * Failed tiles are not: they stay failed while they are in view, wherever the map goes. */
  moved(): void {
    this.other = false;
  }

  /** The map came to rest: with no tile still failed and nothing else failed since, nothing to say. */
  idle(): void {
    if (!this.shown) return;
    for (const tile of this.tiles) if (!stillFailed(tile)) this.tiles.delete(tile);
    if (this.tiles.size || this.other) return;
    this.shown = false;
    this.text.textContent = '';
    this.element.classList.remove('is-on');
    if (this.again) this.again.hidden = true;
  }
}
