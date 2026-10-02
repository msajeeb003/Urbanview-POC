"use client";

/**
 * The admin console's frame (wireframe `renderAdmin`): `.adminbar` with the title, the tab row and
 * "← Back to map", then `.adminbody` with the page. Tabs are routes (`/admin/data` …) and a
 * role only sees the tabs it may open (`lib/admin/sections.ts`); the wireframe's `<button>`s are
 * kept so the stylesheet applies unchanged. Not in the mock: the account button before
 * "← Back to map" (role + name; its menu holds the e-mail, the admin-only audit log and users
 * links, and sign-out), compact so the bar still fits at 1440 px.
 */
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";

import { barLinks, sectionForPath, SIGN_IN_PATH, visibleTabs, type StaffRole } from "@/lib/admin/sections";

import { IconAdminTitle } from "../ui/icons";

import { AccountMenu } from "./account-menu";

export interface FrameUser {
  email: string | null;
  name: string | null;
  role: StaffRole;
  /** The temporary open access (`lib/admin/open-access.ts`): no one is signed in. */
  openAccess?: boolean;
}

export function AdminFrame({ user: signedIn, children }: { user: FrameUser | null; children: ReactNode }) {
  const pathname = usePathname() ?? "";
  const router = useRouter();
  const current = sectionForPath(pathname)?.id;
  // The layout renders this bar once per page load and keeps it while the member moves between
  // tabs, so the sign-in page never shows a member: after a sign-out or an ended session the one
  // it was rendered for is gone, and a new sign-in loads the console afresh (`sign-in.tsx`).
  const user = pathname.replace(/\/+$/, "") === SIGN_IN_PATH ? null : signedIn;
  const tabs = visibleTabs(user?.role);
  const links = barLinks(user?.role);

  return (
    <>
      <div className="adminbar">
        <div className="at">
          <IconAdminTitle /> Admin console
        </div>
        {user && (
          <div className="admintabs" role="tablist" aria-label="Admin sections">
            {tabs.map((t) => (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={current === t.id}
                className={current === t.id ? "active" : undefined}
                data-av={t.id}
                onMouseEnter={() => router.prefetch(t.href)}
                onClick={() => router.push(t.href)}
              >
                {t.label}
              </button>
            ))}
          </div>
        )}
        <div className="admin-account">
          {user && <AccountMenu user={user} links={links} />}
          <button type="button" className="abtn ghost" id="adminExit" onClick={() => router.push("/")}>
            ← Back to map
          </button>
        </div>
      </div>
      <div className="adminbody">{children}</div>
    </>
  );
}
