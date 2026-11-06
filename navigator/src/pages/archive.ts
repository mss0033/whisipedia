/**
 * What a stored zip archive holds, on its document page (2026-11-05, the gNATSGO archive: one
 * 117 GB zip whose page offered only a Download of the whole). The API reads the archive's central
 * directory, never unpacking it; here the files are shown grouped by folder with their sizes, each
 * with its own download (unpacked as it streams), and text, CSV and metadata files can be read in
 * place: ISO 19115 / FGDC metadata as sections and a data dictionary, not raw XML.
 */
import { ApiRequestError } from '../api/client.ts';
import type {
  ArchiveFolder,
  ArchiveListing,
  ArchiveMember,
  ArchiveMetadata,
  ArchiveView,
  DictionaryEntity,
  DocId,
} from '../api/types.ts';
import { append, type Child, h } from '../core/dom.ts';
import { formatBytes, formatNumber } from '../core/format.ts';

/** What the browser needs from the API client. */
export interface ArchiveApi {
  archive(
    id: DocId,
    folder?: string,
    signal?: AbortSignal,
    after?: string,
  ): Promise<ArchiveListing>;
  archiveView(id: DocId, path: string, signal?: AbortSignal): Promise<ArchiveView>;
  url(path: string): string;
}

export interface ArchiveContext {
  api: ArchiveApi;
  docId: DocId;
  signal?: AbortSignal | undefined;
}

/** Folders open by default when the listing is this small; a bigger one opens folder by folder. */
export const OPEN_FOLDERS_UP_TO = 200;
/** Rows of a CSV file shown as a table; the rest are in the download. */
export const CSV_ROWS = 500;
const README = /^[\s_\d-]*read[\s_-]?me/i;

const plural = (n: number, one: string, many = `${one}s`) =>
  `${formatNumber(n)} ${n === 1 ? one : many}`;

function problem(err: unknown): string {
  if (err instanceof ApiRequestError) {
    if (err.isBusy) return 'The library is busy just now; try again in a moment.';
    return err.message;
  }
  return 'The archive could not be read.';
}

/**
 * The archive browser: loads the listing (all of it, or one folder with `folder`) into a section
 * and replaces itself when the reader narrows to a folder or goes back to the whole.
 */
export function archiveBrowser(ctx: ArchiveContext, folder = ''): HTMLElement {
  const box = h(
    'section',
    { class: 'archive', 'aria-label': 'What is in the archive', 'aria-busy': 'true' },
    h('h2', { class: 'archive-heading' }, 'What is in the archive'),
    h('p', { class: 'fineprint' }, 'Reading the archive’s list of files…'),
  );
  const show = (next: string) => box.replaceWith(archiveBrowser(ctx, next));
  ctx.api
    .archive(ctx.docId, folder || undefined, ctx.signal)
    .then((listing) => {
      box.replaceChildren();
      append(box, archiveContents(listing, ctx, show));
      box.removeAttribute('aria-busy');
    })
    .catch((err: unknown) => {
      if (ctx.signal?.aborted) return;
      box.removeAttribute('aria-busy');
      box.replaceChildren(
        h('h2', { class: 'archive-heading' }, 'What is in the archive'),
        h('p', { class: 'notice' }, problem(err)),
      );
    });
  return box;
}

/** The folder a listing's `next` resumes in: a file's folder, or none when it is a folder. */
function resumesIn(next: string | null): string | null {
  return next && !next.endsWith('/') ? next.slice(0, next.lastIndexOf('/') + 1) : null;
}

/**
 * The listing's nodes: a summary, the files to read first, then every folder. A listing cut short
 * lists more in place, page by page from where it stopped (its `next`), so every file is reachable:
 * "Show all" on the folder being listed used to fetch the same first page again (2026-11-05 review).
 */
