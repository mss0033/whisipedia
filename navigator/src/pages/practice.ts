/**
 * wsp://practice — the Exam Practice Hall: multiple-choice drill and mock exams from the library's
 * question pools (NCVEC amateur radio pools today; any source whose documents parse as pools).
 *
 *   wsp://practice                       the pools the library holds
 *   wsp://practice/<native id>           a pool: syllabus with your progress, study by group
 *   wsp://practice/<native id>?mode=exam&seed=N   a mock exam drawn from the pool, one per group
 *
 * Progress (which questions you answered, right or wrong) stays in this browser only.
 */
import { ApiRequestError } from '../api/client.ts';
import type { DocumentResponse, SearchResults } from '../api/types.ts';

type SearchHit = SearchResults['hits'][number];

import { makeAddress, searchAddress } from '../core/address.ts';
import { h } from '../core/dom.ts';
import {
  drawExam,
  isProgress,
  LETTERS,
  type Letter,
  newSeed,
  type Pool,
  type Progress,
  parsePool,
  type Question,
  tally,
} from '../core/exam.ts';
import { formatDate } from '../core/format.ts';
import type { Route } from '../core/router.ts';
import { errorPage, pageHeading, wspLink } from '../ui/components.ts';
import type { AppServices, PageContext, PageResult } from './types.ts';

export const POOL_SOURCE = 'ncvec-question-pools';
type PracticeRoute = Extract<Route, { page: 'practice' }>;

/** Parsed pools by doc id: a pool is fetched once, not at every question (review 2026-10-14 #5). */
const parsed = new Map<string, { title: string; pool: Pool; address: string }>();
/** Answers given in the exam of a seed, so moving between its questions keeps them. */
const exams = new Map<string, Map<string, Letter>>();

function progressKey(docId: string): string {
  return `practice:${docId}`;
}

function loadProgress(app: AppServices, docId: string): Progress {
  return app.storage.read<Progress>(progressKey(docId), { answers: {}, updated: '' }, isProgress);
}

function saveProgress(app: AppServices, docId: string, p: Progress): void {
  app.storage.write(progressKey(docId), { ...p, updated: new Date().toISOString() });
}

async function loadPool(
  ctx: PageContext,
  docId: string,
): Promise<{ title: string; pool: Pool; address: string }> {
  const hit = parsed.get(docId);
  if (hit && !ctx.opts.reload) return hit;
  const doc: DocumentResponse = await ctx.app.api.document(docId, { text: true }, ctx.signal);
  const fresh = {
    title: doc.title,
    pool: parsePool(doc.text ?? '', doc.title),
    address: doc.address,
  };
  parsed.set(docId, fresh);
  return fresh;
}

export async function render(ctx: PageContext): Promise<PageResult> {
  const route = ctx.route as PracticeRoute;
  if (!route.pool) return hall(ctx);
  const docId = `wsp:${POOL_SOURCE}/${route.pool}`;
  let loaded: Awaited<ReturnType<typeof loadPool>>;
  try {
    loaded = await loadPool(ctx, docId);
  } catch (err) {
    if (err instanceof ApiRequestError && err.status === 404) {
      return {
        title: 'Pool not found',
        status: 'error',
        body: errorPage({
          title: 'Pool not found',
          heading: 'No such question pool',
          message: 'The library has no question pool at this address.',
          address: ctx.address,
          suggestions: [wspLink(makeAddress('practice'), 'The pools the library holds')],
        }),
      };
    }
    throw err;
  }
  const { title, pool, address } = loaded;
  if (pool.questions.length === 0) {
    return {
      title,
      status: 'error',
      body: errorPage({
        title,
        heading: 'Not a question pool',
        message: 'This document has no questions in the pool format (id, answer, four choices).',
        address: ctx.address,
        suggestions: [wspLink(address, 'Read the document instead')],
      }),
    };
  }
  if (route.mode === 'exam') {
    if (!route.seed)
      return { redirect: makeAddress('practice', route.pool, { mode: 'exam', seed: newSeed() }) };
    return exam(ctx, route, docId, title, pool);
  }
  return study(ctx, route, docId, title, pool, address);
}

/* ----------------------------------- the hall ----------------------------------- */

