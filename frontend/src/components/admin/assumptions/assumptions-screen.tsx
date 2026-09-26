"use client";

/**
 * Financial assumptions (wireframe `adminFin`): the card "Financial assumptions — Benchmarks by
 * district" with "Save changes", one row per zone (district) with its figures as inputs: land,
 * construction, design & documentation and sale rates (€/m²), the saleable share (blank = the
 * product's 70 %), the range ± and the source note. The row's "Details" opens the absolute bounds
 * per rate and notes, a preview of Group 2 on a test parcel with the unsaved figures, and the
 * version history with a diff against the previous version.
 *
 * Saving sends every changed zone as a new version applying from the chosen date (today by
 * default; a later date schedules it and the panel keeps today's figures until then), after a
 * confirmation line. Failures keep every figure typed. Nothing is ever deleted: earlier versions
 * stay in the history.
 */
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState, useTransition } from "react";

import { ENGINE_VERSION, FORMULA_VERSION } from "@urbanview/feasibility-engine";

import { saveAssumptionsAction } from "@/lib/admin/assumption-actions";
import {
  applyNote,
  boundsText,
  checkDraft,
  dateProblem,
  dayLabel,
  draftFrom,
  isChanged,
  RATE_BASIS,
  RATE_LABELS,
  RATES,
  rangeText,
  statusChip,
  type DraftErrors,
  type RateKey,
  type SetDraft,
  type ZoneRow,
} from "@/lib/admin/assumptions";
import { useShell } from "@/lib/store";

import { AdminCard, StatusChip } from "../parts";

import { ZoneDetail } from "./zone-detail";

const SHORT: Record<RateKey, string> = {
  land: "Land €/m²",
  build: "Construction €/m²",
  design: "Design & doc. €/m²",
  sale: "Sale €/m²",
};

