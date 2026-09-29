import { DataScreen } from "@/components/admin/data/data-screen";
import { AdminUnavailable } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { documentTypeOptions, filterQuery, parseFilters, sourceRows, zoneOptions } from "@/lib/admin/data";
import { guard } from "@/lib/admin/guard";
import { api } from "@/lib/api/endpoints";
import type { AdminDocumentList, AdminJobList, MunicipalityProfile, ZoneIndex } from "@/lib/api/types";

async function orNull<T>(load: () => Promise<T>): Promise<T | null> {
  try {
    return await load();
  } catch {
    return null;
  }
}

// Data sources (wireframe `adminData`): the public sources the platform reads (the profile's, with
// how each reaches UrbanView today), and the planning documents registered from them with each
// file's extraction and geometry, and the zone GeoPackage imports (admins; reviewers read it
// without the write controls: the pilot scope's "read-only documents").
export default async function DataPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const access = await guard("data");
  if (access.denied) return access.denied;
  const filters = parseFilters(await searchParams);

  let list: AdminDocumentList;
  try {
    list = await adminGet<AdminDocumentList>("/v1/admin/documents", filterQuery(filters));
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Data sources" />;
  }
  const [zones, profile, imports] = await Promise.all([
    orNull<ZoneIndex>(() => api.zones({ timeoutMs: 3000 })),
    orNull<MunicipalityProfile>(() => api.municipality({ timeoutMs: 3000 })),
    access.readOnly
      ? Promise.resolve(null)
      : orNull<AdminJobList>(() => adminGet<AdminJobList>("/v1/admin/jobs", { type: "import_zones", limit: 5 })),
  ]);
  return (
    <DataScreen
      documents={list.items}
      total={list.total ?? list.items.length}
      filters={filters}
      zones={zoneOptions(zones?.zones)}
      types={documentTypeOptions(profile?.terminology.document_types, profile?.terminology.document_types_en)}
      sources={sourceRows(profile?.sources)}
      municipality={list.municipality ?? (profile ? { id: profile.id, name: profile.name } : null)}
      zoneImports={imports?.items ?? []}
      readOnly={access.readOnly}
    />
  );
}