async function hall(ctx: PageContext): Promise<PageResult> {
  const sources = await ctx.app.sources();
  const src = sources.find((s) => s.id === POOL_SOURCE);
  const body = h('div', { class: 'page page-practice' }, pageHeading('Exam Practice Hall'));
  if (!src) {
    body.append(
      h(
        'p',
        { class: 'notice' },
        'The library holds no question pools yet. ',
        wspLink(makeAddress('directory'), 'Browse the directory'),
        ' for what it does hold.',
      ),
    );
    return { title: 'Exam Practice Hall', body };
  }
  const found = await ctx.app.api.search(
    { q: 'question pool', source: POOL_SOURCE, page_size: 20 },
    ctx.signal,
  );
  const pools = found.hits.filter((hit) => !/diagrams?/i.test(hit.title));
  const byClass = new Map<string, SearchHit[]>();
  for (const hit of pools) {
    const cls = /\b(Technician|General|Extra)\b/i.exec(hit.title)?.[1] ?? 'Other';
    const key = cls[0]!.toUpperCase() + cls.slice(1).toLowerCase();
    byClass.set(key, [...(byClass.get(key) ?? []), hit]);
  }
  body.append(
    h(
      'p',
      null,
      'Multiple-choice drill and mock exams from the ',
      wspLink(makeAddress(POOL_SOURCE), src.name),
      '. Study a subelement at a time with the answer shown after each question, or sit a mock exam: one question from every group, as the real one is drawn. Your progress stays in this browser.',
    ),
  );
  for (const cls of ['Technician', 'General', 'Extra', 'Other']) {
    const hits = byClass.get(cls);
    if (!hits) continue;
    body.append(
      h('h2', null, `${cls} class`),
      h(
        'ul',
        { class: 'pool-list' },
        hits.map((hit) =>
          h(
            'li',
            null,
            wspLink(
              makeAddress('practice', nativeOf(hit.doc_id)),
              hit.title.replace(/\.pdf$/i, ''),
            ),
            hit.as_of ? h('span', { class: 'as-of' }, ` · ${formatDate(hit.as_of)}`) : null,
          ),
        ),
      ),
    );
  }
  if (pools.length === 0) {
    body.append(
      h(
        'p',
        { class: 'notice' },
        'No pools are indexed yet; try again after the next library update.',
      ),
    );
  }
  return { title: 'Exam Practice Hall', body };
}

