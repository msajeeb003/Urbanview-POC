import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { LegalPageView } from "@/components/legal/legal-page";
import { LEGAL_PAGES, LEGAL_SLUGS, type LegalSlug } from "@/lib/legal";

// The legal pages the order form links to (terms, privacy, refund, disclaimer): static, draft
// wording until the client's lawyer supplies it (`lib/legal.ts`).

type Props = { params: Promise<{ page: string }> };

export const dynamicParams = false;

export function generateStaticParams() {
  return LEGAL_SLUGS.map((page) => ({ page }));
}

const pageOf = (slug: string) => (LEGAL_SLUGS as string[]).includes(slug) ? LEGAL_PAGES[slug as LegalSlug] : null;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const page = pageOf((await params).page);
  return { title: page ? `${page.title} — UrbanView` : "UrbanView" };
}

export default async function LegalRoute({ params }: Props) {
  const page = pageOf((await params).page);
  if (!page) notFound();
  return <LegalPageView page={page} />;
}
