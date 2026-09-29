/**
 * Words of the admin tables: relative times ("2h ago", "Yesterday") and the audit log's
 * before -> after. The order chips live with the orders rules (`orders.ts`).
 */

/** "just now", "2h ago", "Yesterday", "3 days ago", else the date (`12 May 2026`). */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const minutes = Math.floor((now.getTime() - t) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  const d = new Date(t);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d.getUTCDate()} ${months[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** `2026-09-26 14:05` in UTC (the audit trail's time column). */
export function utcStamp(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  return new Date(t).toISOString().slice(0, 16).replace("T", " ");
}

function show(value: unknown): string {
  if (value === undefined) return "∅";
  if (value === null) return "null";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

export interface Change {
  key: string;
  from: string;
  to: string;
}

/** The fields an audited change touched: every key whose value differs between before and after. */
export function auditChanges(
  before: Record<string, unknown> | null | undefined,
  after: Record<string, unknown> | null | undefined,
): Change[] {
  const keys = [...new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})])].sort();
  return keys
    .filter((k) => JSON.stringify(before?.[k]) !== JSON.stringify(after?.[k]))
    .map((k) => ({ key: k, from: show(before?.[k]), to: show(after?.[k]) }));
}
