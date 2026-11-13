/**
 * Search-as-you-type suggestions as an ARIA 1.2 combobox. Debounced 50 ms,
 * stale requests aborted, local history matches shown instantly.
 *
 * A suggestion from the API opens its document, and one an alias matched shows
 * the alias with the title beside it: the list showed each document's own title
 * (磷 for "pho", matched by its English label) and picking one searched for
 * that text (2026-11-05 deep dive). Documents in the Navigator's language come
 * first.
 *
 * A space typed after a word is sent (one, however many were typed): it tells
 * the API the word is finished, so "alan " offers "Alan Turing" and not
 * "Alans". The box's text was trimmed before it was asked, and the API's rule
 * never reached a reader (review of the 2026-11-10 trawl).
 */
import type { Suggestion, Suggestions } from '../api/types.ts';
import { debounce, h, uid, uiLanguage } from '../core/dom.ts';
import type { AppServices } from '../pages/types.ts';

export const SUGGEST_DEBOUNCE_MS = 50;

export type PickedSuggestion = Suggestion;

export interface SuggestOptions {
  onPick: (s: PickedSuggestion) => void;
  /** Include matching local history entries (location bar). */
  history?: boolean;
  limit?: number;
}

/**
 * What the suggestions are asked for: the text typed without the spaces around
 * it, and one space after it when the reader typed any there.
 */
export function typedQuery(value: string): string {
  const q = value.trim();
  return q && /\s$/.test(value) ? `${q} ` : q;
}

/** The API's suggestions, each with the page it opens (an older API sends titles only). */
export function remoteSuggestions(r: Suggestions): PickedSuggestion[] {
  const items = r.items ?? [];
  if (items.length === 0) return r.suggestions.map((text) => ({ text }));
  return items.map((i) => ({
    text: i.text,
    address: i.address ?? undefined,
    title: i.title !== i.text ? i.title : undefined,
  }));
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
  let waiting = false; // suggestions asked for (or about to be) and not shown yet
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
    const word = ql.trim(); // "alan " also shows the page "Alan", typed in full
    list.replaceChildren(
      ...items.map((s, i) => {
        const text = s.text;
        const tl = text.toLowerCase();
        const k = tl.startsWith(ql) ? q.length : tl.startsWith(word) ? word.length : 0;
        const li = h(
          'li',
          { id: `${listId}-${i}`, role: 'option', class: 'suggest-item', 'aria-selected': 'false' },
          h(
            'span',
            { class: 'suggest-text' },
            k ? [h('b', null, text.slice(0, k)), text.slice(k)] : text,
          ),
          s.history
            ? h('span', { class: 'suggest-kind' }, 'History')
            : s.title
              ? h('span', { class: 'suggest-kind' }, `→ ${s.title}`)
              : null,
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
    const typed = q.trim().toLowerCase(); // history is matched anywhere in a title: no word boundary
    const local: PickedSuggestion[] = opts.history
      ? app.history
          .list()
          .filter((e) => e.title.toLowerCase().includes(typed) || e.address.includes(typed))
          .slice(0, 3)
          .map((e) => ({ text: e.title || e.address, address: e.address, history: true }))
      : [];
    if (local.length) show(q, local);
    app.api
      .suggest(q, limit, ac.signal, uiLanguage())
      .then((r) => {
        if (ac.signal.aborted || typedQuery(input.value) !== q) return;
        waiting = false;
        const seen = new Set(local.map((l) => l.text.toLowerCase()));
        const remote = remoteSuggestions(r).filter((s) => !seen.has(s.text.toLowerCase()));
        show(q, [...local, ...remote].slice(0, limit));
      })
      .catch(() => {
        waiting = false; // suggestions are best-effort
      });
  }, SUGGEST_DEBOUNCE_MS);

  const onInput = () => {
    const q = typedQuery(input.value);
    if (q.trim().length < 2 || /^wsp:/i.test(q)) {
      fetchSuggestions.cancel();
      controller?.abort();
      close();
      return;
    }
    waiting = true;
    fetchSuggestions(q);
  };

  const onKey = (ev: KeyboardEvent) => {
    if (ev.key === 'Escape') {
      // Suggestions still on their way are dismissed too: one arriving after Escape opened the list
      // again, over the buttons a reader was about to press (2026-10-19, the Lucky e2e test). Only
      // dismissing: with nothing to dismiss, Escape keeps its own meaning (a search box clears).
      const dismissing = !list.hidden || waiting;
      fetchSuggestions.cancel();
      controller?.abort();
      waiting = false;
      if (dismissing) {
        ev.preventDefault();
        ev.stopPropagation();
      }
      close();
      return;
    }
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
