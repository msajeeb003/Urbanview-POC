"use client";

/**
 * The source viewer: the cited page of a planning document, in the app, with the cited value
 * highlighted, for the public panel (every source chip and row source icon, the document lists).
 * The admin review queue shows cited pages with `PdfPageView` on the same PDF.js loader
 * (`lib/pdf.ts`).
 *
 * - Asks the API for one short-lived signed link: `GET /v1/source/value/{value_id}` (adds the
 *   value's bbox, note and label) or `GET /v1/source/{document_id}/page/{page}`; object keys never
 *   reach the browser. `source_reference_opened { document_id, page, value_id }` once per open.
 * - PDF.js (legacy build: older mobile Safari too) is imported lazily on the first open, so it never
 *   touches the map's first load; the worker comes from `/pdfjs/` (copied at build time). Range
 *   requests with auto-fetch off: only the bytes of the requested page are loaded.
 * - Opens at the cited page; the bbox (PDF points, origin bottom-left) becomes a translucent brand
 *   rectangle over the canvas (two corners through `viewport.convertToViewportPoint`), scrolled
 *   into view.
 * - Previous / next, a page number input, zoom − / +, "Open PDF" (a fresh signed link in a new
 *   tab); ← / → turn pages. A page the document does not have is refused with a note beside the
 *   page number ("This document has 24 pages."). A signed link that expired (403) is fetched again
 *   once, silently.
 * - Loading: a page-shaped skeleton. Failure says why, never in red (`sourceFailure`): the PDF is
 *   not stored yet (with the eRegistri entry), the cited page is not in the document, the value is
 *   no longer published, or connection trouble (only that one offers Retry). The header keeps the
 *   document and page the opener named (`target.hint`).
 */
