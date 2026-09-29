"use client";

/**
 * One review item (the middle column of the queue): the extracted value, what it is and where it
 * came from, and the decision.
 *
 * - The value as the AI read it; once amended, the AI value struck through next to the correction
 *   (the original extraction always stays visible).
 * - Parameter (English and Montenegrin labels), unit, target (urban parcel / block / zone / whole
 *   plan, as the plan names it), the raw text the value was read from, confidence (low confidence
 *   flagged) and the checker's flags, page and file, the extraction run's job and cost, and the
 *   last decision's actor, time and note, with the item's audit trail on demand.
 * - The staged payload: the value as the document printed it and what the contract made of it
 *   (the normalisation rules, a floor count, the land-use class, the table cell).
 * - Approve (Enter), Amend (E): an editor typed per parameter — a number with its unit (FAR,
 *   coverage %, heights, areas), the plan's floor notation ("P+5+Pk"), a land-use designation from
 *   the wordings the document already uses (or typed), free text otherwise — and a note, required;
 *   the API checks the correction with the extraction contract's rules, and a number outside the
 *   field's usual range asks "keep it anyway?" before it is saved. Reject (R): the reason,
 *   required. Esc closes an editor, Ctrl+Enter saves it. One decision per item: there is no bulk
 *   approval (100 % of items reviewed, each against its page).
 */
import { useEffect, useRef, useState, type ReactNode } from "react";

import { relativeTime, utcStamp } from "@/lib/admin/format";
import {
  canDecide,
  editorFor,
  editorStart,
  entityName,
  flagWords,
  formatValue,
  isLowConfidence,
  itemTitle,
  parseCorrection,
  payloadLines,
  statusChip,
  targetLabel,
  unitChoices,
  type Editor,
} from "@/lib/admin/review";
import { historyAction, optionsAction } from "@/lib/admin/review-actions";
import type { AuditPage, ReviewItem, ReviewOptions } from "@/lib/api/types";

import { StatusChip } from "../parts";

export type EditorMode = "amend" | "reject" | null;

/** Why the API refused a decision; `code` names the contract rule a correction broke. */
export interface Refusal {
  message: string;
  code?: string;
}

export interface Submit {
  amend: (correction: { value: number | string; unit: string | null; note: string; confirm?: boolean }) => Promise<Refusal | null>;
  reject: (note: string) => Promise<Refusal | null>;
}

