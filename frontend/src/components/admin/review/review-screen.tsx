"use client";

/**
 * The AI review queue (`/admin/review`, wireframe `adminReview`): the card "AI extraction — review
 * queue" with the pending count, the progress header (reviewed / total for the document in view,
 * Publish), the filters, and a split screen: the queue (left), the item (middle: value,
 * facts, decision) and the cited PDF page with the value's box (right).
 *
 * Built for hundreds of items per document, keyboard first: j / k (or ↓ / ↑) move, Enter
 * approves and moves on to the next pending item at once (the approval is sent in the background
 * and undone with a toast if the API refuses it), e amends, r rejects, n jumps to the next pending
 * item, Esc closes an editor. The queue keeps its own state (no page reload per decision); a
 * decision answers the updated item and the document's counters. Pending first by default, then
 * file / page / parcel; "page, then parcel" and "low confidence first" are the other orders.
 */
import Form from "next/form";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { PdfPageView, type PdfLink } from "@/components/source/pdf-page-view";
import {
  canDecide,
  countAfter,
  ENTITY_TYPES,
  emptyQueueText,
  formatValue,
  isLowConfidence,
  itemTitle,
  nextPending,
  REVIEW_STATUSES,
  SORTS,
  sourceLine,
  statusChip,
  type ReviewFilters,
} from "@/lib/admin/review";
import {
  amendAction,
  approveAction,
  itemAction,
  loadQueueAction,
  rejectAction,
  type Decision,
} from "@/lib/admin/review-actions";
import type { PublishStatus, ReviewCounters, ReviewItem, ReviewPage } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { StatusChip } from "../parts";

import { PublishPanel } from "./publish-panel";
import { ReviewDetail, type EditorMode, type Refusal } from "./review-detail";
import { ReviewTabs } from "./review-tabs";

const DocIcon = (
  <svg width="9" height="10" viewBox="0 0 10 11" fill="none" style={{ verticalAlign: "-1px", marginRight: 3 }} aria-hidden="true">
    <path d="M1 1h5l3 3v6H1z" stroke="currentColor" strokeWidth="1" strokeLinejoin="round" />
    <path d="M6 1v3h3" stroke="currentColor" strokeWidth="1" />
  </svg>
);

function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

