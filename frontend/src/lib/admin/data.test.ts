import { describe, expect, it } from "vitest";

import type { AdminDocument, AdminDocumentFile, AdminJob } from "@/lib/api/types";

import {
  anyActive,
  documentTypeOptions,
  explainProblem,
  extractionPill,
  fieldErrors,
  filterQuery,
  filtersHref,
  formatCost,
  formatDuration,
  guessKind,
  integrationChip,
  jobPill,
  jobTimes,
  kindsForRole,
  parseFilters,
  reasonText,
  redrawText,
  rolesFor,
  sourceRows,
  zoneOptions,
} from "./data";

const NOW = new Date("2026-09-26T10:30:00Z");

function job(over: Partial<AdminJob> = {}): AdminJob {
  return {
    id: 7,
    kind: "extract",
    type: "extract_document",
    queue: "extraction",
    status: "queued",
    requested_by: "ops",
    requested_at: "2026-09-26T10:00:00Z",
    status_url: "/v1/admin/jobs/7",
    attempts: 0,
    max_attempts: 3,
    ...over,
  } as AdminJob;
}

function file(over: Partial<AdminDocumentFile> = {}): AdminDocumentFile {
  return {
    file_id: 3,
    role: "text",
    position: 0,
    is_primary: true,
    kind: "planning_document",
    original_filename: "dup.pdf",
    mime_type: "application/pdf",
    size_bytes: 1000,
    sha256: "ab",
    uploaded_at: "2026-09-26T10:00:00Z",
    added_at: "2026-09-26T10:00:00Z",
    can_remove: true,
    extraction_state: "none",
    ...over,
  } as AdminDocumentFile;
}

describe("uploads", () => {
  it("takes the kind from the extension", () => {
    expect(guessKind("Plan.PDF")).toBe("planning_document");
    expect(guessKind("layers.gpkg")).toBe("gis");
    expect(guessKind("parcele.xlsx")).toBe("cadastral_extract");
    expect(guessKind("notes.docx")).toBeNull();
    expect(guessKind("no-extension")).toBeNull();
  });

  it("lets GIS files be drawings only", () => {
    expect(rolesFor("gis")).toEqual(["drawing"]);
    expect(rolesFor("planning_document")).toEqual(["text", "drawing", "both"]);
    expect(kindsForRole("drawing")).toEqual(["planning_document", "gis"]);
    expect(kindsForRole("text")).toEqual(["planning_document"]);
    expect(kindsForRole("both")).toEqual(["planning_document"]);
  });
});

describe("sources", () => {
  const profile = [
    { id: "eregistri", kind: "planning", name: "eRegistri", url: "https://x", provides: "Plans", format: "PDF", integration: "manual_upload", integration_note: "Uploaded by staff" },
    { id: "ekatastar", kind: "cadastre", name: "eKatastar", url: "https://y", provides: "Owners", integration: "access_pending" },
    { id: "monstat", kind: "market", name: "Monstat", url: "https://z", provides: "Prices", integration: "not_connected" },
  ] as const;

  it("never calls a source Linked unless the profile says it is", () => {
    const rows = sourceRows([...profile]);
    expect(rows.map((r) => integrationChip(r.integration).label)).toEqual(["Manual upload", "Access pending", "Not connected"]);
    expect(rows[0]).toMatchObject({ format: "PDF", note: "Uploaded by staff", url: "https://x" });
    expect(rows[2]).toMatchObject({ format: "—", note: null, integration: "not_connected" });
    expect(integrationChip("linked")).toEqual({ label: "Linked", tone: "ok" });
    expect(sourceRows(null)).toEqual([]);
  });
});

describe("the redraw flag", () => {
  it("names the scanned sheets of a PDF, nothing before its pages are read", () => {
    expect(redrawText({ kind: "planning_document", redraw_pages: [3, 5] })).toBe("needs QGIS redraw · p. 3, 5");
    expect(redrawText({ kind: "planning_document", redraw_pages: [] })).toBeNull();
    expect(redrawText({ kind: "planning_document", redraw_pages: null })).toBeNull();
    expect(redrawText({ kind: "gis", redraw_pages: [1] })).toBeNull();
    expect(redrawText({ kind: "planning_document", redraw_pages: [1, 2, 3, 4, 5, 6, 7, 8] })).toBe(
      "needs QGIS redraw · p. 1, 2, 3, 4, 5, 6 …",
    );
  });
});

