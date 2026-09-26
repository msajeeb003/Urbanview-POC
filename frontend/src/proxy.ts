/**
 * Route guard for the admin console (Next 16 "proxy", formerly middleware): every /admin request
 * passes the section table (`lib/admin/sections.ts`) with the Auth.js session.
 *
 * - not signed in -> the sign-in page (`callbackUrl` = where they were going);
 * - `/admin` -> the role's first tab; a signed-in visitor on the sign-in page -> the same;
 * - a section the role may not open -> the plain "no access" page, under the same URL (a rewrite,
 *   so reloading keeps the address and nothing looks like an error).
 *
 * The pages check again on the server and the API refuses with 403 whatever the console shows.
 */
import { NextResponse } from "next/server";

import { auth } from "@/auth";
import { accessFor, homeFor, isStaffRole, SIGN_IN_PATH } from "@/lib/admin/sections";

export default auth((request) => {
  const { pathname, search } = request.nextUrl;
  const role = isStaffRole(request.auth?.user?.role) ? request.auth.user.role : null;

  if (pathname.replace(/\/+$/, "") === SIGN_IN_PATH) {
    // a signed-in visitor opening the sign-in page goes home, unless a new link is being used
    if (role && !request.nextUrl.searchParams.has("token")) {
      return NextResponse.redirect(new URL(homeFor(role), request.url));
    }
    return NextResponse.next();
  }

  const access = accessFor(pathname, role);
  switch (access.kind) {
    case "sign-in": {
      const url = new URL(SIGN_IN_PATH, request.url);
      url.searchParams.set("callbackUrl", pathname + search);
      return NextResponse.redirect(url);
    }
    case "home":
      return NextResponse.redirect(new URL(access.href, request.url));
    case "deny": {
      const url = new URL("/admin/no-access", request.url);
      url.searchParams.set("section", access.section.id);
      return NextResponse.rewrite(url);
    }
    default:
      return NextResponse.next();
  }
});

export const config = {
  matcher: ["/admin", "/admin/:path*"],
};
