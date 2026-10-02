"use server";

/**
 * Server actions of the admin console's sign-in and sign-out.
 *
 * - `requestLinkAction`: the sign-in form. Every well-formed address gets the same "check your
 *   email" answer, whether or not it belongs to a staff account (the backend decides, silently).
 *   The e-mail is written in the language the console is in.
 * - `completeSignInAction`: the page the e-mailed link opens calls it once with the token; a good
 *   link signs in (Auth.js redirects), a bad one answers a code the page explains.
 * - `signOutAction`: clears the Auth.js cookie; the `signOut` event revokes the backend session.
 */
import { AuthError, CredentialsSignin } from "next-auth";
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

export async function completeSignInAction(token: string, callbackUrl: string | null): Promise<{ error: SignInCode }> {
  try {
    await signIn("magic-link", { token, redirectTo: safeCallback(callbackUrl) ?? "/admin" });
  } catch (err) {
    if (err instanceof CredentialsSignin) {
      return { error: err.code === "unavailable" ? "unavailable" : "invalid_link" };
    }
    if (err instanceof AuthError) return { error: "unavailable" };
    throw err; // Next's redirect after a successful sign-in
  }
  return { error: "invalid_link" };
}

export async function signOutAction(): Promise<void> {
  await signOut({ redirectTo: SIGN_IN_PATH });
}
