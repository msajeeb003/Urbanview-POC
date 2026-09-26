"use client";

/**
 * The queue's progress header: "n of N reviewed" for the document in view with its counters, and
 * publishing. Publish (admins) appears only once the document has nothing pending — until then a
 * chip says how many are left — and publishes every document's approved values as a new data
 * version (the API refuses while any document has pending items; the blocking documents are
 * named). While the job runs the header follows its steps; then it shows the data version the map
 * serves. "Rollback to previous" asks for confirmation first. Corrected values reach the map only
 * after a publish, and the header says so.
 */
import { useEffect, useState } from "react";

import { relativeTime } from "@/lib/admin/format";
import { progressOf } from "@/lib/admin/review";
import { publishAction, publishStatusAction, rollbackAction } from "@/lib/admin/review-actions";
import type { PublishStatus, ReviewCounters } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { StatusChip } from "../parts";

export function PublishPanel({
  counters,
  isAdmin,
  initialStatus,
}: {
  counters: ReviewCounters | null;
  isAdmin: boolean;
  initialStatus: PublishStatus | null;
}) {
  const showToast = useShell((s) => s.showToast);
  const [status, setStatus] = useState<PublishStatus | null>(initialStatus);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const progress = progressOf(counters);
  const active = status?.active_job ?? null;

  // follow a running publish until it finishes
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(async () => {
      const result = await publishStatusAction();
      if (result.ok) {
        setStatus(result.data);
        if (!result.data.active_job) {
          const last = result.data.last_job;
          showToast(
            last?.status === "succeeded" ? `Published — the map serves ${result.data.current?.label ?? "the new version"}` : "The publish did not finish",
          );
        }
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [active, showToast]);

  const publish = async () => {
    setBusy(true);
    const result = await publishAction();
    showToast(result.message);
    const fresh = await publishStatusAction();
    if (fresh.ok) setStatus(fresh.data);
    setBusy(false);
  };

  const rollback = async () => {
    setBusy(true);
    const result = await rollbackAction();
    showToast(result.message);
    if (result.ok) setStatus(result.data);
    setConfirming(false);
    setBusy(false);
  };

  const others = (status?.blockers ?? []).filter((b) => b.document_id !== counters?.document_id);
  const current = status?.current ?? null;
  const previous = current ? (status?.versions ?? []).find((v) => v.id === current.previous_version_id) : null;
  const last = status?.last_job ?? null;
  const step = active?.progress && typeof active.progress === "object" ? (active.progress as { step?: string }).step : null;

  return (
    <div className="rprogress">
      <div className="rprog-main">
        {counters ? (
          <>
            <div className="rprog-count">
              <b className="mono">{progress.reviewed}</b> of <b className="mono">{progress.total}</b> reviewed
              <span className="rsub"> · {counters.document_name}</span>
            </div>
            <div className="rbar" role="progressbar" aria-valuenow={progress.pct} aria-valuemin={0} aria-valuemax={100} aria-label="Reviewed">
              <i style={{ width: `${progress.pct}%` }} />
            </div>
            <div className="rprog-chips mono">
              {counters.pending} pending · {counters.approved} approved · {counters.amended} amended · {counters.rejected} rejected
            </div>
          </>
        ) : (
          <div className="rprog-count rsub">Pick a document to follow its progress and publish it.</div>
        )}
      </div>
      <div className="rprog-publish">
        {counters && progress.pending > 0 && (
          <span
            className="st pend"
            title={`${progress.pending} item${progress.pending === 1 ? "" : "s"} of this document still need a decision; Publish appears when every item is reviewed.`}
          >
            {progress.pending} pending before publish
          </span>
        )}
        {counters && progress.total > 0 && progress.pending === 0 && isAdmin && (
          <button
            type="button"
            className="abtn"
            disabled={busy || !!active || others.length > 0}
            title={others.length ? `Publishing waits for: ${others.map((b) => `${b.document_name} (${b.pending} pending)`).join(", ")}` : "Publish every document's approved values as a new data version"}
            onClick={() => void publish()}
          >
            {active ? "Publishing…" : "Publish"}
          </button>
        )}
        {counters && progress.total > 0 && progress.pending === 0 && !isAdmin && (
          <span className="rsub">Every item is decided — an administrator publishes.</span>
        )}
        {isAdmin && previous && !confirming && (
          <button type="button" className="abtn ghost" disabled={busy || !!active} onClick={() => setConfirming(true)}>
            Rollback to previous
          </button>
        )}
        {confirming && previous && current && (
          <span className="rconfirm">
            Serve {previous.label} again instead of {current.label}?
            <button type="button" className="abtn sm" disabled={busy} onClick={() => void rollback()}>
              Roll back
            </button>
            <button type="button" className="abtn sm ghost" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </span>
        )}
      </div>
      <div className="rprog-status">
        {active ? (
          <span>
            <StatusChip tone="pend">Publishing</StatusChip> {step ? `step: ${step}` : "queued"}
          </span>
        ) : current ? (
          <span>
            The map serves <b className="mono">{current.label}</b>
            {current.published_at && <span className="rsub"> · published {relativeTime(current.published_at)}</span>}
          </span>
        ) : (
          <span className="rsub">Nothing published yet.</span>
        )}
        {!active && last && last.status === "failed" && (
          <span className="rsub" title={last.error ?? undefined}>
            {" "}
            · the last publish did not finish{last.error ? `: ${last.error.slice(0, 120)}` : ""}
          </span>
        )}
        {others.length > 0 && counters && progress.pending === 0 && (
          <span className="rsub"> · waiting for {others.length} other document{others.length === 1 ? "" : "s"} with pending items</span>
        )}
        <span className="rsub rmapnote"> Corrected values reach the map only after a publish.</span>
      </div>
    </div>
  );
}
