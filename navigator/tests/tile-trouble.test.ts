// A map that cannot be read says so; a glyph the library does not hold says nothing (review of
// plan B, F4 items 1-3: by HTTP status, cleared at rest, no promise of a retry that does not happen).
import { describe, expect, it, vi } from 'vitest';
import { mergeCredits } from '../src/atlas/places.ts';
import { classifyMapError, TILE_TROUBLE_TEXT, TileTrouble } from '../src/atlas/tile-trouble.ts';

/** What MapLibre hands its `error` listeners for a failed request (its AJAXError). */
const ajax = (status: number, url = '/v1/maps/tiles/basemap/3/4/2') => ({
  error: Object.assign(new Error(`AJAXError: (${status}): ${url}`), { status, url }),
});

describe('map errors by status', () => {
  it('sorts them', () => {
    expect(classifyMapError(ajax(503).error)).toBe('unreadable');
    expect(classifyMapError(ajax(500).error)).toBe('unreadable');
    expect(classifyMapError(ajax(429).error)).toBe('unreadable');
    expect(classifyMapError(ajax(0).error)).toBe('unreadable'); // no answer at all
    expect(classifyMapError(ajax(404, '/v1/maps/glyphs/Noto/1024-1279.pbf').error)).toBe('absent');
    expect(classifyMapError(ajax(404).error)).toBe('absent');
    expect(classifyMapError(ajax(410).error)).toBe('absent');
    expect(classifyMapError(ajax(403).error)).toBe('other'); // a refusal is not "not held"
    expect(classifyMapError(ajax(400).error)).toBe('other');
    expect(classifyMapError(new Error('Layer "x" already exists on this map.'))).toBe('other');
    expect(classifyMapError(undefined)).toBe('other');
  });
});

/** A tile as MapLibre holds it: failed, in view. */
const tile = () => ({ state: 'errored', aborted: false, uses: 1 });

describe('the notice', () => {
  const setup = () => {
    const retry = vi.fn();
    const say = vi.fn();
    const report = vi.fn();
    const t = new TileTrouble({ retry, say, report });
    return {
      t,
      retry,
      say,
      report,
      status: t.element.querySelector('[role="status"]')!,
      again: t.element.querySelector('button')!,
    };
  };

  it('is an empty live region until a tile cannot be read, then says so once', () => {
    const { t, say, report, status, again } = setup();
    expect(t.visible).toBe(false);
    expect(t.element.hidden).toBe(false); // in the page from the start, so its words are announced
    expect(status.textContent).toBe('');
    expect(again.hidden).toBe(true);
    t.error({ ...ajax(503), tile: tile() });
    t.error({ ...ajax(503, '/v1/maps/tiles/basemap/3/5/2'), tile: tile() });
    t.error(ajax(0));
    expect(t.visible).toBe(true);
    expect(t.element.classList.contains('is-on')).toBe(true);
    expect(status.textContent).toBe(TILE_TROUBLE_TEXT);
    expect(again.hidden).toBe(false);
    expect(say).toHaveBeenCalledTimes(1); // the status line is told once, not per tile
    expect(report).not.toHaveBeenCalled();
  });

  it('says nothing for something the library does not hold (404)', () => {
    const { t, say, report } = setup();
    t.error(ajax(404, '/v1/maps/glyphs/Noto%20Sans%20Regular/1024-1279.pbf'));
    t.error(ajax(404, '/v1/maps/sprites/v4@2x.json'));
    expect(t.visible).toBe(false);
    expect(say).not.toHaveBeenCalled();
    expect(report).not.toHaveBeenCalled(); // normal: not an error of the page either
  });

  it('hands a fault of the page, and a refusal (403), on instead of hiding them', () => {
    const { t, report } = setup();
    const fault = new Error('Source "x" cannot be removed while layer "y" is using it.');
    t.error({ error: fault });
    expect(report).toHaveBeenCalledWith(fault);
    const refused = ajax(403);
    t.error(refused);
    expect(report).toHaveBeenCalledWith(refused.error);
    expect(t.visible).toBe(false);
  });

  it('stays while a failed tile is still in view, however the map moves or is resized', () => {
    const { t, status } = setup();
    const hole = tile();
    t.error({ ...ajax(503), tile: hole });
    t.idle(); // the same view at rest: the other tiles loaded, this one is still missing
    expect(t.visible).toBe(true);
    t.moved(); // a small pan, or the map resized: MapLibre asks for nothing again
    t.idle();
    expect(t.visible).toBe(true);
    hole.uses = 0; // it left the view
    hole.aborted = true;
    t.idle();
    expect(t.visible).toBe(false);
    expect(status.textContent).toBe('');
    expect(t.element.classList.contains('is-on')).toBe(false);
    t.error({ ...ajax(502), tile: tile() }); // and it can come back
    expect(t.visible).toBe(true);
  });

  it('a failure that was not a tile (a sprite) is over once the map has moved and come to rest', () => {
    const { t } = setup();
    t.error(ajax(503, '/v1/maps/sprites/v4.json'));
    t.idle();
    expect(t.visible).toBe(true);
    t.moved();
    t.idle();
    expect(t.visible).toBe(false);
  });

  it('promises no retry: it offers one, and goes when the reload comes to rest unfailed', () => {
    const { t, retry, again } = setup();
    expect(TILE_TROUBLE_TEXT).not.toMatch(/tried again|retr/i);
    const hole = tile();
    t.error({ ...ajax(503), tile: hole });
    expect(again.textContent).toBe('Try again');
    again.click();
    expect(retry).toHaveBeenCalledTimes(1);
    expect(t.visible).toBe(true); // until the reload has shown it worked
    const second = tile();
    t.error({ ...ajax(503), tile: second }); // it did not
    t.idle();
    expect(t.visible).toBe(true);
    again.click();
    t.moved(); // the new style
    t.idle();
    expect(t.visible).toBe(false);
    expect(again.hidden).toBe(true);
  });

  it('a still map has the words and no button, and loses them when a new palette loads it afresh', () => {
    const t = new TileTrouble({ report: () => undefined });
    const hole = tile();
    t.error({ ...ajax(503), tile: hole });
    expect(t.visible).toBe(true);
    expect(t.element.querySelector('button')).toBeNull();
    hole.aborted = true; // setStyle threw the old tiles away
    t.moved();
    t.idle();
    expect(t.visible).toBe(false);
  });
});

describe('map credits', () => {
  it('names a publisher once', () => {
    expect(
      mergeCredits(
        '© Mapterhorn (mapterhorn.com/attribution): Copernicus GLO-30 and national elevation models · © Mapterhorn (mapterhorn.com/attribution): national LiDAR and elevation models',
      ),
    ).toBe(
      '© Mapterhorn (mapterhorn.com/attribution): Copernicus GLO-30 and national elevation models; national LiDAR and elevation models',
    );
    expect(mergeCredits('© OpenStreetMap contributors · Protomaps')).toBe(
      '© OpenStreetMap contributors · Protomaps',
    );
    expect(mergeCredits('© A: x · © A: x · © B')).toBe('© A: x · © B');
    expect(mergeCredits('')).toBe('');
  });
});
