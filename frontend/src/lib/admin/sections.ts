/**
 * The admin console's sections and who may open them: one table the proxy (route guard), the
 * tab row and the pages read, so hiding a tab and refusing its route never disagree.
 *
 * Roles (the staff user's `role`, from `GET /v1/admin/users/me`):
 * - admin: everything, including users, financial assumptions, analytics (A7) and the audit log;
 * - reviewer: Overview, AI review queue, Planning rules (read), Orders, Data sources;
 * - expert: Orders (the API returns only the orders assigned to them).
 *
 * The API enforces the same boundaries on every `/v1/admin/*` route (403, never 404); this table
 * only keeps the console from offering what the API would refuse.
 */

export type StaffRole = "admin" | "reviewer" | "expert";

export const STAFF_ROLES: readonly StaffRole[] = ["admin", "reviewer", "expert"];

export type SectionId =
  | "overview"
  | "review"
  | "rules"
  | "assumptions"
  | "engine"
  | "orders"
  | "data"
  | "analytics"
  | "audit"
  | "users";

export interface Section {
  id: SectionId;
  label: string;
  href: string;
  /** Shown in the wireframe's tab row; the others are reached from the admin bar. */
  tab: boolean;
  roles: readonly StaffRole[];
  /** Roles that see the section without its actions. */
  readOnly?: readonly StaffRole[];
}

export const SECTIONS: readonly Section[] = [
  { id: "overview", label: "Overview", href: "/admin/overview", tab: true, roles: ["admin", "reviewer"] },
  { id: "review", label: "AI review queue", href: "/admin/review", tab: true, roles: ["admin", "reviewer"] },
  {
    id: "rules",
    label: "Planning rules",
    href: "/admin/rules",
    tab: true,
    roles: ["admin", "reviewer"],
    readOnly: ["reviewer"],
  },
  { id: "assumptions", label: "Financial assumptions", href: "/admin/assumptions", tab: true, roles: ["admin"] },
  { id: "engine", label: "Calculation engine", href: "/admin/engine", tab: true, roles: ["admin"] },
  // Reviewers record payments and assign experts too (the orders ticket); experts see their own.
  { id: "orders", label: "Orders", href: "/admin/orders", tab: true, roles: ["admin", "reviewer", "expert"] },
  // Reviewers register documents and drop in their PDFs too (the data sources ticket).
  { id: "data", label: "Data sources", href: "/admin/data", tab: true, roles: ["admin", "reviewer"] },
  // the pilot scope's A7 "Analytics and audit" (GET /v1/admin/analytics is admin only)
  { id: "analytics", label: "Analytics", href: "/admin/analytics", tab: false, roles: ["admin"] },
  { id: "audit", label: "Audit log", href: "/admin/audit", tab: false, roles: ["admin"] },
  { id: "users", label: "Users", href: "/admin/users", tab: false, roles: ["admin"] },
];

/** Routes under /admin that need a session but no particular role. */
export const OPEN_PATHS = ["/admin/no-access"] as const;
export const SIGN_IN_PATH = "/admin/login";

export function isStaffRole(value: unknown): value is StaffRole {
  return typeof value === "string" && (STAFF_ROLES as readonly string[]).includes(value);
}

export function section(id: SectionId): Section {
  const found = SECTIONS.find((s) => s.id === id);
  if (!found) throw new Error(`unknown admin section ${id}`);
  return found;
}

/** The section a path belongs to (`/admin/audit?actor=x` → audit), or null. */
export function sectionForPath(pathname: string): Section | null {
  const path = pathname.split(/[?#]/, 1)[0].replace(/\/+$/, "");
  return SECTIONS.find((s) => path === s.href || path.startsWith(`${s.href}/`)) ?? null;
}

export function canOpen(role: StaffRole | null | undefined, id: SectionId): boolean {
  return role != null && section(id).roles.includes(role);
}

export function isReadOnly(role: StaffRole | null | undefined, id: SectionId): boolean {
  return role != null && (section(id).readOnly ?? []).includes(role);
}

export function visibleTabs(role: StaffRole | null | undefined): Section[] {
  return SECTIONS.filter((s) => s.tab && canOpen(role, s.id));
}

/** Admin-bar links outside the tab row (audit log, users) the role may open. */
export function barLinks(role: StaffRole | null | undefined): Section[] {
  return SECTIONS.filter((s) => !s.tab && canOpen(role, s.id));
}

/** Where a role lands on /admin: its first tab. */
export function homeFor(role: StaffRole): string {
  return visibleTabs(role)[0]?.href ?? "/admin/no-access";
}

export type Access =
  | { kind: "sign-in" }
  | { kind: "allow" }
  | { kind: "home"; href: string }
  | { kind: "deny"; section: Section };

/**
 * What the route guard does with a request for `pathname` by a visitor with `role` (null = not
 * signed in): sign-in page for anyone without a session, the role's home for `/admin`, the
 * "no access" page for a section the role may not open.
 */
export function accessFor(pathname: string, role: StaffRole | null): Access {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === SIGN_IN_PATH) return { kind: "allow" };
  if (role == null) return { kind: "sign-in" };
  if (path === "/admin") return { kind: "home", href: homeFor(role) };
  if ((OPEN_PATHS as readonly string[]).includes(path)) return { kind: "allow" };
  const found = sectionForPath(path);
  if (found == null) return { kind: "home", href: homeFor(role) };
  return canOpen(role, found.id) ? { kind: "allow" } : { kind: "deny", section: found };
}

/** A local path to return to after signing in (never another origin). */
export function safeCallback(value: string | null | undefined): string | null {
  if (!value || !value.startsWith("/admin") || value.startsWith("//") || value.startsWith(SIGN_IN_PATH)) {
    return null;
  }
  return value;
}
