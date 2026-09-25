/**
 * The S4 order form's rules, kept pure (unit-tested): the draft the visitor types, client-side
 * validation mirroring `POST /v1/orders` (`api/schemas/orders.py`: required fields per purchaser
 * type, the same e-mail and telephone patterns, the same lengths), the request body, and what a
 * rejected request means for the form (one sentence, plus the fields the server pointed at).
 *
 * Personal data stays here and in the order request: never in analytics, never in storage.
 */
import type { EventProperties } from "./analytics/tracker";
import { ApiError } from "./api/client";
import type { OrderIn } from "./api/types";
import { hasEdits, toRequestAssumptions, type AssumptionEdits } from "./assumptions";

/** The `product` of the order events (the analytics dashboard groups revenue by it). */
export const ORDER_PRODUCT = "expert_report";

export type PurchaserType = "individual" | "legal_entity";

export interface OrderDraft {
  purchaserType: PurchaserType;
  firstName: string;
  lastName: string;
  telephone: string;
  email: string;
  companyName: string;
  taxNumber: string;
  contactPerson: string;
  registeredAddress: string;
}

export type DraftField = Exclude<keyof OrderDraft, "purchaserType">;
export type FieldErrors = Partial<Record<DraftField, string>>;

export const EMPTY_DRAFT: OrderDraft = {
  purchaserType: "individual",
  firstName: "",
  lastName: "",
  telephone: "",
  email: "",
  companyName: "",
  taxNumber: "",
  contactPerson: "",
  registeredAddress: "",
};

/** Maximum lengths of the API (`OrderIn`). */
export const MAX_LENGTH: Record<DraftField, number> = {
  firstName: 100,
  lastName: 100,
  telephone: 30,
  email: 254,
  companyName: 200,
  taxNumber: 40,
  contactPerson: 200,
  registeredAddress: 500,
};

/** The fields each purchaser type shows, in the wireframe's order. */
export const FIELDS: Record<PurchaserType, DraftField[]> = {
  individual: ["firstName", "lastName", "telephone", "email"],
  legal_entity: ["companyName", "taxNumber", "contactPerson", "telephone", "email", "registeredAddress"],
};

const REQUIRED: Record<PurchaserType, DraftField[]> = {
  individual: ["firstName", "telephone", "email"],
  legal_entity: ["companyName", "taxNumber", "contactPerson", "telephone", "email", "registeredAddress"],
};

const MISSING: Record<DraftField, string> = {
  firstName: "Enter your first name.",
  lastName: "Enter your last name.",
  telephone: "Enter a telephone number, e.g. +382 67 123 456.",
  email: "Enter your email address.",
  companyName: "Enter the company name.",
  taxNumber: "Enter the PIB / VAT number.",
  contactPerson: "Enter a contact person.",
  registeredAddress: "Enter the registered address for the invoice.",
};

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
      if (REQUIRED[draft.purchaserType].includes(field)) errors[field] = MISSING[field];
      continue;
    }
    if (value.length > MAX_LENGTH[field]) errors[field] = `Use at most ${MAX_LENGTH[field]} characters.`;
    else if (field === "email" && !isEmail(value)) errors.email = "Enter a valid email address, e.g. you@email.me.";
    else if (field === "telephone" && !isPhone(value))
      errors.telephone = "Enter a valid telephone number, e.g. +382 67 123 456.";
  }
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
  /** The panel's `basis_area_m2`: what the server prices from. */
  basisAreaM2: number | null;
  calculationBasis: "urban" | "cadastral" | null;
  /** Ids for the order events (`parcel_id`, `urban_parcel_id`, `zone_id`). */
  ids: EventProperties;
}

/**
 * The request body: the location the panel showed, the purchaser's fields for their type (a legal
 * entity's contact person is who the e-mails address; the server fills the name from it) and the
 * visitor's edited assumptions, if any.
 */
export function toOrderIn(draft: OrderDraft, location: OrderLocation, edits: AssumptionEdits): OrderIn {
  const t = (v: string) => v.trim();
  const common = {
    location: { parcel_type: location.parcelType, parcel_id: location.parcelId },
    purchaser_type: draft.purchaserType,
    email: t(draft.email),
    telephone: t(draft.telephone),
    assumptions: hasEdits(edits) ? toRequestAssumptions(edits) : null,
  };
  if (draft.purchaserType === "legal_entity") {
    return {
      ...common,
      company_name: t(draft.companyName),
      tax_number: t(draft.taxNumber),
      contact_person: t(draft.contactPerson),
      registered_address: t(draft.registeredAddress),
    };
  }
  return { ...common, first_name: t(draft.firstName), last_name: t(draft.lastName) || null };
}

const SERVER_FIELDS: Record<string, DraftField> = {
  first_name: "firstName",
  last_name: "lastName",
  telephone: "telephone",
  email: "email",
  company_name: "companyName",
  tax_number: "taxNumber",
  contact_person: "contactPerson",
  registered_address: "registeredAddress",
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
      message: "The order could not be sent — check your connection and try again; nothing has been charged.",
      fields: {},
    };
  }
  if (error.status === 429) {
    return {
      message:
        error.code === "rate_limited" && /e-?mail/i.test(error.message)
          ? "This email address has placed the most orders allowed today — please contact support to order more."
          : "Too many requests just now — please wait a minute and try again.",
      fields: {},
    };
  }
  if (error.status === 404) {
    return { message: "This parcel is no longer available — close the form and pick it again on the map.", fields: {} };
  }
  if (error.status === 422) {
    const fields: FieldErrors = {};
    const details = Array.isArray(error.details) ? error.details : [];
    for (const d of details as { loc?: unknown[]; msg?: string }[]) {
      const key = SERVER_FIELDS[String(d.loc?.[d.loc.length - 1] ?? "")];
      if (key) fields[key] = key === "email" ? "Enter a valid email address, e.g. you@email.me." : "Check this field.";
    }
    const location = details.some((d) => (d as { loc?: unknown[] }).loc?.includes("location"));
    return {
      message: location
        ? "This parcel cannot be ordered yet (it has no area to price from) — please contact support."
        : "Some details were not accepted — check the highlighted fields and try again.",
      fields,
    };
  }
  return { message: "The order could not be placed — please check the form and try again.", fields: {} };
}
