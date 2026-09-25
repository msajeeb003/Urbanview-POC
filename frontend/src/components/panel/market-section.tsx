"use client";

/**
 * Group 2, "Market data & feasibility" (paid badge), under the planning parameters of the urban
 * parcel panel; it sits behind the market-data boundary (`useShell().marketUnlocked`).
 *
 * - **Locked** (wireframe `lockedMarketHTML`): the note, the seven parameters with their plain
 *   definitions and units and a LOCKED chip instead of a value, the engine strip, and the intent
 *   card "Unlock the figures" whose "Unlock →" records `market_data_interest` and opens
 *   "Choose your access".
 * - **Unlocked** (wireframe `marketHTML`): the ROI hero, range rows (land value, construction,
 *   market value, profit: expected value, a 6 px bar with the expected marker, low / expected /
 *   high), design & documentation as a plain range row, saleable area (deterministic), the
 *   assumption sandbox (`assumption-sandbox.tsx`), the engine strip, the disclaimer and the "I want
 *   market data updates" intent. Figures are the payload's `feasibility` block (the shared
 *   engine's output) or, once the visitor edits an assumption, the shared engine package's
 *   `recalculate(engine.inputs, edits)` run in the browser on every change (`lib/assumptions.ts`;
 *   no formula here, no round trip). A settled set of edits is checked against
 *   `POST /v1/feasibility` a second later; a difference is a console warning. A figure the engine
 *   cannot calculate says "cannot calculate — <reason>" and the others still show. Never a single
 *   money figure, never a made-up range. `financials_viewed` fires once per open of a parcel
 *   when the unlocked section is on screen, never per slider move.
 */
import { useEffect, useMemo, useRef } from "react";

import { useTrack } from "@/lib/analytics/react";
import type { EventProperties } from "@/lib/analytics/tracker";
import { api } from "@/lib/api/endpoints";
import type { UrbanPanel } from "@/lib/api/types";
import {
  defaultsOf,
  differences,
  editErrors,
  hasEdits,
  recalculateFeasibility,
  toRequestAssumptions,
} from "@/lib/assumptions";
import { formatEur, formatPct } from "@/lib/format";
import { useShell } from "@/lib/store";

import { ACCESS_LABEL, AccessModal } from "../shell/access-modal";
import { ENGINE_LABEL, EngineModal } from "../shell/engine-modal";
import { Badge } from "../ui/badge";
import { Cta } from "../ui/cta";
import { Disclaimer } from "../ui/disclaimer";
import { IconLockSmall } from "../ui/icons";
import { AssumptionSandbox } from "./assumption-sandbox";
import { formatArea } from "./parcel-parts";

type Feasibility = NonNullable<UrbanPanel["feasibility"]>;
type Figure = Feasibility["fields"][number];

/** The wireframe's locked parameter list (`MARKET_PARAMS`): name, plain definition, unit. */
export const LOCKED_PARAMS: [string, string, string][] = [
  ["Estimated land value", "urban parcel area × zone land rate", "€"],
  ["Construction cost", "gross floor area × build rate", "€"],
  ["Design & documentation", "gross floor area × design rate", "€"],
  ["Estimated market value", "saleable area × sale rate", "€"],
  ["Estimated saleable area", "gross floor area × saleable share", "m²"],
  ["Potential profit", "market value − total cost", "€"],
  ["Return on investment", "profit ÷ total cost, as a range", "%"],
];

/** Provisional copy (ticket wording): the updates feature does not exist yet. */
const UPDATES_NOTED = "Coming soon — noted.";

/** Where the expected value sits between low and high, in % of the bar (the mock's marker). */
export function markerPosition(f: { low: number | null; expected: number | null; high: number | null }): number {
  if (f.low == null || f.expected == null || f.high == null || f.high === f.low) return 50;
  return Math.max(0, Math.min(100, Math.round(((f.expected - f.low) / (f.high - f.low)) * 100)));
}

/** "cannot calculate — no market data for zone Centar" */
export function cannotText(f: Figure): string {
  return f.reason_en ? `cannot calculate — ${f.reason_en}` : "cannot calculate";
}

