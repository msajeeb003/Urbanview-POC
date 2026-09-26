/**
 * Upload proxy of the Data sources screen: one file (multipart `file` + `kind`) from the browser,
 * streamed to `POST /v1/admin/files` with the staff token (`lib/admin/upload-proxy.ts`). The API's
 * answer passes through: 201 new file, 200 when the checksum was already known (the existing
 * record, `created: false`), 413 / 422 refusals in the error envelope.
 */
import { proxyUpload } from "@/lib/admin/upload-proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  return proxyUpload(request, "/v1/admin/files", "data");
}
