"use client";

/**
 * "◐ Test your own assumptions" (wireframe `marketHTML` `.assum`): three range inputs —
 * construction €/m², sale price €/m², saleable % — starting from the zone's defaults in the
 * payload's `assumptions` (never hard-coded), each marked "your assumption" or "default", the
 * other rates in use and their source, and "Reset to defaults". Edits go to the store
 * (`assumptionEdits`, kept for the tab's session); the market section recalculates from them.
 * An edit back on the default value stops being an edit, so Reset and "slide back" agree.
 */
import { useId } from "react";

import {
  SLIDERS,
  defaultsOf,
  fromSlider,
  hasEdits,
  sliderBounds,
  toSlider,
  type EditKey,
} from "@/lib/assumptions";
import type { UrbanPanel } from "@/lib/api/types";
import { formatDate, formatEur } from "@/lib/format";
import { useShell } from "@/lib/store";

function show(key: EditKey, value: number): string {
  return key === "saleable_share" ? `${Math.round(value * 100)}%` : formatEur(value);
}

export function AssumptionSandbox({
  data,
  errors,
}: {
  data: UrbanPanel;
  errors: Partial<Record<EditKey, string>>;
}) {
  const id = useId();
  const edits = useShell((s) => s.assumptionEdits);
  const setAssumptionEdit = useShell((s) => s.setAssumptionEdit);
  const resetAssumptions = useShell((s) => s.resetAssumptions);
  const a = data.assumptions;
  const defaults = defaultsOf(a);
  const marketAvailable = data.market_inputs?.available !== false && !!data.engine;
  const edited = hasEdits(edits);
  const sourceDate = a?.market_source_date ? formatDate(a.market_source_date) : null;
  const versionDate = a?.data_version_date ? formatDate(a.data_version_date) : null;

  return (
    <div className="assum">
      <div className="ah">◐ Test your own assumptions</div>
      {SLIDERS.map((slider) => {
        const def = defaults[slider.key];
        const isEdited = edits[slider.key] !== undefined;
        const value = isEdited ? edits[slider.key]! : def;
        const bounds = sliderBounds(slider, def);
        const inputId = `${id}-${slider.key}`;
        const error = errors[slider.key];
        const clamped = value == null ? bounds.min : Math.min(bounds.max, Math.max(bounds.min, toSlider(slider.key, value)));
        return (
          <div key={slider.key}>
            <div className="arow">
              <label htmlFor={inputId}>
                {slider.label}
                <span className={isEdited ? "atag yours" : "atag"}>{isEdited ? "your assumption" : "default"}</span>
              </label>
              <input
                id={inputId}
                type="range"
                min={bounds.min}
                max={bounds.max}
                step={slider.step}
                value={clamped}
                disabled={!marketAvailable || value == null}
                aria-invalid={error ? true : undefined}
                aria-valuetext={value != null ? show(slider.key, value) : "not available"}
                onChange={(e) => {
                  const next = fromSlider(slider.key, Number(e.target.value));
                  // back on the default value: no longer an edit
                  setAssumptionEdit(slider.key, def != null && next === def ? undefined : next);
                }}
              />
              <span className="av">{value != null ? show(slider.key, value) : "—"}</span>
            </div>
            {error && (
              <div className="aerr" role="alert">
                {error}
              </div>
            )}
          </div>
        );
      })}
      {!marketAvailable && <div className="assumsrc">Needs market data for this zone.</div>}
      <div className="assumsrc">
        {a?.land_rate_eur_m2 != null && `Land ${formatEur(a.land_rate_eur_m2)}/m²`}
        {a?.design_documentation_eur_m2 != null && ` · design & documentation ${formatEur(a.design_documentation_eur_m2)}/m²`}
        {(a?.land_rate_eur_m2 != null || a?.design_documentation_eur_m2 != null) && <br />}
        {a?.market_source
          ? `Market data: ${a.market_source}${sourceDate ? ` · ${sourceDate}` : ""}`
          : "No market data for this zone"}
        <br />
        Formula {a?.formula_version} · data version {a?.data_version}
        {versionDate ? ` · ${versionDate}` : ""}
      </div>
      <button
        type="button"
        className="assumreset"
        aria-disabled={!edited}
        onClick={() => {
          if (edited) resetAssumptions();
        }}
      >
        Reset to defaults
      </button>
    </div>
  );
}
