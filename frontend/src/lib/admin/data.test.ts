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
  guessKind,
  jobPill,
  parseFilters,
  rolesFor,
  zoneOptions,
} from "./data";

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

  it("says why an extraction failed and how many attempts it took", () => {
    const failed = extractionPill(
      file({
        extraction_state: "failed",
        extraction_error: "ExtractionFailed: no readable page",
        extraction_job: job({ status: "failed", attempts: 3 }),
      }),
    );
    expect(failed).toMatchObject({ label: "Failed", detail: "3/3 attempts", reason: "ExtractionFailed: no readable page" });
  });

  it("reads geometry jobs", () => {
    expect(jobPill(null, "text")).toMatchObject({ label: "— text file", muted: true });
    expect(jobPill(null, "drawing").label).toBe("Not run");
    expect(jobPill(job({ status: "running", attempts: 2 })).detail).toBe("attempt 2/3");
    expect(jobPill(job({ status: "failed", attempts: 1, error: "not implemented" }))).toMatchObject({
      label: "Failed",
      reason: "not implemented",
    });
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
