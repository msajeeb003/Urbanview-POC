"use client";

/**
 * The geometry review (`/admin/review/geometry`; the pilot scope's A2: geometry drafts "in the
 * same or a sibling queue", no mock screen: the value queue's card, panes and keys): staged
 * geometry batches with their origin and validity QA (invalid or empty features, the producing
 * run's own warnings), decided before the publish job may apply them.
 *
 * The card (pending count, the switch to the extracted values, counts, filters: status, origin,
 * layer, history), then three panes: the batches (pending first, failing QA first), the batch
 * (what it is, where it came from, its QA issues with the features they name, the georeferencing
 * fit when it has one, the decision) and the preview (the features drawn, issues outlined).
 * Keys: j / k or ↓ / ↑ move, Enter approves (not a batch whose QA fails), r rejects
 * (reason required; final: the geometry is fixed and staged again), n next pending, Esc closes.
 * A rejected or approved batch keeps its row; the counts follow every decision.
 */
import Form from "next/form";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { relativeTime, utcStamp } from "@/lib/admin/format";
import {
  decisionChip,
  draftSource,
  draftTitle,
  emptyGeometryText,
  GEOMETRY_LAYERS,
  GEOMETRY_STATUSES,
  hasGeometryFilters,
  isPending,
  issueName,
  issueTone,
  nextPendingDraft,
  ORIGINS,
  originLabel,
  qaChip,
  runOf,
  type GeometryFilters,
} from "@/lib/admin/geometry";
import {
  approveGeometryAction,
  bulkApproveGeometryAction,
  rejectGeometryAction,
  type GeometryDecision,
  type Result,
} from "@/lib/admin/geometry-actions";
import type { GeometryCounts, GeometryDraft, GeometryPage } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { StatusChip } from "../parts";

import { GeometryPreview } from "./geometry-preview";
import { ReviewTabs } from "./review-tabs";

function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

function Fact({ label, children, wide }: { label: string; children: ReactNode; wide?: boolean }) {
  return (
    <div className={wide ? "rfact wide" : "rfact"}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

const BLOCKER_WORDS: Record<string, string> = {
  qa_failed: "Its checks fail: reject it, fix the geometry and stage it again.",
  published: "Published: changes go through a new import.",
  superseded: "A newer run of the same geometry replaced this batch.",
  rejected: "Rejected: the geometry is fixed and staged again as a new batch.",
};

function RejectEditor({ onSubmit, onCancel }: { onSubmit: (note: string) => Promise<string | null>; onCancel: () => void }) {
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => area.current?.focus(), []);
  const save = async () => {
    if (!note.trim()) {
      setMessage("Give the reason: it goes to the audit log with the decision.");
      return;
    }
    setSaving(true);
    const refused = await onSubmit(note);
    setSaving(false);
    if (refused) setMessage(refused);
  };
  return (
    <div className="reditor" role="group" aria-label="Reject the geometry">
      <textarea
        ref={area}
        className="rinput rnote"
        rows={3}
        aria-label="Reason"
        placeholder="Reason (required): e.g. “parcels 14–18 shifted 3 m east: redo the control points”"
        value={note}
        maxLength={2000}
        onChange={(e) => setNote(e.target.value)}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Escape") onCancel();
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void save();
        }}
      />
      {message && <div className="rmsg">{message}</div>}
      <div className="rbtns">
        <button type="button" className="abtn sm" disabled={saving} onClick={() => void save()}>
          {saving ? "Saving…" : "Reject geometry"}
        </button>
        <button type="button" className="abtn sm ghost" onClick={onCancel}>
          Cancel
        </button>
        <span className="rkeys mono">Ctrl+Enter save · Esc cancel</span>
      </div>
    </div>
  );
}

