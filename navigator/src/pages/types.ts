import type { WspClient } from '../api/client.ts';
import type { DocumentResponse, SourceInfo } from '../api/types.ts';
import type { GlobalHistory, Hotlist, SessionStack } from '../core/history.ts';
import type { Route } from '../core/router.ts';
import type { ThemeManager } from '../core/theme.ts';

export interface NavOptions {
  /** Came from the location bar (enables "I'm Feeling Lucky"-style jumps). */
  fromLocation?: boolean;
  /** Bypass client caches. */
  reload?: boolean;
}

/** Services the App exposes to pages. */
export interface AppServices {
  readonly api: WspClient;
  readonly history: GlobalHistory;
  readonly hotlist: Hotlist;
  readonly theme: ThemeManager;
  readonly stack: SessionStack;
  navigate(address: string, opts?: NavOptions): void;
  sources(): Promise<SourceInfo[]>;
  /** Transient status bar message. */
  status(text: string): void;
  openSourceData(doc: DocumentResponse): void;
  /** Set the document shown by View Source Data (null on other pages). */
  setCurrentDocument(doc: DocumentResponse | null): void;
  /** Document shown before the page being loaded (the page a link was followed from). */
  lastDocId(): string | null;
}

export interface PageContext {
  route: Route;
  address: string;
  app: AppServices;
  signal: AbortSignal;
  opts: NavOptions;
}

export type PageResult =
  | {
      title: string;
      body: Node;
      /** Element to move focus to after render (defaults to the h1). */
      focus?: HTMLElement | undefined;
      /** Fragment id to scroll to after render. */
      scrollTo?: string | undefined;
      /** An error page (not kept in History), or one that retries by itself. Default: a page. */
      status?: 'error' | 'retrying' | undefined;
    }
  | { redirect: string };

export type PageRenderer = (ctx: PageContext) => Promise<PageResult>;
