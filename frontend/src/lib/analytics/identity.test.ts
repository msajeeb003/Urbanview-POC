import { describe, expect, it } from "vitest";

import { memoryStore, readJson } from "@/lib/storage";

import { CLIENT_KEY, Identity, SESSION_IDLE_MS, SESSION_KEY, randomUuid } from "./identity";

const ID = /^[A-Za-z0-9_-]{8,64}$/;
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function clock(start = Date.parse("2026-09-24T10:00:00Z")) {
  let t = start;
  return { now: () => t, advance: (ms: number) => (t += ms) };
}

describe("Identity", () => {
  it("first visit: new client and session, not returning", () => {
    const store = memoryStore();
    const c = clock();
    const v = new Identity(store, c.now).beginVisit();
    expect(v.clientId).toMatch(ID);
    expect(v.sessionId).toMatch(ID);
    expect(v).toMatchObject({ newSession: true, returning: false, sessions: 1, daysSinceLast: null });
    expect(readJson<{ id: string }>(store, CLIENT_KEY)?.id).toBe(v.clientId);
    expect(readJson<{ id: string }>(store, SESSION_KEY)?.id).toBe(v.sessionId);
  });

  it("reload within the idle window continues the session", () => {
    const store = memoryStore();
    const c = clock();
    const first = new Identity(store, c.now).beginVisit();
    c.advance(10 * 60_000);
    const again = new Identity(store, c.now).beginVisit();
    expect(again).toMatchObject({ sessionId: first.sessionId, clientId: first.clientId, newSession: false, returning: false, sessions: 1 });
  });

  it("peeks the live session id for API calls without starting or extending one", () => {
    const store = memoryStore();
    const c = clock();
    const id = new Identity(store, c.now);
    expect(id.peekSessionId()).toBeNull();
    expect(store.get(SESSION_KEY)).toBeNull();
    const v = id.beginVisit();
    expect(id.peekSessionId()).toBe(v.sessionId);
    c.advance(SESSION_IDLE_MS + 1);
    expect(id.peekSessionId()).toBeNull();
  });

  it("a stored client id after the idle window is a return visit", () => {
    const store = memoryStore();
    const c = clock();
    const first = new Identity(store, c.now).beginVisit();
    c.advance(3 * 86_400_000);
    const later = new Identity(store, c.now).beginVisit();
    expect(later.clientId).toBe(first.clientId);
    expect(later.sessionId).not.toBe(first.sessionId);
    expect(later).toMatchObject({ newSession: true, returning: true, sessions: 2, daysSinceLast: 3 });
  });

  it("touch keeps the session alive and rotates it after idling", () => {
    const store = memoryStore();
    const c = clock();
    const id = new Identity(store, c.now);
    const v = id.beginVisit();
    c.advance(SESSION_IDLE_MS - 1000);
    expect(id.touch()).toMatchObject({ sessionId: v.sessionId, rotated: false });
    c.advance(SESSION_IDLE_MS - 1000); // still alive: the touch above refreshed the activity time
    expect(id.touch()).toMatchObject({ sessionId: v.sessionId, rotated: false });
    c.advance(SESSION_IDLE_MS + 1);
    const t = id.touch();
    expect(t.rotated).toBe(true);
    expect(t.sessionId).not.toBe(v.sessionId);
    expect(t.visit).toMatchObject({ returning: true, sessions: 2 });
  });

  it("ignores corrupt storage", () => {
    const store = memoryStore();
    store.set(CLIENT_KEY, "{not json");
    store.set(SESSION_KEY, JSON.stringify({ id: "bad id!", started_at: "x", last_seen_at: "x" }));
    const v = new Identity(store, clock().now).beginVisit();
    expect(v).toMatchObject({ newSession: true, returning: false, sessions: 1 });
    expect(v.sessionId).toMatch(ID);
  });
});

describe("randomUuid", () => {
  it("gives session and client ids as UUID v4, which the API's id rule accepts", () => {
    const v = new Identity(memoryStore()).beginVisit();
    for (const id of [v.sessionId, v.clientId]) {
      expect(id).toMatch(UUID_V4);
      expect(id).toMatch(ID);
    }
    expect(v.sessionId).not.toBe(v.clientId);
  });

  it("builds a v4 UUID from getRandomValues where randomUUID is missing (no secure context)", () => {
    const fallback = { getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) };
    const ids = new Set(Array.from({ length: 50 }, () => randomUuid(fallback)));
    expect(ids.size).toBe(50);
    for (const id of ids) expect(id).toMatch(UUID_V4);
  });
});