function Fact({ label, children, wide }: { label: string; children: ReactNode; wide?: boolean }) {
  return (
    <div className={wide ? "rfact wide" : "rfact"}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function money(eur: number | null | undefined): string | null {
  if (eur == null) return null;
  return eur > 0 && eur < 0.01 ? "< €0.01" : `€${eur.toFixed(2)}`;
}

function AmendEditor({
  item,
  editor,
  onSubmit,
  onCancel,
}: {
  item: ReviewItem;
  editor: Editor;
  onSubmit: Submit["amend"];
  onCancel: () => void;
}) {
  const start = editorStart(item);
  const [value, setValue] = useState(start);
  const [unit, setUnit] = useState<string | null>(editor.kind === "number" ? editor.unit : null);
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [options, setOptions] = useState<ReviewOptions | null>(null);
  const [other, setOther] = useState(false);
  // the API said the number is outside the field's usual range: keep it only when confirmed
  const [unusual, setUnusual] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const first = useRef<HTMLInputElement & HTMLSelectElement>(null);

  useEffect(() => {
    first.current?.focus();
    first.current?.select?.();
  }, [other]);

  useEffect(() => {
    if (editor.kind !== "choice") return;
    let cancelled = false;
    void optionsAction(item.source.document_id, editor.field).then((result) => {
      if (cancelled) return;
      if (result.ok) {
        setOptions(result.data);
        setOther(!result.data.values.some((o) => o.value === start));
      } else {
        setOther(true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [editor, item.source.document_id, start]);

  const save = async () => {
    const parsed = parseCorrection(editor, value, unit);
    if (!parsed.ok) {
      setMessage(parsed.error);
      return;
    }
    if (!note.trim()) {
      setMessage("Add a note: what was wrong with the extracted value.");
      return;
    }
    const confirm = confirmed && unusual === `${parsed.value}`;
    setSaving(true);
    const refused = await onSubmit({ value: parsed.value, unit: parsed.unit, note, confirm });
    setSaving(false);
    if (!refused) return;
    setMessage(refused.message);
    if (refused.code === "out_of_range") {
      setUnusual(`${parsed.value}`);
      setConfirmed(false);
    }
  };

  const keys = (e: React.KeyboardEvent) => {
    e.stopPropagation(); // typing is not a queue shortcut
    if (e.key === "Escape") onCancel();
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void save();
  };

  let input: ReactNode;
  if (editor.kind === "choice" && !other) {
    input = (
      <select
        ref={first}
        className="rinput"
        aria-label="Corrected designation"
        value={value}
        onChange={(e) => (e.target.value === "__other" ? setOther(true) : setValue(e.target.value))}
        onKeyDown={keys}
      >
        {!options && <option value={value}>Loading the document&apos;s designations…</option>}
        {options?.values.map((o) => (
          <option key={o.value} value={o.value}>
            {o.value} · {o.count}×
          </option>
        ))}
        <option value="__other">Other (type it)…</option>
      </select>
    );
  } else {
    input = (
      <input
        ref={first}
        className="rinput"
        aria-label="Corrected value"
        inputMode={editor.kind === "number" ? "decimal" : undefined}
        value={value}
        placeholder={editor.kind === "floors" ? "P+5+Pk" : editor.kind === "number" ? "2.5" : "As printed in the plan"}
        onChange={(e) => {
          setValue(e.target.value);
          setUnusual(null);
        }}
        onKeyDown={keys}
      />
    );
  }

  return (
    <div className="reditor" role="group" aria-label="Correct the value">
      <div className="rrow">
        {input}
        {editor.kind === "number" && (
          <select className="runit" aria-label="Unit" value={unit ?? ""} onChange={(e) => setUnit(e.target.value || null)} onKeyDown={keys}>
            {unitChoices(editor.unit).map((u) => (
              <option key={u ?? "none"} value={u ?? ""}>
                {u ?? "no unit"}
              </option>
            ))}
          </select>
        )}
      </div>
      {editor.kind === "floors" && <div className="rhint">The plan&apos;s floor notation: P ground, S / Po basement, Pk attic.</div>}
      {editor.kind === "choice" && other && options && options.values.length > 0 && (
        <button type="button" className="rlink" onClick={() => setOther(false)}>
          ← pick from the document&apos;s designations
        </button>
      )}
      <textarea
        className="rinput rnote"
        rows={2}
        aria-label="Reviewer note"
        placeholder="Note (required): what was wrong, e.g. “table 3 says P+5+Pk, the AI read P+5”"
        value={note}
        maxLength={2000}
        onChange={(e) => setNote(e.target.value)}
        onKeyDown={keys}
      />
      {message && <div className="rmsg">{message}</div>}
      {unusual && (
        <label className="rconfirmbox">
          <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} /> The plan really says{" "}
          <b className="mono">{unusual}</b>: keep it
        </label>
      )}
      <div className="rbtns">
        <button type="button" className="abtn sm" disabled={saving || (unusual != null && !confirmed)} onClick={() => void save()}>
          {saving ? "Saving…" : "Save correction"}
        </button>
        <button type="button" className="abtn sm ghost" onClick={onCancel}>
          Cancel
        </button>
        <span className="rkeys mono">Ctrl+Enter save · Esc cancel</span>
      </div>
    </div>
  );
}

function RejectEditor({ onSubmit, onCancel }: { onSubmit: Submit["reject"]; onCancel: () => void }) {
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => area.current?.focus(), []);
  const save = async () => {
    if (!note.trim()) {
      setMessage("Give the reason: it goes to the audit log with the decision.");
      return;
    }
    setSaving(true);
    const refused = await onSubmit(note);
    setSaving(false);
    if (refused) setMessage(refused.message);
  };
  return (
    <div className="reditor" role="group" aria-label="Reject the value">
      <textarea
        ref={area}
        className="rinput rnote"
        rows={3}
        aria-label="Reason"
        placeholder="Reason (required): e.g. “the value belongs to UP 13, not UP 12”"
        value={note}
        maxLength={2000}
        onChange={(e) => setNote(e.target.value)}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Escape") onCancel();
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void save();
        }}
      />
      {message && <div className="rmsg">{message}</div>}
      <div className="rbtns">
        <button type="button" className="abtn sm" disabled={saving} onClick={() => void save()}>
          {saving ? "Saving…" : "Reject value"}
        </button>
        <button type="button" className="abtn sm ghost" onClick={onCancel}>
          Cancel
        </button>
        <span className="rkeys mono">Ctrl+Enter save · Esc cancel</span>
      </div>
    </div>
  );
}

function History({ itemId }: { itemId: number }) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<AuditPage | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void historyAction(itemId).then((result) => {
      if (cancelled) return;
      if (result.ok) setPage(result.data);
      else setFailed(true);
    });
    return () => {
      cancelled = true;
    };
  }, [open, itemId]);
  if (!open) {
    return (
      <button type="button" className="rlink" onClick={() => setOpen(true)}>
        Audit trail of this item
      </button>
    );
  }
  if (failed) return <div className="rhint">The history could not be loaded.</div>;
  if (!page) return <div className="rhint">Loading the history…</div>;
  if (!page.items.length) return <div className="rhint">No decision recorded yet.</div>;
  return (
    <ul className="rhistory">
      {page.items.map((e) => (
        <li key={e.id}>
          <span className="mono">{utcStamp(e.created_at)}</span> {e.action.replace("review.", "")} · {e.actor}
          {e.note && <span className="rhnote"> — {e.note}</span>}
        </li>
      ))}
    </ul>
  );
}

