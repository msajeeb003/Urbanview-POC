/**
 * Anonymous identity for analytics. Two random ids, both generated in the browser and persisted
 * in `localStorage`; neither is derived from anything personal.
 *
 * - `client_id` (`uv.client`): created once per browser, never rotated. Lets the dashboard count
 *   sessions per client (the prototype's 3+ sessions target).
 * - `session_id` (`uv.session`): persisted with its start and last-activity time so a reload
 *   continues the same session; after `SESSION_IDLE_MS` without activity the next page load (or
 *   event) starts a new one.
 *
 * `beginVisit()` reports whether this load started a new session and whether the browser had
 * been here before (a stored client id), which is what `return_visit` needs.
 */
import { randomToken } from "@/lib/api/client";
import { readJson, writeJson, type KeyValueStore } from "@/lib/storage";

export const CLIENT_KEY = "uv.client";
export const SESSION_KEY = "uv.session";
export const SESSION_IDLE_MS = 30 * 60_000;

interface StoredClient {
  id: string;
  created_at: string;
  sessions: number;
  last_session_at: string | null;
}

interface StoredSession {
  id: string;
  started_at: string;
  last_seen_at: string;
}

export interface Visit {
  clientId: string;
  sessionId: string;
  /** A new session started with this call. */
  newSession: boolean;
  /** The browser already had a client id before this call. */
  returning: boolean;
  /** Sessions started by this browser, this one included. */
  sessions: number;
  /** Whole days since the previous session started; null on a first visit. */
  daysSinceLast: number | null;
}

/** Id format accepted by the API: `^[A-Za-z0-9_-]{8,64}$`. */
export const newAnonymousId = (): string => randomToken(16);

export class Identity {
  constructor(
    private readonly store: KeyValueStore,
    private readonly now: () => number = Date.now,
  ) {}

  /** The stored session id while it is alive (no side effects); null before the visit began. */
  peekSessionId(): string | null {
    const stored = readJson<StoredSession>(this.store, SESSION_KEY);
    if (!stored?.id || !isValidId(stored.id)) return null;
    return this.now() - new Date(stored.last_seen_at).getTime() < SESSION_IDLE_MS ? stored.id : null;
  }

  /** Call once per page load. Continues or starts the session and updates the counters. */
  beginVisit(): Visit {
    const nowMs = this.now();
    const nowIso = new Date(nowMs).toISOString();
    const storedClient = readJson<StoredClient>(this.store, CLIENT_KEY);
    const returning = !!storedClient?.id && isValidId(storedClient.id);
    const client: StoredClient = returning
      ? storedClient!
      : { id: newAnonymousId(), created_at: nowIso, sessions: 0, last_session_at: null };

    const stored = readJson<StoredSession>(this.store, SESSION_KEY);
    const alive =
      !!stored?.id &&
      isValidId(stored.id) &&
      nowMs - new Date(stored.last_seen_at).getTime() < SESSION_IDLE_MS;

    let session: StoredSession;
    let daysSinceLast: number | null = null;
    if (alive) {
      session = { ...stored!, last_seen_at: nowIso };
    } else {
      session = { id: newAnonymousId(), started_at: nowIso, last_seen_at: nowIso };
      if (client.last_session_at) {
        daysSinceLast = Math.max(
          0,
          Math.floor((nowMs - new Date(client.last_session_at).getTime()) / 86_400_000),
        );
      }
      client.sessions += 1;
      client.last_session_at = nowIso;
    }
    writeJson(this.store, CLIENT_KEY, client);
    writeJson(this.store, SESSION_KEY, session);
    return {
      clientId: client.id,
      sessionId: session.id,
      newSession: !alive,
      returning: returning && !alive,
      sessions: client.sessions,
      daysSinceLast,
    };
  }

  /**
   * Session id for an event happening now. Refreshes the activity time; if the tab sat idle past
   * the limit, a new session starts (and `rotated` is true).
   */
  touch(): { sessionId: string; clientId: string; rotated: boolean; visit?: Visit } {
    const stored = readJson<StoredSession>(this.store, SESSION_KEY);
    const client = readJson<StoredClient>(this.store, CLIENT_KEY);
    const nowMs = this.now();
    if (
      stored?.id &&
      client?.id &&
      nowMs - new Date(stored.last_seen_at).getTime() < SESSION_IDLE_MS
    ) {
      // Throttle writes: only persist when the stored time is more than a minute old.
      if (nowMs - new Date(stored.last_seen_at).getTime() > 60_000) {
        writeJson(this.store, SESSION_KEY, { ...stored, last_seen_at: new Date(nowMs).toISOString() });
      }
      return { sessionId: stored.id, clientId: client.id, rotated: false };
    }
    const visit = this.beginVisit();
    return { sessionId: visit.sessionId, clientId: visit.clientId, rotated: true, visit };
  }
}

function isValidId(id: string): boolean {
  return /^[A-Za-z0-9_-]{8,64}$/.test(id);
}
