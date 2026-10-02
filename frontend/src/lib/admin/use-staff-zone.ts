"use client";

/**
 * The time zone the console writes its exact times in, for client components: the municipality's
 * (the profile's `timezone`), which the shell already holds; UTC until the profile is known, and
 * the name shown beside the times always says which of the two it is. Server pages read it with
 * `staffZone` (`zone-server.ts`).
 */
import { useMemo } from "react";

import { useMunicipality } from "@/lib/api/hooks";

import { staffZoneOf, type StaffZone } from "./format";

export function useStaffZone(): StaffZone {
  const { data: profile } = useMunicipality();
  const timezone = profile?.timezone;
  return useMemo(() => staffZoneOf(timezone), [timezone]);
}
