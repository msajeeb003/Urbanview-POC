import type { Metadata } from "next";

import { OrderStatusPage } from "@/components/order/order-status";

// The public order page: the link of the order confirmation and e-mails (the API's
// `ORDER_PUBLIC_BASE_URL` + `/orders/<reference>`). Status, location and turnaround only.

type Props = { params: Promise<{ reference: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { reference } = await params;
  return {
    title: `Order ${decodeURIComponent(reference)} — UrbanView`,
    robots: { index: false, follow: false },
  };
}

export default async function OrderPage({ params }: Props) {
  const { reference } = await params;
  return <OrderStatusPage reference={decodeURIComponent(reference).trim().toUpperCase()} />;
}
