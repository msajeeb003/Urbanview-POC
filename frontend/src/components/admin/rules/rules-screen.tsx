"use client";

/**
 * Planning rules (wireframe `adminRules`): "Planning rules — Zone parameter sets — each carries its
 * source & verification date" with "+ New rule"; the table Zone, Use, FAR, Coverage, Height, Source
 * (document · page), Status (Verified / Unverified, the date and who in the tooltip). Admins edit a
 * row in place (an editor row under it) and add a rule for a zone without one; each save is a new
 * version of the zone's set, audited by the API. Reviewers read the table (no actions).
 */
import { useRouter } from "next/navigation";
import { Fragment, useState, useTransition } from "react";

import { saveRuleAction, zoneDocumentsAction } from "@/lib/admin/rule-actions";
import {
  checkRule,
  far,
  heightText,
  percent,
  ruleDraftFrom,
  sourceText,
  staleVerification,
  verifiedChip,
  type DocumentOption,
  type RuleDraft,
  type RuleErrors,
} from "@/lib/admin/rules";
import type { ZoneParameterSet } from "@/lib/api/types";
import { useShell } from "@/lib/store";

import { AdminCard, StatusChip } from "../parts";

export interface RuleZone {
  id: number;
  name: string;
}

type Editing = { kind: "new" } | { kind: "edit"; id: number } | null;

