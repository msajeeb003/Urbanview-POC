import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pointerRefreshMs } from "./hooks";

describe("tile pointer refresh", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-29T15:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("reads the pointer again within 5 minutes, sooner when the signed link expires first", () => {
    // a one-hour link: a publish still reaches the open map within 5 minutes
    expect(pointerRefreshMs("2026-09-29T16:00:00Z")).toBe(5 * 60_000);
    // a link expiring in 4 minutes is renewed 2 minutes before it expires
    expect(pointerRefreshMs("2026-09-29T15:04:00Z")).toBe(2 * 60_000);
    // nothing published yet (no link): keep checking every 5 minutes
    expect(pointerRefreshMs(null)).toBe(5 * 60_000);
  });
});
