/**
 * The console's time zone on the Next server (the pages rendered there: the orders queue, the
 * audit log): the municipality profile's `timezone`. Client components use `useStaffZone`.
 *
 * A deployment serves one municipality and its zone does not change while the server runs, so the
 * zone is read once and kept: a slow or failed profile call later never turns a page's times to
 * UTC. Until the first read succeeds the page says UTC, named as such, and the next request asks
 * again.
 */
import { api } from "@/lib/api/endpoints";

import { resolveZone, staffZoneOf, type StaffZone } from "./format";

let known: string | null = null;

export async function staffZone(): Promise<StaffZone> {
  if (known === null) {
    try {
      known = resolveZone((await api.municipality({ timeoutMs: 3000 })).timezone);
    } catch {
      return staffZoneOf(null);
    }
  }
  return staffZoneOf(known);
}