function ok(f: Figure | undefined): f is Figure & { low: number; expected: number; high: number } {
  return !!f && f.status === "ok" && f.low != null && f.expected != null && f.high != null;
}

function CannotRow({ label, field }: { label: string; field: Figure | undefined }) {
  return (
    <div className="prow">
      <span className="pk">{label}</span>
      <span className="pv" style={{ fontSize: "11.5px", whiteSpace: "normal", color: "var(--ink-2)" }}>
        {field ? cannotText(field) : "cannot calculate"}
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
  if (!ok(field)) return <CannotRow label={label} field={field} />;
  return (
    <div className="prow" style={{ flexDirection: "column", alignItems: "stretch", borderBottom: "1px dashed var(--paper-2)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="pk">{label}</span>
        <span className="pv">{formatEur(field.expected)}</span>
      </div>
      <div className="rangebar" role="img" aria-label={`${formatEur(field.low)} to ${formatEur(field.high)}`}>
        <div className="fill" style={{ left: "12%", right: "12%" }} />
        <div className="mark" style={{ left: `${markerPosition(field)}%` }} />
      </div>
      <div style={LABELS_LINE}>
        <span>{formatEur(field.low)}</span>
        <span>expected</span>
        <span>{formatEur(field.high)}</span>
      </div>
    </div>
  );
}

/** A money range without the bar (design & documentation): expected, low — high underneath. */
function PlainRangeRow({ label, field }: { label: string; field: Figure | undefined }) {
  if (!ok(field)) return <CannotRow label={label} field={field} />;
  return (
    <div className="prow" style={{ flexDirection: "column", alignItems: "stretch" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="pk">{label}</span>
        <span className="pv">{formatEur(field.expected)}</span>
      </div>
      <div style={LABELS_LINE}>
        <span>{formatEur(field.low)}</span>
        <span>range</span>
        <span>{formatEur(field.high)}</span>
      </div>
    </div>
  );
}

function EngineStrip({ marketSource }: { marketSource?: string | null }) {
  const openModal = useShell((s) => s.openModal);
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
      <span className="et">Deterministic engine — how every figure is calculated</span>
      <span className="ea">Formulas →</span>
    </button>
  );
}

function Locked({ ids, marketSource }: { ids: EventProperties; marketSource?: string | null }) {
  const openModal = useShell((s) => s.openModal);
  const track = useTrack();
  return (
    <>
      <p style={{ fontSize: "11.5px", color: "var(--ink-2)", margin: "-4px 0 10px", lineHeight: 1.5 }}>
        These are the parameters UrbanView calculates for this urban parcel. The figures behind them are part of the
        market-data subscription.
      </p>
      <div className="lockedlist">
        {LOCKED_PARAMS.map(([name, definition, unit]) => (
          <div key={name} className="lrow">
            <span className="lk2">
              {name}
              <span className="ld">{definition}</span>
            </span>
            <span className="lv">
              <span className="lockval">
                <IconLockSmall />
                LOCKED
              </span>
              <span className="lu">{unit}</span>
            </span>
          </div>
        ))}
      </div>
      <EngineStrip marketSource={marketSource} />
      <div className="lockcta">
        <div className="lki">
          <svg width="16" height="16" viewBox="0 0 18 18" fill="none" aria-hidden>
            <rect x="3" y="8" width="12" height="8" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
            <path d="M6 8V5a3 3 0 016 0v3" stroke="currentColor" strokeWidth="1.5" />
          </svg>
        </div>
        <div className="lct">
          <b>Unlock the figures</b>
          <span>Values, ranges and the assumptions sandbox.</span>
        </div>
        <button
          type="button"
          className="b"
          onClick={() => {
            track("market_data_interest", { ...ids, trigger: "unlock" });
            openModal({ label: ACCESS_LABEL, wide: true, content: <AccessModal focus="market" context={ids} /> });
          }}
        >
          Unlock →
        </button>
      </div>
    </>
  );
}

function Unlocked({ data, ids }: { data: UrbanPanel; ids: EventProperties }) {
  const f = data.feasibility;
  const engine = data.engine;
  const track = useTrack();
  const showToast = useShell((s) => s.showToast);
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

  // a settled set of edits is confirmed against the server; a difference is only a warning
  useEffect(() => {
    if (!view || !engine || !valid || !hasEdits(edits) || typeof ids.urban_parcel_id !== "number") return;
    const controller = new AbortController();
    const parcelId = ids.urban_parcel_id;
    const timer = setTimeout(async () => {
      try {
        const res = await api.feasibility(
          { parcel_id: parcelId, type: "urban", assumptions: toRequestAssumptions(edits) },
          { signal: controller.signal },
        );
        if (!res.feasibility) return;
        const diff = differences(
          [...view.fields, ...view.cost_rows],
          [...res.feasibility.fields, ...res.feasibility.cost_rows],
        );
        if (diff.length) console.warn("[feasibility] the browser's figures differ from POST /v1/feasibility", diff);
      } catch {
        // the check is for us, not for the visitor: a failed request changes nothing on screen
      }
    }, 1000);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [view, engine, valid, edits, ids.urban_parcel_id]);

  // financials_viewed once the unlocked figures are actually on screen
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
    return <p className="panelnote">{data.coverage_note_en ?? "Market figures need an adopted plan for this parcel."}</p>;
  }
  const byKey = new Map([...view.fields, ...view.cost_rows].map((r) => [r.key, r]));
  const roi = byKey.get("roi_pct");
  const saleable = byKey.get("saleable_area_m2");

  return (
    <div ref={rootRef}>
      <div className="roihero">
        <div className="rlab">Return on investment</div>
        {ok(roi) ? (
          <>
            <div className="rval">{formatPct(roi.expected)}</div>
            <div className="rrange">
              range {formatPct(roi.low)} — {formatPct(roi.high)} · expected {formatPct(roi.expected)}
            </div>
          </>
        ) : (
          <>
            <div className="rval">—</div>
            <div className="rrange">{roi ? cannotText(roi) : "cannot calculate"}</div>
          </>
        )}
        <svg className="spark" width="120" height="60" viewBox="0 0 120 60" aria-hidden>
          <path d="M0,50 C30,48 40,20 60,22 C85,24 95,6 120,4 L120,60 L0,60Z" fill="rgba(255,255,255,.14)" />
        </svg>
      </div>
      <div style={{ height: 12 }} />
      <RangeRow label="Estimated land value" field={byKey.get("land_value_eur")} />
      <RangeRow label="Construction cost" field={byKey.get("construction_cost_eur")} />
      <PlainRangeRow label="Design & documentation" field={byKey.get("design_documentation_eur")} />
      <RangeRow label="Estimated market value" field={byKey.get("revenue_eur")} />
      {ok(saleable) ? (
        <div className="prow">
          <span className="pk">Estimated saleable area</span>
          <span className="pv">
            {formatArea(saleable.expected)}
            <span className="u">m²</span>
          </span>
        </div>
      ) : (
        <CannotRow label="Estimated saleable area" field={saleable} />
      )}
      <RangeRow label="Potential profit" field={byKey.get("profit_eur")} />
      <AssumptionSandbox data={data} errors={errors} />
      <EngineStrip marketSource={data.market_inputs?.source} />
      <Disclaimer approved={f.disclaimer_status === "client_approved"} en={f.disclaimer_en} me={f.disclaimer_me} />
      <Cta
        variant="line"
        style={{ marginTop: 11 }}
        onClick={() => {
          track("market_data_interest", { ...ids, trigger: "updates" });
          showToast(UPDATES_NOTED);
        }}
      >
        I want market data updates
      </Cta>
    </div>
  );
}

export function MarketSection({ data, ids }: { data: UrbanPanel; ids: EventProperties }) {
  const unlocked = useShell((s) => s.marketUnlocked);
  return (
    <div className="sect">
      <div className="secthead">
        <span className="lbl">
          Market data &amp; feasibility <Badge tone="paid" />
        </span>
      </div>
      {unlocked ? <Unlocked data={data} ids={ids} /> : <Locked ids={ids} marketSource={data.market_inputs?.source} />}
    </div>
  );
}