export function AssumptionsScreen({ rows, today, timezone }: { rows: ZoneRow[]; today: string; timezone: string }) {
  const router = useRouter();
  const showToast = useShell((s) => s.showToast);
  const [drafts, setDrafts] = useState<Record<number, SetDraft>>(() =>
    Object.fromEntries(rows.map((r) => [r.zoneId, draftFrom(r.live)])),
  );
  const [effective, setEffective] = useState(today);
  const [open, setOpen] = useState<number | null>(null);
  const [errors, setErrors] = useState<Record<number, DraftErrors>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [pending, start] = useTransition();

  const changed = useMemo(() => rows.filter((r) => isChanged(drafts[r.zoneId], r.live)), [rows, drafts]);
  const nextVersion = (row: ZoneRow) => (row.history[0]?.version ?? 0) + 1;

  const edit = (zoneId: number, update: (d: SetDraft) => SetDraft) => {
    setDrafts((all) => ({ ...all, [zoneId]: update(all[zoneId]) }));
    setConfirming(false);
    setErrors((all) => {
      if (!all[zoneId]) return all;
      const rest = { ...all };
      delete rest[zoneId];
      return rest;
    });
  };
  const setRate = (zoneId: number, key: RateKey, field: "expected" | "low" | "high", value: string) =>
    edit(zoneId, (d) => ({ ...d, rates: { ...d.rates, [key]: { ...d.rates[key], [field]: value } } }));

  const reset = (row: ZoneRow) => edit(row.zoneId, () => draftFrom(row.live));

  const review = () => {
    setMessage(null);
    const problem = dateProblem(effective, today);
    if (problem) {
      setMessage(problem);
      return;
    }
    const found: Record<number, DraftErrors> = {};
    for (const row of changed) {
      const checked = checkDraft(drafts[row.zoneId]);
      if (!checked.ok) found[row.zoneId] = checked.errors;
    }
    setErrors(found);
    const bad = Object.keys(found).map(Number);
    if (bad.length) {
      const names = rows.filter((r) => bad.includes(r.zoneId)).map((r) => r.name);
      setMessage(`Check the marked figures: ${names.join(", ")}.`);
      if (open == null || !bad.includes(open)) setOpen(bad[0]);
      return;
    }
    setConfirming(true);
  };

  const save = () =>
    start(async () => {
      const sets = changed.map((row) => {
        const checked = checkDraft(drafts[row.zoneId]);
        if (!checked.ok) throw new Error("unchecked draft");
        return { zoneName: row.name, set: { zone_id: row.zoneId, ...checked.value } };
      });
      const result = await saveAssumptionsAction(effective, sets);
      setConfirming(false);
      if (result.ok) {
        showToast(result.message);
        router.refresh();
      } else {
        setMessage(result.message);
      }
    });

  const summary = changed.map((r) => `${r.name} v${nextVersion(r)}`).join(", ");
  const isToday = effective === today;

  return (
    <AdminCard
      title="Financial assumptions"
      sub="Benchmarks by district — feed the deterministic engine. Sources: Realitica, Estitor, Monstat"
      action={
        <button
          type="button"
          className="abtn"
          disabled={!changed.length || pending}
          title={changed.length ? undefined : "Change a figure first"}
          onClick={review}
        >
          {pending ? "Saving…" : changed.length > 1 ? `Save changes (${changed.length})` : "Save changes"}
        </button>
      }
    >
      <div className="finbar">
        <label>
          Applies from
          <input
            type="date"
            className="rinput"
            value={effective}
            min={today}
            onChange={(e) => {
              setEffective(e.target.value);
              setConfirming(false);
            }}
          />
        </label>
        <span className="finnote">{dateProblem(effective, today) ?? applyNote(effective, today)}</span>
        <span className="finnote fright">
          Formula <b className="mono">{FORMULA_VERSION}</b> · engine <span className="mono">{ENGINE_VERSION}</span> ·{" "}
          <Link href="/admin/engine#changelog">changelog</Link>
        </span>
      </div>
      {message && (
        <div className="finmsg">
          <div className="rmsg">{message}</div>
        </div>
      )}
      {confirming && (
        <div className="finmsg">
          <div className="oconfirm">
            <span>
              Save {changed.length} new version{changed.length === 1 ? "" : "s"} ({summary}), applying from {dayLabel(effective)}
              {isToday ? " (today): the public panel uses them on its next load." : ": until then the panel keeps today's figures."} Earlier
              versions stay in the history.
            </span>
            <div className="rbtns">
              <button type="button" className="abtn sm" disabled={pending} onClick={save}>
                {pending ? "Saving…" : "Confirm"}
              </button>
              <button type="button" className="abtn sm ghost" onClick={() => setConfirming(false)}>
                Back
              </button>
            </div>
          </div>
        </div>
      )}
      <div className="finwrap">
        <table className="tbl fintbl">
          <thead>
            <tr>
              <th>District</th>
              {RATES.map((key) => (
                <th key={key} title={`${RATE_LABELS[key]} ${RATE_BASIS[key]}`}>
                  {SHORT[key]}
                </th>
              ))}
              <th title="Saleable share of the gross floor area; blank = the product default (70 %)">Saleable %</th>
              <th title="Low / high bounds for every figure without its own bounds">Range ±</th>
              <th>Source</th>
              <th aria-label="Details" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const draft = drafts[row.zoneId];
              const rowErrors = errors[row.zoneId] ?? {};
              const dirty = changed.includes(row);
              const isOpen = open === row.zoneId;
              return (
                <ZoneRows
                  key={row.zoneId}
                  row={row}
                  draft={draft}
                  errors={rowErrors}
                  dirty={dirty}
                  open={isOpen}
                  today={today}
                  timezone={timezone}
                  onToggle={() => setOpen(isOpen ? null : row.zoneId)}
                  onRate={(key, field, value) => setRate(row.zoneId, key, field, value)}
                  onEdit={(update) => edit(row.zoneId, update)}
                  onReset={() => reset(row)}
                />
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="finfoot">
        Each saved change is a new version: the public panel, the parcel figures and the order snapshots use the version that applies on the
        day ({timezone.replace("_", " ")} time); nothing is deleted. Figures without their own low / high bounds are widened by the range ±.
      </div>
    </AdminCard>
  );
}

function ZoneRows({
  row,
  draft,
  errors,
  dirty,
  open,
  today,
  timezone,
  onToggle,
  onRate,
  onEdit,
  onReset,
}: {
  row: ZoneRow;
  draft: SetDraft;
  errors: DraftErrors;
  dirty: boolean;
  open: boolean;
  today: string;
  timezone: string;
  onToggle: () => void;
  onRate: (key: RateKey, field: "expected" | "low" | "high", value: string) => void;
  onEdit: (update: (d: SetDraft) => SetDraft) => void;
  onReset: () => void;
}) {
  const live = row.live;
  const chip = live ? statusChip("live") : null;
  const boundsError = RATES.some((key) => errors[`${key}.bounds`]);
  return (
    <>
      <tr className={dirty ? "dirty" : undefined}>
        <td>
          <b>{row.name}</b>
          <span className="fmeta">
            {live ? (
              <>
                {chip && <StatusChip tone={chip.tone}>{`v${live.version} ${chip.label}`}</StatusChip>} since {dayLabel(live.applies_from)}
              </>
            ) : (
              "No figures yet: the panel says “no market data”"
            )}
          </span>
          {row.scheduled.map((s) => (
            <span key={s.id} className="fmeta">
              <StatusChip tone="pend">{`v${s.version} Scheduled`}</StatusChip> from {dayLabel(s.applies_from)}
            </span>
          ))}
          {dirty && <span className="fmeta fchanged">Changed — will be v{(row.history[0]?.version ?? 0) + 1}</span>}
        </td>
        {RATES.map((key) => {
          const range = live?.[`${key}_rate` as const];
          const bounds = draft.rates[key].low || draft.rates[key].high ? `${draft.rates[key].low || "?"} – ${draft.rates[key].high || "?"}` : null;
          return (
            <td key={key}>
              <span className="fcell">
                €
                <input
                  className={`rinput fin${errors[`${key}.expected`] ? " bad" : ""}`}
                  inputMode="decimal"
                  value={draft.rates[key].expected}
                  placeholder="—"
                  aria-label={`${row.name}: ${RATE_LABELS[key]}`}
                  aria-invalid={!!errors[`${key}.expected`]}
                  title={errors[`${key}.expected`] ?? RATE_BASIS[key]}
                  onChange={(e) => onRate(key, "expected", e.target.value)}
                />
              </span>
              <span className={`fmeta${errors[`${key}.bounds`] ? " ferr" : ""}`} title={range ? boundsText(range) ?? undefined : undefined}>
                {errors[`${key}.expected`] ?? errors[`${key}.bounds`] ?? (bounds ? `bounds ${bounds}` : "range ±")}
              </span>
            </td>
          );
        })}
        <td>
          <span className="fcell">
            <input
              className={`rinput fin sm${errors.saleablePct ? " bad" : ""}`}
              inputMode="decimal"
              value={draft.saleablePct}
              placeholder="70"
              aria-label={`${row.name}: saleable share %`}
              title={errors.saleablePct ?? "Blank = the product default, 70 %"}
              onChange={(e) => onEdit((d) => ({ ...d, saleablePct: e.target.value }))}
            />
            %
          </span>
          <span className={`fmeta${errors.saleablePct ? " ferr" : ""}`}>{errors.saleablePct ?? (draft.saleablePct ? "zone default" : "product default")}</span>
        </td>
        <td>
          <span className="fcell">
            −
            <input
              className={`rinput fin xs${errors.lowPct ? " bad" : ""}`}
              inputMode="decimal"
              value={draft.lowPct}
              aria-label={`${row.name}: range low %`}
              title={errors.lowPct ?? "How far below the expected figure the low end lies"}
              onChange={(e) => onEdit((d) => ({ ...d, lowPct: e.target.value }))}
            />
            / +
            <input
              className={`rinput fin xs${errors.highPct ? " bad" : ""}`}
              inputMode="decimal"
              value={draft.highPct}
              aria-label={`${row.name}: range high %`}
              title={errors.highPct ?? "How far above the expected figure the high end lies"}
              onChange={(e) => onEdit((d) => ({ ...d, highPct: e.target.value }))}
            />
            %
          </span>
          <span className={`fmeta${errors.lowPct || errors.highPct ? " ferr" : ""}`}>
            {errors.lowPct ?? errors.highPct ?? (live ? `now ${rangeText(live.range_low_factor, live.range_high_factor)}` : "")}
          </span>
        </td>
        <td>
          <input
            className={`rinput fsrc${errors.source ? " bad" : ""}`}
            value={draft.source}
            placeholder="e.g. Realitica, Estitor, Monstat"
            maxLength={200}
            aria-label={`${row.name}: source`}
            onChange={(e) => onEdit((d) => ({ ...d, source: e.target.value }))}
          />
          {errors.source && <span className="fmeta ferr">{errors.source}</span>}
        </td>
        <td className="fact">
          <button type="button" className={`abtn sm ghost${boundsError ? " flag" : ""}`} aria-expanded={open} onClick={onToggle}>
            {open ? "Close" : "Details"}
          </button>
          {dirty && (
            <button type="button" className="abtn sm ghost" onClick={onReset} title="Back to the live figures">
              Undo
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr className="findetail">
          <td colSpan={RATES.length + 5}>
            <ZoneDetail row={row} draft={draft} errors={errors} today={today} timezone={timezone} onRate={onRate} onEdit={onEdit} />
          </td>
        </tr>
      )}
    </>
  );
}
