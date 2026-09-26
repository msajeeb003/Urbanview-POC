import type { ReactNode } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { api } from "@/lib/api/endpoints";
import type { MunicipalityProfile, TilesCurrent } from "@/lib/api/types";

// The shell is the layout of the map (`/`) and of the admin console (`/admin/*`, rendered into the
// overlay), so the map stays mounted while staff move between them.
//
// The profile (name, centre, bounds, KO list) and the tile pointer (current archive, data version)
// are read per request, in parallel, so the first paint already has them and the map starts
// loading tiles without another round trip; a slow or unreachable API never blocks the page (the
// client refetches instead).
export const dynamic = "force-dynamic";

async function orNull<T>(load: () => Promise<T>): Promise<T | null> {
  try {
    return await load();
  } catch {
    return null;
  }
}

export default async function ShellLayout({ children }: { children: ReactNode }) {
  const [profile, tiles] = await Promise.all([
    orNull<MunicipalityProfile>(() => api.municipality({ timeoutMs: 1500 })),
    orNull<TilesCurrent>(() => api.tilesCurrent({ timeoutMs: 1500 })),
  ]);
  return (
    <AppShell initialProfile={profile} initialTiles={tiles}>
      {children}
    </AppShell>
  );
}
