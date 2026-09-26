"use client";

/**
 * The admin console's frame (wireframe `renderAdmin`): `.adminbar` with the title, the tab row and
 * "← Back to map", then `.adminbody` with the page. Tabs are routes (`/admin/overview` …) and a
 * role only sees the tabs it may open (`lib/admin/sections.ts`); the wireframe's `<button>`s are
 * kept so the stylesheet applies unchanged. Not in the mock: the account button before
 * "← Back to map" (role + name; its menu holds the e-mail, the admin-only audit log and users
 * links, and sign-out), compact so the bar still fits at 1440 px.
 */
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";

import { barLinks, sectionForPath, visibleTabs, type StaffRole } from "@/lib/admin/sections";

import { IconAdminTitle } from "../ui/icons";

import { AccountMenu } from "./account-menu";

export interface FrameUser {
  email: string | null;
  name: string | null;
  role: StaffRole;
}

export function AdminFrame({ user, children }: { user: FrameUser | null; children: ReactNode }) {
  const pathname = usePathname() ?? "";
  const router = useRouter();
  const current = sectionForPath(pathname)?.id;
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
