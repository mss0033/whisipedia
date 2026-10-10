/**
 * The Navigator application: window chrome, navigation controller, keyboard
 * shortcuts. Pages render into <main>; everything else lives here.
 */
import { ApiRequestError, WspClient } from './api/client.ts';
import type { DocumentResponse, SourceInfo } from './api/types.ts';
import { showDialog, sourceDataDialog } from './chrome/dialogs.ts';
import { type IconName, icon, throbberSvg } from './chrome/icons.ts';
import { MenuBar, type MenuDef, type MenuItem } from './chrome/menus.ts';
import { attachSuggest } from './chrome/suggest.ts';
import {
  docIdToAddress,
  HOME_ADDRESS,
  hashToAddress,
  interpretInput,
  isBuiltinHost,
  makeAddress,
  parseAddress,
  searchAddress,
} from './core/address.ts';
import { h } from './core/dom.ts';
import { GlobalHistory, Hotlist, SessionStack } from './core/history.ts';
import { type Route, resolveRoute } from './core/router.ts';
import { SafeStorage } from './core/storage.ts';
import { THEMES, ThemeManager } from './core/theme.ts';
import * as directoryPage from './pages/directory.ts';
import * as documentPage from './pages/document.ts';
import * as homePage from './pages/home.ts';
import {
  renderAbout,
  renderHelp,
  renderInvalid,
  renderOffline,
  renderRandom,
  VERSION,
} from './pages/info.ts';
import { renderHistory, renderHotlist } from './pages/local.ts';
import * as searchPage from './pages/search.ts';
import * as sitePage from './pages/site.ts';
import * as systemPage from './pages/system.ts';
import type {
  AppServices,
  NavOptions,
  PageContext,
  PageRenderer,
  PageResult,
} from './pages/types.ts';
import * as whatsnewPage from './pages/whatsnew.ts';
import { errorPage, wspLink } from './ui/components.ts';

const RENDERERS: Record<Route['page'], PageRenderer> = {
  home: homePage.render,
  directory: directoryPage.render,
  search: searchPage.render,
  whatsnew: whatsnewPage.render,
  system: systemPage.render,
  history: renderHistory,
  hotlist: renderHotlist,
  about: renderAbout,
  help: renderHelp,
  random: renderRandom,
  offline: renderOffline,
  site: sitePage.render,
  document: documentPage.render,
  invalid: renderInvalid,
};

const MAX_REDIRECTS = 5;
const APP_NAME = 'Whisipedia Navigator';

type Mode = 'push' | 'replace' | 'none';

export interface AppOptions {
  api?: WspClient;
  storage?: SafeStorage;
}

export class App implements AppServices {
  readonly api: WspClient;
  readonly history: GlobalHistory;
  readonly hotlist: Hotlist;
  readonly theme: ThemeManager;
  readonly stack = new SessionStack(1000);

  private readonly root: HTMLElement;
  private main!: HTMLElement;
  private titleText!: HTMLElement;
  private locInput!: HTMLInputElement;
  private locLabel!: HTMLLabelElement;
  private statusText!: HTMLElement;
  private progress!: HTMLProgressElement;
  private live!: HTMLElement;
  private throbber!: HTMLButtonElement;
  private menu!: MenuBar;
  private buttons = new Map<string, HTMLButtonElement>();

  private controller: AbortController | null = null;
  private seq = 0;
  private loading = false;
  private defaultStatus = 'Document: Done.';
  private statusTimer: ReturnType<typeof setTimeout> | undefined;
  private sourcesPromise: Promise<SourceInfo[]> | null = null;
  private currentDoc: DocumentResponse | null = null;
  private pendingDoc: DocumentResponse | null = null;
  private currentTitle = APP_NAME;

  constructor(root: HTMLElement, opts: AppOptions = {}) {
    this.root = root;
    this.api = opts.api ?? new WspClient();
    const storage = opts.storage ?? new SafeStorage();
    this.history = new GlobalHistory(storage);
    this.hotlist = new Hotlist(storage);
    this.theme = new ThemeManager(storage, document.documentElement);
    this.build();
  }

  /* ------------------------------ services ------------------------------ */

  sources(): Promise<SourceInfo[]> {
    if (!this.sourcesPromise) {
      this.sourcesPromise = this.api
        .sources()
        .then((r) => r.sources)
        .catch(() => {
          this.sourcesPromise = null;
          return [];
        });
    }
    return this.sourcesPromise;
  }

