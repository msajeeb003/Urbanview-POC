/**
 * The Calculation engine tab (wireframe `adminEngine`): a read-mostly display of the client-owned
 * formulas the shared engine (`@urbanview/feasibility-engine`) runs, the datasets they draw on,
 * the proposals staff recorded, and the engine's version, last update and changelog.
 *
 * The formula rows are this page's words for the engine's figures, keyed by the engine's own
 * result keys (a test keeps them in step with `FIELD_ORDER`). Proposals ("+ Add formula", "+ Add
 * data input") are recorded and audited by the API for the client's review; they never change a
 * calculation, which only a new `FORMULA_VERSION` with validated fixtures does.
 */
import type { ChipTone } from "@/components/admin/parts";
import type { DataSource, EngineProposal } from "@/lib/api/types";

export interface FormulaRow {
  /** The engine's result key (`FIELD_ORDER`). */
  key: string;
  output: string;
  expression: string;
  source: string;
}

// The spec's nine outputs, in the panel's order of reading. The total cost (land + design +
// construction) is part of the profit and ROI rows.
export const FORMULA_ROWS: readonly FormulaRow[] = [
  { key: "max_gfa", output: "Gross floor area (GFA)", expression: "urban parcel area × FAR", source: "adopted plan" },
  { key: "max_coverage_area", output: "Max coverage area", expression: "urban parcel area × site coverage %", source: "adopted plan" },
  {
    key: "saleable_area",
    output: "Saleable area",
    expression: "GFA × saleable share",
    source: "financial assumptions (70 % by default; visitors may edit it)",
  },
  {
    key: "land_value",
    output: "Land value",
    expression: "urban parcel area × land rate",
    source: "financial assumptions: transaction and listing data",
  },
  {
    key: "design_and_documentation_costs",
    output: "Design & documentation",
    expression: "GFA × design rate",
    source: "financial assumptions: fee rates",
  },
  {
    key: "construction_costs",
    output: "Construction cost",
    expression: "GFA × build rate",
    source: "financial assumptions: build rates (visitors may edit them)",
  },
  {
    key: "market_value",
    output: "Market value",
    expression: "saleable area × sale rate",
    source: "financial assumptions: listing and sale data (visitors may edit the rate)",
  },
  { key: "potential_profit", output: "Potential profit", expression: "market value − (land + design + construction)", source: "derived" },
  { key: "roi_pct", output: "Return on investment", expression: "profit ÷ total cost, banded to a range", source: "derived" },
];

export interface InputRow {
  key: string;
  name: string;
  provides: string;
}

/** The three dataset rows the formulas read, named from the municipality profile's sources. */
export function inputRows(sources: readonly DataSource[] | null | undefined): InputRow[] {
  const names = (kind: DataSource["kind"]) =>
    (sources ?? [])
      .filter((s) => s.kind === kind)
      .map((s) => s.name.replace(/\s*\(.*\)$/, ""))
      .join(", ");
  const listed = (text: string, kind: DataSource["kind"]) => {
    const found = names(kind);
    return found ? `${text} (${found})` : text;
  };
  return [
    {
      key: "planning",
      name: "Adopted planning documents",
      provides: listed("DUP / PUP source PDFs — FAR, site coverage, height and land use read per urban parcel, each value reviewed", "planning"),
    },
    { key: "cadastre", name: "Cadastre", provides: listed("parcel geometry, area and ownership status", "cadastre") },
    {
      key: "market",
      name: "Market sources",
      provides: listed("asking and transaction prices, construction cost indicators, entered as financial assumptions", "market"),
    },
  ];
}

export function proposalChip(proposal: Pick<EngineProposal, "status">): { tone: ChipTone; label: string } {
  switch (proposal.status) {
    case "accepted":
      return { tone: "ok", label: "Accepted" };
    case "declined":
      return { tone: "rev", label: "Declined" };
    case "pending":
      return { tone: "pend", label: "Pending" };
    default:
      return { tone: "pend", label: "New" };
  }
}

export function engineFooter(input: {
  engineVersion: string;
  formulaVersion: string;
  formulas: number;
  inputs: number;
  updated: string;
  proposals: number;
}): string {
  const waiting = input.proposals ? ` (${input.proposals} waiting)` : "";
  return (
    `Engine v${input.engineVersion} · formula version ${input.formulaVersion} · ${input.formulas} formulas · ` +
    `${input.inputs} input sources · last updated ${input.updated}. Proposed formulas and datasets are recorded for ` +
    `the client's review${waiting}: the calculation changes only with a new formula version the client has validated.`
  );
}

export interface ProposalDraft {
  name: string;
  expression: string;
  source: string;
  provides: string;
}

export function proposalProblem(kind: "formula" | "data_input", draft: ProposalDraft): string | null {
  if (!draft.name.trim()) return kind === "formula" ? "Name the output." : "Name the dataset.";
  if (kind === "formula" && !draft.expression.trim()) return "Give the expression.";
  if (kind === "data_input" && !draft.provides.trim()) return "Say what it provides.";
  return null;
}
