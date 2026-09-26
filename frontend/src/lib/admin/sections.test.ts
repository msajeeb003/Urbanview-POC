import { describe, expect, it } from "vitest";

import {
  accessFor,
  barLinks,
  canOpen,
  homeFor,
  isReadOnly,
  safeCallback,
  sectionForPath,
  visibleTabs,
} from "./sections";

const labels = (role: Parameters<typeof visibleTabs>[0]) => visibleTabs(role).map((s) => s.label);

describe("admin sections", () => {
  it("shows every tab of the wireframe to an admin, in order", () => {
    expect(labels("admin")).toEqual([
      "Overview",
      "AI review queue",
      "Planning rules",
      "Financial assumptions",
      "Calculation engine",
      "Orders",
      "Data sources",
    ]);
    expect(barLinks("admin").map((s) => s.label)).toEqual(["Audit log", "Users"]);
  });

  it("hides users and assumptions from a reviewer", () => {
    expect(labels("reviewer")).toEqual(["Overview", "AI review queue", "Planning rules", "Data sources"]);
    expect(barLinks("reviewer")).toEqual([]);
    expect(canOpen("reviewer", "users")).toBe(false);
    expect(canOpen("reviewer", "assumptions")).toBe(false);
    expect(isReadOnly("reviewer", "rules")).toBe(true);
    expect(isReadOnly("admin", "rules")).toBe(false);
  });

  it("gives an expert the order queue only", () => {
    expect(labels("expert")).toEqual(["Orders"]);
    expect(homeFor("expert")).toBe("/admin/orders");
    expect(homeFor("reviewer")).toBe("/admin/overview");
  });

  it("guards routes: sign-in, home, allow, deny", () => {
    expect(accessFor("/admin/overview", null)).toEqual({ kind: "sign-in" });
    expect(accessFor("/admin/login", null)).toEqual({ kind: "allow" });
    expect(accessFor("/admin", "expert")).toEqual({ kind: "home", href: "/admin/orders" });
    expect(accessFor("/admin/", "admin")).toEqual({ kind: "home", href: "/admin/overview" });
    expect(accessFor("/admin/orders", "expert")).toEqual({ kind: "allow" });
    expect(accessFor("/admin/overview", "expert")).toMatchObject({ kind: "deny", section: { id: "overview" } });
    expect(accessFor("/admin/users", "reviewer")).toMatchObject({ kind: "deny", section: { id: "users" } });
    expect(accessFor("/admin/audit", "admin")).toEqual({ kind: "allow" });
    expect(accessFor("/admin/no-access", "expert")).toEqual({ kind: "allow" });
    expect(accessFor("/admin/whatever", "reviewer")).toEqual({ kind: "home", href: "/admin/overview" });
  });

  it("maps nested paths to their section", () => {
    expect(sectionForPath("/admin/orders/12")?.id).toBe("orders");
    expect(sectionForPath("/admin/ordersx")).toBeNull();
    expect(sectionForPath("/admin/audit?actor=a")?.id).toBe("audit");
  });

  it("only returns to local admin paths after sign-in", () => {
    expect(safeCallback("/admin/audit?actor=a")).toBe("/admin/audit?actor=a");
    expect(safeCallback("https://evil.example/admin")).toBeNull();
    expect(safeCallback("//evil.example/admin")).toBeNull();
    expect(safeCallback("/admin/login?token=x")).toBeNull();
    expect(safeCallback(null)).toBeNull();
  });
});
