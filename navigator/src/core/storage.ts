/**
 * localStorage access that never throws (private mode, quota, disabled
 * storage, sandboxed iframes). Values are JSON.
 */

const PREFIX = 'wsp.navigator.';

export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function defaultStore(): KeyValueStore | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

export class SafeStorage {
  private readonly store: KeyValueStore | null;
  constructor(store: KeyValueStore | null = defaultStore()) {
    this.store = store;
  }

  read<T>(key: string, fallback: T, validate?: (v: unknown) => v is T): T {
    try {
      const raw = this.store?.getItem(PREFIX + key);
      if (raw == null) return fallback;
      const v: unknown = JSON.parse(raw);
      if (validate && !validate(v)) return fallback;
      return v as T;
    } catch {
      return fallback;
    }
  }

  write(key: string, value: unknown): boolean {
    try {
      if (!this.store) return false;
      this.store.setItem(PREFIX + key, JSON.stringify(value));
      return true;
    } catch {
      return false;
    }
  }

  remove(key: string): void {
    try {
      this.store?.removeItem(PREFIX + key);
    } catch {
      /* ignore */
    }
  }
}

/** In-memory store for tests and storage-less environments. */
export class MemoryStore implements KeyValueStore {
  private readonly map = new Map<string, string>();
  getItem(key: string): string | null {
    return this.map.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.map.set(key, value);
  }
  removeItem(key: string): void {
    this.map.delete(key);
  }
}
