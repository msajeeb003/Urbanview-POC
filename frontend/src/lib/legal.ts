/**
 * The legal pages of the public site (`/legal/<page>`), linked from the S4 order form: terms of
 * service, privacy notice, refund policy, disclaimer (the pilot technical scope's `legal/` route).
 * The wording is provisional: the pilot scope makes legal copy the client's deliverable ("Launch,
 * not build"), so every page says it is a draft until the client's lawyer supplies the text. What
 * the drafts say is what the build does (bank transfer, the order statuses, what is stored).
 */

export type LegalSlug = "terms" | "privacy" | "refund" | "disclaimer";

export interface LegalSection {
  heading: string;
  paragraphs?: string[];
  items?: string[];
}

export interface LegalPage {
  slug: LegalSlug;
  title: string;
  lead: string;
  sections: LegalSection[];
}

export const LEGAL_STATUS = "placeholder" as const;
export const LEGAL_DRAFT_NOTE =
  "Draft wording for the pilot. The final text is supplied by the client's lawyer before launch; until then this page describes how the service works.";

export const LEGAL_PAGES: Record<LegalSlug, LegalPage> = {
  terms: {
    slug: "terms",
    title: "Terms of service",
    lead: "How the UrbanView map and the expert analysis you can order work.",
    sections: [
      {
        heading: "The map",
        paragraphs: [
          "UrbanView shows what the adopted planning documents allow on a parcel in Podgorica: the zone, the planning parameters with a link to the page of the document each value comes from, and indicative financial ranges.",
          "Planning values are transcribed from the adopted documents and checked by an expert before they are published. Financial figures are indicative ranges, not advice (see the disclaimer).",
        ],
      },
      {
        heading: "Ordering an expert analysis",
        items: [
          "No account is needed. You give your name, e-mail address and telephone number (a company may add its name and PIB).",
          "The price is shown before you order. It depends on the parcel area the analysis is based on.",
          "You pay by bank transfer, quoting the order reference. Work starts once the payment is received.",
          "The analysis is prepared by a qualified expert and sent to your e-mail address within the number of business days shown when you order, counted from the payment.",
          "The analysis is based on the data version the map showed when you ordered; it is kept with your order.",
        ],
      },
      {
        heading: "Your order's status",
        paragraphs: [
          "The link in your confirmation shows the status of the order at any time: awaiting payment, payment not received, paid, in progress, delivered or refunded.",
        ],
      },
    ],
  },
  privacy: {
    slug: "privacy",
    title: "Privacy notice",
    lead: "What UrbanView stores about you and why.",
    sections: [
      {
        heading: "When you order an analysis",
        items: [
          "Your name, e-mail address and telephone number, and a company name and PIB if you give them.",
          "The parcel, the panel you saw with any assumptions you edited, the price and the order's status and payments.",
        ],
        paragraphs: [
          "They are used to deliver the analysis, to send you the payment instructions and the report, and for invoicing. Orders placed with the same e-mail address are kept under one customer record. No account is created and no card details are collected.",
        ],
      },
      {
        heading: "E-mails",
        paragraphs: [
          "Order e-mails are sent through an e-mail delivery provider. A record of each e-mail (recipient, subject, delivery status) is kept with the order; the message body is not stored.",
        ],
      },
      {
        heading: "Using the map",
        paragraphs: [
          "The map records anonymous usage events (for example that a parcel was opened) with a random session identifier. It records no name, e-mail address or IP address.",
        ],
      },
      {
        heading: "Your rights",
        paragraphs: [
          "You can ask to see, correct or delete your details by writing to the support address in your order e-mails. How long order records are kept is set in the final notice.",
        ],
      },
    ],
  },
  refund: {
    slug: "refund",
    title: "Refund policy",
    lead: "When the fee for an expert analysis is refunded.",
    sections: [
      {
        heading: "Before the report is delivered",
        paragraphs: [
          "An order that has been paid can be refunded until the report is delivered, while the order is paid or in progress. Write to the support address in your order e-mails with the order reference; the refund is made by bank transfer.",
        ],
      },
      {
        heading: "After delivery",
        paragraphs: ["A delivered analysis is not refundable. If something in the report is wrong, write to us and the expert will review it."],
      },
      {
        heading: "Payments that did not arrive",
        paragraphs: [
          "Nothing is charged until your transfer is received. If it does not reach us, the order shows “payment not received”, and you can still pay with the same instructions.",
        ],
      },
    ],
  },
  disclaimer: {
    slug: "disclaimer",
    title: "Disclaimer",
    lead: "Figures are indicative ranges derived from the adopted plan and public market data, not investment, planning or legal advice.",
    sections: [
      {
        heading: "Planning information",
        paragraphs: [
          "Planning values are transcribed from the adopted planning documents and reviewed before they are published; each one links to the page it comes from. The adopted document itself and the competent authorities remain the reference: check the cited page before relying on a value.",
        ],
      },
      {
        heading: "Financial ranges",
        paragraphs: [
          "Land value, costs, market value, profit and return are shown as low, expected and high ranges calculated with fixed formulas from zone-level market inputs and the assumptions shown, which you can edit. They are estimates for a first look at a site, not valuations.",
        ],
      },
    ],
  },
};

export const LEGAL_SLUGS = Object.keys(LEGAL_PAGES) as LegalSlug[];

export const legalPath = (slug: LegalSlug) => `/legal/${slug}`;
