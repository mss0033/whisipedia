/**
 * English machine translation of a document on the reader's request (ADR-0013).
 *
 * The page is translated block by block: each paragraph, heading, list item, table cell or caption
 * becomes one segment, its inline markup standing as placeholders the model copies back: a link or a
 * formatted span is `<xN>…</xN>`, a picture, a formula, code or a nested block is `⟦N⟧`. The library
 * checks each segment (numbers, placeholders, length, sentences: wsp_oracle.mtcheck). The page is
 * rebuilt from the translated text and clones of the original elements, never from markup the model
 * wrote. A segment that fails a check stays in its own language, with the reason.
 *
 * The banner on top cannot be dismissed. The original is one click away, for the whole page and for
 * each block.
 */
import type { WspClient } from '../api/client.ts';
import type { TranslatedSegment, TranslationStatus } from '../api/types.ts';
import { h } from './dom.ts';

const BLOCKS =
  'p, li, h1, h2, h3, h4, h5, h6, td, th, dd, dt, blockquote, figcaption, caption, summary';
const INLINE = new Set([
  'a',
  'b',
  'strong',
  'i',
  'em',
  'span',
  'sup',
  'sub',
  'small',
  'u',
  's',
  'mark',
  'abbr',
  'cite',
  'q',
  'time',
  'font',
  'bdi',
  'bdo',
  'ins',
  'del',
  'dfn',
  'big',
  'tt',
]); // prettier-ignore
const BATCH = 8;
const MAX_SEGMENT = 4000;
const MAX_WAITS = 30; // starting the server takes about half a minute; a busy GPU longer

/** A block as a segment: its text with placeholders, and what each placeholder stands for. */
export interface BlockText {
  text: string;
  /** `<xN>` -> the element whose shallow clone wraps its translated content. */
  wraps: Map<number, Element>;
  /** `⟦N⟧` -> the node put back as it is (a picture, code, a nested block). */
  keeps: Map<number, Node>;
}

/** The segment text of `block`'s own content (nested blocks are segments of their own: `⟦N⟧`). */
export function blockText(block: Element): BlockText {
  const wraps = new Map<number, Element>();
  const keeps = new Map<number, Node>();
  let n = 0;
  const walk = (node: Node): string => {
    let out = '';
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE) {
        // Only what would read as a placeholder is changed ("x < 5" stays as it is).
        out += (child.textContent ?? '').replace(/<(\/?)x(\d+)>/g, '‹$1x$2›').replace(/[⟦⟧]/g, '|');
      } else if (child instanceof Element) {
        const tag = child.localName;
        if (tag === 'br') {
          out += '\n';
        } else if (
          INLINE.has(tag) &&
          !child.classList.contains('broken-image') &&
          !child.matches('.math, [data-wsp-tex]')
        ) {
          n += 1;
          const id = n;
          wraps.set(id, child);
          out += `<x${id}>${walk(child)}</x${id}>`;
        } else {
          // A picture, a formula, code, an SVG or a nested block: put back as it is (a nested block
          // is a segment of its own).
          n += 1;
          keeps.set(n, child);
          out += `⟦${n}⟧`;
        }
      }
    }
    return out;
  };
  const text = walk(block)
    .replace(/[ \t]+/g, ' ')
    .trim();
  return { text, wraps, keeps };
}

/** The nodes of a translated segment: text, clones of the wrapped elements, the kept nodes. `null`
 * when its placeholders do not fit the block (the library checks them; this is the last guard). */
export function buildTranslated(translated: string, block: BlockText): Node[] | null {
  const tokens = translated.split(/(<x\d+>|<\/x\d+>|⟦\d+⟧)/);
  const root: Node[] = [];
  const stack: { id: number; el: Element }[] = [];
  const used = new Set<string>();
  const add = (node: Node) => {
    const top = stack[stack.length - 1];
    if (top) top.el.append(node);
    else root.push(node);
  };
  for (const tok of tokens) {
    if (!tok) continue;
    let m = /^<x(\d+)>$/.exec(tok);
    if (m) {
      const id = Number(m[1]);
      const orig = block.wraps.get(id);
      if (!orig || used.has(`x${id}`)) return null;
      used.add(`x${id}`);
      const el = orig.cloneNode(false) as Element;
      add(el);
      stack.push({ id, el });
      continue;
    }
    m = /^<\/x(\d+)>$/.exec(tok);
    if (m) {
      if (stack.pop()?.id !== Number(m[1])) return null;
      continue;
    }
    m = /^⟦(\d+)⟧$/.exec(tok);
    if (m) {
      const node = block.keeps.get(Number(m[1]));
      if (!node || used.has(`k${m[1]}`)) return null;
      used.add(`k${m[1]}`);
      add(node);
      continue;
    }
    const lines = tok.split('\n');
    lines.forEach((line, i) => {
      if (i) add(document.createElement('br'));
      if (line) add(document.createTextNode(line));
    });
  }
  if (stack.length || used.size !== block.wraps.size + block.keeps.size) return null;
  return root;
}

