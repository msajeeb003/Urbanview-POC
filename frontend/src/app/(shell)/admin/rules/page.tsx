import { SectionPlaceholder } from "@/components/admin/placeholder";
import { guard } from "@/lib/admin/guard";

// The wireframe's "Planning rules" card; its screen is a later ticket.
export default async function RulesPage() {
  const access = await guard("rules");
  if (access.denied) return access.denied;
  return (
    <SectionPlaceholder
      title="Planning rules"
      sub="Zone parameter sets — each carries its source & verification date"
      action="+ New rule"
      readOnly={access.readOnly}
    />
  );
}
