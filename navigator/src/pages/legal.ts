/**
 * Court opinions (CourtListener) as a reader expects them: a case caption, then the opinion in
 * paragraphs. The refined text carries a fixed header (wsp_adapters.courtlistener.opinion_record):
 *
 *   <case name>
 *   <court>. Filed <YYYY-MM-DD>. Docket <number>      (each part optional)
 *   Judges: <names>                                   (optional)
 *   Opinion by <author>                                (optional)
 *   <body: one paragraph per line, with star pagination such as "*80">
 *
 * Shown as plain monospaced text, a short order ("C. A. 5th Cir. Certiorari denied.") read like a
 * metadata card and a long opinion lost its paragraphs.
 */

import { docIdToAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import { linkify } from '../core/text.ts';

export interface CaseText {
  name: string;
  court: string;
  filed: string;
  docket: string;
  judges: string;
  author: string;
  body: string[];
}

const FACTS_RE =
  /^(?<court>.*?)(?:\.?\s*Filed (?<filed>\d{4}-\d{2}-\d{2}))?(?:\.?\s*Docket (?<docket>.+?))?\.?$/;
/** A court name ends the facts line: "Court of Appeals of Georgia", "Supreme Court of the United States". */
const COURTISH_RE = /\b(court|tribunal|board|commission|circuit|district|appeals?|judicial)\b/i;
/** Star pagination: "*80" on its own line, or glued to the start of a line ("*248Opinion by"). */
const STAR_RE = /^\*(\d{1,5})(?=\s|$|[A-Z(“"'])/;
const SHORT_ORDER_CHARS = 280;

/** The header fields and body paragraphs of an opinion's text, or null if it is not one. */
export function parseCase(text: string, title: string): CaseText | null {
  const lines = text.split('\n');
  if (lines.length < 2) return null;
  const name = lines[0]!.trim();
  if (!name || !(title.startsWith(name) || name.startsWith(title.replace(/\s*\(.*\)$/, ''))))
    return null;
  let i = 1;
  const out: CaseText = {
    name,
    court: '',
    filed: '',
    docket: '',
    judges: '',
    author: '',
    body: [],
  };
  const facts = FACTS_RE.exec(lines[1]!.trim());
  if (
    facts?.groups &&
    (facts.groups.filed || facts.groups.docket || COURTISH_RE.test(facts.groups.court ?? ''))
  ) {
    out.court = (facts.groups.court ?? '').trim();
    out.filed = facts.groups.filed ?? '';
    out.docket = (facts.groups.docket ?? '').trim();
    i = 2;
  } else {
    return null; // not the adapter's header: let the plain renderer show it
  }
  if (lines[i]?.startsWith('Judges: ')) out.judges = lines[i++]!.slice(8).trim();
  if (lines[i]?.startsWith('Opinion by ')) out.author = lines[i++]!.slice(11).trim();
  out.body = lines
    .slice(i)
    .map((l) => l.trim())
    .filter(Boolean);
  return out;
}

function formatFiled(iso: string): string {
  const d = new Date(`${iso}T12:00:00Z`);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'long',
        day: 'numeric',
        timeZone: 'UTC',
      });
}

/** One paragraph, star pagination as small page markers ("p. 80") instead of stray "*80". */
function paragraph(line: string): HTMLElement {
  const p = document.createElement('p');
  let rest = line;
  const lead = STAR_RE.exec(rest);
  if (lead) {
    p.appendChild(
      h('span', { class: 'page-mark', title: `Page ${lead[1]} of the reporter` }, `p. ${lead[1]}`),
    );
    rest = rest.slice(lead[0].length).trimStart();
  }
  // Star pages inside a paragraph: " *81 " between words.
  const parts = rest.split(/\s\*(\d{1,5})\s/);
  parts.forEach((part, n) => {
    if (n % 2 === 1)
      p.appendChild(
        h('span', { class: 'page-mark', title: `Page ${part} of the reporter` }, `p. ${part}`),
      );
    else if (part) p.appendChild(linkify(part));
  });
  return p;
}

const ENDS_SENTENCE_RE = /[.?!:;"”’)\]]$/;

/**
 * Paragraphs across page breaks: a "*80" line becomes an inline marker, and a line that stops
 * mid-sentence ("... when Deal" / "*80" / "ran up to her") continues in the next one.
 */
export function joinPages(lines: string[]): string[] {
  const out: string[] = [];
  let open = false; // the last paragraph stops mid-sentence
  for (const line of lines) {
    const page = /^\*(\d{1,5})$/.exec(line);
    if (page) {
      if (out.length) out[out.length - 1] += ` *${page[1]} `;
      else out.push(`*${page[1]}`);
      continue;
    }
    if (open && out.length) out[out.length - 1] = `${out[out.length - 1]!.trimEnd()} ${line}`;
    else out.push(line);
    open = !ENDS_SENTENCE_RE.test(line);
  }
  return out.map((p) => p.trimEnd());
}

export function renderCase(c: CaseText): HTMLElement {
  const rows: Array<[string, string]> = [];
  if (c.filed) rows.push(['Filed', formatFiled(c.filed)]);
  if (c.docket) rows.push(['Docket', c.docket]);
  if (c.judges) rows.push(['Judges', c.judges]);
  if (c.author) rows.push(['Opinion by', c.author]);
  const pageOnly = (l: string) => /^\*\d{1,5}$/.test(l);
  const text = c.body.filter((l) => !pageOnly(l)).join(' ');
  const short = c.body.length > 0 && text.length <= SHORT_ORDER_CHARS;
  const body = short
    ? h(
        'div',
        { class: 'case-order', role: 'note' },
        h('div', { class: 'case-order-label' }, 'Order of the court'),
        h('p', null, text),
        h(
          'p',
          { class: 'fineprint' },
          'This is the complete text the court published for this entry.',
        ),
      )
    : c.body.length
      ? h('div', { class: 'case-body' }, ...joinPages(c.body).map(paragraph))
      : h('p', { class: 'fineprint' }, 'The court published no opinion text for this entry.');
  return h(
    'div',
    { class: 'case' },
    h(
      'header',
      { class: 'case-caption' },
      c.court ? h('div', { class: 'case-court' }, c.court) : null,
      rows.length
        ? h(
            'dl',
            { class: 'case-facts' },
            ...rows.flatMap(([k, v]) => [h('dt', null, k), h('dd', null, v)]),
          )
        : null,
    ),
    body,
  );
}

// "section 509(a)(2)", "section 1795 of title 12", "section 170(b) of this title" (wsp_adapters.uslaw.TEXT_REF)
const SECTION_RE =
  /\bsection\s+(\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?)(?:\([0-9A-Za-z]+\))*(?:\s+of\s+(?:title\s+(\d+a?)|this\s+title)\b)?/g;
/** A section "of the Social Security Act" (or any act) is not a section of the Code. */
const OF_ACT_RE = /^\s+of\s+(?:the|such|that|this|Public|chapter|subtitle|part|subpart)\b/;

/**
 * The cross references of a US Code section as links ("section 509(a)(2)" opens 26 U.S.C. 509).
 * The XML marks few of them; the adapter lists the sections the text cites, and only those the
 * library holds become links (2026-10-17 field exam: 26 U.S.C. 501 named section 509 as plain text).
 */
export function linkCodeSections(
  root: HTMLElement,
  docId: string,
  links: ReadonlyArray<{ href: string; target?: string | null }>,
): void {
  const own = /^wsp:us-code\/t([0-9]+a?)\//.exec(docId)?.[1];
  if (!own) return;
  const held = new Set(links.filter((l) => l.target).map((l) => l.href));
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.parentElement?.closest('a')) nodes.push(n as Text);
  }
  for (const node of nodes) {
    const text = node.data;
    const f = document.createDocumentFragment();
    let pos = 0;
    for (const m of text.matchAll(SECTION_RE)) {
      const i = m.index ?? 0;
      const end = i + m[0].length;
      const ofTitle = /\sof\s/.test(m[0]);
      if (!ofTitle && OF_ACT_RE.test(text.slice(end))) continue;
      const id = `wsp:us-code/t${m[2] ?? own}/s${m[1]}`;
      if (!held.has(id)) continue;
      if (i > pos) f.appendChild(document.createTextNode(text.slice(pos, i)));
      const addr = docIdToAddress(id);
      f.appendChild(h('a', { href: `#${addr}`, dataset: { wsp: addr } }, m[0]));
      pos = end;
    }
    if (pos === 0) continue;
    if (pos < text.length) f.appendChild(document.createTextNode(text.slice(pos)));
    node.replaceWith(f);
  }
}
