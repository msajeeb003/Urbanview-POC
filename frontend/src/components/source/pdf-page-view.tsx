"use client";

/**
 * One cited page of a stored PDF with the cited value's box on it, for panes that already hold a
 * signed link (the admin review queue: the item's `source.link` from the staff API). The toolbar
 * and page styles are the source viewer's (`.srctools`, `.srcpage`, `.srcsheet`, `.srchl`).
 *
 * - Opens at the cited page and draws the bbox (PDF points, origin bottom-left) as a translucent
 *   brand rectangle, scrolled into view; other pages have no box. A new citation (another item)
 *   goes back to its page; the zoom stays as the reviewer set it.
 * - Documents come from `lib/pdf.ts`: range requests, one load per file, so stepping through the
 *   values of one plan never downloads it again.
 * - Previous / next, the page number, zoom − / + / fit, "Open PDF" (a fresh link from `refresh`);
 *   a link that expired is signed again through `refresh`, once, silently.
 * - Page images (`kind: page_image`) are shown as the cited page only, without a box.
 * - Loading: the page-shaped skeleton; failure: "This page could not be loaded" and Retry, never red.
 */
import type { RenderTask } from "pdfjs-dist";
import { useCallback, useEffect, useRef, useState } from "react";

import { forgetDocument, highlightBox, openDocument } from "@/lib/pdf";

export interface PdfLink {
  url: string;
  kind: "pdf_page" | "page_image";
}

const ZOOM_MIN = 0.5;
const ZOOM_MAX = 4;

