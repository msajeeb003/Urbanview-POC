/**
 * The S4 order form's rules, kept pure (unit-tested): the draft the visitor types, client-side
 * validation mirroring `POST /v1/orders` (`api/schemas/orders.py`: the pilot scope's guest form,
 * name, telephone and e-mail for everyone, a legal entity's company name and PIB optional; the
 * same e-mail and telephone patterns, the same lengths), where the customer pays from (the
 * client, 2026-10-05: a bank in the country gets the domestic account number, one abroad the IBAN
 * and SWIFT / BIC; the form asks, nothing is preselected), the request body, and what a rejected
 * request means for the form (one sentence, plus the fields the server pointed at).
 *
 * Personal data stays here and in the order request: never in analytics, never in storage.
 */
import type { EventProperties } from "./analytics/tracker";
import { ApiError } from "./api/client";
import type { OrderIn } from "./api/types";
import { hasEdits, toRequestAssumptions, type AssumptionEdits } from "./assumptions";
import type { Lang } from "./i18n/strings";

/** The `product` of the order events (the analytics dashboard groups revenue by it). */
export const ORDER_PRODUCT = "expert_report";

export type PurchaserType = "individual" | "legal_entity";
/** Where the customer pays from: decides which bank details the order shows. */
export type PaymentOrigin = NonNullable<OrderIn["payment_origin"]>;

export interface OrderDraft {
  purchaserType: PurchaserType;
  /** Null until the visitor picks one: the form never guesses where a customer's bank is. */
  paymentOrigin: PaymentOrigin | null;
  firstName: string;
  lastName: string;
  telephone: string;
  email: string;
  companyName: string;
  taxNumber: string;
}

/** The typed fields (the two choices, purchaser type and payment origin, are buttons). */
export type DraftField = Exclude<keyof OrderDraft, "purchaserType" | "paymentOrigin">;
export type FieldErrors = Partial<Record<DraftField | "paymentOrigin", string>>;

export const EMPTY_DRAFT: OrderDraft = {
  purchaserType: "individual",
  paymentOrigin: null,
  firstName: "",
  lastName: "",
  telephone: "",
  email: "",
  companyName: "",
  taxNumber: "",
};

/** Maximum lengths of the API (`OrderIn`). */
export const MAX_LENGTH: Record<DraftField, number> = {
  firstName: 100,
  lastName: 100,
  telephone: 30,
  email: 254,
  companyName: 200,
  taxNumber: 40,
};

/** The fields each purchaser type shows, in form order (a legal entity adds its company first). */
export const FIELDS: Record<PurchaserType, DraftField[]> = {
  individual: ["firstName", "lastName", "telephone", "email"],
  legal_entity: ["companyName", "taxNumber", "firstName", "lastName", "telephone", "email"],
};

/** The pilot's three required fields, the same for both purchaser types. */
export const REQUIRED: readonly DraftField[] = ["firstName", "telephone", "email"];

const MISSING: Record<DraftField, string> = {
  firstName: "Enter your first name.",
  lastName: "Enter your last name.",
  telephone: "Enter a telephone number, e.g. +382 67 123 456.",
  email: "Enter your email address.",
  companyName: "Enter the company name.",
  taxNumber: "Enter the PIB (company ID).",
};
export const ORIGIN_MISSING = "Choose where you will pay from.";

// the server's patterns (`EMAIL_RE`, `PHONE_RE` + at least six digits)
const EMAIL_RE = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;
const PHONE_RE = /^\+?[0-9][0-9 ()./-]{5,24}$/;

export const isEmail = (value: string) => EMAIL_RE.test(value.trim());
export const isPhone = (value: string) => {
  const v = value.trim();
  return PHONE_RE.test(v) && (v.match(/\d/g)?.length ?? 0) >= 6;
};

/** Inline messages for the fields the current purchaser type shows; `{}` = ready to send. */
export function validateDraft(draft: OrderDraft): FieldErrors {
  const errors: FieldErrors = {};
  for (const field of FIELDS[draft.purchaserType]) {
    const value = draft[field].trim();
    if (!value) {
      if (REQUIRED.includes(field)) errors[field] = MISSING[field];
      continue;
    }
    if (value.length > MAX_LENGTH[field]) errors[field] = `Use at most ${MAX_LENGTH[field]} characters.`;
    else if (field === "email" && !isEmail(value)) errors.email = "Enter a valid email address, e.g. you@email.me.";
    else if (field === "telephone" && !isPhone(value))
      errors.telephone = "Enter a valid telephone number, e.g. +382 67 123 456.";
  }
  if (!draft.paymentOrigin) errors.paymentOrigin = ORIGIN_MISSING;
  return errors;
}

