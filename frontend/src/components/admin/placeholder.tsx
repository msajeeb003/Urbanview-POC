/**
 * A tab whose screen is a later ticket: the wireframe's card header (title, sub, and the action
 * button for roles that may act) over a plain "not connected yet" line.
 */
import { AdminButton, AdminCard } from "./parts";

export function SectionPlaceholder({
  title,
  sub,
  action,
  readOnly,
}: {
  title: string;
  sub: string;
  action?: string;
  readOnly?: boolean;
}) {
  return (
    <AdminCard
      title={title}
      sub={sub}
      action={
        action && !readOnly ? (
          <AdminButton disabled title="Coming with this section's ticket">
            {action}
          </AdminButton>
        ) : undefined
      }
    >
      <div className="admin-note">Not connected yet.</div>
    </AdminCard>
  );
}