export function PdfPageView({
  link,
  page,
  bbox,
  label,
  refresh,
}: {
  link: PdfLink | null;
  /** The cited page, 1-based. */
  page: number | null;
  /** The cited box on that page (PDF points, origin bottom-left). */
  bbox?: readonly number[] | null;
  /** Accessible name of the page, e.g. "DUP Novi Grad, page 4". */
  label: string;
  /** A freshly signed link (expired links, "Open PDF"). */
  refresh?: () => Promise<PdfLink | null>;
}) {
  const [current, setCurrent] = useState<PdfLink | null>(link);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [attempt, setAttempt] = useState(0);
  const [pageNo, setPageNo] = useState<number>(page ?? 1);
  const [pageInput, setPageInput] = useState(String(page ?? 1));
  const [numPages, setNumPages] = useState<number | null>(null);
  const [zoom, setZoom] = useState<number | null>(null); // null = fit to width
  const [shownScale, setShownScale] = useState(1);
  const [width, setWidth] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const highlightRef = useRef<HTMLDivElement>(null);
  // the link a fresh signature was already asked for (once per citation)
  const refreshedRef = useRef<string | null>(null);
  const refreshRef = useRef(refresh);
  useEffect(() => {
    refreshRef.current = refresh;
  });

  // a new citation: its link and its page (state adjusted while rendering, React's pattern for
  // state that follows a prop)
  const [cited, setCited] = useState({ link, page });
  if (cited.link !== link || cited.page !== page) {
    setCited({ link, page });
    setCurrent(link);
    setPageNo(page ?? 1);
    setPageInput(String(page ?? 1));
  }

  // the pane's width (fit-to-width follows it)
  useEffect(() => {
    const box = scrollRef.current;
    if (!box) return;
    const observer = new ResizeObserver(() => setWidth(box.clientWidth));
    observer.observe(box);
    setWidth(box.clientWidth);
    return () => observer.disconnect();
  }, []);

  // render the page (the document from the cache), then place the box
  useEffect(() => {
    if (!current || current.kind === "page_image") return;
    const canvas = canvasRef.current;
    const box = scrollRef.current;
    if (!canvas || !box || !width) return;
    let task: RenderTask | null = null;
    let cancelled = false;
    (async () => {
      try {
        let doc;
        let pdfPage;
        let target = pageNo;
        try {
          doc = await openDocument(current.url);
          if (cancelled) return;
          target = Math.min(Math.max(1, pageNo), doc.numPages);
          pdfPage = await doc.getPage(target);
        } catch (error) {
          // a signature that expired (on open, or on the range request of another page): sign
          // a fresh link and open the document again, once
          const sign = refreshRef.current;
          const origin = link?.url ?? current.url;
          if (!sign || refreshedRef.current === origin || cancelled) throw error;
          refreshedRef.current = origin;
          forgetDocument(current.url);
          const fresh = await sign();
          if (!fresh || cancelled) throw error;
          setCurrent(fresh);
          return;
        }
        if (cancelled) return;
        setNumPages(doc.numPages);
        const base = pdfPage.getViewport({ scale: 1 });
        const fit = Math.max(ZOOM_MIN, (width - 32) / base.width);
        const scale = zoom ?? fit;
        const dpr = Math.min(window.devicePixelRatio || 1, 3);
        const css = pdfPage.getViewport({ scale });
        const render = pdfPage.getViewport({ scale: scale * dpr });
        canvas.width = Math.floor(render.width);
        canvas.height = Math.floor(render.height);
        canvas.style.width = `${Math.floor(css.width)}px`;
        canvas.style.height = `${Math.floor(css.height)}px`;
        setShownScale(scale);
        task = pdfPage.render({ canvas, viewport: render });
        await task.promise;
        if (cancelled) return;
        setState("ready");
        const highlight = highlightRef.current;
        if (!highlight) return;
        const rect = target === page && bbox ? highlightBox(css, bbox) : null;
        if (rect) {
          highlight.style.left = `${rect.left}px`;
          highlight.style.top = `${rect.top}px`;
          highlight.style.width = `${rect.width}px`;
          highlight.style.height = `${rect.height}px`;
          highlight.hidden = false;
          highlight.scrollIntoView({ block: "center", inline: "nearest" });
        } else {
          highlight.hidden = true;
          box.scrollTop = 0;
        }
      } catch (error) {
        if (!cancelled && (error as { name?: string })?.name !== "RenderingCancelledException") setState("failed");
      }
    })();
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [current, pageNo, zoom, width, page, bbox, attempt, link]);

  const total = current?.kind === "page_image" ? 1 : numPages;
  const goTo = useCallback(
    (n: number) => {
      if (n < 1 || (total != null && n > total)) return;
      setPageNo(n);
      setPageInput(String(n));
    },
    [total],
  );
  const zoomBy = (factor: number) => setZoom(Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, (zoom ?? shownScale) * factor)));

  const openPdf = async () => {
    const tab = window.open("about:blank", "_blank");
    if (tab) tab.opener = null;
    try {
      const sign = refreshRef.current;
      const fresh = (sign ? await sign() : null) ?? current;
      if (!fresh) throw new Error("no link");
      if (tab) tab.location.href = `${fresh.url.split("#")[0]}#page=${pageNo}`;
    } catch {
      tab?.close();
    }
  };

  const image = current?.kind === "page_image";
  const view = !current ? "none" : image ? "ready" : state;
  return (
    <div className="srcviewer pdfview">
      <div className="srctools" role="toolbar" aria-label="Page controls">
        <button type="button" className="srcbtn" aria-label="Previous page" disabled={image || pageNo <= 1} onClick={() => goTo(pageNo - 1)}>
          ‹
        </button>
        <label className="srcpageno">
          <span className="sr-only">Page number</span>
          <input
            inputMode="numeric"
            value={pageInput}
            disabled={image || !current}
            onChange={(e) => setPageInput(e.target.value.replace(/[^0-9]/g, ""))}
            onBlur={() => goTo(Number(pageInput) || pageNo)}
            onKeyDown={(e) => {
              if (e.key === "Enter") goTo(Number(pageInput) || pageNo);
              e.stopPropagation(); // typing a page number is not a queue shortcut
            }}
          />
          <span>of {total ?? "—"}</span>
        </label>
        <button
          type="button"
          className="srcbtn"
          aria-label="Next page"
          disabled={image || (total != null && pageNo >= total)}
          onClick={() => goTo(pageNo + 1)}
        >
          ›
        </button>
        {page != null && pageNo !== page && (
          <button type="button" className="srcbtn srccited" onClick={() => goTo(page)}>
            Cited p.{page}
          </button>
        )}
        <span className="srcspace" />
        <button type="button" className="srcbtn" aria-label="Zoom out" disabled={image || !current} onClick={() => zoomBy(0.8)}>
          −
        </button>
        <button
          type="button"
          className="srczoom mono srcfit"
          title="Fit to width"
          disabled={image || !current}
          onClick={() => setZoom(null)}
        >
          {Math.round(shownScale * 100)}%
        </button>
        <button type="button" className="srcbtn" aria-label="Zoom in" disabled={image || !current} onClick={() => zoomBy(1.25)}>
          +
        </button>
        <button type="button" className="srcbtn srcopen" disabled={!current} onClick={() => void openPdf()}>
          Open PDF ↗
        </button>
      </div>
      <div className="srcpage" ref={scrollRef} aria-label={label}>
        {view === "none" ? (
          <div className="srcfail">
            <p>The document&apos;s PDF is not stored, so the cited page cannot be shown.</p>
          </div>
        ) : view === "failed" ? (
          <div className="srcfail">
            <p>This page could not be loaded.</p>
            <button
              type="button"
              className="cta ghost"
              style={{ width: "auto", padding: "0 16px", height: 38 }}
              onClick={() => {
                if (current) forgetDocument(current.url);
                refreshedRef.current = null;
                setState("loading");
                setAttempt((a) => a + 1);
              }}
            >
              Retry
            </button>
          </div>
        ) : (
          <>
            {view === "loading" && <div className="srcskel" aria-busy="true" aria-label="Loading the page" />}
            {image && current ? (
              // eslint-disable-next-line @next/next/no-img-element -- a signed, short-lived image URL
              <img className="srcimg" src={current.url} alt={label} />
            ) : (
              <div className="srcsheet" hidden={view !== "ready"}>
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
