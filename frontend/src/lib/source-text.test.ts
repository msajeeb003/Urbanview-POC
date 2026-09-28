import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/client";
import { pageRangeNote, sourceFailure, sourceRefText } from "@/lib/source-text";

const notFound = (details: unknown) => new ApiError({ status: 404, code: "not_found", message: "gone", details });

describe("sourceRefText", () => {
  it("names the field, document, page and the plan's note", () => {
    const ref = sourceRefText({
      label: "Max number of floors",
      documentName: "DUP Centar – Zona C2",
      page: 14,
      note: "table 3 – UP 12",
    });
    expect(ref.title).toBe("Max number of floors: DUP Centar – Zona C2, page 14 · table 3 – UP 12");
    expect(ref.ariaLabel).toBe("Open the source of Max number of floors: DUP Centar – Zona C2, page 14, table 3 – UP 12");
  });

  it("tells two values on one row apart", () => {
    const common = { documentName: "DUP Centar – Zona C2", page: 14, note: "table 3 – UP 12" };
    const height = sourceRefText({ ...common, label: "Max building height" });
    const floors = sourceRefText({ ...common, label: "Max number of floors" });
    expect(height.title).not.toBe(floors.title);
    expect(height.ariaLabel).not.toBe(floors.ariaLabel);
  });

  it("marks a plan-wide value and leaves out what is not known", () => {
    expect(sourceRefText({ label: "Min green area", documentName: "PUP Glavni grad", page: 3, fallback: true }).title).toBe(
      "Min green area: PUP Glavni grad, page 3 · plan-wide value",
    );
    expect(sourceRefText({ documentName: "DUP Privaj", note: "  " }).title).toBe("DUP Privaj");
    expect(sourceRefText({}).ariaLabel).toBe("Open the source: Source document");
  });
});

describe("sourceFailure", () => {
  it("says the PDF is not stored and offers the registry entry, without Retry", () => {
    const f = sourceFailure(notFound({ document_id: 46, page: 1, reason: "not_stored" }), {
      registryUrl: "https://lamp.gov.me/PlanningDocument/Details/4182",
    });
    expect(f).toEqual({
      message: "The PDF of this document is not stored in UrbanView yet.",
      retry: false,
      registryUrl: "https://lamp.gov.me/PlanningDocument/Details/4182",
      page: 1,
    });
  });

  it("names a cited page the document does not have", () => {
    const f = sourceFailure(notFound({ document_id: 2, page: 30, page_count: 24 }));
    expect(f.message).toBe("Page 30 is not in this document: it has 24 pages.");
    expect(f.retry).toBe(false);
    expect(f.page).toBe(30);
    expect(sourceFailure(notFound({ page: 2, page_count: 1 })).message).toBe("Page 2 is not in this document: it has 1 page.");
  });

  it("says a value or document is gone", () => {
    expect(sourceFailure(notFound({ value_id: 5 }), { registryUrl: "https://x" })).toEqual({
      message: "This value is no longer published. Reopen the parcel to see the current data.",
      retry: false,
      registryUrl: null,
    });
    expect(sourceFailure(notFound({ document_id: 999 })).message).toBe("This document is no longer available.");
  });

  it("offers Retry for connection and server trouble only", () => {
    for (const status of [0, 429, 503]) {
      const f = sourceFailure(new ApiError({ status, code: "x", message: "x" }), { registryUrl: "https://x" });
      expect(f.retry).toBe(true);
      expect(f.message).toBe("This page could not be loaded. Check your connection and try again.");
      expect(f.registryUrl).toBeNull();
    }
    expect(sourceFailure(new Error("PDF.js could not fetch the range")).retry).toBe(true);
    expect(sourceFailure(new ApiError({ status: 422, code: "validation_error", message: "x" })).retry).toBe(false);
  });
});

describe("pageRangeNote", () => {
  it("states the page count", () => {
    expect(pageRangeNote(24)).toBe("This document has 24 pages.");
    expect(pageRangeNote(1)).toBe("This document has 1 page.");
  });
});
