import type { Metadata, Viewport } from "next";
import { JetBrains_Mono, Schibsted_Grotesk } from "next/font/google";
import { cookies } from "next/headers";

// Order matters: Tailwind (layered) → the wireframe stylesheet verbatim (unlayered, wins) →
// documented overrides.
import "./globals.css";
import "@/styles/wireframe.css";
import "@/styles/overrides.css";

import { HTML_LANG, LANG_COOKIE, langFrom } from "@/lib/i18n/config";

import { Providers } from "./providers";

// The wireframe's two faces, self-hosted at build time from Google Fonts. latin-ext carries the
// Montenegrin letters (č ć đ š ž).
const schibsted = Schibsted_Grotesk({
  subsets: ["latin", "latin-ext"],
  weight: ["400", "500", "600", "700", "800"],
  variable: "--font-schibsted-grotesk",
  display: "swap",
});
const jetbrains = JetBrains_Mono({
  subsets: ["latin", "latin-ext"],
  weight: ["400", "500", "700"],
  variable: "--font-jetbrains-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "UrbanView — Urban feasibility engine",
  description:
    "Find a parcel in Podgorica and see what the adopted plan allows on it, with its source, and whether it is worth building.",
  icons: { icon: "/brand/UrbanView_mark.svg" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#241B12",
};

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  // the visitor's language choice (cookie), so the server renders the page in it
  const lang = langFrom((await cookies()).get(LANG_COOKIE)?.value);
  return (
    <html lang={HTML_LANG[lang]} className={`${schibsted.variable} ${jetbrains.variable}`}>
      <body>
        <Providers lang={lang}>{children}</Providers>
      </body>
    </html>
  );
}