describe("extraction and job pills", () => {
  it("follows a file from queued to ready for review", () => {
    expect(extractionPill(file()).label).toBe("Not extracted");
    expect(extractionPill(file({ role: "drawing" }))).toMatchObject({ label: "— drawing", muted: true });
    expect(extractionPill(file({ extraction_state: "queued", extraction_job: job() })).label).toBe("Queued");
    const ready = extractionPill(
      file({
        extraction_state: "ready_for_review",
        items: { pending: 4, approved: 0, amended: 0, rejected: 0, published: 0, total: 4 },
        extraction: { estimated_cost_eur: 0.4213 } as AdminDocumentFile["extraction"],
      }),
    );
    expect(ready).toMatchObject({ tone: "ok", label: "Ready for review", detail: "4 items · €0.42" });
  });

  it("follows the file's items once the run is done: reviewed, then published", () => {
    const withItems = (items: Partial<NonNullable<AdminDocumentFile["items"]>>) =>
      extractionPill(
        file({
          extraction_state: "ready_for_review",
          items: { pending: 0, approved: 0, amended: 0, rejected: 0, published: 0, total: 450, ...items },
        }),
      ).label;
    // some still to decide
    expect(withItems({ pending: 3, approved: 447 })).toBe("Ready for review");
    // every item decided, the accepted ones not on the map yet
    expect(withItems({ approved: 440, amended: 5, rejected: 5 })).toBe("Reviewed");
    expect(withItems({ approved: 440, amended: 5, rejected: 5, published: 400 })).toBe("Reviewed");
    // every accepted item served: what the document says too
    expect(withItems({ approved: 450, published: 450 })).toBe("Published");
    expect(withItems({ approved: 440, amended: 5, rejected: 5, published: 445 })).toBe("Published");
    // everything rejected: reviewed, nothing to publish
    expect(withItems({ rejected: 450 })).toBe("Reviewed");
  });

  it("says why an extraction failed and how many attempts it took", () => {
    const failed = extractionPill(
      file({
        extraction_state: "failed",
        extraction_error: "ExtractionFailed: no readable page",
        extraction_job: job({
          status: "failed",
          attempts: 3,
          started_at: "2026-09-26T10:01:00Z",
          finished_at: "2026-09-26T10:20:00Z",
          cost: { wall_time_ms: 81_000 },
        }),
      }),
      NOW,
    );
    expect(failed).toMatchObject({
      label: "Failed",
      detail: "3/3 attempts · finished 10 min ago",
      reason: "ExtractionFailed: no readable page",
      times: "started 2026-09-26 10:01 UTC · finished 2026-09-26 10:20 UTC · took 1 min 21 s",
    });
  });

  it("says when a job ran", () => {
    expect(jobTimes(null)).toEqual({ short: null, full: null });
    expect(jobTimes(job(), NOW)).toEqual({ short: "queued 30 min ago", full: "queued 2026-09-26 10:00 UTC" });
    // on the console's clock (the municipality's zone), named
    expect(jobTimes(job(), NOW, "Europe/Podgorica").full).toBe("queued 2026-09-26 12:00 Podgorica time");
    expect(jobTimes(job({ status: "running", started_at: "2026-09-26T10:25:00Z" }), NOW).short).toBe("started 5 min ago");
    expect(formatDuration(650)).toBe("650 ms");
    expect(formatDuration(22_629)).toBe("22.6 s");
  });

  it("reads geometry jobs", () => {
    expect(jobPill(null, "text")).toMatchObject({ label: "— text file", muted: true });
    expect(jobPill(null, "drawing").label).toBe("Not run");
    expect(jobPill(job({ status: "running", attempts: 2, started_at: "2026-09-26T10:29:00Z" }), undefined, NOW).detail).toBe(
      "attempt 2/3 · started 1 min ago",
    );
    expect(jobPill(job({ status: "failed", attempts: 1, error: "not implemented" }))).toMatchObject({
      label: "Failed",
      reason: "not implemented",
    });
    // the geometry job's own refusals read as plain sentences
    expect(
      jobPill(job({ status: "failed", error: "GeometryError: pages 3 are scanned sheets (the week-1 assessment's class C)" })).reason,
    ).toBe("pages 3 are scanned sheets (the week-1 assessment's class C)");
    expect(reasonText("TypeError: boom")).toBe("TypeError: boom");
    expect(reasonText(null)).toBeNull();
  });

  it("formats costs", () => {
    expect(formatCost(null)).toBeNull();
    expect(formatCost(0.004)).toBe("< €0.01");
    expect(formatCost(1.5)).toBe("€1.50");
  });

  it("polls only while something runs", () => {
    const idle = { files: [file({ extraction_state: "ready_for_review" })] } as AdminDocument;
    const busy = { files: [file({ extraction_state: "extracting" })] } as AdminDocument;
    const geo = { files: [file({ geometry_job: job({ kind: "geo", type: "process_geometry", status: "retrying" }) })] } as AdminDocument;
    expect(anyActive([idle])).toBe(false);
    expect(anyActive([idle, busy])).toBe(true);
    expect(anyActive([geo])).toBe(true);
  });
});

