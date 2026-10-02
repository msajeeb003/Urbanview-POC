import { describe, expect, it } from "vitest";

import { closedOrderWords, ORDER_LEAD } from "./order-status";

describe("what an order's status says to the customer", () => {
  it("has one sentence for every status", () => {
    expect(Object.keys(ORDER_LEAD).sort()).toEqual(
      ["delivered", "in_progress", "paid", "payment_failed", "pending_payment", "refunded"],
    );
  });

  it("a reopened confirmation of a closed order says it is closed", () => {
    expect(closedOrderWords("delivered")).toEqual({
      title: "Report delivered",
      text: "Your analysis has been delivered. Check your email for the download link.",
    });
    expect(closedOrderWords("refunded")).toEqual({ title: "Order refunded", text: "This order was refunded." });
  });

  it("an open order keeps the confirmation's own words", () => {
    for (const status of ["pending_payment", "payment_failed", "paid", "in_progress"] as const) {
      expect(closedOrderWords(status)).toBeNull();
    }
    // the order just placed: no status was read back
    expect(closedOrderWords(null)).toBeNull();
  });
});
