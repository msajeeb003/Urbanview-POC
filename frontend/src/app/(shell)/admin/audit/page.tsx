import { AdminCard, AdminUnavailable, DataTable } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { auditChanges } from "@/lib/admin/format";
import { guard } from "@/lib/admin/guard";
import { staffZone } from "@/lib/admin/zone-server";
import type { AuditEntry, AuditPage } from "@/lib/api/types";

// The audit trail (admins): who changed what and when, newest first, from GET /v1/admin/audit
// (append-only). Filters (action prefix, actor) are a plain GET form, so a filtered view is a link.
// The time column is the municipality's clock, named in its header.
const PAGE = 50;

type Params = Record<string, string | string[] | undefined>;
const one = (v: string | string[] | undefined) => ((Array.isArray(v) ? v[0] : v) ?? "").trim();

function Changes({ entry }: { entry: AuditEntry }) {
  const changes = auditChanges(entry.before, entry.after);
  if (changes.length === 0) return <span className="audit-none">{entry.note ?? "—"}</span>;
  return (
    <ul className="audit-changes">
      {changes.map((c) => (
        <li key={c.key}>
          <span className="k">{c.key}</span> {c.from} → {c.to}
        </li>
      ))}
    </ul>
  );
}

export default async function AuditPageView({ searchParams }: { searchParams: Promise<Params> }) {
  const access = await guard("audit");
  if (access.denied) return access.denied;
  const params = await searchParams;
  const action = one(params.action);
  const actor = one(params.actor);
  const offset = Math.max(0, Number.parseInt(one(params.offset) || "0", 10) || 0);

  const zonePromise = staffZone();
  let page: AuditPage;
  try {
    page = await adminGet<AuditPage>("/v1/admin/audit", {
      action: action || undefined,
      actor: actor || undefined,
      limit: PAGE,
      offset,
    });
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Audit log" />;
  }
  const link = (next: number) => {
    const q = new URLSearchParams();
    if (action) q.set("action", action);
    if (actor) q.set("actor", actor);
    if (next) q.set("offset", String(next));
    const s = q.toString();
    return s ? `/admin/audit?${s}` : "/admin/audit";
  };
  const more = page.items.length === PAGE;
  const zone = await zonePromise;

  return (
    <AdminCard
      title="Audit log"
      sub="Who changed what and when · append-only"
      action={
        <form className="audit-filters" method="get" action="/admin/audit">
          <input name="action" defaultValue={action} placeholder="Action, e.g. review." aria-label="Action" />
          <input name="actor" defaultValue={actor} placeholder="Actor (e-mail or token)" aria-label="Actor" />
          <button type="submit" className="abtn sm">
            Filter
          </button>
          {(action || actor) && (
            <a className="abtn sm ghost" href="/admin/audit">
              Clear
            </a>
          )}
        </form>
      }
    >
      <DataTable<AuditEntry>
        rows={page.items}
        rowKey={(e) => e.id}
        empty="No audited changes match these filters."
        columns={[
          { key: "time", label: `Time (${zone.name})`, mono: true, render: (e) => zone.stamp(e.created_at) },
          { key: "actor", label: "Actor", render: (e) => e.actor },
          { key: "action", label: "Action", mono: true, render: (e) => e.action },
          {
            key: "entity",
            label: "Entity",
            mono: true,
            render: (e) => (e.entity_type ? `${e.entity_type}${e.entity_id != null ? ` #${e.entity_id}` : ""}` : "—"),
          },
          { key: "change", label: "Before → after", render: (e) => <Changes entry={e} /> },
        ]}
      />
      {(offset > 0 || more) && (
        <div className="admin-note">
          {offset > 0 && (
            <a className="abtn sm ghost" href={link(Math.max(0, offset - PAGE))}>
              ← Newer
            </a>
          )}
          {more && (
            <a className="abtn sm ghost" href={link(offset + PAGE)}>
              Older →
            </a>
          )}
        </div>
      )}
    </AdminCard>
  );
}