export function RulesScreen({
  rules,
  zones,
  readOnly,
  today,
  me,
}: {
  rules: ZoneParameterSet[];
  zones: RuleZone[];
  readOnly: boolean;
  today: string;
  me: string;
}) {
  const [editing, setEditing] = useState<Editing>(null);
  const withRule = new Set(rules.map((r) => r.zone_id));
  const free = zones.filter((z) => !withRule.has(z.id));
  const columns = readOnly ? 7 : 8;

  return (
    <AdminCard
      title="Planning rules"
      sub="Zone parameter sets — each carries its source & verification date"
      action={
        readOnly ? undefined : (
          <button
            type="button"
            className="abtn"
            disabled={editing?.kind === "new" || !free.length}
            title={free.length ? undefined : "Every zone has a rule: edit its row instead"}
            onClick={() => setEditing({ kind: "new" })}
          >
            + New rule
          </button>
        )
      }
    >
      <table className="tbl ruletbl">
        <thead>
          <tr>
            <th>Zone</th>
            <th>Use</th>
            <th>FAR</th>
            <th>Coverage</th>
            <th>Height</th>
            <th>Source</th>
            <th>Status</th>
            {!readOnly && <th aria-label="Edit" />}
          </tr>
        </thead>
        <tbody>
          {editing?.kind === "new" && (
            <tr className="findetail">
              <td colSpan={columns}>
                <RuleEditor rule={null} zones={free} today={today} me={me} onDone={() => setEditing(null)} />
              </td>
            </tr>
          )}
          {rules.length === 0 && editing?.kind !== "new" && (
            <tr>
              <td colSpan={columns} className="osub">
                No zone has typical values recorded yet.{readOnly ? "" : " “+ New rule” adds the first."}
              </td>
            </tr>
          )}
          {rules.map((rule) => {
            const chip = verifiedChip(rule);
            const open = editing?.kind === "edit" && editing.id === rule.id;
            return (
              <Fragment key={rule.id}>
                <tr className={open ? "sel" : undefined}>
                  <td>
                    <b>{rule.zone_name ?? `Zone ${rule.zone_id}`}</b>
                    <span className="fmeta">v{rule.version}</span>
                  </td>
                  <td>{rule.land_use ?? "—"}</td>
                  <td className="mono">{far(rule.max_far)}</td>
                  <td className="mono">{percent(rule.max_site_coverage_pct)}</td>
                  <td className="mono">{heightText(rule)}</td>
                  <td className="mono" style={{ fontSize: "10.5px" }} title={rule.source?.note ?? undefined}>
                    {sourceText(rule)}
                  </td>
                  <td>
                    <span title={chip.detail}>
                      <StatusChip tone={chip.tone}>{chip.label}</StatusChip>
                    </span>
                    {rule.verified_on && <span className="fmeta">{chip.detail.replace(/^Verified /, "")}</span>}
                  </td>
                  {!readOnly && (
                    <td>
                      <button
                        type="button"
                        className="abtn sm ghost"
                        aria-expanded={open}
                        onClick={() => setEditing(open ? null : { kind: "edit", id: rule.id })}
                      >
                        {open ? "Close" : "Edit"}
                      </button>
                    </td>
                  )}
                </tr>
                {open && (
                  <tr className="findetail">
                    <td colSpan={columns}>
                      <RuleEditor rule={rule} zones={zones} today={today} me={me} onDone={() => setEditing(null)} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      {readOnly && <div className="finfoot">Read only: admins record and verify the planning rules.</div>}
    </AdminCard>
  );
}

function RuleEditor({
  rule,
  zones,
  today,
  me,
  onDone,
}: {
  rule: ZoneParameterSet | null;
  zones: RuleZone[];
  today: string;
  me: string;
  onDone: () => void;
}) {
  const router = useRouter();
  const showToast = useShell((s) => s.showToast);
  const [draft, setDraft] = useState<RuleDraft>(() => ruleDraftFrom(rule, rule ? undefined : zones[0]?.id));
  const [errors, setErrors] = useState<RuleErrors>({});
  const [message, setMessage] = useState<string | null>(null);
  const [documents, setDocuments] = useState<{ zoneId: string; items: DocumentOption[] } | null>(null);
  const [pending, start] = useTransition();

  const set = (field: keyof RuleDraft) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => {
    const value = e.target.value;
    setDraft((d) => ({ ...d, [field]: value, ...(field === "zoneId" ? { documentId: "", page: "" } : {}) }));
    setErrors((all) => ({ ...all, [field]: undefined, values: undefined }));
  };

  const loadDocuments = () => {
    if (!draft.zoneId || documents?.zoneId === draft.zoneId) return;
    const zoneId = draft.zoneId;
    start(async () => {
      const result = await zoneDocumentsAction(Number(zoneId));
      if (result.ok) setDocuments({ zoneId, items: result.items });
      else setMessage(result.message);
    });
  };

  const stale = staleVerification(draft, rule);
  const save = () => {
    const checked = checkRule(draft, today);
    if (!checked.ok) {
      setErrors(checked.errors);
      setMessage("Check the marked fields.");
      return;
    }
    setMessage(null);
    // an old verification never vouches for new values
    const values = stale ? { ...checked.value, verified_on: null, verified_by: null } : checked.value;
    start(async () => {
      const result = await saveRuleAction(rule?.id ?? null, values);
      if (result.ok) {
        showToast(result.message);
        onDone();
        router.refresh();
      } else {
        setMessage(result.message); // everything typed stays
      }
    });
  };

  // the current source stays selectable before the zone's documents are loaded
  const options: DocumentOption[] = documents?.zoneId === draft.zoneId ? [...documents.items] : [];
  if (rule?.source && draft.documentId === String(rule.source.document_id) && !options.some((o) => o.id === rule.source!.document_id)) {
    options.unshift({ id: rule.source.document_id, name: rule.source.document_name ?? `Document ${rule.source.document_id}`, status: "" });
  }
  const err = (field: keyof RuleErrors) => (errors[field] ? <span className="fmeta ferr">{errors[field]}</span> : null);

  return (
    <form
      className="ruleform"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <div className="rulegrid">
        <label>
          <span className="fieldlab">Zone</span>
          {rule ? (
            <b className="rzone">{rule.zone_name ?? `Zone ${rule.zone_id}`}</b>
          ) : (
            <select className="rinput oselect" value={draft.zoneId} onChange={set("zoneId")}>
              {zones.map((z) => (
                <option key={z.id} value={z.id}>
                  {z.name}
                </option>
              ))}
            </select>
          )}
          {err("zoneId")}
        </label>
        <label className="wide">
          <span className="fieldlab">Use</span>
          <input className="rinput" value={draft.landUse} maxLength={300} placeholder="e.g. Mixed use — housing and services" onChange={set("landUse")} />
          {err("landUse")}
        </label>
        <label>
          <span className="fieldlab">FAR (II)</span>
          <input className="rinput" inputMode="decimal" value={draft.far} placeholder="e.g. 3.2" onChange={set("far")} />
          {err("far")}
        </label>
        <label>
          <span className="fieldlab">Coverage (IZ) %</span>
          <input className="rinput" inputMode="decimal" value={draft.coverage} placeholder="e.g. 55" onChange={set("coverage")} />
          {err("coverage")}
        </label>
        <label>
          <span className="fieldlab">Height (m)</span>
          <input className="rinput" inputMode="decimal" value={draft.height} placeholder="e.g. 27.5" onChange={set("height")} />
          {err("height")}
        </label>
        <label>
          <span className="fieldlab">Floors</span>
          <input className="rinput" inputMode="numeric" value={draft.floors} placeholder="e.g. 8" onChange={set("floors")} />
          {err("floors")}
        </label>
        <label className="wide">
          <span className="fieldlab">Source document</span>
          <select className="rinput oselect" value={draft.documentId} onFocus={loadDocuments} onMouseDown={loadDocuments} onChange={set("documentId")}>
            <option value="">{pending && !documents ? "Loading the zone's documents…" : "None recorded"}</option>
            {options.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
                {d.status && d.status !== "adopted" ? ` (${d.status.replace("_", " ")})` : ""}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="fieldlab">Page</span>
          <input className="rinput" inputMode="numeric" value={draft.page} placeholder="e.g. 14" onChange={set("page")} />
          {err("page")}
        </label>
        <label className="wide">
          <span className="fieldlab">Source note</span>
          <input className="rinput" value={draft.sourceNote} maxLength={500} placeholder="e.g. table 3, typical block" onChange={set("sourceNote")} />
          {err("sourceNote")}
        </label>
        <label>
          <span className="fieldlab">Verified on</span>
          <input className="rinput" type="date" value={draft.verifiedOn} max={today} onChange={set("verifiedOn")} />
          {err("verifiedOn")}
        </label>
        <label>
          <span className="fieldlab">Verified by</span>
          <input className="rinput" value={draft.verifiedBy} maxLength={200} placeholder="name" onChange={set("verifiedBy")} />
          {err("verifiedBy")}
        </label>
        <div className="rverify">
          <button
            type="button"
            className="abtn sm ghost"
            onClick={() => {
              setDraft((d) => ({ ...d, verifiedOn: today, verifiedBy: me }));
              setErrors((all) => ({ ...all, verifiedOn: undefined }));
            }}
          >
            Verified against the source today
          </button>
        </div>
        {stale && (
          <div className="full rhint">
            The values changed since the verification of {rule?.verified_on}: mark it verified again, or the rule saves as unverified.
          </div>
        )}
        <label className="full">
          <span className="fieldlab">Notes</span>
          <textarea className="rinput rnote" rows={2} maxLength={2000} value={draft.notes} onChange={set("notes")} />
        </label>
      </div>
      {errors.values && <div className="rmsg">{errors.values}</div>}
      {message && !errors.values && <div className="rmsg">{message}</div>}
      <div className="rbtns">
        <button type="submit" className="abtn sm" disabled={pending}>
          {pending ? "Saving…" : rule ? `Save as v${rule.version + 1}` : "Save rule"}
        </button>
        <button type="button" className="abtn sm ghost" onClick={onDone}>
          Cancel
        </button>
        <span className="rhint">A save is a new version of the zone&apos;s rule; the zone panel on the map shows it at once.</span>
      </div>
    </form>
  );
}
