/**
 * The words of the source references and the source viewer, as pure functions (unit-tested):
 * the label of a value's source icon (field, document, page and the plan's own note, the pilot
 * scope's `source_page` + `source_note`), and what the viewer says when it cannot show a page.
 */
import { ApiError } from "@/lib/api/client";

/** What the opener already knows, so the viewer can name the document and page before (and
 * without) the API's answer. */
export type SourceHint = {
  documentName?: string | null;
  page?: number | null;
  label?: string | null;
  note?: string | null;
  registryUrl?: string | null;
};

export type SourceRef = {
  label?: string | null;
  documentName?: string | null;
  page?: number | null;
  note?: string | null;
  fallback?: boolean;
};

/** `Max number of floors: DUP Centar – Zona C2, page 14 · table 3 – UP 12` (title) and the same as
 * the button's accessible name; a document-level value adds "plan-wide value". */
export function sourceRefText(ref: SourceRef): { title: string; ariaLabel: string } {
  const where = [ref.documentName || "Source document", ref.page ? `page ${ref.page}` : null].filter(Boolean).join(", ");
  const extras = [ref.note?.trim() || null, ref.fallback ? "plan-wide value" : null].filter(Boolean) as string[];
  const title = [ref.label ? `${ref.label}: ${where}` : where, ...extras].join(" · ");
  const ariaLabel = `Open the source${ref.label ? ` of ${ref.label}` : ""}: ${[where, ...extras].join(", ")}`;
  return { title, ariaLabel };
}

export type SourceFailure = {
  message: string;
  /** Worth trying again (connection or server trouble); a missing file or page is not. */
  retry: boolean;
  /** The registry entry to offer when the PDF itself cannot be shown. */
  registryUrl: string | null;
  /** The page the API was asked for, when it says so (the header shows it). */
  page?: number;
};

const GENERIC = "This page could not be loaded.";

/** Why the viewer shows no page, from the API's answer (`details.reason`, `details.page_count`). */
export function sourceFailure(error: unknown, hint: SourceHint = {}): SourceFailure {
  const registryUrl = hint.registryUrl || null;
  if (error instanceof ApiError && error.isNotFound) {
    const d = (error.details && typeof error.details === "object" ? error.details : {}) as {
      reason?: string;
      page?: number;
      page_count?: number;
      value_id?: number;
    };
    const at = typeof d.page === "number" ? { page: d.page } : {};
    if (d.reason === "not_stored") {
      return { message: "The PDF of this document is not stored in UrbanView yet.", retry: false, registryUrl, ...at };
    }
    if (typeof d.page_count === "number" && typeof d.page === "number") {
      const pages = `${d.page_count} ${d.page_count === 1 ? "page" : "pages"}`;
      return { message: `Page ${d.page} is not in this document: it has ${pages}.`, retry: false, registryUrl, page: d.page };
    }
    if (d.value_id != null) {
      return {
        message: "This value is no longer published. Reopen the parcel to see the current data.",
        retry: false,
        registryUrl: null,
      };
    }
    return { message: "This document is no longer available.", retry: false, registryUrl: null };
  }
  if (error instanceof ApiError && !error.isTransient) return { message: GENERIC, retry: false, registryUrl };
  return { message: `${GENERIC} Check your connection and try again.`, retry: true, registryUrl: null };
}

/** The note beside the page number when a typed page is not in the document. */
export function pageRangeNote(total: number): string {
  return `This document has ${total} ${total === 1 ? "page" : "pages"}.`;
}
