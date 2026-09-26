/**
 * Upload proxy of the admin console: the browser posts one file (multipart `file` + `kind`) here
 * with `XMLHttpRequest`, so it can show upload progress, and this handler streams the body on to
 * `POST /v1/admin/files` with the signed-in member's bearer token (the token never reaches the
 * browser). The API's answer passes through unchanged: 201 new file, 200 when the checksum was
 * already known (the existing record, `created: false`), 413 / 422 refusals in the error envelope.
 *
 * Guards: same-origin requests only (the check Next applies to server actions), a staff session
 * whose role may use the Data sources screen, and a multipart body. Server actions are not used
 * because they cap request bodies (planning PDFs run to tens of megabytes).
 */
import { apiBaseUrl, buildUrl, newRequestId } from "@/lib/api/client";
import { canOpen, isReadOnly } from "@/lib/admin/sections";
import { currentStaff, staffApiToken } from "@/lib/admin/session";

export const dynamic = "force-dynamic";

const UPLOAD_TIMEOUT_MS = 10 * 60_000;

function refuse(status: number, code: string, message: string): Response {
  return Response.json(
    { error: { code, message, request_id: null } },
    { status, headers: { "Cache-Control": "no-store" } },
  );
}

function sameOrigin(request: Request): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  const hosts = [request.headers.get("x-forwarded-host"), request.headers.get("host")].filter(Boolean);
  try {
    const { host } = new URL(origin);
    return hosts.some((h) => h!.split(",")[0].trim() === host);
  } catch {
    return false;
  }
}

export async function POST(request: Request): Promise<Response> {
  if (!sameOrigin(request)) return refuse(403, "forbidden", "Uploads are accepted from the admin console only");
  const staff = await currentStaff();
  const token = await staffApiToken();
  if (!staff || !token) return refuse(401, "unauthorized", "Sign in again to upload files");
  if (!canOpen(staff.role, "data") || isReadOnly(staff.role, "data")) {
    return refuse(403, "forbidden", "Your role cannot upload files");
  }
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().startsWith("multipart/form-data") || !request.body) {
    return refuse(415, "unsupported_media_type", "Send the file as multipart/form-data");
  }

  const headers: Record<string, string> = {
    "Content-Type": contentType,
    Authorization: `Bearer ${token}`,
    "X-Request-ID": newRequestId(),
  };
  const length = request.headers.get("content-length");
  if (length) headers["Content-Length"] = length;
  let upstream: Response;
  try {
    upstream = await fetch(buildUrl("/v1/admin/files", undefined, apiBaseUrl()), {
      method: "POST",
      headers,
      body: request.body,
      // a streamed request body (undici): sent as it arrives, never buffered here
      duplex: "half",
      signal: AbortSignal.timeout(UPLOAD_TIMEOUT_MS),
      cache: "no-store",
    } as RequestInit & { duplex: "half" });
  } catch {
    return refuse(503, "service_unavailable", "The data service did not answer");
  }
  return new Response(await upstream.text(), {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") ?? "application/json",
      "Cache-Control": "no-store",
    },
  });
}
