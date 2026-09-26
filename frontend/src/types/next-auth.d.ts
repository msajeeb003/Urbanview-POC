import type { DefaultSession } from "next-auth";

import type { StaffRole } from "@/lib/admin/sections";

declare module "next-auth" {
  interface User {
    role?: StaffRole;
    /** The backend staff session token: stays inside the encrypted JWT, never in the session. */
    apiToken?: string;
    apiTokenExpiresAt?: string;
  }

  interface Session {
    user: { role: StaffRole } & DefaultSession["user"];
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    role?: StaffRole;
    apiToken?: string;
    apiTokenExpiresAt?: string;
    roleCheckedAt?: number;
  }
}
