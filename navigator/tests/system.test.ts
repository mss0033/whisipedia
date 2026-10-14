// The System Monitor shows open alerts (repeats grouped) and what the trawler found.
import { describe, expect, it } from 'vitest';
import { system } from '../mock/handler.ts';
import { alertsBox, autonomyBox, renderSystem, trawlerBox } from '../src/pages/system.ts';

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
    expect(box.textContent).toContain('2 failed in the last week (3 in all)');
    expect(box.textContent).toContain('7 work but have a missing image or a link into nothing');
    expect(box.textContent).toContain('4 catalog card(s) without a direct link');
    expect(box.querySelector('.trawl-list a')?.textContent).toBe(
      'kiwix-learningstatistics-en-all/regression.html',
    );
    expect(trawlerBox(null)).toBeNull();
  });

  it('shows the unattended streak toward the 30-day goal', () => {
    const box = autonomyBox(sys.ops)!;
    expect(box.textContent).toContain('3.5 of 30 days without a critical alert or a manual repair');
    expect(box.textContent).toContain('owner action: retry kiwix-solar/2025-01');
    expect(box.textContent).toContain('2 site(s) behind their freshness target');
    expect(box.querySelector('progress')?.getAttribute('value')).toBe('12');
  });

  it('puts the streak, alerts and trawler at the top of the page', () => {
    const page = renderSystem(sys, new Map());
    const titles = [...page.children].map((c) => c.textContent?.slice(0, 20));
    expect(titles[0]).toContain('Unattended');
    expect(titles[1]).toContain('Alerts');
    expect(titles[2]).toContain('Trawler');
  });
});
