import { describe, expect, it } from "vitest";

import { deltaPhrase, formatArea, parcelNo } from "@/components/panel/parcel-parts";

import { bandLabel, formatPrice, priceFor, priceNote, turnaroundText } from "./pricing";

const PRICING = {
  currency: "EUR",
  tiers: [
    { up_to_m2: 500, price_eur: 100 },
    { up_to_m2: null, price_eur: 200 },
  ],
  turnaround_business_days: 5,
};

describe("priceFor (the server's core.pricing.price_for)", () => {
  it("picks the first tier whose bound covers the area, bounds inclusive", () => {
    expect(priceFor(373, PRICING)).toBe(100);
    expect(priceFor(500, PRICING)).toBe(100);
    expect(priceFor(500.1, PRICING)).toBe(200);
    expect(priceFor(959.6, PRICING)).toBe(200);
  });
  it("has no price without an area or tiers", () => {
    expect(priceFor(null, PRICING)).toBeNull();
    expect(priceFor(400, undefined)).toBeNull();
    expect(priceFor(400, { ...PRICING, tiers: [] })).toBeNull();
  });
  it("formats like the mock", () => {
    expect(formatPrice(100)).toBe("€100");
    expect(formatPrice(149.5)).toBe("€149.50");
    expect(formatPrice(80, "USD")).toBe("USD 80");
  });
});

describe("the order summary's words (from the configured tiers)", () => {
  const three = {
    ...PRICING,
    tiers: [
      { up_to_m2: 500, price_eur: 100 },
      { up_to_m2: 1000, price_eur: 150 },
      { up_to_m2: null, price_eur: 200 },
    ],
  };
  it("names the band an area falls in", () => {
    expect(bandLabel(373, PRICING)).toBe("up to 500 m²");
    expect(bandLabel(959.6, PRICING)).toBe("over 500 m²");
    expect(bandLabel(800, three)).toBe("500–1,000 m²");
    expect(bandLabel(null, PRICING)).toBeNull();
  });
  it("writes the mock's pricing note for any tiers", () => {
    expect(priceNote(PRICING)).toBe(
      "Prototype pricing is set by parcel size alone — €100 up to 500 m², €200 above. Later phases can also weigh the " +
        "planning-document area and other factors affecting the complexity of the analysis.",
    );
    expect(priceNote(three)).toContain("€100 up to 500 m², €150 up to 1,000 m², €200 above.");
    expect(priceNote(undefined)).toBeNull();
  });
  it("words the turnaround", () => {
    expect(turnaroundText(5)).toBe("5 working days");
    expect(turnaroundText(1)).toBe("1 working day");
    expect(turnaroundText(undefined)).toBeNull();
  });
});

describe("parcel wording", () => {
  it("states the area change, never a silent zero", () => {
    expect(deltaPhrase(-30)).toEqual({ delta: "−30%", text: "taken for roads / public space." });
    expect(deltaPhrase(-46.6)).toEqual({ delta: "−47%", text: "taken for roads / public space." });
    expect(deltaPhrase(0.6)).toEqual({ delta: "+1%", text: "larger than the cadastral parcel." });
    expect(deltaPhrase(0.3)).toEqual({ delta: "+0.3%", text: "larger than the cadastral parcel." });
    expect(deltaPhrase(0)).toEqual({ delta: null, text: "Same area as the cadastral parcel." });
  });
  it("formats areas and parcel numbers like the mock", () => {
    expect(formatArea(1370.9)).toBe("1,370.9");
    expect(formatArea(700)).toBe("700");
    expect(parcelNo("2001", "2")).toBe("2001/2");
    expect(parcelNo("1042", null)).toBe("1042");
  });
});
