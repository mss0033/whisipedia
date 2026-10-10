/**
 * Navigation state:
 * - SessionStack: per-window Back/Forward stack (Netscape "Go" menu).
 * - GlobalHistory: every visited address with title and count (Ctrl+H).
 * - Hotlist: bookmarks (Ctrl+D).
 * History and Hotlist are persisted through SafeStorage.
 */
import type { SafeStorage } from './storage.ts';

export interface StackEntry {
  address: string;
  title: string;
  scrollY: number;
}

export class SessionStack {
  private entries: StackEntry[] = [];
  private index = -1;
  readonly limit: number;

  constructor(limit = 100) {
    this.limit = limit;
  }

  get current(): StackEntry | undefined {
    return this.entries[this.index];
  }
  get canBack(): boolean {
    return this.index > 0;
  }
  get canForward(): boolean {
    return this.index < this.entries.length - 1;
  }
  get position(): number {
    return this.index;
  }
  list(): readonly StackEntry[] {
    return this.entries;
  }

  /** Visit a new address: truncates forward history (like every browser). */
  push(address: string, title = address): StackEntry {
    const cur = this.current;
    if (cur && cur.address === address) return cur;
    this.entries = this.entries.slice(0, this.index + 1);
    const entry = { address, title, scrollY: 0 };
    this.entries.push(entry);
    if (this.entries.length > this.limit) this.entries.shift();
    this.index = this.entries.length - 1;
    return entry;
  }

  replace(address: string, title = address): void {
    const cur = this.current;
    if (!cur) {
      this.push(address, title);
      return;
    }
    cur.address = address;
    cur.title = title;
  }

  setTitle(title: string): void {
    if (this.current) this.current.title = title;
  }

  setScroll(y: number): void {
    if (this.current) this.current.scrollY = y;
  }

  back(): StackEntry | undefined {
    if (!this.canBack) return undefined;
    this.index -= 1;
    return this.current;
  }

  forward(): StackEntry | undefined {
    if (!this.canForward) return undefined;
    this.index += 1;
    return this.current;
  }

  go(position: number): StackEntry | undefined {
    if (position < 0 || position >= this.entries.length) return undefined;
    this.index = position;
    return this.current;
  }
}

export interface HistoryEntry {
  address: string;
  title: string;
  firstVisit: number;
  lastVisit: number;
  visits: number;
}

function isHistoryList(v: unknown): v is HistoryEntry[] {
  return (
    Array.isArray(v) &&
    v.every(
      (e) =>
        typeof e === 'object' &&
        e !== null &&
        typeof (e as HistoryEntry).address === 'string' &&
        typeof (e as HistoryEntry).lastVisit === 'number',
    )
  );
}

export class GlobalHistory {
  static readonly KEY = 'history';
  private readonly storage: SafeStorage;
  private readonly limit: number;
  private entries: HistoryEntry[];
  private readonly visited = new Set<string>();

  constructor(storage: SafeStorage, limit = 1000) {
    this.storage = storage;
    this.limit = limit;
    this.entries = storage.read<HistoryEntry[]>(GlobalHistory.KEY, [], isHistoryList);
    for (const e of this.entries) this.visited.add(e.address);
  }

  record(address: string, title: string, now = Date.now()): void {
    const existing = this.entries.find((e) => e.address === address);
    if (existing) {
      existing.lastVisit = now;
      existing.visits += 1;
      if (title) existing.title = title;
      this.entries = [existing, ...this.entries.filter((e) => e !== existing)];
    } else {
      this.entries.unshift({ address, title, firstVisit: now, lastVisit: now, visits: 1 });
      this.visited.add(address);
      if (this.entries.length > this.limit) {
        for (const e of this.entries.splice(this.limit)) this.visited.delete(e.address);
      }
    }
    this.storage.write(GlobalHistory.KEY, this.entries);
  }

  retitle(address: string, title: string): void {
    const e = this.entries.find((x) => x.address === address);
    if (e && title && e.title !== title) {
      e.title = title;
      this.storage.write(GlobalHistory.KEY, this.entries);
    }
  }

  has(address: string): boolean {
    return this.visited.has(address);
  }

  /** Most recent first. */
  list(): readonly HistoryEntry[] {
    return this.entries;
  }

  remove(address: string): void {
    this.entries = this.entries.filter((e) => e.address !== address);
    this.visited.delete(address);
    this.storage.write(GlobalHistory.KEY, this.entries);
  }

  clear(): void {
    this.entries = [];
    this.visited.clear();
    this.storage.write(GlobalHistory.KEY, this.entries);
  }
}

export interface Bookmark {
  address: string;
  title: string;
  added: number;
}

function isBookmarkList(v: unknown): v is Bookmark[] {
  return (
    Array.isArray(v) &&
    v.every(
      (e) =>
        typeof e === 'object' &&
        e !== null &&
        typeof (e as Bookmark).address === 'string' &&
        typeof (e as Bookmark).title === 'string',
    )
  );
}

export class Hotlist {
  static readonly KEY = 'hotlist';
  private readonly storage: SafeStorage;
  private items: Bookmark[];

  constructor(storage: SafeStorage) {
    this.storage = storage;
    this.items = storage.read<Bookmark[]>(Hotlist.KEY, [], isBookmarkList);
  }

  list(): readonly Bookmark[] {
    return this.items;
  }

  has(address: string): boolean {
    return this.items.some((b) => b.address === address);
  }

  /** Adds (or retitles) a bookmark. Returns true when newly added. */
  add(address: string, title: string, now = Date.now()): boolean {
    const existing = this.items.find((b) => b.address === address);
    if (existing) {
      existing.title = title || existing.title;
      this.save();
      return false;
    }
    this.items.push({ address, title: title || address, added: now });
    this.save();
    return true;
  }

  remove(address: string): void {
    this.items = this.items.filter((b) => b.address !== address);
    this.save();
  }

  move(address: string, delta: number): void {
    const i = this.items.findIndex((b) => b.address === address);
    const j = i + delta;
    if (i < 0 || j < 0 || j >= this.items.length) return;
    const [item] = this.items.splice(i, 1);
    this.items.splice(j, 0, item!);
    this.save();
  }

  private save(): void {
    this.storage.write(Hotlist.KEY, this.items);
  }
}