  status(text: string): void {
    this.statusText.textContent = text;
    this.live.textContent = text;
    if (this.statusTimer !== undefined) clearTimeout(this.statusTimer);
    this.statusTimer = setTimeout(() => {
      if (!this.loading) this.statusText.textContent = this.defaultStatus;
    }, 4000);
  }

  openSourceData(doc: DocumentResponse): void {
    showDialog(sourceDataDialog(doc));
  }

  setCurrentDocument(doc: DocumentResponse | null): void {
    this.pendingDoc = doc;
  }

  navigate(address: string, opts: NavOptions = {}): void {
    void this.load(address, 'push', opts);
  }

  /* ------------------------------ lifecycle ----------------------------- */

  start(): void {
    const initial = hashToAddress(location.hash) ?? HOME_ADDRESS;
    window.addEventListener('popstate', (ev) => this.onPopState(ev));
    document.addEventListener('keydown', (ev) => this.onKey(ev));
    void this.load(initial, 'replace', {}, { focus: false });
  }

  back(): void {
    if (this.stack.canBack) history.back();
  }

  forward(): void {
    if (this.stack.canForward) history.forward();
  }

  reload(): void {
    const cur = this.stack.current;
    if (cur) void this.load(cur.address, 'none', { reload: true });
  }

  stop(): void {
    if (!this.loading) return;
    this.controller?.abort();
    this.seq += 1;
    this.endLoading('Transfer interrupted!');
  }

  home(): void {
    this.navigate(HOME_ADDRESS);
  }

  bookmarkCurrent(): void {
    const cur = this.stack.current;
    if (!cur) return;
    const added = this.hotlist.add(cur.address, this.currentTitle);
    this.status(
      added
        ? `Added “${this.currentTitle}” to the Hotlist.`
        : `“${this.currentTitle}” is already in the Hotlist.`,
    );
  }

  viewSourceData(): void {
    if (this.currentDoc) this.openSourceData(this.currentDoc);
    else this.status('View Source Data is available on document pages.');
  }

  focusLocation(): void {
    this.locInput.focus();
    this.locInput.select();
  }

  private onPopState(ev: PopStateEvent): void {
    const pos = (ev.state as { pos?: number } | null)?.pos;
    const entry = typeof pos === 'number' ? this.stack.go(pos) : undefined;
    if (entry) {
      void this.load(entry.address, 'none', {}, { scrollY: entry.scrollY });
      return;
    }
    const addr = hashToAddress(location.hash);
    if (addr) void this.load(addr, 'push', {}, { pushState: false });
  }

  /* ------------------------------ navigation ---------------------------- */

