/**
 * `localStorage` that never throws: private windows, blocked site data and server rendering
 * all degrade to "nothing stored" instead of breaking the page.
 */
export interface KeyValueStore {
  get(key: string): string | null;
  set(key: string, value: string): void;
  remove(key: string): void;
}

export const memoryStore = (): KeyValueStore => {
  const map = new Map<string, string>();
  return {
    get: (k) => map.get(k) ?? null,
    set: (k, v) => void map.set(k, v),
    remove: (k) => void map.delete(k),
  };
};

export const safeLocalStorage: KeyValueStore = {
  get(key) {
    try {
      return typeof window === "undefined" ? null : window.localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      if (typeof window !== "undefined") window.localStorage.setItem(key, value);
    } catch {
      /* storage full or blocked: ignore */
    }
  },
  remove(key) {
    try {
      if (typeof window !== "undefined") window.localStorage.removeItem(key);
    } catch {
      /* ignore */
    }
  },
};

export function readJson<T>(store: KeyValueStore, key: string): T | null {
  const raw = store.get(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export function writeJson(store: KeyValueStore, key: string, value: unknown): void {
  store.set(key, JSON.stringify(value));
}

/** `sessionStorage` with the same never-throw guarantees (the tab's session only). */
export const safeSessionStorage: KeyValueStore = {
  get(key) {
    try {
      return typeof window === "undefined" ? null : window.sessionStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      if (typeof window !== "undefined") window.sessionStorage.setItem(key, value);
    } catch {
      /* storage full or blocked: ignore */
    }
  },
  remove(key) {
    try {
      if (typeof window !== "undefined") window.sessionStorage.removeItem(key);
    } catch {
      /* ignore */
    }
  },
};
