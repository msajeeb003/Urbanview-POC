import { describe, expect, it } from "vitest";

import { checkNewUser, explainUserProblem, isSelf, ROLES } from "./users";

describe("users page rules", () => {
  it("names the pilot scope's three roles", () => {
    expect(ROLES.map((r) => r.value)).toEqual(["admin", "reviewer", "expert"]);
  });

  it("checks the add-staff form as the API does", () => {
    expect(checkNewUser({ email: "ana@example.me", displayName: "", role: "reviewer" })).toBeNull();
    expect(checkNewUser({ email: "ana", displayName: "", role: "reviewer" })).toMatch(/e-mail/);
    expect(checkNewUser({ email: "ana@example.me", displayName: "", role: "" })).toMatch(/role/);
  });

  it("recognises the signed-in admin's own row and explains refusals", () => {
    expect(isSelf("Ana@Example.me", "ana@example.me")).toBe(true);
    expect(isSelf("ana@example.me", null)).toBe(false);
    expect(explainUserProblem({ status: 409, details: { reason: "self" } })).toMatch(/your own/);
    expect(explainUserProblem({ status: 409, message: "A staff user with this e-mail already exists" })).toMatch(/already exists/);
    expect(explainUserProblem({ status: 403 })).toMatch(/administrators/);
    expect(explainUserProblem({ status: 503 })).toMatch(/did not answer/);
  });
});
