"use client";

/**
 * Group 2, "Market data & feasibility", under the planning parameters of the urban parcel panel
 * (wireframe `marketHTML`). The POC has no subscription or paywall: the figures are always shown.
 * The ROI hero, range rows (land value, construction, market value, profit: expected value, a
 * 6 px bar with the expected marker, low / expected / high), design & documentation as a plain
 * range row, saleable area (deterministic), the assumption sandbox (`assumption-sandbox.tsx`),
 * the engine strip and the disclaimer (the "Unlock full market data" intent sits in the panel's
 * button stack, `ParcelCtas`). Figures are the payload's `feasibility` block (the shared engine's
 * output) or, once the visitor edits an assumption, the shared engine package's
 * `recalculate(engine.inputs, edits)` run in the browser on every change (`lib/assumptions.ts`;
 * no formula here, no request: the two engines are held equal by the shared fixtures and the
 * cross-engine test). A
 * figure the engine cannot calculate says "cannot calculate — <reason>" and the others still
 * show. Never a single money figure, never a made-up range. `financials_viewed` fires once per
 * open of a parcel when the section is on screen, never per slider move.
 */
import { useEffect, useMemo, useRef } from "react";

import { useTrack } from "@/lib/analytics/react";
import type { EventProperties } from "@/lib/analytics/tracker";
import type { UrbanPanel } from "@/lib/api/types";
import { defaultsOf, editErrors, recalculateFeasibility } from "@/lib/assumptions";
import { formatEur, formatPct } from "@/lib/format";
import { pickLang, useLang, useT, type Lang, type Translate } from "@/lib/i18n";
import { translate } from "@/lib/i18n/strings";
import { useShell } from "@/lib/store";

import { ENGINE_LABEL, EngineModal } from "../shell/engine-modal";
import { Disclaimer } from "../ui/disclaimer";
import { AssumptionSandbox } from "./assumption-sandbox";
import { formatArea } from "./parcel-parts";

type Feasibility = NonNullable<UrbanPanel["feasibility"]>;
type Figure = Feasibility["fields"][number];

/** Where the expected value sits between low and high, in % of the bar (the mock's marker). */
export function markerPosition(f: { low: number | null; expected: number | null; high: number | null }): number {
  if (f.low == null || f.expected == null || f.high == null || f.high === f.low) return 50;
  return Math.max(0, Math.min(100, Math.round(((f.expected - f.low) / (f.high - f.low)) * 100)));
}

const english: Translate = (key, vars) => translate("en", key, vars);

/** "cannot calculate — no market data for zone Centar" */
export function cannotText(f: Figure, t: Translate = english, lang: Lang = "en"): string {
  return f.reason_en ? t("mkt.cannotReason", { reason: pickLang(lang, f.reason_en, f.reason_me) }) : t("mkt.cannot");
}

function ok(f: Figure | undefined): f is Figure & { low: number; expected: number; high: number } {
  return !!f && f.status === "ok" && f.low != null && f.expected != null && f.high != null;
}

function CannotRow({ label, field }: { label: string; field: Figure | undefined }) {
  const t = useT();
  const { lang } = useLang();
  return (
    <div className="prow">
      <span className="pk">{label}</span>
      <span className="pv" style={{ fontSize: "11.5px", whiteSpace: "normal", color: "var(--ink-2)" }}>
        {field ? cannotText(field, t, lang) : t("mkt.cannot")}
      </span>
    </div>
  );
}

const LABELS_LINE = {
  display: "flex",
  justifyContent: "space-between",
  fontFamily: "var(--mono)",
  fontSize: "9.5px",
  color: "var(--ink-2)",
  opacity: 0.7,
  marginTop: 3,
} as const;