export interface OrderLocation {
  parcelType: "cadastral" | "urban";
  parcelId: number;
}

/** The parcel an order is for, as the panel on screen shows it (carried through, never re-entered). */
export interface OrderTarget extends OrderLocation {
  /** `Parcel #1042/3` (the cadastral parcel, on the urban panel too, as in the mock), else `UP 12`. */
  parcel: string;
  ko: string | null;
  /** The planned urban parcel the figures use (`UP 12`), when there is one. */
  plannedParcel: string | null;
  /** The panel's `data_version`: the published data the visitor saw (stored on the order). */
  dataVersion: string | null;
  /** The panel's `basis_area_m2`: what the server prices from. */
  basisAreaM2: number | null;
  calculationBasis: "urban" | "cadastral" | null;
  /** Ids for the order events (`parcel_id`, `urban_parcel_id`, `zone_id`). */
  ids: EventProperties;
}

/**
 * The request body: the location the panel showed, the name, telephone and e-mail, a legal
 * entity's company name and PIB when given, where the customer pays from, the visitor's edited
 * assumptions, if any, and the language the map is in (the order's e-mails are written in it,
 * one language per e-mail).
 */
export function toOrderIn(draft: OrderDraft, location: OrderLocation, edits: AssumptionEdits, language: Lang): OrderIn {
  const t = (v: string) => v.trim();
  const body: OrderIn = {
    location: { parcel_type: location.parcelType, parcel_id: location.parcelId },
    purchaser_type: draft.purchaserType,
    first_name: t(draft.firstName),
    last_name: t(draft.lastName) || null,
    email: t(draft.email),
    telephone: t(draft.telephone),
    assumptions: hasEdits(edits) ? toRequestAssumptions(edits) : null,
    language,
    payment_origin: draft.paymentOrigin,
  };
  if (draft.purchaserType === "legal_entity") {
    body.company_name = t(draft.companyName) || null;
    body.tax_number = t(draft.taxNumber) || null;
  }
  return body;
}

const SERVER_FIELDS: Record<string, keyof FieldErrors> = {
  first_name: "firstName",
  last_name: "lastName",
  telephone: "telephone",
  email: "email",
  company_name: "companyName",
  tax_number: "taxNumber",
  payment_origin: "paymentOrigin",
};

export interface OrderFailure {
  /** One sentence under the form; the typed data stays. */
  message: string;
  fields: FieldErrors;
}

/** What a failed `POST /v1/orders` means for the visitor (never a dead end). */
export function explainFailure(error: unknown): OrderFailure {
  if (!(error instanceof ApiError) || error.status === 0 || error.status >= 500) {
    return {
      message: "The order could not be sent. Check your connection and try again.",
      fields: {},
    };
  }
  if (error.status === 429) {
    return {
      message:
        error.code === "rate_limited" && /e-?mail/i.test(error.message)
          ? "This email address has reached today's order limit. Contact support to order more."
          : "Too many requests. Please wait a minute and try again.",
      fields: {},
    };
  }
  if (error.status === 404) {
    return { message: "This parcel is no longer available. Pick it again on the map.", fields: {} };
  }
  if (error.status === 422) {
    const fields: FieldErrors = {};
    const details = Array.isArray(error.details) ? error.details : [];
    for (const d of details as { loc?: unknown[]; msg?: string }[]) {
      const key = SERVER_FIELDS[String(d.loc?.[d.loc.length - 1] ?? "")];
      if (key)
        fields[key] =
          key === "email"
            ? "Enter a valid email address, e.g. you@email.me."
            : key === "paymentOrigin"
              ? ORIGIN_MISSING
              : "Check this field.";
    }
    const location = details.some((d) => (d as { loc?: unknown[] }).loc?.includes("location"));
    return {
      message: location
        ? "This parcel cannot be ordered yet. Please contact support."
        : "Check the highlighted fields and try again.",
      fields,
    };
  }
  return { message: "The order could not be placed. Check the form and try again.", fields: {} };
}
