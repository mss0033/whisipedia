// The System Monitor shows open alerts (repeats grouped) and what the trawler found.
import { describe, expect, it } from 'vitest';
import { system } from '../mock/handler.ts';
import { alertsBox, renderSystem, trawlerBox } from '../src/pages/system.ts';

describe('system monitor', () => {
  const sys = system();

  it('lists alerts worst first and groups many of one kind', () => {
    const box = alertsBox(sys.ops);
    const items = [...box.querySelectorAll('li')].map((li) => li.textContent ?? '');
    expect(box.textContent).toContain('Alerts (5 open)');
    expect(items[0]).toContain('quarantined');
    expect(items[1]).toContain('4 warning alerts of kind dormant');
    expect(items).toHaveLength(2);
    expect(alertsBox(null).textContent).toContain('No orchestrator data yet.');
  });

  it('sums up the trawler and links its problem pages', () => {
    const box = trawlerBox(sys.ops)!;
    expect(box.textContent).toContain('1,440 pages checked in 58 sites');
    expect(box.textContent).toContain('2 with problems in the last week (3 in all)');
    expect(box.textContent).toContain('4 catalog card(s) without a direct link');
    expect(box.querySelector('.trawl-list a')?.textContent).toBe(
      'kiwix-learningstatistics-en-all/regression.html',
    );
    expect(trawlerBox(null)).toBeNull();
  });

  it('puts both at the top of the page', () => {
    const page = renderSystem(sys, new Map());
    const titles = [...page.children].map((c) => c.textContent?.slice(0, 20));
    expect(titles[0]).toContain('Alerts');
    expect(titles[1]).toContain('Trawler');
  });
});
