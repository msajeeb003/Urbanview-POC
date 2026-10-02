"use server";

/**
 * Server actions of the admin console's sign-in and sign-out.
 *
 * - `requestLinkAction`: the sign-in form. Every well-formed address gets the same "check your
 *   email" answer, whether or not it belongs to a staff account (the backend decides, silently).
 *   The e-mail is written in the language the console is in.
 * - `completeSignInAction`: the page the e-mailed link opens calls it once with the token; a good
 *   link signs in and answers where to go, a bad one answers a code the page explains. The page
 *   then loads the console afresh (`sign-in.tsx`), no redirect from here: the console's bar is
 *   rendered by the layout, which a redirect out of a server action does not render again behind
 *   the server's reverse proxy, so the first page after sign-in had no tabs and no account menu.
 * - `signOutAction`: clears the Auth.js cookie; the `signOut` event revokes the backend session.
 */
import { CredentialsSignin } from "next-auth";
import { cookies } from "next/headers";

import { signIn, signOut } from "@/auth";
import { LANG_COOKIE, langFrom } from "@/lib/i18n/config";

import { safeCallback, SIGN_IN_PATH } from "./sections";
import { requestMagicLink } from "./staff-api";

export type LinkState =
  | { status: "idle"; email: string }
  | { status: "sent"; email: string }
  | { status: "error"; email: string; reason: "invalid_email" | "unavailable" };

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export async function requestLinkAction(_: LinkState, form: FormData): Promise<LinkState> {
  const email = String(form.get("email") ?? "").trim().toLowerCase();
  if (!EMAIL.test(email) || email.length > 254) return { status: "error", email, reason: "invalid_email" };
  try {
    // the sign-in e-mail is written in the language the console is in (the language cookie)
    const language = langFrom((await cookies()).get(LANG_COOKIE)?.value);
    const result = await requestMagicLink(email, language);
    return result === "sent" ? { status: "sent", email } : { status: "error", email, reason: "invalid_email" };
  } catch {
    return { status: "error", email, reason: "unavailable" };
  }
}

export type SignInCode = "invalid_link" | "unavailable";

export type SignInResult = { error: SignInCode } | { error: null; redirectTo: string };

export async function completeSignInAction(token: string, callbackUrl: string | null): Promise<SignInResult> {
  const redirectTo = safeCallback(callbackUrl) ?? "/admin";
  try {
    await signIn("magic-link", { token, redirectTo, redirect: false });
  } catch (err) {
    if (err instanceof CredentialsSignin) {
      return { error: err.code === "unavailable" ? "unavailable" : "invalid_link" };
    }
    return { error: "unavailable" };
  }
  return { error: null, redirectTo };
}

export async function signOutAction(): Promise<void> {
  await signOut({ redirectTo: SIGN_IN_PATH });
}
