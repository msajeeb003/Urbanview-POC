"use client";

/**
 * The Publish page (`/admin/publish`, the pilot scope's A4 "Publish"; admins and reviewers): what
 * the public map serves, "Publish" (optional label and notes; refused while any document has items
 * pending review, and the blocking documents are named with a link into the review queue), the
 * running job's steps (read again every 2 s until it ends), and every kept version (earlier
 * versions keep their tiles for retention). No mock screen: the
 * wireframe's admin cards, tables and chips. Every call is a server action over the staff API,
 * which checks the role again.
 */
import Link from "next/link";
import { useEffect, useState } from "react";

import { relativeTime, utcStamp } from "@/lib/admin/format";
import {
  blockersText,
  countsText,
  jobSteps,
  shortError,
  sizeText,
  stepChip,
  stepLabel,
  type JobStep,
  type PublishVersion,
} from "@/lib/admin/publish";
import { publishAction, publishStatusAction } from "@/lib/admin/review-actions";
import type { PublishStatus } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { AdminCard, DataTable, StatusChip } from "../parts";

export function PublishScreen({ initial }: { initial: PublishStatus }) {
  const showToast = useShell((s) => s.showToast);
  const [status, setStatus] = useState<PublishStatus>(initial);
  const [label, setLabel] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const active = status.active_job;
  const current = status.current;
  const blockers = blockersText(status.blockers, status.geometry_blockers);

  // follow a running publish until it finishes
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(async () => {
      const result = await publishStatusAction();
      if (!result.ok) return;
      setStatus(result.data);
      if (!result.data.active_job) {
        const last = result.data.last_job;
        showToast(last?.status === "succeeded" ? `Published — the map serves ${result.data.current?.label ?? "the new version"}` : "The publish did not finish");
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [active, showToast]);

  const refresh = async () => {
    const fresh = await publishStatusAction();
    if (fresh.ok) setStatus(fresh.data);
  };

  const publish = async () => {
    setBusy(true);
    const result = await publishAction(label, notes);
    showToast(result.message);
    if (result.ok) {
      setLabel("");
      setNotes("");
    }
    await refresh();
    setBusy(false);
  };

  const steps = jobSteps(active?.progress ?? (status.last_job?.status === "failed" ? status.last_job.progress : null));
  const lastFailed = !active && status.last_job?.status === "failed" ? status.last_job : null;

  return (
    <>
      <AdminCard
        title="Publish"
        sub="Approved values become a new data version: heatmaps and parcel links computed, map tiles built, the live map switched"
      >
        <div className="pubnow">
          {current ? (
            <span>
              The map serves <b className="mono">v{current.version_no} · {current.label}</b>
              {/* "N min ago" is computed again in the browser: a minute may have passed */}
              <span className="rsub" suppressHydrationWarning>
                {" "}
                · published {relativeTime(current.published_at)}
                {current.published_by ? ` by ${current.published_by}` : ""}
              </span>
            </span>
          ) : (
            <span className="rsub">Nothing published yet.</span>
          )}
        </div>
        <form
          className="audit-filters pubform"
          onSubmit={(e) => {
            e.preventDefault();
            void publish();
          }}
        >
          <input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={80} placeholder="Label (optional, default date.n)" aria-label="Label" />
          <input value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={500} placeholder="Notes (optional)" aria-label="Notes" />
          <button type="submit" className="abtn" disabled={busy || !!active || !status.can_publish} title={blockers ?? undefined}>
            {active ? "Publishing…" : "Publish"}
          </button>
        </form>
        {blockers && (
          <div className="admin-note">
            {blockers}.{" "}
            {status.blockers.length > 0 ? (
              <Link href={`/admin/review?document=${status.blockers[0].document_id}`}>Open the review queue →</Link>
            ) : (
              <Link href="/admin/review/geometry">Open the geometry review →</Link>
            )}
          </div>
        )}
        {lastFailed && (
          <div className="admin-note">
            <span title={lastFailed.error ?? undefined}>
              The last publish did not finish{lastFailed.error ? `: ${shortError(lastFailed.error)}` : "."} Nothing changed on the map.
            </span>
          </div>
        )}
      </AdminCard>

      {steps.length > 0 && (
        <AdminCard title={active ? "Publishing" : "Last publish"} sub={active ? "The live map switches at the last-but-one step" : "Where it stopped"}>
          <DataTable<JobStep>
            rows={steps}
            rowKey={(s) => s.name}
            columns={[
              { key: "step", label: "Step", render: (s) => stepLabel(s.name) },
              {
                key: "status",
                label: "Status",
                render: (s) => {
                  const chip = stepChip(s.status);
                  return <StatusChip tone={chip.tone}>{chip.label}</StatusChip>;
                },
              },
              { key: "started", label: "Started", mono: true, render: (s) => (s.started_at ? utcStamp(s.started_at) : "—") },
              { key: "finished", label: "Finished", mono: true, render: (s) => (s.finished_at ? utcStamp(s.finished_at) : "—") },
            ]}
          />
        </AdminCard>
      )}

      <AdminCard title="Versions" sub={`Newest first · the ${status.keep_versions} newest keep their tiles`}>
        <DataTable<PublishVersion>
          rows={status.versions}
          rowKey={(v) => v.id}
          empty="No version published yet."
          columns={[
            {
              key: "label",
              label: "Version",
              render: (v) => (
                <>
                  <span className="mono">v{v.version_no} · {v.label}</span> {v.is_current && <StatusChip tone="ok">Live</StatusChip>}
                </>
              ),
            },
            {
              key: "published",
              label: "Published",
              mono: true,
              render: (v) => `${utcStamp(v.published_at)}${v.published_by ? ` · ${v.published_by}` : ""}`,
            },
            { key: "counts", label: "Holds", render: (v) => countsText(v.counts) },
            {
              key: "archive",
              label: "Tiles",
              mono: true,
              render: (v) =>
                v.archive_pruned_at ? "cleared" : v.archive_key ? `${v.archive_key} · ${sizeText(v.archive_size_bytes)}` : "—",
            },
          ]}
        />
        {current?.notes && <div className="admin-note">Notes of {current.label}: {current.notes}</div>}
      </AdminCard>
    </>
  );
}
