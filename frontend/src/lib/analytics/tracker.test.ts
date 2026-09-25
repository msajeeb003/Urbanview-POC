import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";
import type { EventBatch } from "@/lib/api/types";
import { memoryStore } from "@/lib/storage";

import { Identity } from "./identity";
import { Tracker } from "./tracker";

const T0 = Date.parse("2026-09-24T10:00:00Z");

function setup(opts: { store?: ReturnType<typeof memoryStore>; send?: (b: EventBatch) => Promise<unknown> } = {}) {
  const store = opts.store ?? memoryStore();
  const batches: EventBatch[] = [];
  const keepalives: boolean[] = [];
  const send = vi.fn(async (batch: EventBatch, o: { keepalive: boolean }) => {
    batches.push(batch);
    keepalives.push(o.keepalive);
    if (opts.send) await opts.send(batch);
    return { received: batch.events.length, accepted: batch.events.length, duplicates: 0 };
  });
  const dropped: unknown[] = [];
  const tracker = new Tracker({
    identity: new Identity(store, () => Date.now()),
    send,
    flushIntervalMs: 1000,
    maxBatch: 3,
    onDrop: (_events, error) => dropped.push(error),
  });
  return { tracker, send, batches, keepalives, dropped, store };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
});
afterEach(() => vi.useRealTimers());

describe("Tracker", () => {
  it("first visit emits sessions_per_user only; events carry ids, time and clean properties", async () => {
    const { tracker, batches } = setup();
    tracker.start();
    tracker.track("map_loaded");
    tracker.track("layer_toggled", { layer_id: "land_use", visible: true, ignored: undefined });
    await vi.advanceTimersByTimeAsync(1000);
    const events = batches.flatMap((b) => b.events);
    expect(events.map((e) => e.name)).toEqual(["sessions_per_user", "map_loaded", "layer_toggled"]);
    expect(events[0].properties).toEqual({ sessions: 1 });
    expect(events[2].properties).toEqual({ layer_id: "land_use", visible: true });
    const sessions = new Set(events.map((e) => e.session_id));
    expect(sessions.size).toBe(1);
    expect(new Set(events.map((e) => e.event_id)).size).toBe(3);
    for (const e of events) {
      expect(e.session_id).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
      expect(e.client_id).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
      expect(e.occurred_at).toBe(new Date(T0).toISOString());
    }
  });

  it("a stored client id on a new session emits return_visit", async () => {
    const store = memoryStore();
    const first = setup({ store });
    first.tracker.start();
    await first.tracker.flush();
    vi.setSystemTime(T0 + 2 * 86_400_000);
    const second = setup({ store });
    second.tracker.start();
    second.tracker.trackOnce("map_loaded", "map_loaded");
    second.tracker.trackOnce("map_loaded", "map_loaded");
    await second.tracker.flush();
    const names = second.batches.flatMap((b) => b.events.map((e) => [e.name, e.properties]));
    expect(names).toEqual([
      ["return_visit", { days_since_last: 2 }],
      ["sessions_per_user", { sessions: 2 }],
      ["map_loaded", {}],
    ]);
  });

  it("flushes as soon as a batch is full", async () => {
    const { tracker, batches } = setup();
    tracker.track("map_loaded"); // the session start queues sessions_per_user first: 2 events
    await vi.advanceTimersByTimeAsync(0);
    expect(batches).toHaveLength(0);
    tracker.track("panel_viewed", { panel_type: "urban" }); // third event fills the batch
    await vi.advanceTimersByTimeAsync(0);
    expect(batches).toHaveLength(1);
    expect(batches[0].events.map((e) => e.name)).toEqual(["sessions_per_user", "map_loaded", "panel_viewed"]);
  });

  it("drops a batch the API rejects (4xx) and keeps going", async () => {
    let fail = true;
    const { tracker, batches, dropped } = setup({
      send: async () => {
        if (fail) {
          fail = false;
          throw new ApiError({ status: 422, code: "validation_error", message: "bad" });
        }
      },
    });
    tracker.track("map_loaded");
    await tracker.flush();
    expect(dropped).toHaveLength(1);
    expect(tracker.pending).toBe(0);
    tracker.track("panel_viewed");
    await tracker.flush();
    expect(batches).toHaveLength(2);
  });

  it("keeps events after a transient failure and retries with backoff", async () => {
    let calls = 0;
    const { tracker, batches } = setup({
      send: async () => {
        calls += 1;
        if (calls === 1) throw new ApiError({ status: 0, code: "network_error", message: "offline" });
      },
    });
    tracker.track("map_loaded");
    await tracker.flush();
    expect(tracker.pending).toBe(2); // sessions_per_user + map_loaded kept
    await vi.advanceTimersByTimeAsync(1999);
    expect(calls).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(calls).toBe(2);
    expect(tracker.pending).toBe(0);
    expect(batches[1].events.map((e) => e.name)).toEqual(["sessions_per_user", "map_loaded"]);
  });

  it("flushes with keepalive when the page is hidden", async () => {
    const { tracker, keepalives } = setup();
    const listeners: Record<string, () => void> = {};
    const doc = { visibilityState: "hidden" } as Document;
    tracker.start({
      addEventListener: (type: string, fn: () => void) => (listeners[type] = fn),
      removeEventListener: () => undefined,
      document: doc,
    } as never);
    tracker.track("map_loaded");
    listeners.visibilitychange();
    await vi.advanceTimersByTimeAsync(0);
    expect(keepalives).toEqual([true]);
  });

  it("does nothing when disabled", async () => {
    const send = vi.fn();
    const tracker = new Tracker({ identity: new Identity(memoryStore()), send, enabled: false });
    expect(tracker.start()).toBeNull();
    tracker.track("map_loaded");
    await tracker.flush();
    expect(send).not.toHaveBeenCalled();
  });
});