function nativeOf(docId: string): string {
  return docId.replace(/^wsp:[^/]+\//, '');
}

/* ----------------------------------- study ----------------------------------- */

function study(
  ctx: PageContext,
  route: PracticeRoute,
  docId: string,
  title: string,
  pool: Pool,
  address: string,
): PageResult {
  const progress = loadProgress(ctx.app, docId);
  const group = route.group
    ? pool.subelements.flatMap((s) => s.groups).find((g) => g.id === route.group)
    : null;
  const questions = group ? group.questions : pool.questions;
  const index = Math.min(route.index, questions.length - 1);
  const q = questions[index]!;
  const all = tally(progress, pool.questions);
  const body = h(
    'div',
    { class: 'page page-practice' },
    pageHeading(shortTitle(title, pool)),
    h(
      'p',
      { class: 'practice-meta' },
      `${pool.questions.length} questions in ${pool.subelements.length} subelements. `,
      `Answered ${all.seen}, right ${all.right}. `,
      wspLink(
        makeAddress('practice', route.pool, { mode: 'exam' }),
        h('span', { class: 'btn-like' }, 'Sit a mock exam'),
      ),
      ' · ',
      wspLink(address, 'Read the pool'),
      ' · ',
      wspLink(makeAddress('practice'), 'All pools'),
    ),
    h(
      'div',
      { class: 'practice-layout' },
      syllabus(route, pool, progress),
      card(ctx, route, docId, q, index, questions.length, group?.id ?? null, progress),
    ),
  );
  return { title: shortTitle(title, pool), body, focus: firstChoice(body) };
}

function shortTitle(title: string, pool: Pool): string {
  return pool.licenseClass ? `${pool.licenseClass} class question pool` : title;
}

function syllabus(route: PracticeRoute, pool: Pool, progress: Progress): HTMLElement {
  const rows: HTMLElement[] = [];
  for (const s of pool.subelements) {
    const t = tally(
      progress,
      s.groups.flatMap((g) => g.questions),
    );
    rows.push(
      h(
        'tr',
        { class: 'practice-sub' },
        h('th', { scope: 'row' }, s.id),
        h('th', { scope: 'row', colspan: '2' }, s.title || 'Subelement'),
        h('td', { class: 'n' }, `${t.right}/${t.seen}`),
      ),
    );
    for (const g of s.groups) {
      const gt = tally(progress, g.questions);
      const current = route.group === g.id;
      rows.push(
        h(
          'tr',
          { class: current ? 'practice-current' : '' },
          h('td', null, wspLink(makeAddress('practice', route.pool, { group: g.id }), g.id)),
          h('td', null, g.title || `${g.questions.length} questions`),
          h('td', { class: 'n' }, String(g.questions.length)),
          h('td', { class: 'n' }, gt.seen ? `${gt.right}/${gt.seen}` : '·'),
        ),
      );
    }
  }
  return h(
    'nav',
    { class: 'practice-syllabus', 'aria-label': 'Syllabus' },
    h(
      'p',
      null,
      route.group
        ? [wspLink(makeAddress('practice', route.pool), 'Study every group'), ' · ']
        : h('b', null, 'Studying every group. '),
      'Pick a group to study it alone.',
    ),
    h(
      'table',
      { class: 'grid-table' },
      h(
        'thead',
        null,
        h(
          'tr',
          null,
          h('th', null, 'Group'),
          h('th', null, 'Subject'),
          h('th', null, 'Qs'),
          h('th', null, 'Right'),
        ),
      ),
      h('tbody', null, rows),
    ),
  );
}

function card(
  ctx: PageContext,
  route: PracticeRoute,
  docId: string,
  q: Question,
  index: number,
  total: number,
  group: string | null,
  progress: Progress,
): HTMLElement {
  const here = (i: number) =>
    makeAddress('practice', route.pool, { group: group ?? undefined, n: i });
  const status = h('p', { class: 'practice-status', 'aria-live': 'polite' });
  const buttons = LETTERS.map((l) =>
    h(
      'button',
      { type: 'button', class: 'practice-choice', 'data-letter': l },
      h('b', null, `${l}. `),
      q.choices[l],
    ),
  );
  const next =
    index + 1 < total
      ? wspLink(here(index + 1), h('span', { class: 'btn-like' }, 'Next question'))
      : null;
  const answered = progress.answers[q.id];
  const reveal = (picked: Letter) => {
    for (const b of buttons) {
      const l = b.dataset.letter as Letter;
      b.disabled = true;
      if (l === q.answer) b.classList.add('is-right');
      else if (l === picked) b.classList.add('is-wrong');
    }
    const right = picked === q.answer;
    status.replaceChildren(
      h('b', null, right ? 'Correct. ' : `Not quite: the answer is ${q.answer}. `),
    );
    if (q.reference) status.append(h('span', { class: 'fineprint' }, `FCC rule ${q.reference}`));
    progress.answers[q.id] = right;
    saveProgress(ctx.app, docId, progress);
    next?.focus();
  };
  for (const b of buttons) b.addEventListener('click', () => reveal(b.dataset.letter as Letter));
  const figure = /\bfigure\s+[TGE]-?\d/i.test(q.text)
    ? h(
        'p',
        { class: 'fineprint' },
        'This question refers to a figure: ',
        wspLink(
          searchAddress('diagrams', { source: POOL_SOURCE }),
          'open the pool’s diagram sheets',
        ),
        '.',
      )
    : null;
  const el = h(
    'section',
    { class: 'practice-card box', 'aria-label': 'Question' },
    h(
      'p',
      { class: 'practice-where' },
      h('b', null, q.id),
      ` · ${group ?? 'all groups'} · ${index + 1} of ${total}`,
      answered === undefined
        ? null
        : h('span', { class: 'badge' }, answered ? 'answered right before' : 'missed before'),
    ),
    h('p', { class: 'practice-question' }, q.text),
    h('div', { class: 'practice-choices', role: 'group', 'aria-label': 'Choices' }, buttons),
    figure,
    status,
    h(
      'p',
      { class: 'practice-nav' },
      index > 0 ? [wspLink(here(index - 1), 'Previous'), ' · '] : null,
      next ??
        h(
          'span',
          null,
          'Last question of this set. ',
          wspLink(makeAddress('practice', route.pool), 'Back to the syllabus'),
        ),
    ),
  );
  el.addEventListener('keydown', (ev) => {
    if (ev.ctrlKey || ev.altKey || ev.metaKey) return; // Ctrl+C, Ctrl+D, Alt+D: the browser's
    const key = ev.key.toUpperCase();
    const n = Number.parseInt(key, 10);
    const letter = (LETTERS as readonly string[]).includes(key)
      ? (key as Letter)
      : n >= 1 && n <= 4
        ? LETTERS[n - 1]!
        : null;
    if (letter && !buttons[0]!.disabled) {
      ev.preventDefault();
      reveal(letter);
    } else if (
      (key === 'N' || (ev.key === 'Enter' && !(ev.target instanceof HTMLAnchorElement))) &&
      buttons[0]!.disabled &&
      next
    ) {
      ev.preventDefault();
      next.click();
    }
  });
  return el;
}

/** The first choice still open on a card: the page result focuses it once the page is shown. */
function firstChoice(body: HTMLElement): HTMLElement | undefined {
  return body.querySelector<HTMLElement>('.practice-choice:not(:disabled)') ?? undefined;
}

/* ----------------------------------- exam ----------------------------------- */

function exam(
  ctx: PageContext,
  route: PracticeRoute,
  docId: string,
  title: string,
  pool: Pool,
): PageResult {
  const seed = route.seed!;
  const key = `${docId}#${seed}`;
  let answers = exams.get(key);
  if (!answers) {
    answers = new Map();
    exams.set(key, answers);
  }
  const questions = drawExam(pool, seed);
  const index = Math.min(route.index, questions.length - 1);
  const q = questions[index]!;
  const here = (i: number) => makeAddress('practice', route.pool, { mode: 'exam', seed, n: i });
  const graded = route.index >= questions.length;
  const heading = `${shortTitle(title, pool)}: mock exam`;
  const body = h('div', { class: 'page page-practice' }, pageHeading(heading));
  if (graded) {
    const right = questions.filter((x) => answers!.get(x.id) === x.answer);
    const passed = right.length >= pool.passMark;
    body.append(
      h(
        'p',
        { class: 'practice-result' },
        h('b', null, `${right.length} of ${questions.length} right. `),
        passed ? 'That is a pass' : `A pass needs ${pool.passMark}`,
        ` (${pool.passMark} of ${questions.length}, 74%). `,
        wspLink(
          makeAddress('practice', route.pool, { mode: 'exam' }),
          h('span', { class: 'btn-like' }, 'New exam'),
        ),
        ' · ',
        wspLink(makeAddress('practice', route.pool), 'Back to the syllabus'),
      ),
      h(
        'table',
        { class: 'grid-table' },
        h(
          'thead',
          null,
          h(
            'tr',
            null,
            h('th', null, '#'),
            h('th', null, 'Question'),
            h('th', null, 'You'),
            h('th', null, 'Answer'),
          ),
        ),
        h(
          'tbody',
          null,
          questions.map((x, i) => {
            const mine = answers!.get(x.id);
            return h(
              'tr',
              { class: mine === x.answer ? 'is-right' : 'is-wrong' },
              h('td', null, wspLink(here(i), x.id)),
              h('td', null, x.text),
              h('td', { class: 'n' }, mine ?? '—'),
              h('td', { class: 'n' }, x.answer),
            );
          }),
        ),
      ),
    );
    const p: Progress = loadProgress(ctx.app, docId);
    for (const x of questions)
      if (answers.has(x.id)) p.answers[x.id] = answers.get(x.id) === x.answer;
    saveProgress(ctx.app, docId, p);
    return { title: heading, body };
  }
  const buttons = LETTERS.map((l) =>
    h(
      'button',
      {
        type: 'button',
        class: `practice-choice${answers!.get(q.id) === l ? ' is-picked' : ''}`,
        'data-letter': l,
      },
      h('b', null, `${l}. `),
      q.choices[l],
    ),
  );
  const pick = (l: Letter) => {
    answers!.set(q.id, l);
    for (const b of buttons) b.classList.toggle('is-picked', b.dataset.letter === l);
    ctx.app.navigate(here(index + 1));
  };
  for (const b of buttons) b.addEventListener('click', () => pick(b.dataset.letter as Letter));
  const unanswered = questions.filter((x) => !answers!.has(x.id)).length;
  const el = h(
    'section',
    { class: 'practice-card box', 'aria-label': 'Exam question' },
    h(
      'p',
      { class: 'practice-where' },
      h('b', null, `Question ${index + 1} of ${questions.length}`),
      ` · ${q.id} · ${unanswered} unanswered`,
    ),
    h('p', { class: 'practice-question' }, q.text),
    h('div', { class: 'practice-choices', role: 'group', 'aria-label': 'Choices' }, buttons),
    h(
      'p',
      { class: 'practice-nav' },
      index > 0 ? [wspLink(here(index - 1), 'Previous'), ' · '] : null,
      index + 1 < questions.length ? [wspLink(here(index + 1), 'Skip'), ' · '] : null,
      wspLink(
        here(questions.length),
        h(
          'span',
          { class: 'btn-like' },
          unanswered ? `Grade now (${unanswered} unanswered)` : 'Grade the exam',
        ),
      ),
    ),
  );
  el.addEventListener('keydown', (ev) => {
    if (ev.ctrlKey || ev.altKey || ev.metaKey) return; // Ctrl+C, Ctrl+D, Alt+D: the browser's
    const key = ev.key.toUpperCase();
    const n = Number.parseInt(key, 10);
    const letter = (LETTERS as readonly string[]).includes(key)
      ? (key as Letter)
      : n >= 1 && n <= 4
        ? LETTERS[n - 1]!
        : null;
    if (letter) {
      ev.preventDefault();
      pick(letter);
    }
  });
  body.append(
    h(
      'p',
      { class: 'practice-meta' },
      `Seed ${seed}: the same address draws the same exam. Answers are kept until the page is left.`,
    ),
    el,
  );
  return { title: heading, body, focus: buttons[0] };
}