import type { PDFDocumentLoadingTask, PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { useTrack } from "@/lib/analytics/react";
import { api } from "@/lib/api/endpoints";
import type { SourcePage } from "@/lib/api/types";
import { loadPdfJs } from "@/lib/pdf";
import { pageRangeNote, sourceFailure, type SourceFailure } from "@/lib/source-text";

import { DocStatusChip } from "../panel/panel-parts";
import { IconDoc } from "../ui/icons";
import { ModalHead } from "../ui/modal";

import type { SourceTarget } from "@/lib/source";

const ZOOM_MIN = 0.5;
const ZOOM_MAX = 4;

function fetchSource(target: SourceTarget, page?: number, signal?: AbortSignal): Promise<SourcePage> {
  if ("valueId" in target && page === undefined) return api.sourceValue(target.valueId, { signal });
  const documentId = "valueId" in target ? undefined : target.documentId;
  if (documentId === undefined) throw new Error("page fetch needs the document id");
  return api.sourcePage(documentId, page ?? (target as { page: number }).page, { signal });
}

function isForbidden(error: unknown): boolean {
  const status = (error as { status?: number } | null)?.status;
  return status === 403;
}

export function SourceViewer({ target }: { target: SourceTarget }) {
  const track = useTrack();
  const [meta, setMeta] = useState<SourcePage | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [failure, setFailure] = useState<SourceFailure | null>(null);
  const [pageNote, setPageNote] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [pageNo, setPageNo] = useState<number | null>(null);
  const [pageInput, setPageInput] = useState("");
  const [numPages, setNumPages] = useState<number | null>(null);
  const [zoom, setZoom] = useState<number | null>(null); // null = fit to width
  const [shownScale, setShownScale] = useState(1);
  const docRef = useRef<PDFDocumentProxy | null>(null);
  const loadingRef = useRef<PDFDocumentLoadingTask | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const highlightRef = useRef<HTMLDivElement>(null);
  const trackedRef = useRef(false);
  const refreshedRef = useRef(false);

  // 1. the signed link and the citation, then the document
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    (async () => {
      try {
        let source = await fetchSource(target, undefined, controller.signal);
        if (cancelled) return;
        setMeta(source);
        setPageNo(source.page);
        setPageInput(String(source.page));
        if (!trackedRef.current) {
          trackedRef.current = true;
          track("source_reference_opened", {
            document_id: source.document_id,
            page: source.page,
            ...(source.value ? { value_id: source.value.value_id } : {}),
          });
        }
        const pdfjs = await loadPdfJs();
        const open = (url: string) => {
          void loadingRef.current?.destroy();
          loadingRef.current = pdfjs.getDocument({
            url: url.split("#")[0],
            disableAutoFetch: true,
            disableStream: true,
            rangeChunkSize: 65536,
          });
          return loadingRef.current.promise;
        };
        let doc: PDFDocumentProxy;
        try {
          doc = await open(source.url);
        } catch (error) {
          // a link that expired in the meantime: sign a fresh one, once
          if (!isForbidden(error) || refreshedRef.current) throw error;
          refreshedRef.current = true;
          source = await fetchSource(target, undefined, controller.signal);
          setMeta(source);
          doc = await open(source.url);
        }
        if (cancelled) return;
        docRef.current = doc;
        setNumPages(doc.numPages);
        setState("ready");
      } catch (error) {
        if (!cancelled) {
          setFailure(sourceFailure(error, target.hint));
          setState("failed");
        }
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
      docRef.current = null;
      void loadingRef.current?.destroy();
      loadingRef.current = null;
    };
  }, [target, attempt, track]);

  // 2. render the current page at the current zoom, then place the highlight
  useEffect(() => {
    const doc = docRef.current;
    const canvas = canvasRef.current;
    const box = scrollRef.current;
    if (state !== "ready" || !doc || !canvas || !box || pageNo == null || !meta) return;
    let task: RenderTask | null = null;
    let cancelled = false;
    (async () => {
      try {
        const page = await doc.getPage(pageNo);
        if (cancelled) return;
        const base = page.getViewport({ scale: 1 });
        const fit = Math.max(ZOOM_MIN, (box.clientWidth - 32) / base.width);
        const scale = zoom ?? fit;
        const dpr = typeof window !== "undefined" ? Math.min(window.devicePixelRatio || 1, 3) : 1;
        const css = page.getViewport({ scale });
        const render = page.getViewport({ scale: scale * dpr });
        canvas.width = Math.floor(render.width);
        canvas.height = Math.floor(render.height);
        canvas.style.width = `${Math.floor(css.width)}px`;
        canvas.style.height = `${Math.floor(css.height)}px`;
        setShownScale(scale);
        // The frame goes on before the page is drawn: its place is known from the page's size
        // alone, and a heavy plan page can take a while to draw (the frame used to wait for
        // that, so the value looked unmarked meanwhile).
        const highlight = highlightRef.current;
        const bbox = pageNo === meta.page ? meta.value?.bbox : null;
        if (highlight) {
          if (bbox && bbox.length === 4) {
            const [x1, y1] = css.convertToViewportPoint(bbox[0], bbox[1]) as number[];
            const [x2, y2] = css.convertToViewportPoint(bbox[2], bbox[3]) as number[];
            const pad = 3;
            highlight.style.left = `${Math.min(x1, x2) - pad}px`;
            highlight.style.top = `${Math.min(y1, y2) - pad}px`;
            highlight.style.width = `${Math.abs(x2 - x1) + pad * 2}px`;
            highlight.style.height = `${Math.abs(y2 - y1) + pad * 2}px`;
            highlight.hidden = false;
            highlight.scrollIntoView({ block: "center", inline: "nearest" });
          } else {
            highlight.hidden = true;
          }
        }
        task = page.render({ canvas, viewport: render });
        await task.promise;
      } catch (error) {
        if (!cancelled && (error as { name?: string })?.name !== "RenderingCancelledException") {
          setFailure(sourceFailure(error));
          setState("failed");
        }
      }
    })();
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [state, pageNo, zoom, meta]);

  const total = numPages ?? meta?.page_count ?? null;
  const goTo = (n: number) => {
    if (n < 1 || (total != null && n > total)) {
      // a page the document does not have: keep the page on screen and say why
      setPageInput(String(pageNo ?? meta?.page ?? ""));
      if (total != null) setPageNote(pageRangeNote(total));
      return;
    }
    setPageNote(null);
    setPageNo(n);
    setPageInput(String(n));
  };
  const usable = meta != null && state !== "failed";
  const zoomBy = (factor: number) => setZoom(Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, (zoom ?? shownScale) * factor)));

  const openPdf = async () => {
    if (!meta) return;
    const tab = window.open("about:blank", "_blank");
    if (tab) tab.opener = null;
    try {
      // a fresh link unless the current one has more than a minute left
      const fresh = Date.parse(meta.expires_at) - Date.now() > 60_000 ? meta : await fetchSource(target, pageNo ?? undefined);
      const url = `${fresh.url.split("#")[0]}#page=${pageNo ?? fresh.page}`;
      // a blocked pop-up leaves nothing to point at; the viewer stays as it is
      if (tab) tab.location.href = url;
    } catch {
      tab?.close();
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).tagName === "INPUT") return;
    if (e.key === "ArrowLeft" && pageNo != null) goTo(pageNo - 1);
    if (e.key === "ArrowRight" && pageNo != null) goTo(pageNo + 1);
  };

  const value = meta?.value;
  const hint = target.hint;
  const valueText =
    value && value.value != null
      ? `${value.label_en}: ${value.value}${value.unit ? ` ${value.unit}` : ""}`
      : meta
        ? null
        : (hint?.label ?? null);
  const note = value?.note ?? (meta ? null : hint?.note);

  return (
    <div className="srcviewer" onKeyDown={onKeyDown}>
      <ModalHead
        icon={<IconDoc />}
        iconStyle={{ background: "var(--brand-tint)", color: "var(--brand)" }}
        eyebrow="Source document"
        title={meta?.document_name ?? hint?.documentName ?? "Source document"}
        lead={
          <span className="srclead">
            <span className="mono">p.{pageNo ?? meta?.page ?? failure?.page ?? hint?.page ?? "—"}</span>
            {meta && <DocStatusChip status={meta.document_status} />}
            {valueText && <span>{valueText}</span>}
            {note && <span className="srcnote">{note}</span>}
          </span>
        }
      />
      <div className="srctools" role="toolbar" aria-label="Page controls">
        <button
          type="button"
          className="srcbtn"
          aria-label="Previous page"
          disabled={!usable || !pageNo || pageNo <= 1}
          onClick={() => pageNo && goTo(pageNo - 1)}
        >
          ‹
        </button>
        <label className="srcpageno">
          <span className="sr-only">Page number</span>
          <input
            inputMode="numeric"
            value={pageInput}
            disabled={!usable}
            onChange={(e) => setPageInput(e.target.value.replace(/[^0-9]/g, ""))}
            onBlur={() => goTo(Number(pageInput) || pageNo || 1)}
            onKeyDown={(e) => {
              if (e.key === "Enter") goTo(Number(pageInput) || pageNo || 1);
            }}
          />
          <span>of {total ?? "—"}</span>
        </label>
        <button
          type="button"
          className="srcbtn"
          aria-label="Next page"
          disabled={!usable || !pageNo || (total != null && pageNo >= total)}
          onClick={() => pageNo && goTo(pageNo + 1)}
        >
          ›
        </button>
        <span className="srcpagenote" role="status">
          {pageNote}
        </span>
        <span className="srcspace" />
        <button type="button" className="srcbtn" aria-label="Zoom out" disabled={!usable} onClick={() => zoomBy(0.8)}>
          −
        </button>
        <span className="srczoom mono">{Math.round(shownScale * 100)}%</span>
        <button type="button" className="srcbtn" aria-label="Zoom in" disabled={!usable} onClick={() => zoomBy(1.25)}>
          +
        </button>
        <button type="button" className="srcbtn srcopen" disabled={!usable} onClick={() => void openPdf()}>
          Open PDF ↗
        </button>
      </div>
      <div className="srcpage" ref={scrollRef}>
        {state === "failed" ? (
          <div className="srcfail">
            <p>{failure?.message ?? "This page could not be loaded."}</p>
            {failure?.registryUrl && (
              <a className="srclink" href={failure.registryUrl} target="_blank" rel="noopener noreferrer">
                See the document in eRegistri ↗
              </a>
            )}
            {(failure?.retry ?? true) && (
              <button
                type="button"
                className="cta ghost"
                style={{ width: "auto", padding: "0 16px", height: 38 }}
                onClick={() => {
                  refreshedRef.current = false;
                  setFailure(null);
                  setState("loading");
                  setAttempt((a) => a + 1);
                }}
              >
                Retry
              </button>
            )}
          </div>
        ) : (
          <>
            {state === "loading" && <div className="srcskel" aria-busy="true" aria-label="Loading the page" />}
            <div className="srcsheet" hidden={state !== "ready"}>
              <canvas ref={canvasRef} />
              <div className="srchl" ref={highlightRef} hidden aria-hidden />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
