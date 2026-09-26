import { AdminCard, AdminUnavailable, DataTable, StatusChip } from "@/components/admin/parts";
import { AdminAccessDenied, adminGet } from "@/lib/admin/api";
import { orderChip, relativeTime } from "@/lib/admin/format";
import { guard } from "@/lib/admin/guard";
import type { OrderList } from "@/lib/api/types";

type OrderRow = OrderList["items"][number];

// Orders (wireframe `adminOrders`): the fulfilment queue. The API scopes it: admins see every
// order, an expert only the orders assigned to them.
export default async function OrdersPage() {
  const access = await guard("orders");
  if (access.denied) return access.denied;

  let data: OrderList;
  try {
    data = await adminGet<OrderList>("/v1/admin/orders", { limit: 100 });
  } catch (err) {
    if (err instanceof AdminAccessDenied) return null;
    return <AdminUnavailable what="Expert analysis orders" />;
  }
  const expert = access.staff.role === "expert";
  const now = new Date();

  return (
    <AdminCard
      title="Expert analysis orders"
      sub={expert ? "Manual fulfilment queue · the orders assigned to you" : "Manual fulfilment queue"}
    >
      <DataTable<OrderRow>
        rows={data.items}
        rowKey={(o) => o.id}
        empty={expert ? "No orders are assigned to you." : "No orders yet."}
        columns={[
          { key: "ref", label: "Ref", mono: true, render: (o) => o.reference },
          { key: "parcel", label: "Parcel", mono: true, render: (o) => o.location.parcel_label },
          {
            key: "customer",
            label: "Customer",
            render: (o) => (o.company_name ? `${o.customer_name} · ${o.company_name}` : o.customer_name),
          },
          { key: "placed", label: "Placed", render: (o) => relativeTime(o.placed_at, now) },
          {
            key: "status",
            label: "Status",
            render: (o) => {
              const chip = orderChip(o.status);
              return <StatusChip tone={chip.tone}>{chip.label}</StatusChip>;
            },
          },
          ...(expert
            ? []
            : [{ key: "assignee", label: "Expert", render: (o: OrderRow) => o.assignee?.display_name ?? o.assignee?.email ?? "—" }]),
        ]}
      />
    </AdminCard>
  );
}
