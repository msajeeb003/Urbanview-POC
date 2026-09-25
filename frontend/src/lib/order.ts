/**
 * Where every order button leads ("Order expert analysis" on the parcel panel, "Order a report" in
 * "Choose your access", the methodology's "Order this analysis →"): the S4 order modal for the
 * parcel whose panel is on screen (`orderTarget`), with `order_started`. Without a parcel panel
 * the visitor is asked to pick one.
 */
import { orderModalSpec } from "@/components/order/order-modal";

import { getTracker } from "./analytics/react";
import { ORDER_PRODUCT } from "./order-form";
import { useShell } from "./store";

/** Provisional copy (not in the wireframe). */
export const PICK_A_PARCEL = "Pick a parcel on the map to order its analysis.";

export type OrderTrigger = "panel" | "access" | "methodology";

export function requestOrder(trigger: OrderTrigger): void {
  const { orderTarget, openModal, showToast } = useShell.getState();
  if (!orderTarget) {
    showToast(PICK_A_PARCEL);
    return;
  }
  getTracker().track("order_started", {
    ...orderTarget.ids,
    panel_type: orderTarget.parcelType,
    product: ORDER_PRODUCT,
    trigger,
  });
  openModal(orderModalSpec(orderTarget));
}
