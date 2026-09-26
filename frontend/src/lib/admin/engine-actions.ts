"use server";

/**
 * Server action of the Calculation engine tab (admins): "+ Add formula" and "+ Add data input"
 * record a proposal (`POST /v1/admin/engine/proposals`, audited `engine.proposal`) for the
 * client's review. Nothing here changes the deterministic engine.
 */
import { revalidatePath } from "next/cache";

import type { EngineProposal, EngineProposalIn } from "@/lib/api/types";

import { adminSend } from "./api";
import { proposalProblem, type ProposalDraft } from "./engine";
import { currentStaff } from "./session";

export type ProposalResult = { ok: true; message: string } | { ok: false; message: string };

export async function proposeAction(kind: "formula" | "data_input", draft: ProposalDraft): Promise<ProposalResult> {
  const staff = await currentStaff();
  if (staff?.role !== "admin") return { ok: false, message: "Only admins record engine proposals." };
  const problem = proposalProblem(kind, draft);
  if (problem) return { ok: false, message: problem };
  const body: EngineProposalIn =
    kind === "formula"
      ? { kind, name: draft.name.trim(), expression: draft.expression.trim(), source: draft.source.trim() || null }
      : { kind, name: draft.name.trim(), provides: draft.provides.trim() };
  const answer = await adminSend<EngineProposal>("POST", "/v1/admin/engine/proposals", body);
  if (!answer.ok) {
    if (answer.status === 403) return { ok: false, message: "Your role cannot do this." };
    if (answer.status === 0 || answer.status >= 500) {
      return { ok: false, message: "The data service did not answer. Try again in a moment." };
    }
    const details = Array.isArray(answer.details) ? (answer.details as { msg?: string }[]) : [];
    const msgs = details.map((d) => d.msg?.replace(/^Value error, /, "")).filter(Boolean);
    return { ok: false, message: msgs.join("; ") || answer.message || "The proposal was refused." };
  }
  revalidatePath("/admin/engine");
  return {
    ok: true,
    message:
      kind === "formula"
        ? "Formula proposed — recorded for the client's review; the engine is unchanged"
        : "Data input proposed — recorded for the client's review",
  };
}
