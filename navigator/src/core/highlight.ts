/**
 * Snippet highlighting. Prefers server-provided ranges (code point offsets);
 * falls back to matching query terms client-side. Output is DOM, never HTML.
 */

const STOPWORDS = new Set(
  'a an and are as at be by for from how in is it of on or that the to was what when where which who why with about does do'.split(
    ' ',
  ),
);

export function queryTerms(query: string): string[] {
  const seen = new Set<string>();
  for (const raw of query.toLowerCase().split(/[^\p{L}\p{N}]+/u)) {
    if (raw.length < 2 || STOPWORDS.has(raw)) continue;
    seen.add(raw);
  }
  return [...seen];
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export type Segment = { text: string; hit: boolean };

/** Merge and clamp ranges; returns segments covering the whole string. */
export function segmentsFromRanges(text: string, ranges: Array<[number, number]>): Segment[] {
  const cps = Array.from(text);
  const sorted = ranges
    .map(([a, b]) => [Math.max(0, a), Math.min(cps.length, b)] as [number, number])
    .filter(([a, b]) => b > a)
    .sort((x, y) => x[0] - y[0]);
  const merged: Array<[number, number]> = [];
  for (const r of sorted) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([r[0], r[1]]);
  }
  const out: Segment[] = [];
  let pos = 0;
  for (const [a, b] of merged) {
    if (a > pos) out.push({ text: cps.slice(pos, a).join(''), hit: false });
    out.push({ text: cps.slice(a, b).join(''), hit: true });
    pos = b;
  }
  if (pos < cps.length) out.push({ text: cps.slice(pos).join(''), hit: false });
  return out;
}

export function segmentsFromQuery(text: string, query: string): Segment[] {
  const terms = queryTerms(query);
  if (terms.length === 0) return [{ text, hit: false }];
  const re = new RegExp(
    `(?<![\\p{L}\\p{N}])(${terms
      .map(escapeRe)
      .sort((a, b) => b.length - a.length)
      .join('|')})`,
    'giu',
  );
  const out: Segment[] = [];
  let pos = 0;
  for (const m of text.matchAll(re)) {
    const i = m.index ?? 0;
    // extend the match to the end of the word ("vaccine" -> "vaccines")
    let end = i + m[0].length;
    while (end < text.length && /[\p{L}\p{N}]/u.test(text[end]!)) end += 1;
    if (i < pos) continue;
    if (i > pos) out.push({ text: text.slice(pos, i), hit: false });
    out.push({ text: text.slice(i, end), hit: true });
    pos = end;
  }
  if (pos < text.length) out.push({ text: text.slice(pos), hit: false });
  return out.length ? out : [{ text, hit: false }];
}

export function highlightSnippet(
  text: string,
  query: string,
  ranges?: Array<[number, number]>,
): DocumentFragment {
  const segs =
    ranges && ranges.length > 0 ? segmentsFromRanges(text, ranges) : segmentsFromQuery(text, query);
  const f = document.createDocumentFragment();
  for (const seg of segs) {
    if (seg.hit) {
      const b = document.createElement('b');
      b.className = 'hl';
      b.textContent = seg.text;
      f.appendChild(b);
    } else {
      f.appendChild(document.createTextNode(seg.text));
    }
  }
  return f;
}
