/**
 * Staff sign-in for the admin console (Auth.js v5), magic links only, no passwords.
 *
 * The backend sends and verifies the link (see `lib/admin/staff-api.ts`), so Auth.js needs no
 * database: its Credentials provider `magic-link` takes the link's single-use token, exchanges it
 * for a backend staff session and reads the role from `GET /v1/admin/users/me`. The Auth.js
 * session is a JWT (encrypted cookie, `AUTH_SESSION_MAX_AGE`, default 24 h). The backend bearer
 * token lives only inside that JWT: the `session` callback (what `/api/auth/session` and
 * `useSession` expose) carries e-mail, name and role, never the token; server code reads it with
 * `lib/admin/session.ts`. The role is checked again every `ROLE_RECHECK_MS`, so a changed role or
 * a deactivated user takes effect without a new sign-in; sign-out revokes the backend session.
 */
import NextAuth, { CredentialsSignin } from "next-auth";
import Credentials from "next-auth/providers/credentials";

import { SIGN_IN_PATH, type StaffRole } from "@/lib/admin/sections";
import { exchangeMagicLink, fetchMe, revokeSession } from "@/lib/admin/staff-api";

export const SESSION_MAX_AGE_SECONDS = Number(process.env.AUTH_SESSION_MAX_AGE ?? 24 * 60 * 60);
const ROLE_RECHECK_MS = 5 * 60 * 1000;

/** The sign-in page shows "this link is invalid, expired or already used" for this code. */
class InvalidLink extends CredentialsSignin {
  code = "invalid_link";
}

class ConsoleUnavailable extends CredentialsSignin {
  code = "unavailable";
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  trustHost: true,
  session: { strategy: "jwt", maxAge: SESSION_MAX_AGE_SECONDS },
  pages: { signIn: SIGN_IN_PATH, error: SIGN_IN_PATH },
  providers: [
    Credentials({
      id: "magic-link",
      name: "Magic link",
      credentials: { token: { type: "text" } },
      async authorize(credentials) {
        const token = typeof credentials?.token === "string" ? credentials.token.trim() : "";
        if (token.length < 16) throw new InvalidLink();
        let session;
        try {
          session = await exchangeMagicLink(token);
        } catch {
          throw new ConsoleUnavailable();
        }
        if (session == null) throw new InvalidLink();
        const me = await fetchMe(session.token);
        if (me.kind === "unavailable") throw new ConsoleUnavailable();
        if (me.kind !== "ok") throw new InvalidLink();
        return {
          id: String(me.me.id ?? session.user.id),
          email: me.me.email ?? session.user.email,
          name: me.me.display_name ?? null,
          role: me.role,
          apiToken: session.token,
          apiTokenExpiresAt: session.expires_at,
        };
      },
    }),
  ],
  callbacks: {
    async jwt({ token, user }) {
      if (user) {
        token.role = user.role;
        token.apiToken = user.apiToken;
        token.apiTokenExpiresAt = user.apiTokenExpiresAt;
        token.roleCheckedAt = Date.now();
        return token;
      }
      if (typeof token.apiToken !== "string") return null;
      if (token.apiTokenExpiresAt && Date.parse(token.apiTokenExpiresAt) <= Date.now()) return null;
      if (Date.now() - (token.roleCheckedAt ?? 0) > ROLE_RECHECK_MS) {
        const me = await fetchMe(token.apiToken);
        if (me.kind === "unauthorized") return null; // revoked, expired or deactivated: signed out
        if (me.kind === "ok") {
          token.role = me.role;
          token.name = me.me.display_name ?? null;
          token.roleCheckedAt = Date.now();
        }
        // unavailable: keep the last known role and try again on the next request
      }
      return token;
    },
    async session({ session, token }) {
      session.user.role = token.role as StaffRole;
      session.user.email = typeof token.email === "string" ? token.email : session.user.email;
      session.user.name = typeof token.name === "string" ? token.name : null;
      return session;
    },
  },
  events: {
    async signOut(message) {
      const apiToken = "token" in message ? message.token?.apiToken : undefined;
      if (typeof apiToken === "string") await revokeSession(apiToken);
    },
  },
});
