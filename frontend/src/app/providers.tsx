"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { AnalyticsBoot } from "@/lib/analytics/react";
import { LangProvider } from "@/lib/i18n";
import type { Lang } from "@/lib/i18n/strings";

export function Providers({ lang, children }: { lang: Lang; children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { refetchOnWindowFocus: false, staleTime: 30_000 },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <LangProvider initialLang={lang}>
        <AnalyticsBoot />
        {children}
      </LangProvider>
    </QueryClientProvider>
  );
}