  private async load(
    address: string,
    mode: Mode,
    opts: NavOptions,
    extra: { scrollY?: number; focus?: boolean; redirects?: number; pushState?: boolean } = {},
  ): Promise<void> {
    if (this.stack.current) this.stack.setScroll(this.main.scrollTop);
    this.controller?.abort();
    const ac = new AbortController();
    this.controller = ac;
    const seq = ++this.seq;
    const started = performance.now();
    const parsed = parseAddress(address);
    this.beginLoading(parsed?.host ?? 'unknown');
    this.pendingDoc = null;

    const route = resolveRoute(address);
    const ctx: PageContext = { route, address, app: this, signal: ac.signal, opts };
    let result: PageResult;
    try {
      const waiting = setTimeout(() => {
        if (seq === this.seq) this.setProgress('Host contacted. Waiting for reply…', 40);
      }, 120);
      result = await RENDERERS[route.page](ctx);
      clearTimeout(waiting);
    } catch (err) {
      if (ac.signal.aborted || seq !== this.seq) return;
      result = this.failurePage(err, address);
    }
    if (seq !== this.seq) return;

    if ('redirect' in result) {
      const n = (extra.redirects ?? 0) + 1;
      if (n <= MAX_REDIRECTS) {
        void this.load(result.redirect, mode === 'none' ? 'replace' : mode, opts, {
          ...extra,
          redirects: n,
          scrollY: 0,
        });
        return;
      }
      result = {
        title: 'Too many redirects',
        body: errorPage({
          title: 'Redirect loop',
          heading: 'Redirect loop',
          message: 'This address redirects too many times.',
          address,
        }),
      };
    }

    this.setProgress(`Transferring data from ${parsed?.host ?? 'library'}…`, 80);

    // Commit
    const title = result.title;
    if (mode === 'push') {
      this.stack.push(address, title);
      if (extra.pushState !== false)
        history.pushState({ pos: this.stack.position }, '', `#${address}`);
      else history.replaceState({ pos: this.stack.position }, '', `#${address}`);
    } else if (mode === 'replace') {
      this.stack.replace(address, title);
      history.replaceState({ pos: this.stack.position }, '', `#${address}`);
    } else {
      this.stack.setTitle(title);
    }
    this.currentDoc = this.pendingDoc;
    this.currentTitle = title;
    this.history.record(address, title);

    this.main.replaceChildren(result.body);
    this.markVisited();
    this.setTitle(title);
    this.setLocation(address);
    this.updateButtons();

    if (result.scrollTo) {
      const el = document.getElementById(result.scrollTo);
      if (el) el.scrollIntoView?.();
      else this.main.scrollTop = 0;
    } else {
      this.main.scrollTop = extra.scrollY ?? 0;
    }
    if (extra.focus !== false) {
      const target = result.focus ?? this.main.querySelector<HTMLElement>('h1');
      if (target) {
        if (
          !target.hasAttribute('tabindex') &&
          !/^(INPUT|BUTTON|A|TEXTAREA|SELECT)$/.test(target.tagName)
        )
          target.setAttribute('tabindex', '-1');
        target.focus({ preventScroll: !!result.scrollTo || (extra.scrollY ?? 0) > 0 });
      }
    }
    const secs = ((performance.now() - started) / 1000).toFixed(2);
    this.endLoading(`Document: Done. (${secs} secs)`);
    this.live.textContent = `Loaded: ${title}`;
  }

