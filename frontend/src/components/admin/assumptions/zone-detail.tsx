"use client";

/**
 * A district's details under its row in Financial assumptions: the absolute low / high bounds per
 * rate (blank = the range ±) and the notes of the draft; and the version history with each version's status
 * (the one that applies today is Live), author, date and a diff against the previous version.
 */
import { useState } from "react";

import {
  dayLabel,
  diffVersions,
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
      <History row={row} today={today} timezone={timezone} />
    </div>
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
