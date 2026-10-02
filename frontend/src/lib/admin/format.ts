/**
 * Words of the admin tables: relative times ("2h ago", "Yesterday"), the exact times, and the
 * audit log's before -> after. The order chips live with the orders rules (`orders.ts`).
 *
 * The console's exact times are written in the municipality's time zone (the profile's
 * `timezone`, e.g. Europe/Podgorica: staff compare them with bank statements and e-mails), and
 * every screen names the zone (`zoneName`). The API's timestamps are UTC; a zone is never guessed
 * from the browser, so the server and the browser render the same text. Client components get
 * the zone from `useStaffZone`, server pages from `staffZone` (`zone-server.ts`).
 */

export const UTC = "UTC";

const clocks = new Map<string, Intl.DateTimeFormat | null>();

/** The zone's clock, or null for a name the runtime does not know. */
function clock(zone: string): Intl.DateTimeFormat | null {
  let found = clocks.get(zone);
  if (found === undefined) {
    try {
      found = new Intl.DateTimeFormat("en-GB", {
        timeZone: zone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      });
    } catch {
      found = null;
    }
    clocks.set(zone, found);
  }
  return found;
}

/** The zone the times are written in: the given IANA name when the runtime knows it, else UTC. */
export function resolveZone(zone: string | null | undefined): string {
  return zone && clock(zone) ? zone : UTC;
}

/** How a zone is named beside its times: "Podgorica time" (Europe/Podgorica), "UTC". */
export function zoneName(zone: string): string {
  if (zone === UTC) return UTC;
  return `${zone.slice(zone.lastIndexOf("/") + 1).replace(/_/g, " ")} time`;
}

function parts(t: number, zone: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (clock(zone) ?? clock(UTC)!).formatToParts(t)) out[part.type] = part.value;
  return out;
}

/** `2026-09-26 16:05`: the moment on the zone's clock (UTC when no zone is given). */
export function zoneStamp(iso: string, zone: string = UTC): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const p = parts(t, zone);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
}

/** The console's clock on one screen: the zone, its name and the two ways a time is written. */
export interface StaffZone {
  zone: string;
  /** "Podgorica time" / "UTC". */
  name: string;
  /** `2026-10-02 18:07` (the screen names the zone once). */
  stamp: (iso: string) => string;
  /** `2026-10-02 18:07 Podgorica time`: a time that stands alone (a tooltip). */
  named: (iso: string) => string;
}

export function staffZoneOf(timezone: string | null | undefined): StaffZone {
  const zone = resolveZone(timezone);
  const name = zoneName(zone);
  return {
    zone,
    name,
    stamp: (iso) => zoneStamp(iso, zone),
    named: (iso) => `${zoneStamp(iso, zone)} ${name}`,
  };
}

/** "just now", "2h ago", "Yesterday", "3 days ago", else the date in the zone (`12 May 2026`). */
export function relativeTime(iso: string, now: Date = new Date(), zone: string = UTC): string {
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
  const p = parts(t, zone);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${Number(p.day)} ${months[Number(p.month) - 1]} ${p.year}`;
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