  private failurePage(err: unknown, address: string): PageResult {
    const retry = h('button', { type: 'button', class: 'btn' }, 'Try Again');
    retry.addEventListener('click', () => this.reload());
    if (err instanceof ApiRequestError) {
      return {
        title: `Error ${err.status}`,
        body: errorPage({
          title: 'Error',
          heading: `Error ${err.status}: ${err.code}`,
          message: err.message,
          address,
          suggestions: [
            retry,
            wspLink(
              searchAddress(address.replace(/^wsp:\/\//, '').replace(/[/_]+/g, ' ')),
              'Search for it instead',
            ),
          ],
        }),
      };
    }
    return {
      title: 'Unable to locate the server',
      body: errorPage({
        title: 'Unable to locate the server',
        heading: 'Unable to locate the server',
        message: `${APP_NAME} is unable to reach the Whisipedia daemon on this computer. It may not be running. (${String(err)})`,
        address,
        suggestions: [retry],
      }),
    };
  }

  private markVisited(): void {
    for (const a of this.main.querySelectorAll<HTMLAnchorElement>('a[data-wsp]')) {
      if (this.history.has(a.dataset.wsp!)) a.classList.add('visited');
    }
  }

  private beginLoading(host: string): void {
    this.loading = true;
    this.root.classList.add('is-loading');
    this.throbber.classList.add('is-active');
    this.progress.hidden = false;
    this.root.setAttribute('aria-busy', 'true');
    this.setProgress(`Contacting host: ${host}…`, 10);
    this.updateButtons();
  }

  private endLoading(text: string): void {
    this.loading = false;
    this.root.classList.remove('is-loading');
    this.throbber.classList.remove('is-active');
    this.progress.hidden = true;
    this.root.removeAttribute('aria-busy');
    this.defaultStatus = text;
    this.statusText.textContent = text;
    this.updateButtons();
  }

  private setProgress(text: string, pct: number): void {
    this.statusText.textContent = text;
    this.progress.value = pct;
  }

  private setTitle(title: string): void {
    this.titleText.textContent = `${APP_NAME} - [${title}]`;
    document.title = `${title} - ${APP_NAME}`;
  }

  private setLocation(address: string): void {
    this.locInput.value = address;
    const host = parseAddress(address)?.host ?? '';
    this.locLabel.textContent = isBuiltinHost(host) ? 'Netsite:' : 'Location:';
  }

  private updateButtons(): void {
    const set = (k: string, enabled: boolean) => {
      const b = this.buttons.get(k);
      if (b) b.disabled = !enabled;
    };
    set('back', this.stack.canBack);
    set('forward', this.stack.canForward);
    set('stop', this.loading);
  }

  /* ------------------------------- keyboard ----------------------------- */

  private onKey(ev: KeyboardEvent): void {
    const t = ev.target as HTMLElement | null;
    const typing =
      !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    const ctrl = ev.ctrlKey || ev.metaKey;
    const key = ev.key;
    let handled = true;

    if (ev.altKey && !ctrl && key === 'ArrowLeft') this.back();
    else if (ev.altKey && !ctrl && key === 'ArrowRight') this.forward();
    else if (ev.altKey && !ctrl && key === 'Home') this.home();
    else if (
      (ctrl && key.toLowerCase() === 'l') ||
      (ev.altKey && key.toLowerCase() === 'd') ||
      key === 'F6'
    )
      this.focusLocation();
    else if (ctrl && !ev.shiftKey && key.toLowerCase() === 'd') this.bookmarkCurrent();
    else if (ctrl && key.toLowerCase() === 'h') this.navigate(makeAddress('history'));
    else if (ctrl && key.toLowerCase() === 'b') this.navigate(makeAddress('hotlist'));
    else if (ctrl && key.toLowerCase() === 'u') this.viewSourceData();
    else if ((ctrl && key.toLowerCase() === 'r' && !ev.shiftKey) || key === 'F5') this.reload();
    else if (ctrl && key === ']') this.theme.zoom(1);
    else if (ctrl && key === '[') this.theme.zoom(-1);
    else if (ctrl && key === '0') this.theme.zoom(0);
    else if (key === 'F10' && !ev.shiftKey) this.menu.focusFirst();
    else if (key === 'Escape' && this.loading) this.stop();
    else if (key === '/' && !typing && !ctrl && !ev.altKey) {
      const box = this.main.querySelector<HTMLInputElement>('input[data-search-box]');
      if (box) {
        box.focus();
        box.select();
      } else this.focusLocation();
    } else handled = false;

    if (handled) ev.preventDefault();
  }

  /* -------------------------------- chrome ------------------------------ */

  private menus(): MenuDef[] {
    const go = (addr: string) => () => this.navigate(addr);
    const prefs = () => this.theme.current;
    return [
      {
        label: 'File',
        items: () => [
          {
            type: 'item',
            label: 'New Window',
            shortcut: 'Ctrl+N',
            action: () =>
              window.open(`#${this.stack.current?.address ?? HOME_ADDRESS}`, '_blank', 'noopener'),
          },
          {
            type: 'item',
            label: 'Open Location…',
            shortcut: 'Ctrl+L',
            action: () => this.focusLocation(),
          },
          { type: 'sep' },
          { type: 'item', label: 'Print…', shortcut: 'Ctrl+P', action: () => window.print() },
          { type: 'sep' },
          { type: 'item', label: 'Close Menu', action: () => undefined },
        ],
      },
      {
        label: 'Edit',
        items: () => [
          {
            type: 'item',
            label: 'Copy Address',
            action: () => void navigator.clipboard?.writeText(this.stack.current?.address ?? ''),
          },
          {
            type: 'item',
            label: 'Find in Page…',
            shortcut: 'Ctrl+F',
            action: () => this.status('Use your browser’s Find (Ctrl+F).'),
          },
          { type: 'sep' },
          {
            type: 'item',
            label: 'Search the Library…',
            shortcut: '/',
            action: go(makeAddress('search')),
          },
        ],
      },
      {
        label: 'View',
        items: () => [
          { type: 'item', label: 'Reload', shortcut: 'Ctrl+R', action: () => this.reload() },
          {
            type: 'item',
            label: 'View Source Data',
            shortcut: 'Ctrl+U',
            disabled: !this.currentDoc,
            action: () => this.viewSourceData(),
          },
          { type: 'sep' },
          { type: 'header', label: 'Theme' },
          ...THEMES.map(
            (t): MenuItem => ({
              type: 'radio',
              label: t.name,
              checked: prefs().theme === t.id,
              action: () => this.theme.setTheme(t.id),
            }),
          ),
          { type: 'sep' },
          {
            type: 'check',
            label: 'High Contrast',
            checked: prefs().highContrast,
            action: () => this.theme.update({ highContrast: !prefs().highContrast }),
          },
          {
            type: 'check',
            label: 'CRT Scanlines',
            checked: prefs().scanlines,
            action: () => this.theme.update({ scanlines: !prefs().scanlines }),
          },
          { type: 'sep' },
          {
            type: 'item',
            label: 'Larger Text',
            shortcut: 'Ctrl+]',
            action: () => this.theme.zoom(1),
          },
          {
            type: 'item',
            label: 'Smaller Text',
            shortcut: 'Ctrl+[',
            action: () => this.theme.zoom(-1),
          },
          {
            type: 'item',
            label: 'Normal Text Size',
            shortcut: 'Ctrl+0',
            action: () => this.theme.zoom(0),
          },
        ],
      },
      {
        label: 'Go',
        items: () => {
          const list = this.stack.list();
          const pos = this.stack.position;
          const from = Math.max(0, list.length - 12);
          return [
            {
              type: 'item',
              label: 'Back',
              shortcut: 'Alt+Left',
              disabled: !this.stack.canBack,
              action: () => this.back(),
            },
            {
              type: 'item',
              label: 'Forward',
              shortcut: 'Alt+Right',
              disabled: !this.stack.canForward,
              action: () => this.forward(),
            },
            { type: 'item', label: 'Home', shortcut: 'Alt+Home', action: () => this.home() },
            { type: 'sep' },
            { type: 'item', label: 'Search', action: go(makeAddress('search')) },
            { type: 'item', label: 'Directory', action: go(makeAddress('directory')) },
            { type: 'item', label: "What's New!", action: go(makeAddress('whatsnew')) },
            { type: 'item', label: 'Random Page', action: go(makeAddress('random')) },
            { type: 'item', label: 'System Monitor', action: go(makeAddress('system')) },
            {
              type: 'item',
              label: 'History',
              shortcut: 'Ctrl+H',
              action: go(makeAddress('history')),
            },
            { type: 'sep' },
            ...list.slice(from).map((e, k): MenuItem => {
              const i = from + k;
              return {
                type: 'radio',
                label: e.title || e.address,
                checked: i === pos,
                action: () => history.go(i - pos),
              };
            }),
          ];
        },
      },
      {
        label: 'Bookmarks',
        items: () => [
          {
            type: 'item',
            label: 'Add Bookmark',
            shortcut: 'Ctrl+D',
            action: () => this.bookmarkCurrent(),
          },
          {
            type: 'item',
            label: 'Go to Hotlist',
            shortcut: 'Ctrl+B',
            action: go(makeAddress('hotlist')),
          },
          ...(this.hotlist.list().length ? [{ type: 'sep' } as MenuItem] : []),
          ...this.hotlist
            .list()
            .slice(0, 25)
            .map((b): MenuItem => ({ type: 'item', label: b.title, action: go(b.address) })),
        ],
      },
      {
        label: 'Options',
        items: () => [
          {
            type: 'check',
            label: 'Show Toolbar',
            checked: prefs().showToolbar,
            action: () => this.theme.update({ showToolbar: !prefs().showToolbar }),
          },
          {
            type: 'check',
            label: 'Show Directory Buttons',
            checked: prefs().showDirectoryButtons,
            action: () =>
              this.theme.update({ showDirectoryButtons: !prefs().showDirectoryButtons }),
          },
          { type: 'sep' },
          { type: 'header', label: 'Motion' },
          {
            type: 'radio',
            label: 'Follow System Setting',
            checked: prefs().motion === 'system',
            action: () => this.theme.update({ motion: 'system' }),
          },
          {
            type: 'radio',
            label: 'Reduce Motion',
            checked: prefs().motion === 'reduce',
            action: () => this.theme.update({ motion: 'reduce' }),
          },
          {
            type: 'radio',
            label: 'Full Animation',
            checked: prefs().motion === 'full',
            action: () => this.theme.update({ motion: 'full' }),
          },
          { type: 'sep' },
          { type: 'item', label: 'Clear History…', action: go(makeAddress('history')) },
        ],
      },
      {
        label: 'Help',
        items: () => [
          { type: 'item', label: 'Handbook', action: go(makeAddress('help')) },
          { type: 'item', label: 'Search Tips', action: go(makeAddress('help', 'search')) },
          { type: 'item', label: 'Keyboard Shortcuts', action: go(makeAddress('help')) },
          { type: 'sep' },
          { type: 'item', label: `About ${APP_NAME}`, action: go(makeAddress('about')) },
        ],
      },
    ];
  }

  private toolButton(
    key: string,
    label: string,
    iconName: IconName,
    action: () => void,
    shortcut?: string,
  ): HTMLButtonElement {
    const b = h(
      'button',
      {
        type: 'button',
        class: `tool-btn tool-${key}`,
        title: shortcut ? `${label} (${shortcut})` : label,
        'aria-keyshortcuts': shortcut?.replace(/Ctrl/g, 'Control'),
      },
      icon(iconName),
      h('span', { class: 'tool-label' }, label),
    );
    b.addEventListener('click', action);
    this.buttons.set(key, b);
    return b;
  }

  private build(): void {
    this.root.classList.add('app');
    const skip = h('a', { class: 'skip-link', href: '#content' }, 'Skip to page content');
    skip.addEventListener('click', (ev) => {
      ev.preventDefault();
      this.main.focus();
    });

    this.titleText = h('span', { class: 'titlebar-text' }, APP_NAME);
    const titlebar = h(
      'header',
      { class: 'titlebar' },
      h('span', { class: 'titlebar-icon', 'aria-hidden': 'true' }, 'W'),
      this.titleText,
      h(
        'span',
        { class: 'titlebar-buttons', 'aria-hidden': 'true' },
        h('span', { class: 'titlebar-btn' }, '_'),
        h('span', { class: 'titlebar-btn' }, '□'),
        h('span', { class: 'titlebar-btn' }, '×'),
      ),
    );

    this.menu = new MenuBar(this.menus(), () => this.main.focus({ preventScroll: true }));

    const toolbar = h(
      'div',
      { class: 'toolbar', role: 'toolbar', 'aria-label': 'Navigation' },
      this.toolButton('back', 'Back', 'back', () => this.back(), 'Alt+Left'),
      this.toolButton('forward', 'Forward', 'forward', () => this.forward(), 'Alt+Right'),
      this.toolButton('home', 'Home', 'home', () => this.home(), 'Alt+Home'),
      this.toolButton('reload', 'Reload', 'reload', () => this.reload(), 'Ctrl+R'),
      this.toolButton('search', 'Search', 'search', () => this.navigate(makeAddress('search'))),
      this.toolButton(
        'bookmark',
        'Hotlist',
        'bookmark',
        () => this.navigate(makeAddress('hotlist')),
        'Ctrl+B',
      ),
      this.toolButton('print', 'Print', 'print', () => window.print(), 'Ctrl+P'),
      this.toolButton('stop', 'Stop', 'stop', () => this.stop(), 'Esc'),
    );

    this.throbber = h(
      'button',
      {
        type: 'button',
        class: 'throbber',
        title: 'Go to the Start Page',
        'aria-label': 'Start Page (throbber)',
      },
      throbberSvg(),
    );
    this.throbber.addEventListener('click', () => this.home());

    this.locLabel = h('label', { for: 'location', class: 'loc-label' }, 'Location:');
    this.locInput = h('input', {
      id: 'location',
      type: 'text',
      class: 'loc-input',
      autocomplete: 'off',
      spellcheck: 'false',
      'aria-describedby': 'loc-hint',
      'aria-keyshortcuts': 'Control+L',
    });
    const locForm = h(
      'form',
      { class: 'location', role: 'search', 'aria-label': 'Location' },
      this.locLabel,
      this.locInput,
      h(
        'span',
        { id: 'loc-hint', class: 'visually-hidden' },
        'Type a wsp:// address, or anything else to search.',
      ),
    );
    locForm.addEventListener('submit', (ev) => {
      ev.preventDefault();
      this.submitLocation(this.locInput.value);
    });
    this.locInput.addEventListener('input', () => {
      this.locLabel.textContent = 'Go to:';
    });
    this.locInput.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && !ev.defaultPrevented) {
        this.setLocation(this.stack.current?.address ?? '');
        this.locInput.select();
      }
    });
    attachSuggest(this.locInput, this, {
      history: true,
      onPick: (s) => {
        if (s.address) this.navigate(s.address);
        else if (s.doc_id) this.navigate(docIdToAddress(s.doc_id));
        else this.navigate(searchAddress(s.text), { fromLocation: true });
      },
    });

