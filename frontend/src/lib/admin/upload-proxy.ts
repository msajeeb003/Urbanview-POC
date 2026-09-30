/**
 * Uploads from the admin console to the staff API, for route handlers: the browser posts one
 * multipart body here with `XMLHttpRequest` (so it can show progress) and the body is streamed on
 * to the API with the signed-in member's bearer token (the token never reaches the browser). The
 * API's answer passes through unchanged.
 *
 * Guards: same-origin requests only (the check Next applies to server actions), a staff session
 * whose role may open the section, and a multipart body. Server actions are not used because they
 * cap request bodies (planning PDFs and reports run to tens of megabytes).
 */
import { apiBaseUrl, buildUrl, newRequestId } from "@/lib/api/client";

import { canOpen, isReadOnly, type SectionId } from "./sections";
import { consoleApiToken, currentStaff } from "./session";

const UPLOAD_TIMEOUT_MS = 10 * 60_000;

function refuse(status: number, code: string, message: string): Response {
  return Response.json({ error: { code, message, request_id: null } }, { status, headers: { "Cache-Control": "no-store" } });
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

export async function proxyUpload(request: Request, apiPath: string, section: SectionId): Promise<Response> {
  if (!sameOrigin(request)) return refuse(403, "forbidden", "Uploads are accepted from the admin console only");
  const staff = await currentStaff();
  const token = await consoleApiToken();
  if (!staff || !token) return refuse(401, "unauthorized", "Sign in again to upload files");
  if (!canOpen(staff.role, section) || isReadOnly(staff.role, section)) {
    return refuse(403, "forbidden", "Your role cannot upload files here");
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
    upstream = await fetch(buildUrl(apiPath, undefined, apiBaseUrl()), {
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
