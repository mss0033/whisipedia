/**
 * The credit line of a map layer. A module with no imports, on purpose: the style builder
 * (`style.ts`) is bundled with MapLibre, and whatever it imports goes into that chunk with it. When
 * this lived beside the place rows, their imports (`core/format.ts`, `core/geo.ts`) went along, and
 * every page had to load MapLibre before it could start.
 */

/**
 * One credit per publisher. The library joins the credits of the tile sets behind a layer with
 * " · "; two sets of one publisher ("© Mapterhorn (…): world models · © Mapterhorn (…): national
 * models") then name it twice. Credits that start alike, up to the first colon, become one:
 * "© Mapterhorn (…): world models; national models". Exact repeats are dropped.
 */
export function mergeCredits(text: string): string {
  const order: string[] = [];
  const details = new Map<string, string[]>();
  for (const part of text.split(' · ')) {
    const credit = part.trim();
    if (!credit) continue;
    const colon = credit.indexOf(': ');
    const who = colon > 0 ? credit.slice(0, colon) : credit;
    const what = colon > 0 ? credit.slice(colon + 2).trim() : '';
    let list = details.get(who);
    if (!list) {
      list = [];
      details.set(who, list);
      order.push(who);
    }
    if (what && !list.includes(what)) list.push(what);
  }
  return order
    .map((who) => {
      const list = details.get(who) ?? [];
      return list.length ? `${who}: ${list.join('; ')}` : who;
    })
    .join(' · ');
}
