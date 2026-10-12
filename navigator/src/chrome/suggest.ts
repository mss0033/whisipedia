/**
 * Search-as-you-type suggestions as an ARIA 1.2 combobox. Debounced 50 ms,
 * stale requests aborted, local history matches shown instantly.
 */
import type { Suggestion } from '../api/types.ts';
import { debounce, h, uid } from '../core/dom.ts';
import type { AppServices } from '../pages/types.ts';

export const SUGGEST_DEBOUNCE_MS = 50;

export type PickedSuggestion = Suggestion;

export interface SuggestOptions {
  onPick: (s: PickedSuggestion) => void;
  /** Include matching local history entries (location bar). */
  history?: boolean;
  limit?: number;
}

export function attachSuggest(
  input: HTMLInputElement,
  app: AppServices,
  opts: SuggestOptions,
): () => void {
  const listId = uid('suggest');
  const list = h('ul', {
    id: listId,
    role: 'listbox',
    class: 'suggest-list',
    hidden: true,
    'aria-label': 'Suggestions',
  });
  const wrap = h('span', { class: 'suggest-wrap' });
  input.replaceWith(wrap);
  wrap.append(input, list);
  input.setAttribute('role', 'combobox');
  input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-expanded', 'false');
  input.setAttribute('aria-controls', listId);

  let items: PickedSuggestion[] = [];
  let active = -1;
  let controller: AbortController | null = null;
  const limit = opts.limit ?? 8;

  const close = () => {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    active = -1;
  };

  const setActive = (i: number) => {
    active = i;
    for (const [j, li] of Array.from(list.children).entries()) {
      li.setAttribute('aria-selected', j === i ? 'true' : 'false');
    }
    const el = list.children[i];
    if (el) input.setAttribute('aria-activedescendant', el.id);
    else input.removeAttribute('aria-activedescendant');
  };

  const show = (q: string, next: PickedSuggestion[]) => {
    items = next;
    if (items.length === 0 || document.activeElement !== input) {
      close();
      return;
    }
    const ql = q.toLowerCase();
    list.replaceChildren(
      ...items.map((s, i) => {
        const text = s.text;
        const k = text.toLowerCase().startsWith(ql) ? q.length : 0;
        const li = h(
          'li',
          { id: `${listId}-${i}`, role: 'option', class: 'suggest-item', 'aria-selected': 'false' },
          h(
            'span',
            { class: 'suggest-text' },
            k ? [h('b', null, text.slice(0, k)), text.slice(k)] : text,
          ),
          s.address ? h('span', { class: 'suggest-kind' }, 'History') : null,
        );
        li.addEventListener('pointerdown', (ev) => ev.preventDefault());
        li.addEventListener('click', () => {
          close();
          opts.onPick(s);
        });
        return li;
      }),
    );
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    active = -1;
  };

  const fetchSuggestions = debounce((q: string) => {
    controller?.abort();
    const ac = new AbortController();
    controller = ac;
    const local: PickedSuggestion[] = opts.history
      ? app.history
          .list()
          .filter(
            (e) =>
              e.title.toLowerCase().includes(q.toLowerCase()) ||
              e.address.includes(q.toLowerCase()),
          )
          .slice(0, 3)
          .map((e) => ({ text: e.title || e.address, address: e.address }))
      : [];
    if (local.length) show(q, local);
    app.api
      .suggest(q, limit, ac.signal)
      .then((r) => {
        if (ac.signal.aborted || input.value.trim() !== q) return;
        const seen = new Set(local.map((l) => l.text.toLowerCase()));
        const remote: PickedSuggestion[] = r.suggestions
          .filter((text) => !seen.has(text.toLowerCase()))
          .map((text) => ({ text }));
        show(q, [...local, ...remote].slice(0, limit));
      })
      .catch(() => {
        /* suggestions are best-effort */
      });
  }, SUGGEST_DEBOUNCE_MS);

  const onInput = () => {
    const q = input.value.trim();
    if (q.length < 2 || /^wsp:/i.test(q)) {
      fetchSuggestions.cancel();
      controller?.abort();
      close();
      return;
    }
    fetchSuggestions(q);
  };

  const onKey = (ev: KeyboardEvent) => {
    if (list.hidden) {
      if (ev.key === 'ArrowDown' && input.value.trim().length >= 2) onInput();
      return;
    }
    if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      setActive((active + 1) % items.length);
    } else if (ev.key === 'ArrowUp') {
      ev.preventDefault();
      setActive(active <= 0 ? items.length - 1 : active - 1);
    } else if (ev.key === 'Escape') {
      ev.preventDefault();
      ev.stopPropagation();
      close();
    } else if (ev.key === 'Enter' && active >= 0) {
      ev.preventDefault();
      const s = items[active]!;
      close();
      opts.onPick(s);
    } else if (ev.key === 'Tab') {
      close();
    }
  };

  input.addEventListener('input', onInput);
  input.addEventListener('keydown', onKey);
  input.addEventListener('blur', close);

  return () => {
    fetchSuggestions.cancel();
    controller?.abort();
    input.removeEventListener('input', onInput);
    input.removeEventListener('keydown', onKey);
    input.removeEventListener('blur', close);
  };
}
