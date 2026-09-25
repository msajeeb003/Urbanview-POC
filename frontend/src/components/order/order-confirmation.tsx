"use client";

/**
 * S5 "Order confirmed" (wireframe `orderSuccess`, `screens/success.png`): the check, the title, the
 * delivery sentence with the turnaround, the reference and parcel in the mono chip, "Done" (toast
 * "Order placed — check your email"). No online checkout in this build, so the bank-transfer
 * instructions of `POST /v1/orders` follow in the mock's `.paysummary` lines (payee, IBAN, bank,
 * SWIFT, the reference to quote, the amount due as the total line), with the API's note (work
 * starts when the payment is received), where they were e-mailed, and the public order page.
 */
import { useEffect, useState } from "react";

import type { OrderCreated } from "@/lib/api/types";
import type { OrderTarget } from "@/lib/order-form";
import { turnaroundText } from "@/lib/pricing";
import { useShell, type ModalSpec } from "@/lib/store";

import { Cta } from "../ui/cta";

export const CONFIRMED_LABEL = "Order confirmed";
export const PLACED_TOAST = "Order placed — check your email";

export function confirmationSpec(order: OrderCreated, target: OrderTarget, email: string): ModalSpec {
  return { label: CONFIRMED_LABEL, content: <OrderConfirmation order={order} parcel={target.parcel} email={email} /> };
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

function OrderConfirmation({ order, parcel, email }: { order: OrderCreated; parcel: string; email: string }) {
  const closeModal = useShell((s) => s.closeModal);
  const showToast = useShell((s) => s.showToast);
  const pay = order.payment_instructions;
  const turnaround = turnaroundText(order.turnaround.business_days);
  const emailed = order.email_status === "queued" || order.email_status === "sent";
  const amount = `${pay.currency === "EUR" ? "€" : `${pay.currency} `}${pay.amount_eur.toFixed(2)}`;

  return (
    <>
      <div className="mbody">
        <div className="success">
          <div className="ok">
            <svg width="30" height="30" viewBox="0 0 30 30" fill="none" aria-hidden>
              <path d="M8 15l5 5 9-11" stroke="#B5613B" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <h2>Order confirmed</h2>
          <p>
            An expert will prepare your site &amp; feasibility analysis and email it within <b>{turnaround}</b>.{" "}
            {emailed
              ? "A confirmation is on its way now."
              : "The confirmation email could not be sent, so please keep the details below."}
          </p>
          <div className="orderref">
            {order.reference} · {parcel}
          </div>
        </div>

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
        <p className="paynote">
          {pay.note_en}
          {emailed && <> The same instructions were emailed to {email}.</>}{" "}
          <a href={orderPagePath(order.reference)} target="_blank" rel="noopener">
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
            showToast(PLACED_TOAST);
          }}
        >
          Done
        </Cta>
      </div>
    </>
  );
}
