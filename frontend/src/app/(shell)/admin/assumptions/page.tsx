import { SectionPlaceholder } from "@/components/admin/placeholder";
import { guard } from "@/lib/admin/guard";

// The wireframe's "Financial assumptions" card; its screen is a later ticket.
export default async function AssumptionsPage() {
  const access = await guard("assumptions");
  if (access.denied) return access.denied;
  return (
    <SectionPlaceholder
      title="Financial assumptions"
      sub="Benchmarks by district — feed the deterministic engine. Sources: Realitica, Estitor, Monstat"
      action="Save changes"
      readOnly={access.readOnly}
    />
  );
}
