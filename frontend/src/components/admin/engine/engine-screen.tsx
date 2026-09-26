"use client";

/**
 * Calculation engine (wireframe `adminEngine`): the "Formulas" card (Output, Expression, Source,
 * Status) with "+ Add formula", the "Input data" card (Dataset, Provides, Status) with "+ Add
 * data input", the footer line with the engine version, formula version, counts and last update,
 * and the engine's changelog (`#changelog`, linked from Financial assumptions).
 *
 * The formulas are the client's and the shared engine's (`@urbanview/feasibility-engine`):
 * nothing here edits them. The two dialogs record proposals for the client's review (status New /
 * Pending, audited); the engine changes only with a new formula version the client validated.
 */
import { useState, useTransition } from "react";

import { ENGINE_CHANGELOG, ENGINE_UPDATED, ENGINE_VERSION, FORMULA_VERSION } from "@urbanview/feasibility-engine";

import { Cta } from "@/components/ui/cta";
import { Modal, ModalHead } from "@/components/ui/modal";
import { dayLabel } from "@/lib/admin/assumptions";
import { proposeAction } from "@/lib/admin/engine-actions";
import { engineFooter, FORMULA_ROWS, proposalChip, proposalProblem, type InputRow, type ProposalDraft } from "@/lib/admin/engine";
import type { EngineProposal } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { AdminCard, StatusChip } from "../parts";

type Kind = "formula" | "data_input";
const EMPTY: ProposalDraft = { name: "", expression: "", source: "", provides: "" };

