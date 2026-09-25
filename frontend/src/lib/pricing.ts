/**
 * The expert-analysis price of a parcel, shown on the panel's gold CTA before any order exists.
 * Tiers are configuration (`GET /v1/orders/pricing`, `ORDER_PRICE_TIERS`); the area is the panel's
 * `basis_area_m2` (planned urban parcel area, else cadastral). The rule is the server's
 * `core.pricing.price_for`: the first tier whose bound covers the area, bounds inclusive, the last
 * tier open-ended — so the panel shows exactly what `POST /v1/orders` will charge.
 */
import type { OrderPricing } from "./api/types";

export function priceFor(area: number | null | undefined, pricing: OrderPricing | undefined): number | null {
  if (area == null || !pricing || pricing.tiers.length === 0) return null;
  for (const tier of pricing.tiers) {
    if (tier.up_to_m2 == null || area <= tier.up_to_m2) return tier.price_eur;
  }
  return pricing.tiers[pricing.tiers.length - 1].price_eur;
}

/** `€100` (whole euros as the mock shows them; cents when a tier has them). */
export function formatPrice(eur: number, currency = "EUR"): string {
  const symbol = currency === "EUR" ? "€" : `${currency} `;
  return `${symbol}${Number.isInteger(eur) ? eur : eur.toFixed(2)}`;
}

const m2 = (n: number) => `${n.toLocaleString("en-US")} m²`;

/**
 * The tier an area falls in, worded as the order summary shows it: `up to 500 m²` (first tier),
 * `500–1,000 m²` (a middle one), `over 500 m²` (the open-ended top tier).
 */
export function bandLabel(area: number | null | undefined, pricing: OrderPricing | undefined): string | null {
  if (area == null || !pricing || pricing.tiers.length === 0) return null;
  const tiers = pricing.tiers;
  const i = tiers.findIndex((t) => t.up_to_m2 == null || area <= t.up_to_m2);
  const at = i === -1 ? tiers.length - 1 : i;
  const bound = tiers[at].up_to_m2;
  const below = at > 0 ? tiers[at - 1].up_to_m2 : null;
  if (bound == null) return below == null ? "any size" : `over ${m2(below)}`;
  return below == null ? `up to ${m2(bound)}` : `${below.toLocaleString("en-US")}–${m2(bound)}`;
}

/**
 * The order form's pricing note, from the configured tiers: `Prototype pricing is set by parcel
 * size alone — €100 up to 500 m², €200 above. …` (the mock's sentence for its two tiers).
 */
export function priceNote(pricing: OrderPricing | undefined): string | null {
  if (!pricing || pricing.tiers.length === 0) return null;
  const parts = pricing.tiers.map((t, i) => {
    const price = formatPrice(t.price_eur, pricing.currency);
    if (t.up_to_m2 != null) return `${price} up to ${m2(t.up_to_m2)}`;
    return i === 0 ? `${price} for any size` : `${price} above`;
  });
  return (
    `Prototype pricing is set by parcel size alone — ${parts.join(", ")}. Later phases can also weigh the ` +
    "planning-document area and other factors affecting the complexity of the analysis."
  );
}

/** `5 working days` (the configured turnaround, counted from the payment). */
export function turnaroundText(businessDays: number | null | undefined): string | null {
  if (businessDays == null) return null;
  return `${businessDays} working day${businessDays === 1 ? "" : "s"}`;
}