/** Expected value, 6 px bar with the expected marker, low / expected / high underneath. */
function RangeRow({ label, field }: { label: string; field: Figure | undefined }) {
  const t = useT();
  if (!ok(field)) return <CannotRow label={label} field={field} />;
  return (
    <div className="prow" style={{ flexDirection: "column", alignItems: "stretch", borderBottom: "1px dashed var(--paper-2)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="pk">{label}</span>
        <span className="pv">{formatEur(field.expected)}</span>
      </div>
      <div className="rangebar" role="img" aria-label={t("mkt.rangeAria", { low: formatEur(field.low), high: formatEur(field.high) })}>
        <div className="fill" style={{ left: "12%", right: "12%" }} />
        <div className="mark" style={{ left: `${markerPosition(field)}%` }} />
      </div>
      <div style={LABELS_LINE}>
        <span>{formatEur(field.low)}</span>
        <span>{t("mkt.expected")}</span>
        <span>{formatEur(field.high)}</span>
      </div>
    </div>
  );
}

/** A money range without the bar (design & documentation): expected, low — high underneath. */
function PlainRangeRow({ label, field }: { label: string; field: Figure | undefined }) {
  const t = useT();
  if (!ok(field)) return <CannotRow label={label} field={field} />;
  return (
    <div className="prow" style={{ flexDirection: "column", alignItems: "stretch" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="pk">{label}</span>
        <span className="pv">{formatEur(field.expected)}</span>
      </div>
      <div style={LABELS_LINE}>
        <span>{formatEur(field.low)}</span>
        <span>{t("mkt.rangeWord")}</span>
        <span>{formatEur(field.high)}</span>
      </div>
    </div>
  );
}

function EngineStrip({ marketSource }: { marketSource?: string | null }) {
  const openModal = useShell((s) => s.openModal);
  const t = useT();
  return (
    <button
      type="button"
      className="enginestrip"
      style={{ width: "100%", textAlign: "left" }}
      onClick={() => openModal({ label: ENGINE_LABEL, wide: true, content: <EngineModal marketSource={marketSource} /> })}
    >
      <span className="ei">
        <svg width="15" height="15" viewBox="0 0 20 20" fill="none" aria-hidden>
          <path d="M3 17V11l6-6 6 6M10 3h7v7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
      <span className="et">{t("mkt.engine")}</span>
      <span className="ea">{t("mkt.formulas")}</span>
    </button>
  );
}

function Figures({ data, ids }: { data: UrbanPanel; ids: EventProperties }) {
  const f = data.feasibility;
  const engine = data.engine;
  const track = useTrack();
  const t = useT();
  const { lang } = useLang();
  const edits = useShell((s) => s.assumptionEdits);
  const rootRef = useRef<HTMLDivElement>(null);
  const idsKey = JSON.stringify(ids);

  const errors = useMemo(() => editErrors(edits, defaultsOf(data.assumptions)), [edits, data.assumptions]);
  const valid = Object.keys(errors).length === 0;
  // the figures on screen: the payload's, or the shared engine's for the visitor's edits
  const view = useMemo(() => {
    if (!f) return null;
    if (!engine || !valid) return { fields: f.fields, cost_rows: f.cost_rows };
    return recalculateFeasibility(f, engine, edits);
  }, [f, engine, edits, valid]);

  // financials_viewed once the figures are actually on screen
  useEffect(() => {
    const node = rootRef.current;
    if (!node || !f) return;
    const props = JSON.parse(idsKey) as EventProperties;
    if (typeof IntersectionObserver === "undefined") {
      track("financials_viewed", props);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          track("financials_viewed", props);
          observer.disconnect();
        }
      },
      { threshold: 0.25 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [track, idsKey, f]);

  if (!f || !view) {
    return (
      <p className="panelnote">
        {data.coverage_note_en ? pickLang(lang, data.coverage_note_en, data.coverage_note_me) : t("mkt.needsPlan")}
      </p>
    );
  }
  const byKey = new Map([...view.fields, ...view.cost_rows].map((r) => [r.key, r]));
  const roi = byKey.get("roi_pct");
  const saleable = byKey.get("saleable_area_m2");

  return (
    <div ref={rootRef}>
      <div className="roihero">
        <div className="rlab">{t("mkt.roi")}</div>
        {ok(roi) ? (
          <>
            <div className="rval">{formatPct(roi.expected)}</div>
            <div className="rrange">
              {t("mkt.range", { low: formatPct(roi.low), high: formatPct(roi.high), expected: formatPct(roi.expected) })}
            </div>
          </>
        ) : (
          <>
            <div className="rval">—</div>
            <div className="rrange">{roi ? cannotText(roi, t, lang) : t("mkt.cannot")}</div>
          </>
        )}
      </div>
      <div style={{ height: 12 }} />
      <RangeRow label={t("mkt.land")} field={byKey.get("land_value_eur")} />
      <RangeRow label={t("mkt.construction")} field={byKey.get("construction_cost_eur")} />
      <PlainRangeRow label={t("mkt.design")} field={byKey.get("design_documentation_eur")} />
      <RangeRow label={t("mkt.marketValue")} field={byKey.get("revenue_eur")} />
      {ok(saleable) ? (
        <div className="prow">
          <span className="pk">{t("mkt.saleable")}</span>
          <span className="pv">
            {formatArea(saleable.expected)}
            <span className="u">m²</span>
          </span>
        </div>
      ) : (
        <CannotRow label={t("mkt.saleable")} field={saleable} />
      )}
      <RangeRow label={t("mkt.profit")} field={byKey.get("profit_eur")} />
      <AssumptionSandbox data={data} errors={errors} />
      <EngineStrip marketSource={data.market_inputs?.source} />
      <Disclaimer approved={f.disclaimer_status === "client_approved"} en={f.disclaimer_en} me={f.disclaimer_me} />
    </div>
  );
}

export function MarketSection({ data, ids }: { data: UrbanPanel; ids: EventProperties }) {
  const t = useT();
  return (
    <div className="sect">
      <div className="secthead">
        <span className="lbl">{t("sect.market")}</span>
      </div>
      <Figures data={data} ids={ids} />
    </div>
  );
}