export function archiveContents(
  listing: ArchiveListing,
  ctx: ArchiveContext,
  show: (folder: string) => void,
): Child[] {
  let listed = listing.folders.reduce((n, f) => n + f.files.length, 0);
  let next = listing.next;
  const open = listed <= OPEN_FOLDERS_UP_TO;
  const views = new Map<string, FolderView>();
  const firstList = h('ul', null);
  const firstBox = h(
    'div',
    { class: 'archive-first', hidden: true },
    h('h3', null, 'Read first'),
    firstList,
  );
  const foldersBox = h('div', { class: 'archive-folders' });
  const banner = h('p', { class: 'notice archive-truncated' });
  const moreButton = h('button', { type: 'button', class: 'btn archive-next-page' }, 'List more');
  const moreProblem = h('span', { class: 'archive-next-problem' });
  const morePara = h('p', { class: 'archive-next' }, moreButton, ' ', moreProblem);

  const refresh = () => {
    const cut = listing.truncated && next !== null;
    banner.hidden = !cut;
    morePara.hidden = !cut;
    banner.textContent = cut
      ? `${plural(listed, 'file')} of ${formatNumber(listing.members)} listed so far. ` +
        '“List more” lists the next ones; “Show all” on a folder lists just that folder.'
      : '';
    const resume = resumesIn(next);
    for (const v of views.values()) v.update(resume);
  };
  const addPage = (page: ArchiveListing) => {
    const first = page.folders
      .flatMap((f) => f.files)
      .filter((m) => m.view === 'metadata' || (m.view === 'text' && README.test(m.name)));
    for (const m of first) firstList.append(h('li', null, memberLine(m, listing, ctx)));
    if (first.length) firstBox.hidden = false;
    for (const f of page.folders) {
      const known = views.get(f.path);
      if (known) known.add(f.files);
      else if (f.count > 0 || f.total_count === 0) {
        // Folders that only hold folders are not shown as empty boxes. A page lists folders in path
        // order, past every folder an earlier page listed: a new one goes at the end.
        const v = folderView(f, listing, ctx, open, show, () => loadMore());
        v.add(f.files);
        views.set(f.path, v);
        foldersBox.append(v.el);
      }
    }
  };
  let loading = false;
  const loadMore = () => {
    if (loading || next === null) return;
    loading = true;
    moreButton.disabled = true;
    moreProblem.textContent = '';
    for (const v of views.values()) v.busy(true);
    ctx.api
      .archive(ctx.docId, listing.folder || undefined, ctx.signal, next)
      .then((page) => {
        listed += page.folders.reduce((n, f) => n + f.files.length, 0);
        next = page.next;
        addPage(page);
      })
      .catch((err: unknown) => {
        if (ctx.signal?.aborted) return;
        moreProblem.textContent = problem(err);
      })
      .finally(() => {
        loading = false;
        moreButton.disabled = false;
        for (const v of views.values()) v.busy(false);
        refresh();
      });
  };
  moreButton.addEventListener('click', loadMore);
  addPage(listing);
  refresh();

  return [
    h(
      'h2',
      { class: 'archive-heading' },
      listing.folder ? `In the folder ${listing.folder}` : 'What is in the archive',
    ),
    h(
      'p',
      { class: 'archive-summary' },
      `${plural(listing.members, 'file')}, ${formatBytes(listing.bytes)} unpacked`,
      listing.packed !== listing.bytes ? ` (${formatBytes(listing.packed)} as stored)` : '',
      listing.folders.length > 1 ? `, in ${plural(listing.folders.length, 'folder')}` : '',
      '. Each file downloads on its own, unpacked as it is sent.',
      listing.folder
        ? [
            ' ',
            h(
              'button',
              { type: 'button', class: 'btn archive-all', onclick: () => show('') },
              'Show the whole archive',
            ),
          ]
        : null,
    ),
    banner,
    firstBox,
    foldersBox,
    morePara,
  ];
}

function depth(path: string): number {
  return path ? path.split('/').length - 2 : 0;
}

