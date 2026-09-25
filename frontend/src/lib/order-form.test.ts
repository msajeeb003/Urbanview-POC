import { describe, expect, it } from "vitest";

import { ApiError } from "./api/client";
import { EMPTY_DRAFT, explainFailure, isEmail, isPhone, toOrderIn, validateDraft, type OrderDraft } from "./order-form";

const individual: OrderDraft = {
  ...EMPTY_DRAFT,
  firstName: " Marko ",
  telephone: "+382 67 123 456",
  email: " marko@email.me ",
};
const company: OrderDraft = {
  ...EMPTY_DRAFT,
  purchaserType: "legal_entity",
  companyName: "Gradnja d.o.o.",
  taxNumber: "02345678",
  contactPerson: "Marko Petrović",
  telephone: "067/123-456",
  email: "office@company.me",
  registeredAddress: "Bulevar Svetog Petra Cetinjskog 1, Podgorica",
};
const location = { parcelType: "urban" as const, parcelId: 12 };

describe("order form validation (the API's rules)", () => {
  it("needs first name, telephone and email from an individual; the last name is optional", () => {
    expect(validateDraft(individual)).toEqual({});
    expect(Object.keys(validateDraft(EMPTY_DRAFT)).sort()).toEqual(["email", "firstName", "telephone"]);
  });

  it("needs company, PIB / VAT, contact person, telephone, email and address from a legal entity", () => {
    expect(validateDraft(company)).toEqual({});
    const missing = validateDraft({ ...EMPTY_DRAFT, purchaserType: "legal_entity" });
    expect(Object.keys(missing).sort()).toEqual(
      ["companyName", "contactPerson", "email", "registeredAddress", "taxNumber", "telephone"].sort(),
    );
    // an individual's fields are not asked of a company, and the other way round
    expect(validateDraft({ ...company, firstName: "" })).toEqual({});
  });

  it("checks email and telephone as the server does", () => {
    expect(isEmail("ana.novak@example.com")).toBe(true);
    expect(isEmail("not-an-email")).toBe(false);
    expect(isPhone("+382 67 123 456")).toBe(true);
    expect(isPhone("067/123-456")).toBe(true);
    expect(isPhone("12")).toBe(false);
    expect(isPhone("call me")).toBe(false);
    expect(validateDraft({ ...individual, email: "marko@" }).email).toMatch(/valid email/);
    expect(validateDraft({ ...individual, telephone: "+3" }).telephone).toMatch(/valid telephone/);
  });
});

describe("the order request", () => {
  it("sends an individual's names, trimmed, and no company fields", () => {
    expect(toOrderIn(individual, location, {})).toEqual({
      location: { parcel_type: "urban", parcel_id: 12 },
      purchaser_type: "individual",
      email: "marko@email.me",
      telephone: "+382 67 123 456",
      assumptions: null,
      first_name: "Marko",
      last_name: null,
    });
  });

  it("sends a company's fields (the server addresses its contact person) and the visitor's edits", () => {
    const body = toOrderIn(company, location, { sale_price_eur_m2: 2600 });
    expect(body).toMatchObject({
      purchaser_type: "legal_entity",
      company_name: "Gradnja d.o.o.",
      tax_number: "02345678",
      contact_person: "Marko Petrović",
      registered_address: "Bulevar Svetog Petra Cetinjskog 1, Podgorica",
      assumptions: { construction_cost_per_m2: null, selling_price_per_m2: 2600, saleable_share: null },
    });
    expect(body).not.toHaveProperty("first_name");
  });
});

describe("a failed order keeps the form and says why in one sentence", () => {
  const err = (status: number, code: string, message = "", details?: unknown) =>
    new ApiError({ status, code, message, details });

  it("network trouble and server errors: try again, nothing charged", () => {
    expect(explainFailure(err(0, "network_error")).message).toMatch(/try again/);
    expect(explainFailure(err(503, "service_unavailable")).message).toMatch(/nothing has been charged/);
    expect(explainFailure(new Error("boom")).fields).toEqual({});
  });

  it("maps the server's field errors onto the form", () => {
    const f = explainFailure(
      err(422, "validation_error", "Request validation failed", [
        { loc: ["body", "email"], msg: "Value error, not an e-mail address" },
        { loc: ["body", "telephone"], msg: "Value error, not a telephone number" },
      ]),
    );
    expect(Object.keys(f.fields).sort()).toEqual(["email", "telephone"]);
    expect(f.message).toMatch(/highlighted fields/);
  });

  it("the daily cap per e-mail address, the rate limiter and a parcel that is gone", () => {
    expect(
      explainFailure(err(429, "rate_limited", "Too many orders for this e-mail address today; please contact support"))
        .message,
    ).toMatch(/contact support/);
    expect(explainFailure(err(429, "rate_limited", "Too many requests")).message).toMatch(/wait a minute/);
    expect(explainFailure(err(404, "not_found")).message).toMatch(/pick it again/);
  });
});
