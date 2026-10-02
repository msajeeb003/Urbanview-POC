"use client";

/**
 * S5 "Order confirmed" (wireframe `orderSuccess`, `screens/success.png`): the check, the title, the
 * delivery sentence with the turnaround, the reference and parcel in the mono chip, "Done" (toast
 * "Order placed — check your email"). No online checkout in this build, so the bank-transfer
 * instructions follow in the mock's `.paysummary` lines (payee, IBAN, bank, SWIFT, the reference to
 * quote, the amount due as the total line), with the API's note (work starts when the payment is
 * received), where they were e-mailed, and the public order page.
 *
 * Reload-safe: while it is on screen the address bar carries `?order=<reference>`; opening the map
 * with it reads `GET /v1/orders/{reference}` (no personal data: the e-mail address is only known
 * right after the order) and shows the order again (`reopenConfirmation`) with its status as it
 * is now ("Status: awaiting payment"), never the first moment's "a confirmation is on its way".
 * An order that is closed says so: "Report delivered" / "Order refunded" with the order page's
 * sentence instead of "Order confirmed … an expert will prepare your analysis" (`closedOrderWords`).
 * Closing it removes the parameter.
 *
 * The e-mail sentence follows the API's `email_status`: only a queued or sent message is
 * announced; a server that cannot mail (no SMTP yet) says so and asks to keep the details.
 */
import { useEffect, useState } from "react";

import { api } from "@/lib/api/endpoints";
import type { OrderCreated, OrderPublic } from "@/lib/api/types";
import { closedOrderWords, type PublicOrderStatus } from "@/lib/order-status";
import { turnaroundText } from "@/lib/pricing";
import { useShell, type ModalSpec } from "@/lib/store";
import { syncOrderParam } from "@/lib/url-state";

import { Cta } from "../ui/cta";

export const CONFIRMED_LABEL = "Order confirmed";
export const PLACED_TOAST = "Order placed — check your email";
export const PLACED_TOAST_NO_MAIL = "Order placed — keep your order reference";

type Instructions = OrderCreated["payment_instructions"];

/** What the confirmation shows: from the order just placed, or read back after a reload. */
interface Confirmation {
  reference: string;
  parcel: string;
  businessDays: number;
  instructions: Instructions | null;
  /** Payment received already (after a reload). */
  paid: boolean;
  /** The address the instructions went to (only right after the order). */
  email: string | null;
  emailed: boolean;
  /** Read back after a reload: the order's status as it is now (`awaiting payment`). */
  statusNow: string | null;
  /** Read back after a reload: the status itself (a closed order says so). */
  status: PublicOrderStatus | null;
}

export function confirmationSpec(order: OrderCreated, parcel: string, email: string): ModalSpec {
  return spec({
    reference: order.reference,
    parcel,
    businessDays: order.turnaround.business_days,
    instructions: order.payment_instructions,
    paid: false,
    email,
    emailed: order.email_status === "queued" || order.email_status === "sent",
    statusNow: null,
    status: null,
  });
}

/** The confirmation read back from `GET /v1/orders/{reference}` (a reload of the S5 link). */
export function publicConfirmationSpec(order: OrderPublic): ModalSpec {
  return spec({
    reference: order.reference,
    parcel: order.location.parcel_label,
    businessDays: order.turnaround.business_days,
    instructions: order.payment_instructions ?? null,
    paid: !order.payment_due,
    email: null,
    // whether the first e-mail went out is not known from the reference alone: nothing is claimed
    emailed: false,
    statusNow: order.status_label_en,
    status: order.status,
  });
}

const spec = (c: Confirmation): ModalSpec => ({
  label: closedOrderWords(c.status)?.title ?? CONFIRMED_LABEL,
  content: <OrderConfirmation c={c} />,
});

/** Opens the confirmation of `?order=` again; a reference that matches nothing drops the parameter. */
export async function reopenConfirmation(reference: string): Promise<void> {
  try {
    const order = await api.order(reference);
    useShell.getState().openModal(publicConfirmationSpec(order));
  } catch {
    syncOrderParam(null);
  }
}

