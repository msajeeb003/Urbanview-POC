/**
 * Choropleth classes → map colours, legend rows and Mapbox expressions, all from the one set of
 * breaks the API serves with the tile pointer (`cell_classes`), so the legend always matches what
 * the map draws. Null values never get a class: they are drawn with the "no data" hatch.
 *
 * - Parameter choropleth (block cells): graded classes along the wireframe's FAR-heatmap gradient
 *   #EFE3CE → #B4744A.
 * - Sale-price choropleth (zone cells): the wireframe's price bands (grey "not saleable" for 0,
 *   then gold alpha steps).
 * Without served classes (nothing published yet, or an older API) the wireframe's own look is the
 * fallback: a continuous gradient ("Low → high") and the wireframe's fixed €/m² bands.
 */
import type { components } from "@/lib/api/schema";

export type MetricClasses = components["schemas"]["MetricClasses"];
export type CellClasses = components["schemas"]["CellClasses"];

export type ParamMetric = "max_far" | "max_site_coverage_pct" | "max_height_m" | "max_gfa_m2";
export type PriceMetric = "low" | "expected" | "high";

export interface ParamMetricDef {
  key: ParamMetric;
  /** Chip label on the card. */
  short: string;
  /** Card sub-label and legend title. */
  label: string;
  legendTitle: string;
  legendUnit: string;
  decimals: number;
  /** Upper end of the fallback gradient when no classes are served. */
  fallbackMax: number;
}

export const PARAM_METRICS: readonly ParamMetricDef[] = [
  { key: "max_far", short: "FAR", label: "Floor area ratio", legendTitle: "FAR intensity", legendUnit: "floor area ratio", decimals: 2, fallbackMax: 3.4 },
  { key: "max_site_coverage_pct", short: "Coverage", label: "Site coverage", legendTitle: "Site coverage", legendUnit: "% of parcel", decimals: 0, fallbackMax: 100 },
  { key: "max_height_m", short: "Height", label: "Building height", legendTitle: "Building height", legendUnit: "m", decimals: 1, fallbackMax: 40 },
  { key: "max_gfa_m2", short: "GFA", label: "Max gross floor area", legendTitle: "Max GFA", legendUnit: "m² per block", decimals: 0, fallbackMax: 20000 },
];

export const PRICE_METRICS: readonly { key: PriceMetric; short: string; column: string; label: string }[] = [
  { key: "low", short: "Low", column: "sale_rate_low_eur_m2", label: "Low sale price" },
  { key: "expected", short: "Expected", column: "sale_rate_eur_m2", label: "Expected sale price" },
  { key: "high", short: "High", column: "sale_rate_high_eur_m2", label: "High sale price" },
];

export const paramMetric = (key: ParamMetric) => PARAM_METRICS.find((m) => m.key === key)!;
export const priceMetric = (key: PriceMetric) => PRICE_METRICS.find((m) => m.key === key)!;

/** The wireframe's price bands when the API serves none (€/m²). */
export const FALLBACK_PRICE_BREAKS = [1300, 1700, 2100];
export const NOT_SALEABLE_COLOUR = "rgba(150,145,132,.34)";
const GOLD_ALPHAS = [0.26, 0.45, 0.62, 0.82];
const GRADIENT_FROM = [0xef, 0xe3, 0xce];
const GRADIENT_TO = [0xb4, 0x74, 0x4a];

type Expr = unknown[];

const hex = (rgb: number[]) => `#${rgb.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("")}`.toUpperCase();

/** `n` colours evenly along #EFE3CE → #B4744A (one class: the middle). */
export function gradientColours(n: number): string[] {
  if (n <= 1) return [hex(GRADIENT_FROM.map((a, i) => (a + GRADIENT_TO[i]) / 2))];
  return Array.from({ length: n }, (_, k) => hex(GRADIENT_FROM.map((a, i) => a + ((GRADIENT_TO[i] - a) * k) / (n - 1))));
}

