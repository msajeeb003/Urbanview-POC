import type { Metadata } from "next";
import type { ReactNode } from "react";

import { AdminFrame } from "@/components/admin/admin-frame";
import { currentStaff } from "@/lib/admin/session";

export const metadata: Metadata = {
  title: "Admin console — UrbanView",
  robots: { index: false, follow: false },
};

// The admin console's frame around every /admin page (the sign-in page included, without tabs).
// Role gating: src/proxy.ts, then each page (lib/admin/guard.tsx), then the API itself.
export default async function AdminLayout({ children }: { children: ReactNode }) {
  const staff = await currentStaff();
  return <AdminFrame user={staff}>{children}</AdminFrame>;
}