/** The blocks of `root` to translate, in reading order: those with words of their own. */
export function translatableBlocks(root: Element): Element[] {
  return Array.from(root.querySelectorAll(BLOCKS)).filter((el) => {
    if (el.closest('pre, code, .mt-banner, .mt-toggle, [data-mt]')) return false;
    const own = Array.from(el.childNodes)
      .map((c) =>
        c.nodeType === Node.TEXT_NODE || (c instanceof Element && INLINE.has(c.localName))
          ? (c.textContent ?? '')
          : '',
      )
      .join('');
    return /\p{L}{2}/u.test(own);
  });
}

export function languageName(lang: string): string {
  try {
    return new Intl.DisplayNames(['en'], { type: 'language' }).of(lang) ?? lang;
  } catch {
    return lang;
  }
}

/** The document's language as the translator names it, or null when there is nothing to translate. */
export function sourceLanguage(lang: string | null | undefined): string | null {
  const code = (lang ?? '').split(/[-_]/)[0]?.toLowerCase() ?? '';
  return code && !['en', 'mul', 'und', 'zxx'].includes(code) ? code : null;
}

let statusPromise: Promise<TranslationStatus | null> | null = null;

function status(api: WspClient): Promise<TranslationStatus | null> {
  statusPromise ??= api.translationStatus().catch(() => null);
  return statusPromise;
}

interface Translated {
  block: Element;
  original: Node[];
  text: BlockText;
  english: string | null;
  showing: 'original' | 'translation';
  toggle: HTMLButtonElement;
}

/**
 * The bar that offers the translation of a non-English page (`null` when there is none to offer), and
 * on request the banner and the translated page. `content` is the rendered document body; `heading`
 * its title element, if any.
 */
export function translationBar(
  api: WspClient,
  doc: { lang?: string | null; title: string },
  content: HTMLElement,
  opts: { legal?: boolean; heading?: () => HTMLElement | null; signal?: AbortSignal } = {},
): HTMLElement | null {
  const lang = sourceLanguage(doc.lang);
  if (!lang) return null;
  const name = languageName(lang);
  const bar = h('p', { class: 'notice mt-offer', hidden: true });
  const button = h(
    'button',
    { type: 'button', class: 'mt-start' },
    'Read in English (machine translation)',
  );
  bar.append(button, ' ', h('span', { class: 'fineprint' }, `This page is in ${name}.`));
  void status(api).then((st) => {
    if (st?.available && st.languages.includes(lang)) bar.hidden = false;
  });
  button.addEventListener('click', () => {
    void status(api).then((st) => {
      if (!st) return;
      const banner = startTranslation(
        api,
        lang,
        name,
        st.model ?? 'the library’s translator',
        doc.title,
        content,
        opts,
      );
      bar.replaceWith(banner);
    });
  });
  return bar;
}