export function ReviewScreen({
  initial,
  filters,
  counters: initialCounters,
  zones,
  publish,
  geometryPending,
  me,
  canPublish,
}: {
  initial: ReviewPage;
  filters: ReviewFilters;
  counters: ReviewCounters[];
  zones: { id: number; name: string }[];
  publish: PublishStatus | null;
  /** Staged geometry batches waiting for a decision (the sibling list), null when unknown. */
  geometryPending: number | null;
  me: string;
  canPublish: boolean;
}) {
  const showToast = useShell((s) => s.showToast);
  const [items, setItems] = useState<ReviewItem[]>(initial.items);
  const [total, setTotal] = useState(initial.total);
  const [cursor, setCursor] = useState(() => {
    const i = initial.items.findIndex((it) => it.status === "pending" && canDecide(it));
    return i >= 0 ? i : 0;
  });
  const [mode, setMode] = useState<EditorMode>(null);
  const [counters, setCounters] = useState<ReviewCounters[]>(initialCounters);
  const [loadingMore, setLoadingMore] = useState(false);
  const inflight = useRef(0);
  const listRef = useRef<HTMLDivElement>(null);
  const itemsRef = useRef(items);
  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  const item = items[cursor] ?? null;
  const documentId = filters.document ?? item?.source.document_id ?? null;
  const docCounters = counters.find((c) => c.document_id === documentId) ?? null;
  const pendingInScope = filters.document
    ? (docCounters?.pending ?? 0)
    : counters.reduce((sum, c) => sum + c.pending, 0);

  // keep the selected row in view
  useEffect(() => {
    const row = listRef.current?.querySelector<HTMLElement>(`[data-index="${cursor}"]`);
    row?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  const replace = useCallback((next: ReviewItem) => {
    setItems((list) => list.map((i) => (i.id === next.id ? next : i)));
  }, []);

  const applyCounters = useCallback((next: ReviewCounters | null) => {
    if (!next || inflight.current > 0) return; // optimistic counts stand while approvals are in flight
    setCounters((list) => {
      const found = list.some((c) => c.document_id === next.document_id);
      return found ? list.map((c) => (c.document_id === next.document_id ? next : c)) : [...list, next];
    });
  }, []);

  const moveTo = useCallback((index: number) => {
    setMode(null);
    setCursor(Math.max(0, Math.min(index, itemsRef.current.length - 1)));
  }, []);

  const advanceFrom = useCallback(
    (index: number, list: ReviewItem[]) => {
      const next = nextPending(list, index);
      moveTo(next ?? Math.min(index + 1, list.length - 1));
    },
    [moveTo],
  );

  const loadMore = useCallback(async () => {
    if (loadingMore || itemsRef.current.length >= total) return;
    setLoadingMore(true);
    const result = await loadQueueAction(filters, itemsRef.current.length);
    setLoadingMore(false);
    if (!result.ok) {
      showToast(result.message);
      return;
    }
    setTotal(result.data.total);
    setItems((list) => {
      const known = new Set(list.map((i) => i.id));
      return [...list, ...result.data.items.filter((i) => !known.has(i.id))];
    });
  }, [filters, loadingMore, showToast, total]);

  // approve: at once on screen, then the API (undone if it refuses)
  const approve = useCallback(() => {
    const list = itemsRef.current;
    const current = list[cursor];
    if (!current || !canDecide(current) || current.status === "approved") return;
    const optimistic: ReviewItem = {
      ...current,
      status: "approved",
      amended: null,
      effective: current.extracted,
      reviewed_by: me,
      reviewed_at: new Date().toISOString(),
      review_note: null,
    };
    const nextList = list.map((i) => (i.id === current.id ? optimistic : i));
    setItems(nextList);
    setCounters((cs) => cs.map((c) => (c.document_id === current.source.document_id ? countAfter(c, current.status, "approved") : c)));
    showToast("Approved");
    advanceFrom(cursor, nextList);
    inflight.current += 1;
    void approveAction(current.id)
      .then((result) => {
        inflight.current -= 1;
        if (result.ok) {
          replace(result.data.item);
          applyCounters(result.data.counters);
        } else {
          replace(current);
          setCounters((cs) => cs.map((c) => (c.document_id === current.source.document_id ? countAfter(c, "approved", current.status) : c)));
          showToast(`Not approved: ${result.message}`);
        }
      })
      .catch(() => {
        inflight.current -= 1;
        replace(current);
        showToast("Not approved: the data service did not answer.");
      });
    if (cursor >= list.length - 5) void loadMore();
  }, [advanceFrom, applyCounters, cursor, loadMore, me, replace, showToast]);

  const settle = useCallback(
    (result: { ok: true; message: string; data: Decision } | { ok: false; message: string; code?: string }): Refusal | null => {
      if (!result.ok) return { message: result.message, code: result.code };
      const list = itemsRef.current.map((i) => (i.id === result.data.item.id ? result.data.item : i));
      setItems(list);
      applyCounters(result.data.counters);
      showToast(result.message);
      setMode(null);
      advanceFrom(cursor, list);
      return null;
    },
    [advanceFrom, applyCounters, cursor, showToast],
  );

  const submit = useMemo(
    () => ({
      amend: async (correction: { value: number | string; unit: string | null; note: string; confirm?: boolean }) =>
        item ? settle(await amendAction(item.id, correction)) : null,
      reject: async (note: string) => (item ? settle(await rejectAction(item.id, note)) : null),
    }),
    [item, settle],
  );

  // the keyboard: j / k, Enter, e, r, n, Esc
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || typing(e.target)) return;
      if (document.querySelector(".overlay.on")) return; // a modal is open
      const key = e.key;
      if (key === "j" || key === "ArrowDown") {
        e.preventDefault();
        moveTo(cursor + 1);
        if (cursor >= itemsRef.current.length - 5) void loadMore();
      } else if (key === "k" || key === "ArrowUp") {
        e.preventDefault();
        moveTo(cursor - 1);
      } else if (key === "Enter" && mode === null) {
        // a focused button or link takes Enter itself (the browser clicks it)
        if ((e.target as HTMLElement | null)?.closest?.("button, a")) return;
        e.preventDefault();
        approve();
      } else if (key === "e" && item && canDecide(item)) {
        e.preventDefault();
        setMode("amend");
      } else if (key === "r" && item && canDecide(item)) {
        e.preventDefault();
        setMode("reject");
      } else if (key === "n") {
        e.preventDefault();
        const next = nextPending(itemsRef.current, cursor);
        if (next != null) moveTo(next);
      } else if (key === "Escape") {
        setMode(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [approve, cursor, item, loadMore, mode, moveTo]);

  // a fresh signed link for the item on screen (links expire)
  const itemId = item?.id ?? null;
  const refresh = useCallback(async (): Promise<PdfLink | null> => {
    if (itemId == null) return null;
    const result = await itemAction(itemId);
    return result.ok && result.data.source.link ? { url: result.data.source.link.url, kind: result.data.source.link.kind } : null;
  }, [itemId]);
  const link = item?.source.link ?? null;
  const pdfLink = useMemo<PdfLink | null>(() => (link ? { url: link.url, kind: link.kind } : null), [link]);

  const documents = useMemo(() => [...counters].sort((a, b) => a.document_name.localeCompare(b.document_name)), [counters]);

  return (
    <div className="rvscreen">
      <div className="card rvcard">
        <div className="cardhd">
          <div>
            <h3>AI extraction — review queue</h3>
            <div className="sub">100% of extracted values need expert approval before they publish</div>
          </div>
          <span className={pendingInScope ? "st pend" : "st ok"}>{pendingInScope} pending</span>
        </div>
        <ReviewTabs
          active="values"
          valuesPending={counters.reduce((sum, c) => sum + c.pending, 0)}
          geometryPending={geometryPending}
        />
        <PublishPanel counters={docCounters} canPublish={canPublish} initialStatus={publish} />
        <Form action="/admin/review" className="datafilters rvfilters" role="search">
          <select
            name="document"
            defaultValue={filters.document ?? ""}
            aria-label="Document"
            onChange={(e) => e.currentTarget.form?.requestSubmit()}
          >
            <option value="">All documents</option>
            {documents.map((d) => (
              <option key={d.document_id} value={d.document_id}>
                {d.document_name} — {d.pending} pending
              </option>
            ))}
          </select>
          <select name="status" defaultValue={filters.status ?? ""} aria-label="Status" onChange={(e) => e.currentTarget.form?.requestSubmit()}>
            <option value="">Any status</option>
            {REVIEW_STATUSES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          <select name="zone" defaultValue={filters.zone ?? ""} aria-label="Zone" onChange={(e) => e.currentTarget.form?.requestSubmit()}>
            <option value="">All zones</option>
            {zones.map((z) => (
              <option key={z.id} value={z.id}>
                {z.name}
              </option>
            ))}
          </select>
          <select name="entity" defaultValue={filters.entity ?? ""} aria-label="Entity" onChange={(e) => e.currentTarget.form?.requestSubmit()}>
            <option value="">Any target</option>
            {ENTITY_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
          <input name="page" defaultValue={filters.page ?? ""} inputMode="numeric" placeholder="Page" aria-label="Page" className="rvpage" />
          <select name="sort" defaultValue={filters.sort} aria-label="Order" onChange={(e) => e.currentTarget.form?.requestSubmit()}>
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
          {filters.file && <input type="hidden" name="file" value={filters.file} />}
          <button type="submit" className="abtn sm">
            Filter
          </button>
          {(filters.document || filters.status || filters.zone || filters.entity || filters.page || filters.file || filters.sort !== "pending") && (
            <Link className="abtn sm ghost" href="/admin/review">
              Clear
            </Link>
          )}
          {filters.file && <span className="rsub">one file of the document</span>}
        </Form>
      </div>

      {items.length === 0 ? (
        <div className="card">
          <div className="admin-note">{emptyQueueText(filters)}</div>
        </div>
      ) : (
        <div className="rvsplit">
          <div className="card rvlist">
            <div className="rvlist-items" ref={listRef} role="listbox" aria-label="Extracted values" aria-activedescendant={item ? `rv-${item.id}` : undefined}>
              {items.map((it, index) => {
                const chip = statusChip(it);
                return (
                  <div
                    key={it.id}
                    id={`rv-${it.id}`}
                    data-index={index}
                    role="option"
                    aria-selected={index === cursor}
                    className={["review-item", index === cursor ? "sel" : "", it.status !== "pending" ? "done" : ""].filter(Boolean).join(" ")}
                    onClick={() => moveTo(index)}
                  >
                    <div className="rv">
                      <div className="rq">
                        {isLowConfidence(it) && (
                          <span className="rflag" title="Low confidence">
                            ⚑{" "}
                          </span>
                        )}
                        {itemTitle(it)}
                      </div>
                      <div className="rsrc">
                        {DocIcon}
                        {sourceLine(it)}
                      </div>
                    </div>
                    <div className="rvright">
                      <span className={it.status === "rejected" ? "rextract rrejected" : "rextract"}>
                        {formatValue(it.status === "amended" && it.amended ? it.amended : it.extracted, it.value_type)}
                      </span>
                      {it.status !== "pending" && <StatusChip tone={chip.tone}>{chip.label}</StatusChip>}
                    </div>
                  </div>
                );
              })}
              {items.length < total && (
                <button type="button" className="abtn sm ghost rvmore" disabled={loadingMore} onClick={() => void loadMore()}>
                  {loadingMore ? "Loading…" : `Load more (${items.length} of ${total})`}
                </button>
              )}
            </div>
            <div className="rvkeys mono">j / k move · Enter approve · e amend · r reject · n next pending</div>
          </div>
          <div className="card rvitem">
            {item ? (
              <ReviewDetail
                item={item}
                mode={mode}
                onMode={setMode}
                onApprove={approve}
                submit={submit}
              />
            ) : null}
          </div>
          <div className="card rvpdf">
            {item && (
              <PdfPageView
                link={pdfLink}
                page={item.source.page ?? null}
                bbox={item.source.bbox ?? null}
                label={`${item.source.document_name}, page ${item.source.page ?? "—"}`}
                refresh={refresh}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