/** The public order page (`app/orders/[reference]`), the link the e-mail carries too. */
export const orderPagePath = (reference: string) => `/orders/${encodeURIComponent(reference)}`;

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(t);
  }, [copied]);
  return (
    <button
      type="button"
      className="paycopy"
      aria-label={`Copy the ${label}`}
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(
          () => setCopied(true),
          () => undefined,
        );
      }}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

/** The bank-transfer lines (the confirmation and the order page). */
export function PayInstructions({ pay }: { pay: Instructions }) {
  const amount = `${pay.currency === "EUR" ? "€" : `${pay.currency} `}${pay.amount_eur.toFixed(2)}`;
  return (
    <>
      <div className="fieldlab paylab">Pay by bank transfer</div>
      <div className="paysummary payinstr">
        <div className="payline">
          <span>Payee</span>
          <span>{pay.beneficiary}</span>
        </div>
        <div className="payline">
          <span>IBAN</span>
          <span className="mono">
            {pay.iban} <CopyButton text={pay.iban} label="IBAN" />
          </span>
        </div>
        {pay.bank_name && (
          <div className="payline">
            <span>Bank</span>
            <span>{pay.bank_name}</span>
          </div>
        )}
        {pay.swift && (
          <div className="payline">
            <span>SWIFT / BIC</span>
            <span className="mono">{pay.swift}</span>
          </div>
        )}
        <div className="payline">
          <span>Payment reference</span>
          <span className="mono">
            {pay.reference_to_quote} <CopyButton text={pay.reference_to_quote} label="payment reference" />
          </span>
        </div>
        <div className="payline total">
          <span>Amount due</span>
          <span className="mono">{amount}</span>
        </div>
      </div>
    </>
  );
}

function OrderConfirmation({ c }: { c: Confirmation }) {
  const closeModal = useShell((s) => s.closeModal);
  const showToast = useShell((s) => s.showToast);
  const turnaround = turnaroundText(c.businessDays);
  const pay = c.instructions;
  const closed = closedOrderWords(c.status);

  // the address bar names the order while the confirmation is open (a reload shows it again)
  useEffect(() => {
    syncOrderParam(c.reference);
    return () => syncOrderParam(null);
  }, [c.reference]);

  return (
    <>
      <div className="mbody">
        <div className="success">
          <div className="ok">
            <svg width="30" height="30" viewBox="0 0 30 30" fill="none" aria-hidden>
              <path d="M8 15l5 5 9-11" stroke="#B5613B" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <h2>{closed ? closed.title : CONFIRMED_LABEL}</h2>
          {closed ? (
            <p>{closed.text}</p>
          ) : (
            <p>
              An expert will prepare your site &amp; feasibility analysis and email it within <b>{turnaround}</b>.{" "}
              {c.paid
                ? "Your payment has been received."
                : c.statusNow
                  ? `Status: ${c.statusNow}.`
                  : c.emailed
                    ? "A confirmation is on its way now."
                    : "The confirmation email could not be sent, so please keep the details below."}
            </p>
          )}
          <div className="orderref">
            {c.reference} · {c.parcel}
          </div>
        </div>

        {pay && <PayInstructions pay={pay} />}
        <p className="paynote">
          {pay && (
            <>
              {pay.note_en}
              {c.emailed && (c.email ? <> The same instructions were emailed to {c.email}.</> : <> The same instructions were emailed to you.</>)}{" "}
            </>
          )}
          <a href={orderPagePath(c.reference)} target="_blank" rel="noopener">
            Track your order ↗
          </a>
        </p>
      </div>
      <div className="mfoot">
        <Cta
          variant="primary"
          style={{ width: "auto", padding: "0 24px" }}
          onClick={() => {
            closeModal();
            // a reopened order was placed earlier: nothing to announce
            if (!c.statusNow) showToast(c.emailed ? PLACED_TOAST : PLACED_TOAST_NO_MAIL);
          }}
        >
          Done
        </Cta>
      </div>
    </>
  );
}
