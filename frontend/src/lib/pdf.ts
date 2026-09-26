/**
 * PDF.js for the source viewer and the admin review queue: loaded lazily (its own chunk, never on
 * the map's first load), the worker from `/pdfjs/` (copied at build time), documents opened with
 * range requests (auto-fetch off: only the bytes of the pages asked for) and kept in a small cache
 * keyed by the object's path, so moving between values cited from the same file does not load it
 * again (the signed query string changes with every link; the path is the object).
 */
import type { PDFDocumentLoadingTask, PDFDocumentProxy, PageViewport } from "pdfjs-dist";

type PdfJs = typeof import("pdfjs-dist");

let pdfjsPromise: Promise<PdfJs> | null = null;

/** PDF.js on first use only (its own chunk), with the worker copied to /pdfjs/ at build time. */
export function loadPdfJs(): Promise<PdfJs> {
  pdfjsPromise ??= import("pdfjs-dist/legacy/build/pdf.mjs").then((mod) => {
    const pdfjs = mod as unknown as PdfJs;
    pdfjs.GlobalWorkerOptions.workerSrc = "/pdfjs/pdf.worker.min.mjs";
    return pdfjs;
  });
  return pdfjsPromise;
}

/** The object a signed link points at: origin + path, without the signature or the #page anchor. */
export function objectPath(url: string): string {
  try {
    const u = new URL(url);
    return `${u.origin}${u.pathname}`;
  } catch {
    return url.split(/[?#]/, 1)[0];
  }
}

const CACHE_SIZE = 6;

interface Entry {
  promise: Promise<PDFDocumentProxy>;
  destroy: () => void;
}

const documents = new Map<string, Entry>();

/** The document behind a signed PDF link, loaded once per object (range requests). */
export function openDocument(url: string): Promise<PDFDocumentProxy> {
  const key = objectPath(url);
  const cached = documents.get(key);
  if (cached) {
    documents.delete(key); // most recently used last
    documents.set(key, cached);
    return cached.promise;
  }
  let task: PDFDocumentLoadingTask | null = null;
  const promise = loadPdfJs().then((pdfjs) => {
    task = pdfjs.getDocument({
      url: url.split("#")[0],
      disableAutoFetch: true,
      disableStream: true,
      rangeChunkSize: 65536,
    });
    return task.promise;
  });
  const entry: Entry = { promise, destroy: () => void (task as PDFDocumentLoadingTask | null)?.destroy() };
  documents.set(key, entry);
  promise.catch(() => {
    if (documents.get(key) === entry) documents.delete(key);
  });
  while (documents.size > CACHE_SIZE) {
    const oldest = documents.keys().next().value as string;
    documents.get(oldest)?.destroy();
    documents.delete(oldest);
  }
  return promise;
}

/** Forget a document (its signed link expired: the next open signs a fresh one). */
export function forgetDocument(url: string): void {
  const key = objectPath(url);
  documents.get(key)?.destroy();
  documents.delete(key);
}

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** A cited bbox (PDF points, origin bottom-left) as CSS pixels on the rendered page, padded. */
export function highlightBox(viewport: PageViewport, bbox: readonly number[], pad = 3): Box | null {
  if (bbox.length !== 4 || bbox.some((n) => !Number.isFinite(n))) return null;
  const [x1, y1] = viewport.convertToViewportPoint(bbox[0], bbox[1]) as number[];
  const [x2, y2] = viewport.convertToViewportPoint(bbox[2], bbox[3]) as number[];
  return {
    left: Math.min(x1, x2) - pad,
    top: Math.min(y1, y2) - pad,
    width: Math.abs(x2 - x1) + pad * 2,
    height: Math.abs(y2 - y1) + pad * 2,
  };
}
