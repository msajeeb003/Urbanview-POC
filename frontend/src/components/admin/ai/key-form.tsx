"use client";

/**
 * The write-only key field of the AI extraction page: a password input checked against the API's
 * format rules (`lib/admin/ai.ts`, the same messages) before `saveKeyAction` sends it. The value
 * lives in this component's state only: cleared after a save, never stored, logged or put in a
 * URL. Disabled while the server cannot encrypt (no `SECRETS_ENCRYPTION_KEY`).
 */
import { useId, useState, useTransition } from "react";

import { keyProblem } from "@/lib/admin/ai";
import { saveKeyAction } from "@/lib/admin/ai-actions";
import { useShell } from "@/lib/store";

export function KeyForm({ disabled, hasSavedKey }: { disabled: boolean; hasSavedKey: boolean }) {
  const showToast = useShell((s) => s.showToast);
  const id = useId();
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();

  const submit = () => {
    const problem = keyProblem(value);
    if (problem) {
      setMessage(problem);
      return;
    }
    start(async () => {
      try {
        const result = await saveKeyAction(value.trim());
        if (result.ok) {
          setValue("");
          setMessage(null);
          showToast(result.message);
        } else {
          setMessage(result.message); // the typed key stays for a correction
        }
      } catch {
        setMessage("The data service did not answer. Try again in a moment.");
      }
    });
  };

  return (
    <form
      className="aikeyform"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="field">
        <label htmlFor={id}>{hasSavedKey ? "Replace the saved key" : "Anthropic API key"}</label>
        <div className="aikeyrow">
          <input
            id={id}
            type={reveal ? "text" : "password"}
            value={value}
            placeholder="sk-ant-…"
            autoComplete="off"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            disabled={disabled || pending}
            maxLength={300}
            onChange={(e) => {
              setValue(e.target.value);
              if (message) setMessage(null);
            }}
          />
          <button type="button" className="abtn sm ghost" disabled={disabled || pending} onClick={() => setReveal((v) => !v)}>
            {reveal ? "Hide" : "Show"}
          </button>
          <button type="submit" className="abtn sm" disabled={disabled || pending || !value.trim()} aria-busy={pending || undefined}>
            {pending ? "Saving…" : hasSavedKey ? "Replace key" : "Save key"}
          </button>
        </div>
        <span className="aihint">
          Create a key in the Anthropic Console (API keys). It is encrypted on the server and never shown again; the worker uses it for the
          next extraction, no restart needed.
        </span>
      </div>
      {message && (
        <p className="formmsg" role="alert">
          {message}
        </p>
      )}
    </form>
  );
}