function startTranslation(
  api: WspClient,
  lang: string,
  name: string,
  model: string,
  title: string,
  content: HTMLElement,
  opts: { legal?: boolean; heading?: () => HTMLElement | null; signal?: AbortSignal },
): HTMLElement {
  const progress = h('span', { class: 'mt-progress' }, 'Translating…');
  const pageToggle = h('button', { type: 'button', class: 'mt-page-toggle' }, 'Show the original');
  const banner = h(
    'div',
    { class: 'notice mt-banner', role: 'note' },
    h('b', null, `Machine translation from ${name}`),
    ` by ${model}. It may be inaccurate: names, numbers, technical terms and legal meaning can be wrong. The original is the authority.`,
    opts.legal
      ? h('span', { class: 'mt-legal' }, ' Not a legal translation: consult the original.')
      : null,
    h('span', { class: 'mt-controls' }, pageToggle, ' ', progress),
  );
  const done: Translated[] = [];
  const view: { page: 'translation' | 'original' } = { page: 'translation' }; // the whole page's choice
  pageToggle.addEventListener('click', () => {
    view.page = view.page === 'translation' ? 'original' : 'translation';
    pageToggle.textContent =
      view.page === 'translation' ? 'Show the original' : 'Show the translation';
    for (const t of done) show(t, view.page);
  });

  const blocks = translatableBlocks(content);
  const texts = blocks.map((b) => blockText(b));
  const titleText = title.trim();
  void (async () => {
    let failed = 0;
    // The title first: it says at once whether the translation reads.
    const jobs: { i: number; text: string }[] = texts
      .map((t, i) => ({ i, text: t.text }))
      .filter((j) => j.text.length > 0 && j.text.length <= MAX_SEGMENT);
    for (const t of texts) if (t.text.length > MAX_SEGMENT) failed += 1;
    const segments = [titleText, ...jobs.map((j) => j.text)];
    const results: (TranslatedSegment | null)[] = [];
    for (let at = 0; at < segments.length; at += BATCH) {
      if (opts.signal?.aborted || (!banner.isConnected && at > 0)) return;
      const batch = segments.slice(at, at + BATCH);
      const answer = await translateBatch(api, lang, batch, progress, opts.signal);
      if (!answer) {
        progress.textContent =
          'The translator is not available now; the page stays in its own language.';
        return;
      }
      results.push(...answer);
      for (let k = 0; k < answer.length; k++) {
        const index = at + k;
        const seg = answer[k];
        if (!seg) continue;
        if (index === 0) {
          if (seg.ok && seg.text) addTitle(opts.heading?.() ?? null, seg.text);
          continue;
        }
        const job = jobs[index - 1];
        if (!job) continue;
        const block = blocks[job.i];
        const text = texts[job.i];
        if (!block || !text) continue;
        if (!seg.ok || !seg.text) {
          failed += 1;
          block.append(
            h(
              'span',
              { class: 'mt-failed' },
              ` (not translated: ${seg.reason ?? 'it did not pass its checks'})`,
            ),
          );
          continue;
        }
        const t = applyToBlock(block, text, seg.text);
        if (t) {
          done.push(t);
          if (view.page === 'original') show(t, 'original');
        } else failed += 1;
      }
      progress.textContent = `Translated ${Math.min(results.length - 1, jobs.length)} of ${jobs.length} passages…`;
    }
    progress.textContent =
      `${done.length} of ${blocks.length} passages translated` +
      (failed ? `; ${failed} left in ${name} (marked).` : '.');
  })();
  return banner;
}

async function translateBatch(
  api: WspClient,
  lang: string,
  segments: string[],
  progress: HTMLElement,
  signal?: AbortSignal,
): Promise<TranslatedSegment[] | null> {
  for (let wait = 0; wait < MAX_WAITS; wait++) {
    let answer: Awaited<ReturnType<WspClient['translate']>>;
    try {
      answer = await api.translate(lang, segments, signal);
    } catch {
      return null;
    }
    if ('segments' in answer) return answer.segments;
    progress.textContent = `${answer.message[0]?.toUpperCase() ?? ''}${answer.message.slice(1)}; trying again in ${answer.retryAfter} s…`;
    await new Promise((r) => setTimeout(r, Math.min(answer.retryAfter, 60) * 1000));
    if (signal?.aborted) return null;
  }
  return null;
}

/** Replace `block`'s content with its translation; the original nodes are kept for the toggle. */
export function applyToBlock(block: Element, text: BlockText, english: string): Translated | null {
  const original = Array.from(block.childNodes);
  const built = buildTranslated(english, text);
  if (!built) return null;
  const toggle = h(
    'button',
    {
      type: 'button',
      class: 'mt-toggle',
      title: 'Show this passage in its own language',
      'aria-label': 'Show the original of this passage',
    },
    '⇄',
  );
  const t: Translated = { block, original, text, english, showing: 'translation', toggle };
  toggle.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    show(t, t.showing === 'translation' ? 'original' : 'translation');
  });
  block.setAttribute('data-mt', 'translated');
  block.replaceChildren(...built, toggle);
  return t;
}

function show(t: Translated, which: 'original' | 'translation'): void {
  if (t.showing === which) return;
  if (which === 'original') {
    t.block.replaceChildren(...t.original, t.toggle);
    t.block.setAttribute('data-mt', 'original');
  } else {
    const built = t.english ? buildTranslated(t.english, t.text) : null;
    if (!built) return;
    t.block.replaceChildren(...built, t.toggle);
    t.block.setAttribute('data-mt', 'translated');
  }
  t.showing = which;
  t.toggle.title =
    which === 'translation'
      ? 'Show this passage in its own language'
      : 'Show this passage in English';
}

function addTitle(heading: HTMLElement | null, english: string): void {
  if (!heading || heading.nextElementSibling?.classList.contains('mt-title')) return;
  heading.after(
    h('p', { class: 'mt-title' }, h('span', { lang: 'en' }, english), ' (machine translation)'),
  );
}
