"use client";

/**
 * "How the figures are calculated" (wireframe `openEngine`, wide modal): the formulas table
 * (name, expression, source) and the input data list, from the market
 * section's engine strip. The mock's table (engine v1.4, land value × 0.55, "banded" ROI) is
 * replaced by the client-owned formulas the shared engine implements (`@urbanview/feasibility-engine`,
 * `FORMULA_VERSION`), as the design spec's §9 allows; the market source is the zone's own.
 */
import { ENGINE_VERSION, FORMULA_VERSION } from "@urbanview/feasibility-engine";

import { ModalClose, ModalHead } from "../ui/modal";

export const ENGINE_LABEL = "How the figures are calculated";

/** The poc-1 formulas (CLAUDE.md "Formulas", client-owned; `client_validated: false` until P0 gate 3). */
const FORMULAS: [string, string, string][] = [
  ["Gross floor area (GFA)", "urban parcel area × FAR", "adopted plan"],
  ["Max coverage area", "urban parcel area × site coverage %", "adopted plan"],
  ["Saleable area", "GFA × saleable share (70%, editable)", "UrbanView assumption"],
  ["Land value", "urban parcel area × zone land rate", "market data"],
  ["Design & documentation", "GFA × design rate", "market data"],
  ["Construction cost", "GFA × build rate", "market data"],
  ["Market value", "saleable area × sale rate", "market data"],
  ["Potential profit", "market value − (land + design + construction)", "derived"],
  ["Return on investment", "profit ÷ total cost × 100, as a range", "derived"],
];

export function EngineModal({ marketSource }: { marketSource?: string | null }) {
  const inputs: [string, string][] = [
    ["Adopted planning documents", "FAR, site coverage and height per urban parcel, each with its page"],
    ["Cadastre", "parcel area: the urban parcel's first, the cadastral as fallback"],
    ["Market sources", `${marketSource ?? "no market data for this zone yet"}: land, build, design and sale rates per zone`],
    ["Your assumptions", "construction cost, sale price and saleable share, when you change them"],
  ];
  return (
    <>
      <ModalHead
        icon={
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden>
            <path d="M3 17V11l6-6 6 6M10 3h7v7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        }
        iconStyle={{ background: "var(--brand-tint)", color: "var(--brand)" }}
        eyebrow={`Engine v${ENGINE_VERSION} · formulas ${FORMULA_VERSION}`}
        title={ENGINE_LABEL}
        lead="UrbanView's formulas applied to the parameters of the adopted plan."
      />
      <div className="mbody">
        <div className="secthead" style={{ padding: "0 0 8px" }}>
          <span className="lbl">Formulas</span>
        </div>
        <div className="ftable">
          {FORMULAS.map(([name, expression, source]) => (
            <div key={name} className="frow2">
              <span className="fn">{name}</span>
              <span className="ff mono">{expression}</span>
              <span className="fs">{source}</span>
            </div>
          ))}
        </div>
        <div className="secthead" style={{ padding: "16px 0 8px" }}>
          <span className="lbl">Input data</span>
        </div>
        <div className="itable">
          {inputs.map(([name, description]) => (
            <div key={name} className="irow">
              <span className="in2">{name}</span>
              <span className="id2">{description}</span>
            </div>
          ))}
        </div>
        <p style={{ fontSize: "10.5px", color: "var(--ink-2)", opacity: 0.8, marginTop: 12, lineHeight: 1.5 }}>
          Indicative ranges, not investment advice.
        </p>
      </div>
      <div className="mfoot">
        <span className="fnote">
          Formulas pending client validation
        </span>
        <ModalClose className="cta ghost" style={{ width: "auto", padding: "0 16px", height: 38 }}>
          Close
        </ModalClose>
      </div>
    </>
  );
}
