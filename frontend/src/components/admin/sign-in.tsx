"use client";

/**
 * The admin console's sign-in page (not in the mock; built from the design system's `.card`,
 * `.field` and `.cta` classes). Two states:
 *
 * - the form: e-mail + "Send magic link", then "Check your email" whatever the address (the
 *   backend never says whether it belongs to a staff account);
 * - the link: `/admin/login?token=…` signs in once on arrival (Auth.js then redirects), or explains
 *   that the link is invalid, expired or already used and offers the form again.
 */
import { useActionState, useEffect, useRef, useState } from "react";

import { completeSignInAction, requestLinkAction, type LinkState, type SignInCode } from "@/lib/admin/actions";

const LINK_MINUTES = 15;

const SIGN_IN_ERRORS: Record<SignInCode, string> = {
  invalid_link: "This sign-in link is invalid, expired or already used. Request a new one below.",
  unavailable: "Signing in is not possible right now. Try again in a minute.",
};

export function SignInCard({
  token,
  callbackUrl,
  notice,
}: {
  token: string | null;
  callbackUrl: string | null;
  notice: string | null;
}) {
  const [linkError, setLinkError] = useState<SignInCode | null>(null);
  const [signingIn, setSigningIn] = useState(Boolean(token));
  const started = useRef(false);

  useEffect(() => {
    if (!token || started.current) return;
    started.current = true; // the link is single-use: exchange it once, also in strict mode
    void completeSignInAction(token, callbackUrl).then((result) => {
      setLinkError(result.error);
      setSigningIn(false);
    });
  }, [token, callbackUrl]);

  if (signingIn) {
    return (
      <Frame title="Signing you in…" sub="Checking your sign-in link.">
        <p className="admin-signin-text">One moment.</p>
      </Frame>
    );
  }
  return <RequestLink notice={linkError ? SIGN_IN_ERRORS[linkError] : notice} />;
}

function RequestLink({ notice }: { notice: string | null }) {
  const [state, action, pending] = useActionState<LinkState, FormData>(requestLinkAction, {
    status: "idle",
    email: "",
  });
  const [again, setAgain] = useState(false);

  if (state.status === "sent" && !again) {
    return (
      <Frame title="Check your email" sub="Staff sign-in">
        <p className="admin-signin-text">
          If <b>{state.email}</b> belongs to a staff account, a sign-in link is on its way. It works once and
          expires in {LINK_MINUTES} minutes.
        </p>
        <button type="button" className="cta ghost" onClick={() => setAgain(true)}>
          Use a different address
        </button>
      </Frame>
    );
  }

  const error =
    state.status === "error"
      ? state.reason === "invalid_email"
        ? "Enter a valid e-mail address."
        : "The link could not be sent just now. Try again in a minute."
      : null;

  return (
    <Frame title="Sign in to the admin console" sub="Staff only · we e-mail you a sign-in link, no password">
      {notice && <p className="admin-signin-notice">{notice}</p>}
      <form action={(form) => { setAgain(false); action(form); }} noValidate>
        <div className="field">
          <label htmlFor="admin-email">Work e-mail</label>
          <input
            id="admin-email"
            name="email"
            type="email"
            autoComplete="email"
            required
            defaultValue={state.email}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "admin-email-error" : undefined}
            placeholder="name@company.me"
          />
          {error && (
            <p className="admin-signin-notice" id="admin-email-error" role="alert">
              {error}
            </p>
          )}
        </div>
        <button type="submit" className="cta primary" disabled={pending}>
          {pending ? "Sending…" : "Send magic link"}
        </button>
      </form>
    </Frame>
  );
}

function Frame({ title, sub, children }: { title: string; sub: string; children: React.ReactNode }) {
  return (
    <div className="admin-signin">
      <div className="card">
        <div className="cardhd">
          <div>
            <h3>{title}</h3>
            <div className="sub">{sub}</div>
          </div>
        </div>
        <div className="admin-signin-body">{children}</div>
      </div>
    </div>
  );
}
