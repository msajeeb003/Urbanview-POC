/**
 * Batching analytics tracker for `POST /v1/events`.
 *
 * - `track(name, properties)` queues one event with the anonymous `session_id` / `client_id`, a
 *   random `event_id` (the API de-duplicates retried batches by it) and the client clock.
 * - The queue is flushed every `flushIntervalMs`, as soon as `maxBatch` events are waiting, and
 *   when the page is hidden or unloaded (`keepalive` so the request survives navigation).
 * - A rejected batch (422: an event the API refuses) is dropped, never retried forever; network
 *   trouble, 429 and 5xx keep the events and retry with backoff. The queue is capped.
 * - Properties must stay anonymous: the API rejects personal keys (email, phone, address …) and
 *   string values that look like an e-mail or IP address. Never put purchaser data here.
 */
import { ApiError } from "@/lib/api/client";
import type { AnalyticsEventName, EventBatch, EventIn, IngestResult } from "@/lib/api/types";

import { Identity, newAnonymousId, type Visit } from "./identity";

export type EventProperties = Record<string, string | number | boolean | null | undefined>;

export interface TrackerOptions {
  identity: Identity;
  send: (batch: EventBatch, opts: { keepalive: boolean }) => Promise<IngestResult>;
  now?: () => number;
  enabled?: boolean;
  flushIntervalMs?: number;
  maxBatch?: number;
  maxQueue?: number;
  onDrop?: (events: EventIn[], error: unknown) => void;
}

const MAX_BACKOFF_MS = 60_000;

export class Tracker {
  private readonly identity: Identity;
  private readonly send: TrackerOptions["send"];
  private readonly now: () => number;
  private readonly enabled: boolean;
  private readonly flushIntervalMs: number;
  private readonly maxBatch: number;
  private readonly maxQueue: number;
  private readonly onDrop?: TrackerOptions["onDrop"];

  private queue: EventIn[] = [];
  private inflight: Promise<void> | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private failures = 0;
  private started = false;
  private readonly once = new Set<string>();
  private detach: (() => void) | null = null;

  constructor(opts: TrackerOptions) {
    this.identity = opts.identity;
    this.send = opts.send;
    this.now = opts.now ?? Date.now;
    this.enabled = opts.enabled ?? true;
    this.flushIntervalMs = opts.flushIntervalMs ?? 4_000;
    this.maxBatch = Math.min(opts.maxBatch ?? 50, 100);
    this.maxQueue = opts.maxQueue ?? 500;
    this.onDrop = opts.onDrop;
  }

  /** Start the visit (session rotation, return_visit, sessions_per_user) and page listeners. */
  start(target?: Pick<Window, "addEventListener" | "removeEventListener"> & { document?: Document }): Visit | null {
    if (this.started || !this.enabled) return null;
    this.started = true;
    const visit = this.identity.beginVisit();
    this.visitEvents(visit);
    if (target) {
      const onHidden = () => {
        if (target.document?.visibilityState === "hidden") void this.flush({ keepalive: true });
      };
      const onPageHide = () => void this.flush({ keepalive: true });
      target.addEventListener("visibilitychange", onHidden);
      target.addEventListener("pagehide", onPageHide);
      this.detach = () => {
        target.removeEventListener("visibilitychange", onHidden);
        target.removeEventListener("pagehide", onPageHide);
      };
    }
    return visit;
  }

  /**
   * The session id every API call carries (`X-Session-ID`). The first call may come before the
   * page started the visit, so it starts it (`start` runs once). Null while analytics is off.
   */
  currentSessionId(target?: Parameters<Tracker["start"]>[0]): string | null {
    if (!this.enabled) return null;
    if (!this.started) this.start(target);
    return this.identity.peekSessionId();
  }

  stop(): void {
    this.detach?.();
    this.detach = null;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  track(name: AnalyticsEventName, properties: EventProperties = {}): void {
    if (!this.enabled) return;
    const { sessionId, clientId, rotated, visit } = this.identity.touch();
    if (rotated && visit) this.visitEvents(visit);
    this.enqueue(name, properties, sessionId, clientId);
  }

  /** Track an event at most once per page load (e.g. `map_loaded` under React strict mode). */
  trackOnce(key: string, name: AnalyticsEventName, properties: EventProperties = {}): void {
    if (this.once.has(key)) return;
    this.once.add(key);
    this.track(name, properties);
  }

  get pending(): number {
    return this.queue.length;
  }

  async flush({ keepalive = false }: { keepalive?: boolean } = {}): Promise<void> {
    if (this.inflight) return this.inflight;
    if (this.queue.length === 0) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    const batch = this.queue.splice(0, this.maxBatch);
    this.inflight = (async () => {
      try {
        await this.send({ events: batch }, { keepalive });
        this.failures = 0;
      } catch (error) {
        if (error instanceof ApiError && !error.isTransient) {
          this.onDrop?.(batch, error); // the API refused the batch; retrying cannot help
        } else {
          this.failures += 1;
          this.queue = [...batch, ...this.queue].slice(0, this.maxQueue);
        }
      } finally {
        this.inflight = null;
      }
    })();
    await this.inflight;
    if (this.queue.length > 0) this.schedule();
  }

  private visitEvents(visit: Visit): void {
    if (!visit.newSession) return;
    if (visit.returning) {
      this.enqueue(
        "return_visit",
        visit.daysSinceLast === null ? {} : { days_since_last: visit.daysSinceLast },
        visit.sessionId,
        visit.clientId,
      );
    }
    this.enqueue("sessions_per_user", { sessions: visit.sessions }, visit.sessionId, visit.clientId);
  }

  private enqueue(name: AnalyticsEventName, properties: EventProperties, sessionId: string, clientId: string): void {
    const clean: Record<string, string | number | boolean | null> = {};
    for (const [key, value] of Object.entries(properties)) {
      if (value !== undefined) clean[key] = value;
    }
    this.queue.push({
      name,
      session_id: sessionId,
      client_id: clientId,
      event_id: newAnonymousId(),
      occurred_at: new Date(this.now()).toISOString(),
      properties: clean,
    });
    if (this.queue.length > this.maxQueue) this.queue.splice(0, this.queue.length - this.maxQueue);
    if (this.queue.length >= this.maxBatch) void this.flush();
    else this.schedule();
  }

  private schedule(): void {
    if (this.timer || !this.enabled) return;
    const delay =
      this.failures === 0
        ? this.flushIntervalMs
        : Math.min(MAX_BACKOFF_MS, this.flushIntervalMs * 2 ** this.failures);
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.flush();
    }, delay);
  }
}
