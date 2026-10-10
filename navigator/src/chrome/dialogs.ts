/** Period "window" dialogs built on <dialog> (native focus trap, Esc to close). */
import type { DocumentResponse } from '../api/types.ts';
import { docIdToAddress } from '../core/address.ts';
import { type Child, h, uid } from '../core/dom.ts';
import { formatDateTime } from '../core/format.ts';

export function windowDialog(title: string, ...content: Child[]): HTMLDialogElement {
  const titleId = uid('dlg');
  const close = h('button', { type: 'button', class: 'titlebar-btn', 'aria-label': 'Close' }, '×');
  const ok = h('button', { type: 'button', class: 'btn' }, 'Close');
  const dlg = h(
    'dialog',
    { class: 'win-dialog', 'aria-labelledby': titleId },
    h(
      'div',
      { class: 'titlebar' },
      h('span', { class: 'titlebar-text', id: titleId }, title),
      close,
    ),
    h('div', { class: 'win-dialog-body' }, ...content),
    h('div', { class: 'win-dialog-buttons' }, ok),
  );
  const done = () => {
    dlg.close();
  };
  close.addEventListener('click', done);
  ok.addEventListener('click', done);
  dlg.addEventListener('close', () => dlg.remove());
  return dlg;
}

export function showDialog(dlg: HTMLDialogElement): void {
  document.body.append(dlg);
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
}

/** "View Source Data": where the bytes live in the vault, plus the raw record. */
export function sourceDataDialog(doc: DocumentResponse): HTMLDialogElement {
  const rows: Array<[string, Child]> = [
    ['Document id', h('code', null, doc.doc_id)],
    ['Address', h('code', null, docIdToAddress(doc.doc_id))],
    ['Source', `${doc.source_name} (${doc.source_id})`],
    ['Native id', h('code', null, doc.native_id)],
    ['Revision', h('code', null, doc.revision)],
    ['As of', formatDateTime(doc.timestamp)],
    ['License', doc.license],
    ['Content hash', h('code', { class: 'wrap' }, doc.content_hash)],
    ['Generation', h('code', null, doc.generation_id)],
    ['Vault file', h('code', { class: 'wrap' }, doc.vault.path)],
    ['Manifest', h('code', { class: 'wrap' }, doc.vault.manifest)],
    ['SHA-256 (manifest)', h('code', { class: 'wrap' }, doc.vault.sha256)],
  ];
  if (doc.vault.offset !== undefined)
    rows.push(['Offset / length', `${doc.vault.offset} / ${doc.vault.length ?? '?'} bytes`]);
  rows.push(['Upstream (not fetched)', h('code', { class: 'wrap' }, doc.url_native)]);

  const copy = h('button', { type: 'button', class: 'btn btn-small' }, 'Copy vault path');
  copy.addEventListener('click', () => {
    navigator.clipboard?.writeText(doc.vault.path).then(
      () => {
        copy.textContent = 'Copied!';
      },
      () => {
        copy.textContent = 'Copy failed';
      },
    );
  });
  const { html: _html, text: _text, ...meta } = doc;
  return windowDialog(
    `View Source Data — ${doc.title}`,
    h(
      'p',
      null,
      'The direct route: this is where the document lives on disk. The vault is plain files and can be read without this software.',
    ),
    h(
      'table',
      { class: 'kv' },
      h(
        'tbody',
        null,
        ...rows.map(([k, v]) => h('tr', null, h('th', { scope: 'row' }, k), h('td', null, v))),
      ),
    ),
    h('p', null, copy),
    h(
      'details',
      null,
      h('summary', null, 'Raw API record (JSON, body omitted)'),
      h('pre', { class: 'raw-json' }, JSON.stringify(meta, null, 2)),
    ),
  );
}
