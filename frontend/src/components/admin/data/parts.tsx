"use client";

/**
 * Small pieces of the "Data sources" screen:
 *
 * - `PillView`: a status chip (`.st`) with its mono detail (attempts, items, cost) and the failure
 *   reason in the API's words;
 * - `ActionButton`: runs one server action, shows it pending, and reports the outcome in the
 *   shell's toast (the wireframe's `✦` toast), success or refusal alike;
 * - `AutoRefresh`: re-reads the page's server data every few seconds while something is queued or
 *   running, and stops once everything is finished (or while the tab is hidden).
 */
import { useRouter } from "next/navigation";
import { useEffect, useTransition, type ReactNode } from "react";

import { StatusChip } from "@/components/admin/parts";
import type { Pill } from "@/lib/admin/data";
import type { ActionResult } from "@/lib/admin/data-actions";
import { useShell } from "@/lib/store";

export function PillView({ pill }: { pill: Pill }) {
  if (pill.muted) return <span className="pillcell muted">{pill.label}</span>;
  return (
    <span className="pillcell">
      <StatusChip tone={pill.tone}>{pill.label}</StatusChip>
      {pill.detail && <span className="mono pd">{pill.detail}</span>}
      {pill.reason && (
        <span className="pr" title={pill.reason}>
          {pill.reason}
        </span>
      )}
    </span>
  );
}

export function ActionButton({
  action,
  children,
  ghost = true,
  disabled,
  title,
  confirm,
  onDone,
}: {
  action: () => Promise<ActionResult<unknown>>;
  children: ReactNode;
  ghost?: boolean;
  disabled?: boolean;
  title?: string;
  /** Asked with `window.confirm` first (removals). */
  confirm?: string;
  onDone?: (ok: boolean) => void;
}) {
  const showToast = useShell((s) => s.showToast);
  const [pending, start] = useTransition();
  return (
    <button
      type="button"
      className={ghost ? "abtn sm ghost" : "abtn sm"}
      disabled={disabled || pending}
      aria-busy={pending || undefined}
      title={title}
      onClick={() => {
        if (confirm && !window.confirm(confirm)) return;
        start(async () => {
          try {
            const result = await action();
            showToast(result.message);
            onDone?.(result.ok);
          } catch {
            showToast("The data service did not answer. Try again in a moment.");
            onDone?.(false);
          }
        });
      }}
    >
      {pending ? "Working…" : children}
    </button>
  );
}

export function AutoRefresh({ active, intervalMs = 3000 }: { active: boolean; intervalMs?: number }) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const tick = () => {
      if (document.visibilityState === "visible") router.refresh();
    };
    const timer = window.setInterval(tick, intervalMs);
    const onVisible = () => document.visibilityState === "visible" && router.refresh();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [active, intervalMs, router]);
  return null;
}
