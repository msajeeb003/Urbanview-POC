"use client";

/**
 * The source viewer: the cited page of a planning document, in the app, with the cited value
 * highlighted. One component for the public panel (every source chip and row source icon, the
 * document lists) and, later, the admin review queue.
 *
 * - Asks the API for one short-lived signed link: `GET /v1/source/value/{value_id}` (adds the
 *   value's bbox, note and label) or `GET /v1/source/{document_id}/page/{page}`; object keys never
 *   reach the browser. `source_reference_opened { document_id, page, value_id }` once per open.
 * - PDF.js (legacy build: older mobile Safari too) is imported lazily on the first open, so it never
 *   touches the map's first load; the worker comes from `/pdfjs/` (copied at build time). Range
 *   requests with auto-fetch off: only the bytes of the requested page are loaded.
 * - Opens at the cited page; the bbox (PDF points, origin bottom-left) becomes a translucent brand
 *   rectangle over the canvas (two corners through `viewport.convertToViewportPoint`), scrolled
 *   into view. Pages rendered as images (`kind: page_image`) are shown as images, without a
 *   rectangle (their pixel size says nothing about the page's points).
 * - Previous / next, a page number input, zoom − / +, "Open PDF" (a fresh signed link in a new
 *   tab); ← / → turn pages. A signed link that expired (403) is fetched again once, silently.
 * - Loading: a page-shaped skeleton. Failure: "This page could not be loaded" and Retry, never a
 *   red error.
 */
import type { PDFDocumentLoadingTask, PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";

import { useTrack } from "@/lib/analytics/react";
import { api } from "@/lib/api/endpoints";
import type { SourcePage } from "@/lib/api/types";

import { DocStatusChip } from "../panel/panel-parts";
import { IconDoc } from "../ui/icons";
import { ModalHead } from "../ui/modal";

import type { SourceTarget } from "@/lib/source";

type PdfJs = typeof import("pdfjs-dist");

let pdfjsPromise: Promise<PdfJs> | null = null;

/** PDF.js on first use only (its own chunk), with the worker copied to /pdfjs/ at build time. */
function loadPdfJs(): Promise<PdfJs> {
  pdfjsPromise ??= import("pdfjs-dist/legacy/build/pdf.mjs").then((mod) => {
    const pdfjs = mod as unknown as PdfJs;
    pdfjs.GlobalWorkerOptions.workerSrc = "/pdfjs/pdf.worker.min.mjs";
    return pdfjs;
  });
  return pdfjsPromise;
}

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
  const [attempt, setAttempt] = useState(0);
  const [pageNo, setPageNo] = useState<number | null>(null);
  const [pageInput, setPageInput] = useState("");
  const [numPages, setNumPages] = useState<number | null>(null);
  const [zoom, setZoom] = useState<number | null>(null); // null = fit to width
  const [shownScale, setShownScale] = useState(1);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
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
        if (source.kind === "page_image") {
          setImageUrl(source.url);
          setNumPages(source.page_count);
          setState("ready");
          return;
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
      } catch {
        if (!cancelled) setState("failed");
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
        task = page.render({ canvas, viewport: render });
        await task.promise;
        if (cancelled) return;
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
      } catch (error) {
        if (!cancelled && (error as { name?: string })?.name !== "RenderingCancelledException") setState("failed");
      }
    })();
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [state, pageNo, zoom, meta]);

  // page images: another page is another signed image
  const goToImagePage = useCallback(
    async (n: number) => {
      if (!meta) return;
      setState("loading");
      try {
        const next = await api.sourcePage(meta.document_id, n);
        setImageUrl(next.url);
        setState("ready");
      } catch {
        setState("failed");
      }
    },
    [meta],
  );

  const total = numPages ?? meta?.page_count ?? null;
  const goTo = (n: number) => {
    if (total != null && (n < 1 || n > total)) return;
    if (n < 1) return;
    setPageNo(n);
    setPageInput(String(n));
    if (meta?.kind === "page_image") void goToImagePage(n);
  };
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
  const valueText =
    value && value.value != null ? `${value.label_en}: ${value.value}${value.unit ? ` ${value.unit}` : ""}` : null;

  return (
    <div className="srcviewer" onKeyDown={onKeyDown}>
      <ModalHead
        icon={<IconDoc />}
        iconStyle={{ background: "var(--brand-tint)", color: "var(--brand)" }}
        eyebrow="Source document"
        title={meta?.document_name ?? "Source document"}
        lead={
          <span className="srclead">
            <span className="mono">p.{pageNo ?? meta?.page ?? "—"}</span>
            {meta && <DocStatusChip status={meta.document_status} />}
            {valueText && <span>{valueText}</span>}
            {value?.note && <span className="srcnote">{value.note}</span>}
          </span>
        }
      />
      <div className="srctools" role="toolbar" aria-label="Page controls">
        <button type="button" className="srcbtn" aria-label="Previous page" disabled={!pageNo || pageNo <= 1} onClick={() => pageNo && goTo(pageNo - 1)}>
          ‹
        </button>
        <label className="srcpageno">
          <span className="sr-only">Page number</span>
          <input
            inputMode="numeric"
            value={pageInput}
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
          disabled={!pageNo || (total != null && pageNo >= total)}
          onClick={() => pageNo && goTo(pageNo + 1)}
        >
          ›
        </button>
        <span className="srcspace" />
        <button type="button" className="srcbtn" aria-label="Zoom out" disabled={meta?.kind === "page_image"} onClick={() => zoomBy(0.8)}>
          −
        </button>
        <span className="srczoom mono">{Math.round(shownScale * 100)}%</span>
        <button type="button" className="srcbtn" aria-label="Zoom in" disabled={meta?.kind === "page_image"} onClick={() => zoomBy(1.25)}>
          +
        </button>
        <button type="button" className="srcbtn srcopen" disabled={!meta} onClick={() => void openPdf()}>
          Open PDF ↗
        </button>
      </div>
      <div className="srcpage" ref={scrollRef}>
        {state === "failed" ? (
          <div className="srcfail">
            <p>This page could not be loaded.</p>
            <button
              type="button"
              className="cta ghost"
              style={{ width: "auto", padding: "0 16px", height: 38 }}
              onClick={() => {
                refreshedRef.current = false;
                setState("loading");
                setAttempt((a) => a + 1);
              }}
            >
              Retry
            </button>
          </div>
        ) : (
          <>
            {state === "loading" && <div className="srcskel" aria-busy="true" aria-label="Loading the page" />}
            {meta?.kind === "page_image" && imageUrl ? (
              // eslint-disable-next-line @next/next/no-img-element -- a signed, short-lived image URL
              <img className="srcimg" src={imageUrl} alt={`${meta.document_name}, page ${pageNo}`} hidden={state !== "ready"} />
            ) : (
              <div className="srcsheet" hidden={state !== "ready"}>
                <canvas ref={canvasRef} />
                <div className="srchl" ref={highlightRef} hidden aria-hidden />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