function folderSummary(f: ArchiveFolder): string {
  const own = f.count ? `${plural(f.count, 'file')}, ${formatBytes(f.bytes)}` : 'empty';
  return f.total_count > f.count
    ? `${own}; ${plural(f.total_count, 'file')}, ${formatBytes(f.total_bytes)} with the folders in it`
    : own;
}

/** One folder's box, filled in as later pages list more of its files. */
interface FolderView {
  el: HTMLElement;
  /** Files of the folder a later page listed. */
  add(files: ArchiveMember[]): void;
  /** Its "more files" line, given the folder the next page resumes in. */
  update(resumesIn: string | null): void;
  busy(on: boolean): void;
}

function folderView(
  f: ArchiveFolder,
  listing: ArchiveListing,
  ctx: ArchiveContext,
  open: boolean,
  show: (folder: string) => void,
  more: () => void,
): FolderView {
  const name = f.path || '(top of the archive)';
  let shown = 0;
  const tbody = h('tbody', null);
  const table = h(
    'table',
    { class: 'archive-files', hidden: true },
    h(
      'thead',
      null,
      h(
        'tr',
        null,
        h('th', { scope: 'col' }, 'File'),
        h('th', { scope: 'col' }, 'Size'),
        h('th', { scope: 'col' }, 'Dated'),
        h('th', { scope: 'col' }, h('span', { class: 'visually-hidden' }, 'Actions')),
      ),
    ),
    tbody,
  );
  const moreLine = h('p', { class: 'archive-more', hidden: true });
  const el = h(
    'details',
    {
      class: `archive-folder archive-depth-${Math.min(depth(f.path), 6)}`,
      open: open && f.count > 0,
    },
    h(
      'summary',
      null,
      h('span', { class: 'archive-folder-name' }, name),
      ' ',
      h('span', { class: 'archive-folder-size' }, folderSummary(f)),
    ),
    table,
    moreLine,
  );
  let nextButton: HTMLButtonElement | null = null;
  return {
    el,
    add(files) {
      for (const m of files) append(tbody, memberRow(m, listing, ctx));
      shown += files.length;
      if (shown) table.hidden = false;
    },
    update(resume) {
      const left = f.count - shown;
      nextButton = null;
      moreLine.hidden = left <= 0;
      if (left <= 0) {
        moreLine.replaceChildren();
        return;
      }
      // The next page starts in this folder: list it in place. Any other folder cut short (one past
      // the last file listed) is listed on its own.
      nextButton =
        resume === f.path
          ? h(
              'button',
              { type: 'button', class: 'btn archive-next-folder', onclick: more },
              'List more',
            )
          : null;
      moreLine.replaceChildren(
        `${plural(left, 'more file')} in this folder. `,
        nextButton ??
          h(
            'button',
            { type: 'button', class: 'btn archive-show-folder', onclick: () => show(f.path) },
            `Show all ${formatNumber(f.count)}`,
          ),
      );
    },
    busy(on) {
      if (nextButton) nextButton.disabled = on;
    },
  };
}

export function memberUrl(listing: ArchiveListing, ctx: ArchiveContext, m: ArchiveMember): string {
  return ctx.api.url(`${listing.member_url}?path=${encodeURIComponent(m.path)}`);
}

function download(m: ArchiveMember, listing: ArchiveListing, ctx: ArchiveContext): HTMLElement {
  return h(
    'a',
    { href: memberUrl(listing, ctx, m), download: m.name, class: 'archive-download' },
    'Download',
  );
}

function memberRow(m: ArchiveMember, listing: ArchiveListing, ctx: ArchiveContext): HTMLElement[] {
  const slot = h('td', { colspan: '4', class: 'archive-view-slot' });
  const viewRow = h('tr', { class: 'archive-view-row', hidden: true }, slot);
  const read = m.view ? readButton(m, ctx, slot, viewRow) : null;
  const row = h(
    'tr',
    null,
    h('td', { class: 'archive-name' }, m.name),
    h('td', { class: 'archive-size' }, formatBytes(m.size)),
    h('td', { class: 'archive-date' }, m.modified ? m.modified.slice(0, 10) : ''),
    h('td', { class: 'archive-actions' }, download(m, listing, ctx), read ? [' · ', read] : null),
  );
  return [row, viewRow];
}