describe("filters", () => {
  it("parses the URL and ignores what the API would refuse", () => {
    const filters = parseFilters({ zone: "3", status: "adopted", state: "bogus", job: "failed", q: " Centar ", page: "2" });
    expect(filters).toEqual({ zone: 3, status: "adopted", state: null, job: "failed", q: "Centar", page: 2 });
    expect(filterQuery(filters)).toMatchObject({ zone_id: 3, status: "adopted", job_state: "failed", q: "Centar", limit: 50, offset: 50 });
    expect(filtersHref(filters, 3)).toBe("/admin/data?zone=3&status=adopted&job=failed&q=Centar&page=3");
    expect(filtersHref(parseFilters({}))).toBe("/admin/data");
  });

  it("orders document types DUP / PUP / PGR first", () => {
    const options = documentTypeOptions(
      { UP: "Urbanistički projekat", PGR: "Plan generalne regulacije", DUP: "Detaljni urbanistički plan", PUP: "Prostorno-urbanistički plan" },
      { DUP: "DUP — Detailed urban plan", PUP: "Spatial-urban plan" },
    );
    expect(options.map((o) => o.value)).toEqual(["DUP", "PUP", "PGR", "UP"]);
    expect(options[0].label).toBe("DUP — Detailed urban plan");
    expect(options[1].label).toBe("PUP — Spatial-urban plan");
    expect(options[3].label).toBe("UP — Urbanistički projekat");
  });

  it("sorts zones by name", () => {
    expect(zoneOptions([{ id: 2, name: "Zabjelo" }, { id: 1, name: "Centar" }]).map((z) => z.id)).toEqual([1, 2]);
    expect(zoneOptions(null)).toEqual([]);
  });
});

describe("refusals", () => {
  it("explains the pipeline's 409 reasons in plain words", () => {
    expect(explainProblem({ status: 409, details: { reason: "no_coverage_geometry" } })).toMatch(/geometry job first/);
    expect(explainProblem({ status: 409, details: { reason: "items_accepted" } })).toMatch(/approved/);
    expect(explainProblem({ status: 0 })).toMatch(/did not answer/);
    expect(explainProblem({ status: 409, message: "Only PDFs are extracted" })).toBe("Only PDFs are extracted");
  });

  it("maps 422 details onto form fields", () => {
    expect(
      fieldErrors([
        { loc: ["body", "name"], msg: "String should have at least 1 character" },
        { loc: ["body", "files", 0, "file_id"], msg: "no such stored file" },
        { loc: ["body", "adopted_on"], msg: "Value error, adopted_on cannot be in the future" },
      ]),
    ).toEqual({
      name: "String should have at least 1 character",
      files: "no such stored file",
      adopted_on: "adopted_on cannot be in the future",
    });
  });
});