export function EngineScreen({ proposals, inputs }: { proposals: EngineProposal[]; inputs: InputRow[] }) {
  const [dialog, setDialog] = useState<Kind | null>(null);
  const formulas = proposals.filter((p) => p.kind === "formula");
  const datasets = proposals.filter((p) => p.kind === "data_input");
  const proposed = (p: EngineProposal) => `proposed by ${p.created_by} · ${dayLabel(p.created_at)}`;

  return (
    <>
      <AdminCard
        title="Formulas"
        sub="Calculated outputs the engine derives for every parcel — extend the model over time"
        action={
          <button type="button" className="abtn" onClick={() => setDialog("formula")}>
            + Add formula
          </button>
        }
      >
        <table className="tbl">
          <thead>
            <tr>
              <th>Output</th>
              <th>Expression</th>
              <th>Source</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {FORMULA_ROWS.map((f) => (
              <tr key={f.key}>
                <td>
                  <b>{f.output}</b>
                </td>
                <td className="mono" style={{ fontSize: 12 }}>
                  {f.expression}
                </td>
                <td>{f.source}</td>
                <td>
                  <span title={`Formula version ${FORMULA_VERSION}: the fixtures await the client's validation`}>
                    <StatusChip tone="ok">Live</StatusChip>
                  </span>
                </td>
              </tr>
            ))}
            {formulas.map((p) => {
              const chip = proposalChip(p);
              return (
                <tr key={`p${p.id}`} className="eprop">
                  <td>
                    <b>{p.name}</b>
                    <span className="fmeta">{proposed(p)}</span>
                  </td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    {p.expression}
                  </td>
                  <td>{p.source ?? "custom"}</td>
                  <td>
                    <span title="Recorded for the client's review; not part of the calculation">
                      <StatusChip tone={chip.tone}>{chip.label}</StatusChip>
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </AdminCard>

      <AdminCard
        title="Input data"
        sub="Datasets the formulas draw on — add new sources to improve accuracy"
        action={
          <button type="button" className="abtn" onClick={() => setDialog("data_input")}>
            + Add data input
          </button>
        }
      >
        <table className="tbl">
          <thead>
            <tr>
              <th>Dataset</th>
              <th>Provides</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {inputs.map((i) => (
              <tr key={i.key}>
                <td>
                  <b>{i.name}</b>
                </td>
                <td>{i.provides}</td>
                <td>
                  <StatusChip tone="ok">Connected</StatusChip>
                </td>
              </tr>
            ))}
            {datasets.map((p) => {
              const chip = proposalChip(p);
              return (
                <tr key={`p${p.id}`} className="eprop">
                  <td>
                    <b>{p.name}</b>
                    <span className="fmeta">{proposed(p)}</span>
                  </td>
                  <td>{p.provides}</td>
                  <td>
                    <span title="Recorded for the client's review; no formula reads it">
                      <StatusChip tone={chip.tone}>{chip.label}</StatusChip>
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </AdminCard>

      <p className="engfoot">
        {engineFooter({
          engineVersion: ENGINE_VERSION,
          formulaVersion: FORMULA_VERSION,
          formulas: FORMULA_ROWS.length,
          inputs: inputs.length,
          updated: dayLabel(ENGINE_UPDATED),
          proposals: proposals.filter((p) => p.status === "new" || p.status === "pending").length,
        })}
      </p>

      <section id="changelog" className="engchangelog">
        <AdminCard title="Changelog" sub="Engine releases, newest first — the formulas are the client's; a release that changes one gets a new formula version">
          <ol className="englog">
            {ENGINE_CHANGELOG.map((release) => (
              <li key={release.engine_version}>
                <div className="fhrow">
                  <b className="mono">v{release.engine_version}</b>
                  <span className="osub">
                    formula version <span className="mono">{release.formula_version}</span> · {dayLabel(release.date)}
                  </span>
                </div>
                <ul>
                  {release.changes.map((change) => (
                    <li key={change}>{change}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
        </AdminCard>
      </section>

      {dialog && <ProposalDialog kind={dialog} onClose={() => setDialog(null)} />}
    </>
  );
}

function ProposalDialog({ kind, onClose }: { kind: Kind; onClose: () => void }) {
  const showToast = useShell((s) => s.showToast);
  const [draft, setDraft] = useState<ProposalDraft>(EMPTY);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();
  const formula = kind === "formula";
  const set = (field: keyof ProposalDraft) => (e: React.ChangeEvent<HTMLInputElement>) => setDraft((d) => ({ ...d, [field]: e.target.value }));

  const submit = () => {
    const problem = proposalProblem(kind, draft);
    if (problem) {
      setMessage(problem);
      return;
    }
    start(async () => {
      const result = await proposeAction(kind, draft);
      if (result.ok) {
        showToast(result.message);
        onClose();
      } else {
        setMessage(result.message); // everything typed stays
      }
    });
  };

  return (
    <Modal open onOpenChange={(open) => !open && onClose()} label={formula ? "Add a formula" : "Add a data input"}>
      <ModalHead
        eyebrow="Calculation engine"
        title={formula ? "Add a formula" : "Add a data input"}
        lead={
          formula
            ? "Propose a new calculated output. It is recorded for the client's review; the engine's formulas change only when the client validates a new formula version."
            : "Propose a new dataset the formulas could draw on. It is recorded for the client's review."
        }
      />
      <form
        className="mbody"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        {formula ? (
          <>
            <div className="field">
              <label htmlFor="efName">Output name</label>
              <input id="efName" value={draft.name} maxLength={200} placeholder="e.g. Parking spaces required" onChange={set("name")} autoFocus />
            </div>
            <div className="field">
              <label htmlFor="efExpr">Expression</label>
              <input id="efExpr" value={draft.expression} maxLength={500} placeholder="e.g. GFA ÷ 60" onChange={set("expression")} />
            </div>
            <div className="field">
              <label htmlFor="efSrc">
                Source <span className="opt">optional</span>
              </label>
              <input id="efSrc" value={draft.source} maxLength={200} placeholder="e.g. adopted plan" onChange={set("source")} />
            </div>
          </>
        ) : (
          <>
            <div className="field">
              <label htmlFor="eiName">Dataset name</label>
              <input id="eiName" value={draft.name} maxLength={200} placeholder="e.g. Utility connection costs" onChange={set("name")} autoFocus />
            </div>
            <div className="field">
              <label htmlFor="eiDesc">What it provides</label>
              <input
                id="eiDesc"
                value={draft.provides}
                maxLength={500}
                placeholder="e.g. per-parcel water, sewage & power hookup rates"
                onChange={set("provides")}
              />
            </div>
          </>
        )}
        {message && (
          <p className="formmsg" role="alert">
            {message}
          </p>
        )}
        <button type="submit" hidden />
      </form>
      <div className="mfoot">
        <Cta variant="ghost" style={{ width: "auto", padding: "0 18px" }} onClick={onClose}>
          Cancel
        </Cta>
        <Cta variant="primary" style={{ width: "auto", padding: "0 22px" }} disabled={pending} onClick={submit}>
          {pending ? "Recording…" : formula ? "Add formula" : "Add data input"}
        </Cta>
      </div>
    </Modal>
  );
}