function memberLine(m: ArchiveMember, listing: ArchiveListing, ctx: ArchiveContext): Child[] {
  const slot = h('div', { class: 'archive-view-slot', hidden: true });
  return [
    h('code', null, m.path),
    ` (${formatBytes(m.size)}) · `,
    readButton(m, ctx, slot, slot),
    ' · ',
    download(m, listing, ctx),
    slot,
  ];
}

/** "Read" / "Read the metadata": loads the file into `slot` once, then shows and hides it. */
function readButton(
  m: ArchiveMember,
  ctx: ArchiveContext,
  slot: HTMLElement,
  shown: HTMLElement,
): HTMLButtonElement {
  const label = m.view === 'metadata' ? 'Read the metadata' : 'Read';
  const button = h(
    'button',
    { type: 'button', class: 'btn archive-read', 'aria-expanded': 'false' },
    label,
  );
  let loaded = false;
  button.addEventListener('click', () => {
    const open = shown.hidden;
    shown.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    button.textContent = open ? 'Hide' : label;
    if (!open || loaded) return;
    loaded = true;
    slot.replaceChildren(h('p', { class: 'fineprint' }, `Reading ${m.name}…`));
    ctx.api
      .archiveView(ctx.docId, m.path, ctx.signal)
      .then((view) => slot.replaceChildren(renderView(view)))
      .catch((err: unknown) => {
        loaded = false; // asked again, it tries again
        slot.replaceChildren(h('p', { class: 'notice' }, problem(err)));
      });
  });
  return button;
}

/** A file read in place: metadata as sections, a CSV as a table, anything else as text. */
export function renderView(view: ArchiveView): HTMLElement {
  if (view.metadata) return renderMetadata(view.metadata);
  const text = view.text ?? '';
  const cut = view.truncated
    ? h(
        'p',
        { class: 'fineprint' },
        `Only the start of the file is shown (it is ${formatBytes(view.size)}); download it for the rest.`,
      )
    : null;
  const sep = /\.tsv$|\.tab$/i.test(view.name) ? '\t' : /\.csv$/i.test(view.name) ? ',' : null;
  const rows = sep ? parseDelimited(text, sep, CSV_ROWS + 1, view.truncated) : null;
  if (rows && rows.length > 1 && rows[0]!.length > 1) {
    const [head, ...body] = rows;
    return h(
      'div',
      { class: 'archive-view' },
      h(
        'div',
        { class: 'archive-table-wrap' },
        h(
          'table',
          { class: 'archive-csv' },
          h(
            'thead',
            null,
            h(
              'tr',
              null,
              head!.map((c) => h('th', { scope: 'col' }, c)),
            ),
          ),
          h(
            'tbody',
            null,
            body.slice(0, CSV_ROWS).map((r) =>
              h(
                'tr',
                null,
                r.map((c) => h('td', null, c)),
              ),
            ),
          ),
        ),
      ),
      body.length > CSV_ROWS
        ? h('p', { class: 'fineprint' }, `The first ${formatNumber(CSV_ROWS)} rows are shown.`)
        : null,
      cut,
    );
  }
  return h('div', { class: 'archive-view' }, h('pre', { class: 'archive-text' }, text), cut);
}

/**
 * Rows of delimited text (RFC 4180 quoting), at most `limit`. A file cut short (`truncated`)
 * loses its last, probably partial, row.
 */
