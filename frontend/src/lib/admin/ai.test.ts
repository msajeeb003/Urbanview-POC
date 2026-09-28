import { describe, expect, it } from "vitest";

import type { AiCheck, AiChecklistItem } from "@/lib/api/types";

import { checkChip, checklistChip, checkToast, eur, KEY_MESSAGES, keyProblem, latency, maskedKey, missingRequired, testInFlight, tokens, waitedSeconds } from "./ai";

const GOOD = "sk-ant-api03-" + "a".repeat(40);

function check(over: Partial<AiCheck> & { jobStatus?: AiCheck["job"]["status"]; error?: string | null } = {}): AiCheck {
  const { jobStatus = "succeeded", error = null, ...rest } = over;
  return {
    job: {
      id: 7,
      kind: "extract",
      type: "ai_check",
      queue: "extraction",
      status: jobStatus,
      payload: {},
      attempts: 1,
      max_attempts: 1,
      manual_retries: 0,
      requested_by: "admin@example.com",
      requested_at: "2026-09-28T10:00:00Z",
      started_at: jobStatus === "queued" ? null : "2026-09-28T10:00:01Z",
      finished_at: jobStatus === "succeeded" ? "2026-09-28T10:00:02Z" : null,
      error,
      cost: { llm_model: null, llm_tokens_in: 0, llm_tokens_out: 0, estimated_cost_eur: null, wall_time_ms: null },
      status_url: "/v1/admin/jobs/7",
    } as unknown as AiCheck["job"],
    status: "ok",
    detail_en: null,
    input_tokens: 12,
    output_tokens: 1,
    matches_current_key: true,
    ...rest,
  } as AiCheck;
}

describe("key format (the API's rules and messages, in its order)", () => {
  it("accepts a well-formed key, trimmed", () => {
    expect(keyProblem(GOOD)).toBeNull();
    expect(keyProblem(`  ${GOOD}\n`)).toBeNull();
  });

  it("names the first rule broken", () => {
    expect(keyProblem("")).toBe(KEY_MESSAGES.empty);
    expect(keyProblem("   ")).toBe(KEY_MESSAGES.empty);
    expect(keyProblem("sk-ant-api03 " + "a".repeat(40))).toBe(KEY_MESSAGES.whitespace);
    expect(keyProblem("sk-live-" + "a".repeat(40))).toBe(KEY_MESSAGES.prefix);
    expect(keyProblem("sk-ant-admin01-" + "a".repeat(40))).toBe(KEY_MESSAGES.admin);
    expect(keyProblem("sk-ant-short")).toBe(KEY_MESSAGES.short);
    expect(keyProblem("sk-ant-" + "a".repeat(260))).toBe(KEY_MESSAGES.long);
    expect(keyProblem("sk-ant-api03-" + "a".repeat(30) + "!!")).toBe(KEY_MESSAGES.characters);
  });

  it("shows a key only masked", () => {
    expect(maskedKey("1234")).toBe("sk-ant-…1234");
    expect(maskedKey(null)).toBe("none");
  });
});

describe("connection test", () => {
  it("knows when a test is in flight", () => {
    expect(testInFlight(null)).toBe(false);
    expect(testInFlight(check({ jobStatus: "queued", status: null }))).toBe(true);
    expect(testInFlight(check({ jobStatus: "running", status: null }))).toBe(true);
    expect(testInFlight(check())).toBe(false);
  });

  it("chips the outcome", () => {
    expect(checkChip(null)).toEqual({ tone: "pend", label: "Never tested" });
    expect(checkChip(check({ jobStatus: "queued", status: null }))).toEqual({ tone: "rev", label: "Queued" });
    expect(checkChip(check({ jobStatus: "running", status: null }))).toEqual({ tone: "rev", label: "Testing" });
    expect(checkChip(check())).toEqual({ tone: "ok", label: "Connected" });
    expect(checkChip(check({ status: "no_credit" }))).toEqual({ tone: "pend", label: "No credit" });
    expect(checkChip(check({ jobStatus: "failed", status: "error", error: "boom" }))).toEqual({ tone: "pend", label: "Error" });
  });

  it("toasts the outcome with the API's advice", () => {
    expect(checkToast(check())).toBe("Connection test: Connected.");
    expect(checkToast(check({ status: "no_credit", detail_en: "Add credit in the Anthropic Console." }))).toBe(
      "Connection test: No credit. Add credit in the Anthropic Console.",
    );
    expect(checkToast(check({ jobStatus: "failed", status: "error", error: "worker crashed" }))).toBe("Connection test: Error. worker crashed");
  });

  it("counts how long a queued test has waited", () => {
    const queued = check({ jobStatus: "queued", status: null });
    expect(waitedSeconds(queued, new Date("2026-09-28T10:00:45Z"))).toBe(45);
    expect(waitedSeconds(check(), new Date("2026-09-28T10:00:45Z"))).toBe(0);
    expect(waitedSeconds(null, new Date())).toBe(0);
  });
});

describe("readiness", () => {
  const item = (over: Partial<AiChecklistItem>): AiChecklistItem => ({
    key: "api_key",
    ok: true,
    required: true,
    label_en: "x",
    detail_en: "y",
    ...over,
  });

  it("chips Ready / Missing / Unknown", () => {
    expect(checklistChip(item({ ok: true }))).toEqual({ tone: "ok", label: "Ready" });
    expect(checklistChip(item({ ok: false }))).toEqual({ tone: "pend", label: "Missing" });
    expect(checklistChip(item({ ok: false, required: false }))).toEqual({ tone: "rev", label: "Missing" });
    expect(checklistChip(item({ ok: null }))).toEqual({ tone: "rev", label: "Unknown" });
  });

  it("lists the required items still missing", () => {
    const items = [item({ key: "api_key", ok: true }), item({ key: "worker", ok: null }), item({ key: "smtp", ok: false, required: false })];
    expect(missingRequired(items).map((i) => i.key)).toEqual(["worker"]);
  });
});

describe("formats", () => {
  it("prints money, tokens and latency", () => {
    expect(eur(0.0123)).toBe("€0.0123");
    expect(eur(12.345)).toBe("€12.35");
    expect(eur(null)).toBe("—");
    expect(tokens(1234567)).toBe("1 234 567");
    expect(tokens(12)).toBe("12");
    expect(latency(850)).toBe("850 ms");
    expect(latency(2400)).toBe("2.4 s");
  });
});
