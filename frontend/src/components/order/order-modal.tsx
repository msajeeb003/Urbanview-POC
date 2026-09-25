"use client";

/**
 * S4 "Order expert analysis" (wireframe `openOrder`, the 520 px modal of `screens/order.png` /
 * `order-legal.png`): paid-tinted header, the context strip with the parcel carried through from
 * the panel (never re-entered), the order summary (parcel size and its basis, the fee and its
 * band, the turnaround: all from `GET /v1/orders/pricing`, the server's rule), the pricing note,
 * "Ordering as" Individual | Legal entity with the mock's fields, the methodology card and the
 * guest-checkout note; footer `€100 · 5 working days`, Cancel, gold "Place order →".
 *
 * No account, no password, no card: "Place order →" validates inline (the API's rules,
 * `lib/order-form.ts`), then `POST /v1/orders` with the location and the visitor's edited
 * assumptions, and opens the S5 confirmation. A rejected or failed request keeps the form and its
 * data and says why in one sentence. The typed data lives in the store (memory only) so the
 * methodology detour and a failure never lose it; a placed order clears it. One request at a time.
 */
import { useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { getTracker } from "@/lib/analytics/react";
import { formatArea } from "@/lib/format";
import { useCreateOrder, useOrderPricing } from "@/lib/api/hooks";
import {
  FIELDS,
  MAX_LENGTH,
  ORDER_PRODUCT,
  explainFailure,
  toOrderIn,
  validateDraft,
  type DraftField,
  type FieldErrors,
  type OrderTarget,
  type PurchaserType,
} from "@/lib/order-form";
import { bandLabel, formatPrice, priceFor, priceNote, turnaroundText } from "@/lib/pricing";
import { useShell, type ModalSpec } from "@/lib/store";
import { cn } from "@/lib/utils";

import { METHODOLOGY_LABEL, MethodologyModal } from "../shell/methodology-modal";
import { Cta } from "../ui/cta";
import { ModalHead } from "../ui/modal";
import { confirmationSpec } from "./order-confirmation";

export const ORDER_LABEL = "Order expert analysis";

export function orderModalSpec(target: OrderTarget): ModalSpec {
  return { label: ORDER_LABEL, content: <OrderModal target={target} /> };
}

/** `Parcel #1042/3 · Podgorica I` (the context strip, the methodology's context). */
export const targetLabel = (t: OrderTarget) => (t.ko ? `${t.parcel} · ${t.ko}` : t.parcel);

interface FieldSpec {
  label: string;
  note?: string;
  placeholder: string;
  type?: "text" | "email" | "tel";
  autoComplete: string;
  mono?: boolean;
}

// labels and placeholders are the mock's
const SPECS: Record<PurchaserType, Partial<Record<DraftField, FieldSpec>>> = {
  individual: {
    firstName: { label: "First name", placeholder: "Marko", autoComplete: "given-name" },
    lastName: { label: "Last name", placeholder: "Petrović", autoComplete: "family-name" },
    telephone: { label: "Telephone", placeholder: "+382 …", type: "tel", autoComplete: "tel" },
    email: { label: "Email address", placeholder: "you@email.me", type: "email", autoComplete: "email" },
  },
  legal_entity: {
    companyName: { label: "Company name", placeholder: "Company d.o.o.", autoComplete: "organization" },
    taxNumber: { label: "PIB / VAT number", placeholder: "02345678", autoComplete: "off", mono: true },
    contactPerson: { label: "Contact person", placeholder: "Marko Petrović", autoComplete: "name" },
    telephone: { label: "Telephone", placeholder: "+382 …", type: "tel", autoComplete: "tel" },
    email: { label: "Email address", placeholder: "office@company.me", type: "email", autoComplete: "email" },
    registeredAddress: {
      label: "Registered address",
      note: "for the invoice",
      placeholder: "Bulevar Svetog Petra Cetinjskog 1, Podgorica",
      autoComplete: "street-address",
    },
  },
};

// the mock's grid: pairs in `.frow`, the legal entity's e-mail and address full width
const ROWS: Record<PurchaserType, DraftField[][]> = {
  individual: [
    ["firstName", "lastName"],
    ["telephone", "email"],
  ],
  legal_entity: [["companyName", "taxNumber"], ["contactPerson", "telephone"], ["email"], ["registeredAddress"]],
};

const IconCard = () => (
  <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden>
    <path d="M3 4h14v12H3z" stroke="currentColor" strokeWidth="1.4" />
    <path d="M3 8h14M6 12h4" stroke="currentColor" strokeWidth="1.4" />
  </svg>
);

function OrderModal({ target }: { target: OrderTarget }) {
  const draft = useShell((s) => s.orderDraft);
  const setDraft = useShell((s) => s.setOrderDraft);
  const clearDraft = useShell((s) => s.clearOrderDraft);
  const edits = useShell((s) => s.assumptionEdits);
  const openModal = useShell((s) => s.openModal);
  const closeModal = useShell((s) => s.closeModal);
  const pricing = useOrderPricing();
  const create = useCreateOrder();
  const [errors, setErrors] = useState<FieldErrors>({});
  const [failure, setFailure] = useState<string | null>(null);
  const sending = useRef(false);
  const failureRef = useRef<HTMLParagraphElement>(null);
  const formId = useId();
  const idOf = (field: DraftField) => `${formId}-${field}`;

  const tiers = pricing.data;
  const price = priceFor(target.basisAreaM2, tiers);
  const band = bandLabel(target.basisAreaM2, tiers);
  const turnaround = turnaroundText(tiers?.turnaround_business_days);
  const note = priceNote(tiers);
  const noArea = target.basisAreaM2 == null;
  const busy = create.isPending;

  const change = (field: DraftField, value: string) => {
    setDraft({ [field]: value });
    // an inline message goes away as soon as the field is right
    if (errors[field]) {
      const next = validateDraft({ ...draft, [field]: value });
      setErrors((e) => ({ ...e, [field]: next[field] }));
    }
  };
  const switchTo = (purchaserType: PurchaserType) => {
    if (purchaserType === draft.purchaserType) return;
    setDraft({ purchaserType });
    setErrors({});
    setFailure(null);
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (sending.current || busy) return; // one request at a time
    const found = validateDraft(draft);
    setErrors(found);
    setFailure(null);
    const first = FIELDS[draft.purchaserType].find((f) => found[f]);
    if (first) {
      document.getElementById(idOf(first))?.focus();
      return;
    }
    if (price == null || noArea) return;
    sending.current = true;
    const email = draft.email.trim();
    create.mutate(toOrderIn(draft, target, edits), {
      onSuccess: (order) => {
        getTracker().track("checkout_completed", {
          ...target.ids,
          panel_type: target.parcelType,
          product: ORDER_PRODUCT,
          order_id: order.reference,
          amount_eur: order.pricing.price_eur,
          currency: order.pricing.currency,
        });
        clearDraft();
        openModal(confirmationSpec(order, target, email));
      },
      onError: (error) => {
        const f = explainFailure(error);
        setErrors(f.fields);
        setFailure(f.message);
        requestAnimationFrame(() => failureRef.current?.scrollIntoView({ block: "nearest" }));
      },
      onSettled: () => {
        sending.current = false;
      },
    });
  };

  const openMethodology = () =>
    openModal({
      label: METHODOLOGY_LABEL,
      wide: true,
      // "Order this analysis →" comes back to this form, with what was typed
      content: <MethodologyModal initialStep={0} context={targetLabel(target)} onOrder={() => openModal(orderModalSpec(target))} />,
    });

  const field = (name: DraftField) => {
    const spec = SPECS[draft.purchaserType][name]!;
    const error = errors[name];
    const id = idOf(name);
    return (
      <div key={name} className={cn("field", error && "err")}>
        <label htmlFor={id}>
          {spec.label}
          {spec.note && (
            <>
              {" "}
              <span className="opt">{spec.note}</span>
            </>
          )}
        </label>
        <input
          id={id}
          name={name}
          type={spec.type ?? "text"}
          className={spec.mono ? "mono" : undefined}
          placeholder={spec.placeholder}
          autoComplete={spec.autoComplete}
          maxLength={MAX_LENGTH[name]}
          value={draft[name]}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-err` : undefined}
          onChange={(e) => change(name, e.target.value)}
        />
        {error && (
          <p className="ferr" id={`${id}-err`}>
            {error}
          </p>
        )}
      </div>
    );
  };

  return (
    <>
      <ModalHead
        icon={<IconCard />}
        iconStyle={{ background: "var(--paid-tint)", color: "var(--paid)" }}
        eyebrow="Pay per service · one-off"
        eyebrowColor="var(--paid)"
        title="Order expert analysis"
        lead="A qualified expert produces a site analysis & feasibility study for this parcel — interpretation, hidden risks, development scenarios and benchmarking beyond the automated figures. Delivered by email."
      />
      <div className="mbody">
        <form id={formId} noValidate onSubmit={submit}>
          <div className="ctx">
            <span className="ci">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
                <path d="M8 1v14M1 8h14" stroke="currentColor" strokeWidth="1.2" />
              </svg>
            </span>
            <div className="cd">
              Analysing <b>{targetLabel(target)}</b> — carried through automatically, no need to re-enter.
            </div>
          </div>

          <div className="ordersum">
            <div className="osrow">
              <span className="osl">
                Parcel size<em>{target.calculationBasis === "cadastral" ? "cadastral parcel" : "urban parcel"}</em>
              </span>
              <span className="mono">{noArea ? "—" : `${formatArea(target.basisAreaM2!)} m²`}</span>
            </div>
            <div className="osrow">
              <span className="osl">
                Analysis fee{band && <em>{band}</em>}
              </span>
              <span className="mono">{price != null ? formatPrice(price, tiers?.currency) : "—"}</span>
            </div>
            <div className="osrow deliv">
              <span className="osl">Expected delivery</span>
              <span className="mono">{turnaround ?? "—"}</span>
            </div>
          </div>
          {pricing.isError && !tiers ? (
            <p className="pricenote">
              The fee could not be loaded.{" "}
              <button type="button" className="orderretry" onClick={() => void pricing.refetch()}>
                Try again
              </button>
            </p>
          ) : noArea ? (
            <p className="pricenote">This parcel has no area to price from yet, so it cannot be ordered online — please contact us.</p>
          ) : (
            note && <p className="pricenote">{note}</p>
          )}

          <div className="fieldlab" id={`${formId}-as`}>
            Ordering as
          </div>
          <div className="seg" role="radiogroup" aria-labelledby={`${formId}-as`}>
            {(
              [
                ["individual", "Individual"],
                ["legal_entity", "Legal entity"],
              ] as const
            ).map(([value, text]) => (
              <button
                key={value}
                type="button"
                role="radio"
                aria-checked={draft.purchaserType === value}
                className={cn("segb", draft.purchaserType === value && "on")}
                onClick={() => switchTo(value)}
              >
                {text}
              </button>
            ))}
          </div>

          {ROWS[draft.purchaserType].map((row) =>
            row.length === 2 ? (
              <div key={row.join()} className="frow">
                {row.map(field)}
              </div>
            ) : (
              field(row[0])
            ),
          )}

          <div
            className="methlink"
            role="button"
            tabIndex={0}
            onClick={openMethodology}
            onKeyDown={(e: KeyboardEvent) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                openMethodology();
              }
            }}
          >
            <span className="mli">
              <svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden>
                <circle cx="3" cy="3" r="1.6" stroke="currentColor" strokeWidth="1.2" />
                <circle cx="3" cy="12" r="1.6" stroke="currentColor" strokeWidth="1.2" />
                <path d="M3 4.6v5.8M6.5 3H13M6.5 7.5H13M6.5 12H13" stroke="currentColor" strokeWidth="1.2" />
              </svg>
            </span>
            <span className="mlt">
              <b>How we analyze this parcel</b>
              <span>The six steps this report follows, from locating the parcel to the final package.</span>
            </span>
            <span className="mla">→</span>
          </div>

          <p style={{ fontSize: 11, color: "var(--ink-2)", lineHeight: 1.5, marginTop: 12 }}>
            No account needed — guest checkout. You&apos;ll get the report and an order reference by email.
          </p>
          {failure && (
            <p className="orderfail" role="alert" ref={failureRef}>
              {failure}
            </p>
          )}
        </form>
      </div>
      <div className="mfoot">
        <span className="fnote">
          {price != null ? formatPrice(price, tiers?.currency) : "—"} · {turnaround ?? "—"}
        </span>
        <Cta variant="ghost" style={{ width: "auto", padding: "0 18px" }} onClick={closeModal}>
          Cancel
        </Cta>
        <Cta
          variant="gold"
          type="submit"
          form={formId}
          style={{ width: "auto", padding: "0 22px" }}
          disabled={busy || price == null || noArea}
          aria-busy={busy || undefined}
        >
          {busy ? "Placing order…" : "Place order →"}
        </Cta>
      </div>
    </>
  );
}
