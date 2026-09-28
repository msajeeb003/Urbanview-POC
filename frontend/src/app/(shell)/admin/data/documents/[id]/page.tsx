import Link from "next/link";

import { DocumentDetail } from "@/components/admin/data/document-detail";
import { AdminCard, AdminUnavailable } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { documentTypeOptions, zoneOptions } from "@/lib/admin/data";
import { guard } from "@/lib/admin/guard";
import { ApiError } from "@/lib/api/client";
import { api } from "@/lib/api/endpoints";
import type { AdminDocument, MunicipalityProfile, ZoneIndex } from "@/lib/api/types";

async function orNull<T>(load: () => Promise<T>): Promise<T | null> {
  try {
    return await load();
  } catch {
    return null;
  }
}

function NotFound() {
  return (
    <AdminCard title="Document not found" sub="It may have been removed, or the link is wrong.">
      <div className="admin-note">
        <Link className="abtn sm ghost" href="/admin/data">
          ← Data sources
        </Link>
      </div>
    </AdminCard>
  );
}

// One planning document version: facts, files (drop PDFs, watch each extraction), versions.
export default async function DocumentPage({ params }: { params: Promise<{ id: string }> }) {
  const access = await guard("data");
  if (access.denied) return access.denied;
  const documentId = Number((await params).id);
  if (!Number.isInteger(documentId) || documentId <= 0) return <NotFound />;

  let doc: AdminDocument;
  try {
    doc = await adminGet<AdminDocument>(`/v1/admin/documents/${documentId}`);
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    if (err instanceof ApiError && err.status === 404) return <NotFound />;
    return <AdminUnavailable what="Planning document" />;
  }
  const [zones, profile] = await Promise.all([
    orNull<ZoneIndex>(() => api.zones({ timeoutMs: 3000 })),
    orNull<MunicipalityProfile>(() => api.municipality({ timeoutMs: 3000 })),
  ]);
  return (
    <DocumentDetail
      doc={doc}
      zones={zoneOptions(zones?.zones)}
      types={documentTypeOptions(profile?.terminology.document_types, profile?.terminology.document_types_en)}
      readOnly={access.readOnly}
    />
  );
}
