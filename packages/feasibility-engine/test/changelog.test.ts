import { describe, expect, it } from "vitest";

import { ENGINE_CHANGELOG, ENGINE_UPDATED, ENGINE_VERSION, FORMULA_VERSION, changelogIsCurrent } from "../src/index.js";

describe("changelog", () => {
  it("describes the running version first", () => {
    expect(changelogIsCurrent()).toBe(true);
    expect(ENGINE_CHANGELOG[0]).toMatchObject({ engine_version: ENGINE_VERSION, formula_version: FORMULA_VERSION });
    expect(ENGINE_UPDATED).toBe(ENGINE_CHANGELOG[0].date);
  });

  it("lists releases newest first with ISO dates and notes", () => {
    const dates = ENGINE_CHANGELOG.map((r) => r.date);
    expect([...dates].sort().reverse()).toEqual(dates);
    for (const release of ENGINE_CHANGELOG) {
      expect(release.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      expect(release.changes.length).toBeGreaterThan(0);
    }
  });
});
