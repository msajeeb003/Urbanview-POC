"use client";

/**
 * The Users page (`/admin/users`, admins; the pilot scope's `/api/admin/users` "Staff and roles"):
 * "Add staff" (work e-mail, name, role: they sign in with an e-mailed link, no password) and the
 * staff table with each member's role (a select) and status ("Deactivate" closes their sessions,
 * confirmed first; "Activate"). The signed-in admin's own row cannot be changed (the API refuses
 * too). Every write is a server action the API audits; the page re-reads after it.
 */
import { useState, useTransition } from "react";

import { relativeTime } from "@/lib/admin/format";
import type { StaffRole } from "@/lib/admin/sections";
import { addUserAction, updateUserAction } from "@/lib/admin/user-actions";
import { checkNewUser, isSelf, ROLES, type NewUser } from "@/lib/admin/users";
import type { StaffUserList } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { AdminCard, DataTable, StatusChip } from "../parts";

type UserRow = StaffUserList["items"][number];

const EMPTY: NewUser = { email: "", displayName: "", role: "" };

function RoleCell({ user, self }: { user: UserRow; self: boolean }) {
  const showToast = useShell((s) => s.showToast);
  const [pending, start] = useTransition();
  if (self) return <StatusChip tone="rev">{user.role}</StatusChip>;
  return (
    <select
      className="rolesel"
      aria-label={`Role of ${user.email}`}
      value={user.role}
      disabled={pending || !user.is_active}
      title={user.is_active ? ROLES.find((r) => r.value === user.role)?.hint : "Activate the member to change the role"}
      onChange={(e) => {
        const role = e.target.value as StaffRole;
        start(async () => showToast((await updateUserAction(user.id, { role })).message));
      }}
    >
      {ROLES.map((r) => (
        <option key={r.value} value={r.value} title={r.hint}>
          {r.label}
        </option>
      ))}
    </select>
  );
}

function StatusCell({ user, self }: { user: UserRow; self: boolean }) {
  const showToast = useShell((s) => s.showToast);
  const [pending, start] = useTransition();
  const chip = <StatusChip tone={user.is_active ? "ok" : "pend"}>{user.is_active ? "Active" : "Inactive"}</StatusChip>;
  if (self) return chip;
  const next = !user.is_active;
  return (
    <span className="rowacts">
      {chip}
      <button
        type="button"
        className="abtn sm ghost"
        disabled={pending}
        onClick={() => {
          if (!next && !window.confirm(`Deactivate ${user.email}? Their open sessions end now and they can no longer sign in.`)) return;
          start(async () => showToast((await updateUserAction(user.id, { is_active: next })).message));
        }}
      >
        {pending ? "Working…" : next ? "Activate" : "Deactivate"}
      </button>
    </span>
  );
}

export function UsersScreen({ users, me }: { users: UserRow[]; me: string | null }) {
  const showToast = useShell((s) => s.showToast);
  const [draft, setDraft] = useState<NewUser>(EMPTY);
  const [problem, setProblem] = useState<string | null>(null);
  const [pending, start] = useTransition();
  const now = new Date();

  const add = () => {
    const found = checkNewUser(draft);
    setProblem(found);
    if (found) return;
    start(async () => {
      const result = await addUserAction(draft);
      if (result.ok) {
        setDraft(EMPTY);
        showToast(result.message);
      } else setProblem(result.message);
    });
  };

  return (
    <>
      <AdminCard title="Add staff" sub="They sign in with a link e-mailed to this address · no password">
        <form
          className="audit-filters userform"
          onSubmit={(e) => {
            e.preventDefault();
            add();
          }}
        >
          <input
            type="email"
            value={draft.email}
            onChange={(e) => setDraft({ ...draft, email: e.target.value })}
            placeholder="Work e-mail"
            aria-label="Work e-mail"
            maxLength={254}
          />
          <input
            value={draft.displayName}
            onChange={(e) => setDraft({ ...draft, displayName: e.target.value })}
            placeholder="Name (optional)"
            aria-label="Name"
            maxLength={200}
          />
          <select value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value as StaffRole | "" })} aria-label="Role">
            <option value="">Role…</option>
            {ROLES.map((r) => (
              <option key={r.value} value={r.value} title={r.hint}>
                {r.label}
              </option>
            ))}
          </select>
          <button type="submit" className="abtn sm" disabled={pending}>
            {pending ? "Adding…" : "Add staff"}
          </button>
        </form>
        {problem && <div className="admin-note">{problem}</div>}
        <div className="admin-note">
          {ROLES.map((r) => (
            <span key={r.value} className="rolehint">
              <b>{r.label}</b> — {r.hint}.{" "}
            </span>
          ))}
        </div>
      </AdminCard>

      <AdminCard title="Staff users" sub="Sign-in by e-mailed link only · every change is in the audit log">
        <DataTable<UserRow>
          rows={users}
          rowKey={(u) => u.id}
          empty="No staff yet."
          columns={[
            {
              key: "email",
              label: "E-mail",
              mono: true,
              render: (u) => (
                <>
                  {u.email}
                  {isSelf(u.email, me) && <span className="rsub"> · you</span>}
                </>
              ),
            },
            { key: "name", label: "Name", render: (u) => u.display_name ?? "—" },
            { key: "role", label: "Role", render: (u) => <RoleCell user={u} self={isSelf(u.email, me)} /> },
            { key: "active", label: "Status", render: (u) => <StatusCell user={u} self={isSelf(u.email, me)} /> },
            {
              key: "login",
              label: "Last sign-in",
              render: (u) => (u.last_login_at ? relativeTime(u.last_login_at, now) : "never"),
            },
            { key: "sessions", label: "Sessions", mono: true, render: (u) => u.open_sessions },
          ]}
        />
      </AdminCard>
    </>
  );
}