/** Gold steps for the price bands (the wireframe's four alphas, interpolated for other counts). */
export function goldColours(n: number): string[] {
  if (n === GOLD_ALPHAS.length) return GOLD_ALPHAS.map((a) => `rgba(201,154,46,${a})`);
  return Array.from({ length: n }, (_, k) => {
    const a = n <= 1 ? 0.54 : GOLD_ALPHAS[0] + ((GOLD_ALPHAS[3] - GOLD_ALPHAS[0]) * k) / (n - 1);
    return `rgba(201,154,46,${Math.round(a * 100) / 100})`;
  });
}

const EUR = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
function num(v: number, decimals: number): string {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: decimals, minimumFractionDigits: 0 }).format(v);
}

export interface ClassRow {
  colour: string;
  label: string;
}

export interface ChoroplethScheme {
  /** Legend rows, lowest class first (the zero class first when there is one). */
  rows: ClassRow[];
  /** `fill-color` for features that have a value (an expression, or one colour for one class). */
  fillColor: Expr | string;
  /** Property the cell layer is coloured by. */
  column: string;
  /** False: no classes were served, the legend shows the fallback gradient. */
  classed: boolean;
}

/** `["step", value, c0, b1, c1, …]` — class i = number of breaks ≤ value. */
function step(value: Expr, colours: string[], breaks: number[]): Expr {
  const out: unknown[] = ["step", value, colours[0]];
  breaks.forEach((b, i) => out.push(b, colours[i + 1]));
  return out;
}

export function paramScheme(metric: ParamMetric, classes: MetricClasses | null | undefined): ChoroplethScheme {
  const def = paramMetric(metric);
  const value: Expr = ["to-number", ["get", metric]];
  if (!classes || classes.count === 0 || classes.min == null || classes.max == null) {
    return {
      column: metric,
      classed: false,
      rows: [],
      fillColor: ["interpolate", ["linear"], value, 0, gradientColours(2)[0], def.fallbackMax, gradientColours(2)[1]],
    };
  }
  const breaks = classes.breaks;
  const colours = gradientColours(breaks.length + 1);
  const edges = [classes.min, ...breaks, classes.max];
  const rows = colours.map((colour, i) => {
    const lo = edges[i];
    const hi = edges[i + 1];
    const label = lo === hi ? num(lo, def.decimals) : `${num(lo, def.decimals)} – ${num(hi, def.decimals)}`;
    return { colour, label };
  });
  return { column: metric, classed: true, rows, fillColor: breaks.length ? step(value, colours, breaks) : colours[0] };
}

/** The price legend's words (`{v}` = the amount); English by default. */
export interface PriceWords {
  notSaleable: string;
  under: string;
  above: string;
}
const PRICE_WORDS: PriceWords = { notSaleable: "not saleable", under: "under {v}", above: "{v} and above" };

export function priceScheme(
  metric: PriceMetric,
  classes: MetricClasses | null | undefined,
  words: PriceWords = PRICE_WORDS,
): ChoroplethScheme {
  const { column } = priceMetric(metric);
  const breaks = classes?.breaks?.length ? classes.breaks : FALLBACK_PRICE_BREAKS;
  const colours = goldColours(breaks.length + 1);
  const value: Expr = ["to-number", ["get", column]];
  const rows: ClassRow[] = [{ colour: NOT_SALEABLE_COLOUR, label: words.notSaleable }];
  colours.forEach((colour, i) => {
    const lo = i === 0 ? null : breaks[i - 1];
    const hi = i < breaks.length ? breaks[i] : null;
    const label =
      lo == null
        ? words.under.replace("{v}", `€${EUR.format(hi!)}`)
        : hi == null
          ? words.above.replace("{v}", `€${EUR.format(lo)}`)
          : `€${EUR.format(lo)} – ${EUR.format(hi)}`;
    rows.push({ colour, label });
  });
  return {
    column,
    classed: !!classes?.breaks?.length,
    rows,
    fillColor: ["case", ["<=", value, 0], NOT_SALEABLE_COLOUR, step(value, colours, breaks)],
  };
}

/** Features that have a value for the column (MVT drops null properties). */
export const hasValue = (column: string): Expr => ["has", column];
/** Features without a value: the "no data" hatch, never the lowest class. */
export const noValue = (column: string): Expr => ["!", ["has", column]];
