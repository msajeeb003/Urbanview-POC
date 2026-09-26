import { SectionPlaceholder } from "@/components/admin/placeholder";
import { guard } from "@/lib/admin/guard";

// The wireframe's "Formulas" card; its screen is a later ticket.
export default async function EnginePage() {
  const access = await guard("engine");
  if (access.denied) return access.denied;
  return (
    <SectionPlaceholder
      title="Formulas"
      sub="Calculated outputs the engine derives for every parcel — extend the model over time"
      action="+ Add formula"
      readOnly={access.readOnly}
    />
  );
}
