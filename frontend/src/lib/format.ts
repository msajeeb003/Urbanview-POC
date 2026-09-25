/** Display helpers shared by the shell (wireframe formats). */

/** `42.4411° N · 19.2636° E` (wireframe coordinates chip). */
export function formatCoords(p: { lat: number; lng: number }): string {
  const lat = `${Math.abs(p.lat).toFixed(4)}° ${p.lat >= 0 ? "N" : "S"}`;
  const lng = `${Math.abs(p.lng).toFixed(4)}° ${p.lng >= 0 ? "E" : "W"}`;
  return `${lat} · ${lng}`;
}

/** Zoom relative to the municipality framing, as the mock shows it (`1.0×`). */
export function formatZoomFactor(zoom: number, baseZoom: number): string {
  return `${Math.pow(2, zoom - baseZoom).toFixed(1)}×`;
}

const NICE = [1, 2, 2.5, 5];

/**
 * Scale bar for a Web Mercator map with 512 px tiles: the longest "nice" distance that fits in
 * `maxPx` at this latitude and zoom, and the bar width for it.
 */
export function scaleBar(lat: number, zoom: number, maxPx = 80): { label: string; widthPx: number } {
  const metresPerPx = (40_075_016.686 * Math.cos((lat * Math.PI) / 180)) / (512 * Math.pow(2, zoom));
  const maxMetres = metresPerPx * maxPx;
  const exp = Math.pow(10, Math.floor(Math.log10(maxMetres)));
  let nice = exp;
  for (const n of NICE) if (n * exp <= maxMetres) nice = n * exp;
  const label = nice >= 1000 ? `${trim(nice / 1000)} km` : `${trim(nice)} m`;
  return { label, widthPx: Math.round(nice / metresPerPx) };
}

function trim(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

/**
 * Parcel reference typed in the search box: `1042`, `1042/3`, `#1042`, `parcel 1042/3`.
 * The cadastral municipality (KO) is chosen from the suggestions, never guessed.
 */
export function parseParcelNumber(input: string): { number: string; sub: string | null } | null {
  const m = /^\s*(?:parcel\s*)?#?\s*(\d{1,6})(?:\s*\/\s*(\d{1,4}))?\s*$/i.exec(input);
  if (!m) return null;
  return { number: m[1], sub: m[2] ?? null };
}

/** A planning figure as the panel shows it: at most `decimals` decimals, no trailing zeros (`3.2`, `55`). */
export function formatFigure(value: number, decimals = 2): string {
  return String(Number(value.toFixed(decimals)));
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** An ISO date (`2019-05-12`) as `12 May 2019` (UTC, three-letter months); null when it does not parse. */
export function formatDate(iso: string): string | null {
  const t = Date.parse(iso.length === 10 ? `${iso}T00:00:00Z` : iso);
  if (Number.isNaN(t)) return null;
  const d = new Date(t);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** `€1,295,460` (the mock's `eur()`: whole euros, US grouping); a loss reads `−€315,536`. */
export function formatEur(value: number): string {
  const rounded = Math.round(value);
  const text = `€${Math.abs(rounded).toLocaleString("en-US")}`;
  return rounded < 0 ? `−${text}` : text;
}

/** `25%`, `−7%` (whole percent, a true minus sign). */
export function formatPct(value: number): string {
  const rounded = Math.round(value);
  return rounded < 0 ? `−${Math.abs(rounded)}%` : `${rounded}%`;
}

/** `1,370.9` (the mock's `toLocaleString('en-US')`; areas carry one decimal). */
export function formatArea(m2: number): string {
  return m2.toLocaleString("en-US", { maximumFractionDigits: 1 });
}