    const dirButton = (label: string, addr: string) => wspLink(addr, label);
    const dirbuttons = h(
      'nav',
      { class: 'dirbuttons', 'aria-label': 'Directory buttons' },
      dirButton("What's New!", makeAddress('whatsnew')),
      dirButton("What's Cool?", makeAddress('directory')),
      dirButton('Handbook', makeAddress('help')),
      dirButton('Net Search', makeAddress('search')),
      dirButton('Net Directory', makeAddress('directory')),
      dirButton('System', makeAddress('system')),
    );

    this.main = h('main', {
      id: 'content',
      class: 'viewport',
      tabindex: '-1',
      'aria-label': 'Page',
    });
    this.statusText = h('span', { class: 'status-text' }, 'Document: Done.');
    this.progress = h('progress', {
      class: 'status-progress',
      max: 100,
      value: 0,
      hidden: true,
      'aria-label': 'Loading progress',
    });
    this.live = h('div', { class: 'visually-hidden', role: 'status', 'aria-live': 'polite' });
    const statusbar = h(
      'footer',
      { class: 'statusbar' },
      h('span', { class: 'status-cell status-main' }, this.statusText),
      h('span', { class: 'status-cell status-progress-cell' }, this.progress),
      h(
        'span',
        {
          class: 'status-cell status-offline',
          title: 'No network: everything is served from this computer',
        },
        h('span', { class: 'lock', 'aria-hidden': 'true' }, '🔒'),
        ' Offline',
      ),
    );

