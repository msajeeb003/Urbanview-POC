import { SignInCard } from "@/components/admin/sign-in";
import { safeCallback } from "@/lib/admin/sections";

// The backend's magic-link e-mail opens `${ADMIN_BASE_URL}/login?token=…`, i.e. this page with
// ADMIN_BASE_URL = <site>/admin.
type Params = Record<string, string | string[] | undefined>;

function first(value: string | string[] | undefined): string | null {
  return (Array.isArray(value) ? value[0] : value) ?? null;
}

export default async function LoginPage({ searchParams }: { searchParams: Promise<Params> }) {
  const params = await searchParams;
  const token = first(params.token);
  const notice =
    first(params.expired) != null
      ? "Your session has ended. Sign in again."
      : first(params.error) != null
        ? "That sign-in did not work. Request a new link below."
        : null;
  return (
    <SignInCard
      key={token ?? "form"}
      token={token}
      callbackUrl={safeCallback(first(params.callbackUrl))}
      notice={notice}
    />
  );
}
