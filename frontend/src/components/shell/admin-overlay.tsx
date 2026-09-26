"use client";

/**
 * The admin view (wireframe `.admin`): a full-area `--paper` overlay over the map row. Its content
 * is the `/admin/*` route (`app/(shell)/admin/`): the frame (admin bar, tabs, signed-in user,
 * "← Back to map") and the page, role-gated by `src/proxy.ts`.
 */
import type { ReactNode } from "react";

export function AdminOverlay({ on, children }: { on: boolean; children?: ReactNode }) {
  return (
    <div className={on ? "admin on" : "admin"} id="admin" aria-hidden={!on}>
      {on && children}
    </div>
  );
}