export function ReviewDetail({
  item,
  mode,
  onMode,
  onApprove,
  submit,
}: {
  item: ReviewItem;
  mode: EditorMode;
  onMode: (mode: EditorMode) => void;
  onApprove: () => void;
  submit: Submit;
}) {
  const chip = statusChip(item);
  const open = canDecide(item);
  const editor = editorFor(item);
  const unit = item.effective.unit ?? item.field_unit ?? null;
  const low = isLowConfidence(item);
  const confidence = item.source.confidence;
  const run = item.run;

  return (
    <div className="rdetail">
      <div className="rhead">
        <div className="reyebrow mono">
          {entityName(item.target.entity_type)} · {item.source.document_name}
        </div>
        <h2 className="rtitle">{itemTitle(item)}</h2>
        <div className="rvalues">
          {item.status === "amended" && item.amended ? (
            <>
              <span className="rai">
                <span className="rlab mono">AI</span>
                <s>{formatValue(item.extracted, item.value_type)}</s>
              </span>
              <span className="rarrow" aria-hidden="true">
                →
              </span>
              <span className="rextract rfixed">{formatValue(item.amended, item.value_type)}</span>
            </>
          ) : (
            <span className={item.status === "rejected" ? "rextract rrejected" : "rextract"}>
              {formatValue(item.extracted, item.value_type)}
            </span>
          )}
          <StatusChip tone={chip.tone}>{chip.label}</StatusChip>
          {low && (
            <span className="rlow" title="The extractor was not sure: check this one closely">
              ⚑ low confidence
            </span>
          )}
        </div>
        <div className="rlast">
          {item.reviewed_by && item.reviewed_at ? (
            <>
              {chip.label} by <b>{item.reviewed_by}</b> · <span title={utcStamp(item.reviewed_at)}>{relativeTime(item.reviewed_at)}</span>
              {item.review_note && <span className="rnoteline">“{item.review_note}”</span>}
            </>
          ) : (
            <>Not decided yet — the value is not on the map until it is approved and published.</>
          )}
        </div>
      </div>

      {open ? (
        <div className="racts">
          <button type="button" className="abtn" onClick={onApprove} disabled={item.status === "approved"}>
            Approve <span className="rkbd">Enter</span>
          </button>
          <button type="button" className={mode === "amend" ? "abtn" : "abtn ghost"} onClick={() => onMode(mode === "amend" ? null : "amend")}>
            Amend <span className="rkbd">E</span>
          </button>
          <button type="button" className={mode === "reject" ? "abtn" : "abtn ghost"} onClick={() => onMode(mode === "reject" ? null : "reject")}>
            Reject <span className="rkbd">R</span>
          </button>
        </div>
      ) : (
        <div className="rhint rclosed">
          {item.published ? "Published: this value is on the map; a change goes through a new extraction." : "A newer reading replaced this item."}
        </div>
      )}
      {open && mode === "amend" && (
        <AmendEditor key={`amend-${item.id}`} item={item} editor={editor} onSubmit={submit.amend} onCancel={() => onMode(null)} />
      )}
      {open && mode === "reject" && <RejectEditor key={`reject-${item.id}`} onSubmit={submit.reject} onCancel={() => onMode(null)} />}

      <dl className="rfacts">
        <Fact label="Parameter">
          {item.label_en}
          {item.label_me !== item.label_en && <span className="rsub"> · {item.label_me}</span>}
        </Fact>
        <Fact label="Unit">{unit ?? (item.value_type === "text" ? "text" : "none (a ratio)")}</Fact>
        <Fact label="Target">
          {entityName(item.target.entity_type)} {targetLabel(item.target)}
          {item.target.block_ref && item.target.entity_type === "urban_parcel" && <span className="rsub"> · block {item.target.block_ref}</span>}
          {item.target.zone_name && item.target.entity_type !== "zone" && <span className="rsub"> · {item.target.zone_name}</span>}
          {!item.target.matched && <span className="rsub"> · not matched to a geometry (cannot publish)</span>}
        </Fact>
        <Fact label="Source">
          {item.source.document_name} · p.{item.source.page ?? "—"}
          {item.source.file_name && <span className="rsub"> · {item.source.file_name}</span>}
          {item.source.note && <span className="rsub"> · {item.source.note}</span>}
        </Fact>
        <Fact label="Read from" wide>
          {item.source.raw_text ? <q className="rraw">{item.source.raw_text}</q> : <span className="rsub">no text recorded</span>}
        </Fact>
        {payloadLines(item.payload).map((line) => (
          <Fact key={line.label} label={line.label} wide={line.label === "Table cell" || line.label === "Normalised"}>
            <span className={line.label === "As printed" ? "mono" : undefined}>{line.text}</span>
          </Fact>
        ))}
        <Fact label="Confidence">
          {confidence != null ? `${Math.round(confidence * 100)}%` : "—"}
          {item.source.extraction_method && <span className="rsub"> · read as {item.source.extraction_method}</span>}
        </Fact>
        <Fact label="Checks">
          {(item.flags ?? []).length ? (item.flags ?? []).map(flagWords).join(" · ") : <span className="rsub">nothing flagged</span>}
        </Fact>
        <Fact label="Extraction" wide>
          {run ? (
            <>
              job <span className="mono">#{run.job_id ?? "—"}</span> · run <span className="mono">#{run.id}</span>
              {money(run.estimated_cost_eur) && <> · run cost <span className="mono">{money(run.estimated_cost_eur)}</span></>}
              {run.items_written != null && <span className="rsub"> for {run.items_written} items</span>}
              {run.model_version && <span className="rsub"> · {run.model_version}</span>}
            </>
          ) : (
            <span className="rsub">{item.extracted_by} (no extraction run)</span>
          )}
        </Fact>
        {item.previous && (
          <Fact label="Previous reading" wide>
            {formatValue(item.previous.value, item.value_type)} <span className="rsub">({item.previous.status}; this reading is {item.change})</span>
          </Fact>
        )}
        <Fact label="History" wide>
          <History key={item.id} itemId={item.id} />
        </Fact>
      </dl>
    </div>
  );
}
