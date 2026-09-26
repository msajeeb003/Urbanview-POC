/**
 * Report upload of the Orders tab: the finished PDF (multipart `file` + optional `note`) from the
 * browser, streamed to `POST /v1/admin/orders/{id}/report` with the staff token
 * (`lib/admin/upload-proxy.ts`). The API delivers the order and e-mails the customer; an expert
 * may upload only for the orders assigned to them (the API answers 403 otherwise).
 */
import { proxyUpload } from "@/lib/admin/upload-proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request, { params }: { params: Promise<{ id: string }> }): Promise<Response> {
  const id = Number((await params).id);
  if (!Number.isInteger(id) || id <= 0) {
    return Response.json({ error: { code: "not_found", message: "No such order", request_id: null } }, { status: 404 });
  }
  return proxyUpload(request, `/v1/admin/orders/${id}/report`, "orders");
}
