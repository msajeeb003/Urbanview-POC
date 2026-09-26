"use client";

/**
 * A district's details under its row in Financial assumptions: the absolute low / high bounds per
 * rate (blank = the range ±) and the notes of the draft; "Preview on a test parcel", which asks
 * the API for the parcel's Group 2 today and with the unsaved figures (the panel's own builders
 * and the shared engine: nothing is written); and the version history with each version's status
 * (the one that applies today is Live), author, date and a diff against the previous version.
 */
import { useState, useTransition } from "react";

import { previewAction, previewParcelsAction } from "@/lib/admin/assumption-actions";
import {
  checkDraft,
  dayLabel,
  diffVersions,
  plannedLabel,
  previewRows,
  previousVersion,
  RATE_BASIS,
  RATE_LABELS,
  RATES,
  statusChip,
  type DraftErrors,
  type RateKey,
  type SetDraft,
  type ZoneRow,
} from "@/lib/admin/assumptions";
import { utcStamp } from "@/lib/admin/format";
import type { AssumptionsPreview, PreviewParcel } from "@/lib/api/types";

import { StatusChip } from "../parts";

export function ZoneDetail({
  row,
  draft,
  errors,
  today,
  timezone,
  onRate,
  onEdit,
}: {
  row: ZoneRow;
  draft: SetDraft;
  errors: DraftErrors;
  today: string;
  timezone: string;
  onRate: (key: RateKey, field: "expected" | "low" | "high", value: string) => void;
  onEdit: (update: (d: SetDraft) => SetDraft) => void;
}) {
  return (
    <div className="fingrid">
      <section className="finsect">
        <h4>Ranges and notes</h4>
        <p className="ohint">
          Bounds give a figure its own low and high; left blank, the range ± applies. The engine pairs them pessimistically (costs high
          with revenue low).
        </p>
        <table className="fbounds">
          <thead>
            <tr>
              <th>Figure</th>
              <th>Low</th>
              <th>High</th>
            </tr>
          </thead>
          <tbody>
            {RATES.map((key) => (
              <tr key={key}>
                <td>
                  {RATE_LABELS[key]}
                  <span className="fmeta">{RATE_BASIS[key]}</span>
                  {errors[`${key}.bounds`] && <span className="fmeta ferr">{errors[`${key}.bounds`]}</span>}
                </td>
                {(["low", "high"] as const).map((field) => (
                  <td key={field}>
                    <input
                      className={`rinput fin${errors[`${key}.bounds`] ? " bad" : ""}`}
                      inputMode="decimal"
                      value={draft.rates[key][field]}
                      placeholder="range ±"
                      aria-label={`${row.name}: ${RATE_LABELS[key]} ${field}`}
                      onChange={(e) => onRate(key, field, e.target.value)}
                    />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        <textarea
          className="rinput rnote"
          rows={2}
          maxLength={2000}
          value={draft.notes}
          placeholder="Notes for the next reader (optional): what the figures are based on"
          aria-label={`${row.name}: notes`}
          onChange={(e) => onEdit((d) => ({ ...d, notes: e.target.value }))}
        />
      </section>
      <Preview row={row} draft={draft} />
      <History row={row} today={today} timezone={timezone} />
    </div>
  );
}

function Preview({ row, draft }: { row: ZoneRow; draft: SetDraft }) {
  const [parcels, setParcels] = useState<PreviewParcel[] | null>(null);
  const [parcelId, setParcelId] = useState<string>("");
  const [preview, setPreview] = useState<AssumptionsPreview | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, start] = useTransition();

  const load = () =>
    start(async () => {
      setMessage(null);
      const result = await previewParcelsAction(row.zoneId);
      if (!result.ok) {
        setMessage(result.message);
        return;
      }
      setParcels(result.items);
      if (result.items[0]) setParcelId(String(result.items[0].parcel_id));
      if (!result.items.length) setMessage("No covered parcel in this district to preview on.");
    });

  const run = () =>
    start(async () => {
      setMessage(null);
      const checked = checkDraft(draft);
      if (!checked.ok) {
        setMessage("Fix the marked figures first.");
        return;
      }
      const { land_rate, build_rate, design_rate, sale_rate, range_low_factor, range_high_factor, saleable_share } = checked.value;
      const result = await previewAction({
        parcel_id: Number(parcelId),
        zone_id: row.zoneId,
        land_rate,
        build_rate,
        design_rate,
        sale_rate,
        range_low_factor,
        range_high_factor,
        saleable_share,
      });
      if (result.ok) setPreview(result.preview);
      else setMessage(result.message);
    });

  const rows = preview ? previewRows(preview) : [];
  const liveVersion = preview?.current.market?.version?.version;
  return (
    <section className="finsect">
      <h4>Preview on a test parcel</h4>
      <p className="ohint">Group 2 as the public panel shows it today, and with the figures above. Nothing is saved.</p>
      {parcels == null ? (
        <button type="button" className="abtn sm ghost" disabled={pending} onClick={load}>
          {pending ? "Loading parcels…" : "Choose a test parcel"}
        </button>
      ) : (
        <div className="oacts">
          <select className="rinput oselect" value={parcelId} aria-label="Test parcel" onChange={(e) => setParcelId(e.target.value)}>
            {parcels.map((p) => (
              <option key={p.parcel_id} value={p.parcel_id}>
                {p.title} · {p.urban_parcel_number ? `${plannedLabel(p.urban_parcel_number)} · ${p.planned_area_m2} m²` : `${p.area_m2} m²`}
              </option>
            ))}
          </select>
          <button type="button" className="abtn sm" disabled={pending || !parcelId} onClick={run}>
            {pending ? "Calculating…" : "Preview"}
          </button>
        </div>
      )}
      {message && <div className="rmsg">{message}</div>}
      {preview && (
        <div className="fpreview">
          <div className="osub">
            {preview.title} · {preview.calculation_basis === "urban" ? "planned parcel" : "cadastral"} area {preview.basis_area_m2} m²
            {preview.zone_mismatch && " · outside this district: its own district's figures apply on the map"}
          </div>
          {!preview.covered ? (
            <div className="rmsg">{preview.coverage_note_en ?? "No adopted plan covers this parcel: the panel shows no figures."}</div>
          ) : (
            <table className="tbl otbl fprev">
              <thead>
                <tr>
                  <th>Figure</th>
                  <th>Now{liveVersion ? ` (v${liveVersion})` : ""}</th>
                  <th>With these figures</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.key} className={r.changed ? "changed" : undefined}>
                    <td>{r.label}</td>
                    <td className="mono">
                      {r.now.main}
                      {r.now.range && <span className="fmeta">{r.now.range}</span>}
                    </td>
                    <td className="mono">
                      {r.draft.main}
                      {r.draft.range && <span className="fmeta">{r.draft.range}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </section>
  );
}

function History({ row, today, timezone }: { row: ZoneRow; today: string; timezone: string }) {
  const [diffOf, setDiffOf] = useState<number | null>(null);
  if (!row.history.length) {
    return (
      <section className="finsect">
        <h4>Version history</h4>
        <p className="ohint">No version yet. Fill in the four figures and a source, then save: that is version 1.</p>
      </section>
    );
  }
  return (
    <section className="finsect">
      <h4>Version history</h4>
      <p className="ohint">
        Live = what the panel uses today ({dayLabel(today)}, {timezone.replace("_", " ")}). A later date waits as scheduled.
      </p>
      <ol className="fhistory">
        {row.history.map((set) => {
          const chip = statusChip(set.status);
          const previous = previousVersion(row.history, set);
          const lines = diffOf === set.id ? diffVersions(previous, set) : [];
          return (
            <li key={set.id}>
              <div className="fhrow">
                <b className="mono">v{set.version}</b>
                <StatusChip tone={chip.tone}>{chip.label}</StatusChip>
                <span className="osub">
                  from {dayLabel(set.applies_from)}
                  {set.effective_from !== set.applies_from ? ` (dated ${dayLabel(set.effective_from)})` : ""} · {set.created_by ?? "—"} ·{" "}
                  {utcStamp(set.created_at)}
                </span>
                <button type="button" className="abtn sm ghost" onClick={() => setDiffOf(diffOf === set.id ? null : set.id)}>
                  {diffOf === set.id ? "Hide diff" : previous ? `View diff vs v${previous.version}` : "View figures"}
                </button>
              </div>
              {diffOf === set.id && (
                <ul className="fdiff">
                  {lines.length ? (
                    lines.map((line) => (
                      <li key={line.label}>
                        <span>{line.label}</span> <s className="mono">{line.before}</s> → <b className="mono">{line.after}</b>
                      </li>
                    ))
                  ) : (
                    <li className="osub">Same figures as v{previous?.version}.</li>
                  )}
                </ul>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