export function parseDelimited(
  text: string,
  sep: string,
  limit: number,
  truncated = false,
): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = '';
  let quoted = false;
  let i = 0;
  const end = () => {
    row.push(cell);
    cell = '';
  };
  while (i < text.length && rows.length < limit) {
    const c = text[i]!;
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        cell += '"';
        i += 2;
        continue;
      }
      if (c === '"') quoted = false;
      else cell += c;
      i += 1;
      continue;
    }
    if (c === '"' && cell === '') quoted = true;
    else if (c === sep) end();
    else if (c === '\n' || c === '\r') {
      end();
      rows.push(row);
      row = [];
      if (c === '\r' && text[i + 1] === '\n') i += 1;
    } else cell += c;
    i += 1;
  }
  if (rows.length < limit && (cell || row.length) && !truncated) {
    end();
    rows.push(row);
  }
  return rows;
}

/** ISO 19115 / FGDC metadata: title and abstract, then a collapsible section per part. */
export function renderMetadata(meta: ArchiveMetadata): HTMLElement {
  return h(
    'div',
    { class: 'archive-view archive-metadata' },
    h('p', { class: 'fineprint' }, `Metadata (${meta.standard})`),
    meta.title ? h('h3', { class: 'archive-metadata-title' }, meta.title) : null,
    meta.abstract ? h('p', { class: 'archive-metadata-abstract' }, meta.abstract) : null,
    meta.sections.map((s, i) =>
      h(
        'details',
        { class: 'archive-metadata-section', open: i === 0 },
        h('summary', null, s.heading),
        h(
          'dl',
          null,
          s.fields.map((f) => [h('dt', null, f.label || s.heading), h('dd', null, f.value)]),
        ),
        s.more
          ? h('p', { class: 'fineprint' }, `${plural(s.more, 'more field')} not shown.`)
          : null,
      ),
    ),
    meta.dictionary.length
      ? h(
          'details',
          { class: 'archive-metadata-section archive-dictionary' },
          h(
            'summary',
            null,
            `Data dictionary: ${plural(meta.dictionary.length + meta.more_entities, 'table')}`,
          ),
          meta.dictionary.map(entityBlock),
          meta.more_entities
            ? h(
                'p',
                { class: 'fineprint' },
                `${plural(meta.more_entities, 'more table')} not shown.`,
              )
            : null,
        )
      : null,
  );
}

function entityBlock(e: DictionaryEntity): HTMLElement {
  return h(
    'details',
    { class: 'archive-entity' },
    h(
      'summary',
      null,
      h('code', null, e.name),
      e.label ? ` ${e.label}` : '',
      ` (${plural(e.attributes.length + e.more_attributes, 'column')})`,
    ),
    e.definition ? h('p', null, e.definition) : null,
    e.attributes.length
      ? h(
          'div',
          { class: 'archive-table-wrap' },
          h(
            'table',
            { class: 'archive-columns' },
            h(
              'thead',
              null,
              h(
                'tr',
                null,
                h('th', { scope: 'col' }, 'Column'),
                h('th', { scope: 'col' }, 'Meaning'),
                h('th', { scope: 'col' }, 'Type'),
                h('th', { scope: 'col' }, 'Values'),
              ),
            ),
            h(
              'tbody',
              null,
              e.attributes.map((a) =>
                h(
                  'tr',
                  null,
                  h('td', null, h('code', null, a.name)),
                  h('td', null, a.definition ?? ''),
                  h(
                    'td',
                    null,
                    [a.type, a.unit ? `in ${a.unit}` : null].filter(Boolean).join(', '),
                  ),
                  h(
                    'td',
                    null,
                    a.values.length
                      ? h(
                          'ul',
                          { class: 'archive-values' },
                          a.values.map((v) => h('li', null, v)),
                          a.more_values
                            ? h('li', null, `and ${formatNumber(a.more_values)} more`)
                            : null,
                        )
                      : '',
                  ),
                ),
              ),
            ),
          ),
        )
      : null,
    e.more_attributes
      ? h('p', { class: 'fineprint' }, `${plural(e.more_attributes, 'more column')} not shown.`)
      : null,
  );
}