function GeometryDetail({
  draft,
  rejecting,
  onReject,
  onApprove,
  onSubmitReject,
  runPending,
  onBulk,
}: {
  draft: GeometryDraft;
  rejecting: boolean;
  onReject: (open: boolean) => void;
  onApprove: () => void;
  onSubmitReject: (note: string) => Promise<string | null>;
  runPending: number;
  onBulk: () => void;
}) {
  const chip = decisionChip(draft);
  const qa = qaChip(draft.qa_status);
  const geo = draft.georeference;
  const issues = draft.qa_issues ?? [];
  const open = draft.status === "staged";
  return (
    <div className="rdetail">
      <div className="rhead">
        <div className="reyebrow mono">
          {originLabel(draft.origin)}
          {runOf(draft) ? ` · ${runOf(draft)}` : ""}
        </div>
        <h2 className="rtitle">{draftTitle(draft)}</h2>
        <div className="rvalues">
          <StatusChip tone={chip.tone}>{chip.label}</StatusChip> <StatusChip tone={qa.tone}>{qa.label}</StatusChip>
        </div>
        <div className="rlast">
          {draft.reviewed_by && draft.reviewed_at ? (
            <>
              {chip.label} by <b>{draft.reviewed_by}</b> · <span title={utcStamp(draft.reviewed_at)}>{relativeTime(draft.reviewed_at)}</span>
              {draft.review_note && <span className="rnoteline">“{draft.review_note}”</span>}
            </>
          ) : open ? (
            <>Not decided yet — this geometry is not on the map until it is approved and published.</>
          ) : (
            <>{chip.label}.</>
          )}
        </div>
      </div>

      {open ? (
        <div className="racts">
          <button
            type="button"
            className="abtn"
            onClick={onApprove}
            disabled={!draft.can_approve || draft.review_status === "approved"}
            title={draft.approve_blocker ? BLOCKER_WORDS[draft.approve_blocker] : "Approve: the next publish applies it"}
          >
            Approve <span className="rkbd">Enter</span>
          </button>
          <button type="button" className={rejecting ? "abtn" : "abtn ghost"} onClick={() => onReject(!rejecting)}>
            Reject <span className="rkbd">R</span>
          </button>
        </div>
      ) : (
        <div className="rhint rclosed">{draft.approve_blocker ? BLOCKER_WORDS[draft.approve_blocker] : chip.label}</div>
      )}
      {open && draft.approve_blocker === "qa_failed" && <div className="rhint">{BLOCKER_WORDS.qa_failed}</div>}
      {open && rejecting && <RejectEditor key={`reject-${draft.id}`} onSubmit={onSubmitReject} onCancel={() => onReject(false)} />}
      {open && runPending > 1 && runOf(draft) && (
        <div className="rbulk">
          <button type="button" className="abtn sm ghost" onClick={onBulk}>
            Approve all {runPending} pending of {runOf(draft)}
          </button>
        </div>
      )}

      <div className="gvissues">
        <div className="gvh">Checks</div>
        {draft.qa_status === null ? (
          <div className="rhint">Not checked yet: the checks run when it is first approved.</div>
        ) : issues.length === 0 ? (
          <div className="rhint">Every feature is a valid geometry; the producing run reported no warnings.</div>
        ) : (
          <ul>
            {issues.map((issue, i) => (
              <li key={`${issue.code}-${i}`} className="gvissue">
                <div>
                  <StatusChip tone={issueTone(issue)}>{issueName(issue.code)}</StatusChip>
                  {issue.count > 1 && <span className="mono gvcount"> ×{issue.count.toLocaleString("en-US")}</span>}
                </div>
                <div className="gvmsg">{issue.message}</div>
                {(issue.features ?? []).length > 0 && (
                  <div className="gvfeat mono">
                    {(issue.features ?? []).slice(0, 8).join(" · ")}
                    {(issue.features ?? []).length > 8 ? ` · +${(issue.features ?? []).length - 8} more` : ""}
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <dl className="rfacts">
        <Fact label="Origin">
          {originLabel(draft.origin)}
          {draft.origin && <span className="rsub"> · {ORIGINS.find((o) => o.value === draft.origin)?.note}</span>}
        </Fact>
        <Fact label="Layer">{draft.layer_label}</Fact>
        <Fact label="Document">
          {draft.document ? (
            <Link href={`/admin/data/documents/${draft.document.id}`}>
              {draft.document.short_code ? `${draft.document.short_code} · ` : ""}
              {draft.document.name}
            </Link>
          ) : (
            <span className="rsub">—</span>
          )}
        </Fact>
        <Fact label="Produced by">
          {draft.dataset ? (
            <>
              <span className="mono">{draft.dataset.version}</span>
              <span className="rsub"> · {draft.dataset.kind} · {draft.dataset.status}</span>
            </>
          ) : (
            <span className="mono">{draft.dataset_version ?? draft.produced_by ?? "—"}</span>
          )}
        </Fact>
        <Fact label="Features">{draft.feature_count.toLocaleString("en-US")}</Fact>
        <Fact label="Staged">
          <span title={utcStamp(draft.created_at)}>{relativeTime(draft.created_at)}</span>
        </Fact>
        {geo && (
          <Fact label="Georeferencing" wide>
            {geo.method === "native" ? (
              <>GIS drawing in its own coordinate system ({geo.crs}), reprojected without control points</>
            ) : (
              <>
                {geo.method} fit on {geo.points_used} points · RMSE <span className="mono">{geo.rmse_m?.toFixed(3) ?? "—"} m</span>
                {geo.max_rmse_m != null && <span className="rsub"> (limit {geo.max_rmse_m} m)</span>}
              </>
            )}
            {geo.snapped_ratio != null && <span className="rsub"> · {Math.round(geo.snapped_ratio * 100)}% of vertices snapped to the cadastre</span>}
            {geo.systematic_offset_m != null && <span className="rsub"> · mean offset {geo.systematic_offset_m.toFixed(2)} m</span>}
          </Fact>
        )}
        {draft.published_at && (
          <Fact label="Published">
            <span title={utcStamp(draft.published_at)}>{relativeTime(draft.published_at)}</span>
            {draft.published_version_id != null && <span className="rsub"> · version #{draft.published_version_id}</span>}
          </Fact>
        )}
      </dl>
    </div>
  );
}

export function GeometryScreen({
  initial,
  filters,
  valuesPending,
  me,
}: {
  initial: GeometryPage;
  filters: GeometryFilters;
  valuesPending: number | null;
  me: string;
}) {
  const showToast = useShell((s) => s.showToast);
  const [drafts, setDrafts] = useState<GeometryDraft[]>(initial.items);
  const [counts, setCounts] = useState<GeometryCounts>(initial.counts);
  const [cursor, setCursor] = useState(() => {
    const i = initial.items.findIndex(isPending);
    return i >= 0 ? i : 0;
  });
  const [rejecting, setRejecting] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const draftsRef = useRef(drafts);
  useEffect(() => {
    draftsRef.current = drafts;
  }, [drafts]);
  const draft = drafts[cursor] ?? null;

  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-index="${cursor}"]`)?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  const moveTo = useCallback((index: number) => {
    setRejecting(false);
    setCursor(Math.max(0, Math.min(index, draftsRef.current.length - 1)));
  }, []);

  const settle = useCallback(
    (result: Result<GeometryDecision>): string | null => {
      if (!result.ok) {
        showToast(result.message);
        return result.message;
      }
      const list = draftsRef.current.map((d) => (d.id === result.data.draft.id ? result.data.draft : d));
      setDrafts(list);
      if (result.data.counts) setCounts(result.data.counts);
      showToast(result.message);
      setRejecting(false);
      const next = nextPendingDraft(list, cursor);
      if (next != null) moveTo(next);
      return null;
    },
    [cursor, moveTo, showToast],
  );

  const approve = useCallback(async () => {
    const current = draftsRef.current[cursor];
    if (!current || !current.can_approve || current.review_status === "approved") return;
    settle(await approveGeometryAction(current.id));
  }, [cursor, settle]);

  const reject = useCallback(
    async (note: string) => {
      const current = draftsRef.current[cursor];
      return current ? settle(await rejectGeometryAction(current.id, note)) : null;
    },
    [cursor, settle],
  );

  const run = draft ? runOf(draft) : null;
  const runPending = run ? drafts.filter((d) => runOf(d) === run && isPending(d)).length : 0;

  const bulk = useCallback(async () => {
    const current = draftsRef.current[cursor];
    const version = current ? runOf(current) : null;
    if (!version) return;
    const result = await bulkApproveGeometryAction(version);
    showToast(result.message);
    if (!result.ok) return;
    const approved = new Set(result.data.result.approved);
    const now = new Date().toISOString();
    const list = draftsRef.current.map((d) =>
      approved.has(d.id) ? { ...d, review_status: "approved" as const, reviewed_by: me, reviewed_at: now, review_note: null } : d,
    );
    setDrafts(list);
    if (result.data.counts) setCounts(result.data.counts);
    const next = nextPendingDraft(list, cursor);
    if (next != null) moveTo(next);
  }, [cursor, me, moveTo, showToast]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || typing(e.target)) return;
      if (document.querySelector(".overlay.on")) return;
      const key = e.key;
      if (key === "j" || key === "ArrowDown") {
        e.preventDefault();
        moveTo(cursor + 1);
      } else if (key === "k" || key === "ArrowUp") {
        e.preventDefault();
        moveTo(cursor - 1);
      } else if (key === "Enter" && !rejecting) {
        if ((e.target as HTMLElement | null)?.closest?.("button, a")) return; // a focused button takes it
        e.preventDefault();
        void approve();
      } else if (key === "r" && draft?.can_reject) {
        e.preventDefault();
        setRejecting(true);
      } else if (key === "n") {
        e.preventDefault();
        const next = nextPendingDraft(draftsRef.current, cursor);
        if (next != null) moveTo(next);
      } else if (key === "Escape") {
        setRejecting(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [approve, cursor, draft, moveTo, rejecting]);

  return (
    <div className="rvscreen">
      <div className="card rvcard">
        <div className="cardhd">
          <div>
            <h3>Geometry — review queue</h3>
            <div className="sub">Staged geometry is checked (valid, non-empty features; the producing run&apos;s warnings) and approved before it publishes</div>
          </div>
          <span className={counts.pending ? "st pend" : "st ok"}>{counts.pending} pending</span>
        </div>
        <ReviewTabs active="geometry" valuesPending={valuesPending} geometryPending={counts.pending} />
        <div className="gvcounts mono">
          {counts.pending} pending · {counts.approved} approved, waiting for the next publish · {counts.rejected} rejected
          {counts.failing > 0 && ` · ${counts.failing} failing their checks`}
          <span className="rsub"> · Publishing waits until no geometry is pending.</span>
        </div>
        <Form action="/admin/review/geometry" className="datafilters rvfilters" role="search">
          <select name="status" defaultValue={filters.status ?? ""} aria-label="Status" onChange={(e) => e.currentTarget.form?.requestSubmit()}>
            <option value="">Any status</option>
            {GEOMETRY_STATUSES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          <select name="origin" defaultValue={filters.origin ?? ""} aria-label="Origin" onChange={(e) => e.currentTarget.form?.requestSubmit()}>
            <option value="">Any origin</option>
            {ORIGINS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
          <select name="layer" defaultValue={filters.layer ?? ""} aria-label="Layer" onChange={(e) => e.currentTarget.form?.requestSubmit()}>
            <option value="">Any layer</option>
            {GEOMETRY_LAYERS.map((l) => (
              <option key={l.value} value={l.value}>
                {l.label}
              </option>
            ))}
          </select>
          {filters.document && <input type="hidden" name="document" value={filters.document} />}
          <label className="gvcheck">
            <input type="checkbox" name="history" value="1" defaultChecked={filters.history} onChange={(e) => e.currentTarget.form?.requestSubmit()} />{" "}
            include published
          </label>
          <button type="submit" className="abtn sm">
            Filter
          </button>
          {hasGeometryFilters(filters) && (
            <Link className="abtn sm ghost" href="/admin/review/geometry">
              Clear
            </Link>
          )}
        </Form>
      </div>

      {drafts.length === 0 ? (
        <div className="card">
          <div className="admin-note">{emptyGeometryText(filters)}</div>
        </div>
      ) : (
        <div className="rvsplit">
          <div className="card rvlist">
            <div className="rvlist-items" ref={listRef} role="listbox" aria-label="Staged geometry" aria-activedescendant={draft ? `gv-${draft.id}` : undefined}>
              {drafts.map((d, index) => {
                const chip = decisionChip(d);
                const qa = qaChip(d.qa_status);
                return (
                  <div
                    key={d.id}
                    id={`gv-${d.id}`}
                    data-index={index}
                    role="option"
                    aria-selected={index === cursor}
                    className={["review-item", index === cursor ? "sel" : "", isPending(d) ? "" : "done"].filter(Boolean).join(" ")}
                    onClick={() => moveTo(index)}
                  >
                    <div className="rv">
                      <div className="rq">{draftTitle(d)}</div>
                      <div className="rsrc">{draftSource(d)}</div>
                    </div>
                    <div className="rvright gvright">
                      <StatusChip tone={qa.tone}>{qa.label}</StatusChip>
                      {!isPending(d) && <StatusChip tone={chip.tone}>{chip.label}</StatusChip>}
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="rvkeys mono">j / k move · Enter approve · r reject · n next pending</div>
          </div>
          <div className="card rvitem">
            {draft && (
              <GeometryDetail
                draft={draft}
                rejecting={rejecting}
                onReject={setRejecting}
                onApprove={() => void approve()}
                onSubmitReject={reject}
                runPending={runPending}
                onBulk={() => void bulk()}
              />
            )}
          </div>
          <div className="card rvpdf gvpane">{draft && <GeometryPreview key={draft.id} batchId={draft.id} label={draftTitle(draft)} />}</div>
        </div>
      )}
    </div>
  );
}
