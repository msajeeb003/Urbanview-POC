/**
 * What an order's status says to the customer: one sentence per status (the public order page's
 * lead) and what the S5 confirmation says when it is opened again from its `?order=` link. An
 * order that is closed (delivered, refunded) says so there too: the first moment's "An expert
 * will prepare your analysis" is no longer true of it. Provisional copy (not in the wireframe).
 */
import type { OrderPublic } from "./api/types";

export type PublicOrderStatus = OrderPublic["status"];

export const ORDER_LEAD: Record<PublicOrderStatus, string> = {
  pending_payment: "We are waiting for your bank transfer. Work starts once it arrives.",
  payment_failed: "Your bank transfer has not reached us yet. Work starts once it arrives.",
  paid: "Payment received. An expert will start on your analysis shortly.",
  in_progress: "An expert is preparing your analysis.",
  delivered: "Your analysis has been delivered. Check your email for the download link.",
  refunded: "This order was refunded.",
};

const CLOSED_TITLE: Partial<Record<PublicOrderStatus, string>> = {
  delivered: "Report delivered",
  refunded: "Order refunded",
};

/**
 * The reopened confirmation of a closed order: its title and its one sentence. Null while the
 * order is open (the confirmation keeps the wireframe's "Order confirmed" and delivery sentence).
 */
export function closedOrderWords(status: PublicOrderStatus | null | undefined): { title: string; text: string } | null {
  const title = status ? CLOSED_TITLE[status] : undefined;
  return status && title ? { title, text: ORDER_LEAD[status] } : null;
}