    const chromeTop = h(
      'div',
      { class: 'chrome-top' },
      titlebar,
      this.menu.el,
      h('div', { class: 'toolbar-row' }, toolbar, this.throbber),
      h('div', { class: 'location-row' }, locForm),
      dirbuttons,
    );

    const win = h('div', { class: 'window' }, chromeTop, this.main, statusbar);
    this.root.replaceChildren(skip, win, this.live);
    this.root.dataset.version = VERSION;

    // Delegated link handling and hover status.
    this.root.addEventListener('click', (ev) => this.onClick(ev));
    this.root.addEventListener('mouseover', (ev) => this.onHover(ev));
    this.root.addEventListener('focusin', (ev) => this.onHover(ev));
    this.root.addEventListener('mouseout', (ev) => this.onUnhover(ev));
    this.root.addEventListener('focusout', (ev) => this.onUnhover(ev));
    this.updateButtons();
  }

  submitLocation(text: string): void {
    const r = interpretInput(text);
    if (!r) return;
    if (r.type === 'search') this.navigate(searchAddress(r.query), { fromLocation: true });
    else this.navigate(r.address, { fromLocation: true });
  }

  private onClick(ev: MouseEvent): void {
    if (
      ev.defaultPrevented ||
      ev.button !== 0 ||
      ev.ctrlKey ||
      ev.metaKey ||
      ev.shiftKey ||
      ev.altKey
    )
      return;
    const a = (ev.target as Element | null)?.closest?.('a');
    if (!a) return;
    if (a.dataset.wsp) {
      ev.preventDefault();
      this.navigate(a.dataset.wsp);
    } else if (a.dataset.anchor) {
      ev.preventDefault();
      const el = document.getElementById(a.dataset.anchor);
      if (el) {
        el.scrollIntoView?.();
        if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '-1');
        el.focus({ preventScroll: true });
      }
    } else if (a.getAttribute('href')?.startsWith('#') && a.getAttribute('href') !== '#content') {
      const addr = hashToAddress(a.getAttribute('href')!);
      if (addr) {
        ev.preventDefault();
        this.navigate(addr);
      }
    }
  }

  private onHover(ev: Event): void {
    if (this.loading) return;
    const a = (ev.target as Element | null)?.closest?.('a');
    if (!a) return;
    const text = a.dataset.missing
      ? `Not in library: ${a.dataset.missing}`
      : (a.dataset.wsp ?? a.getAttribute('href') ?? '');
    this.statusText.textContent = text;
  }

  private onUnhover(ev: Event): void {
    if (this.loading) return;
    if ((ev.target as Element | null)?.closest?.('a'))
      this.statusText.textContent = this.defaultStatus;
  }
}
