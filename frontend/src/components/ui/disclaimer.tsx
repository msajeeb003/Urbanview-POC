"use client";

/**
 * The feasibility disclaimer under the figures (the wireframe's small 10.5 px note). Until the
 * lawyer approves the API's wording (`disclaimer_status = client_approved`) the wireframe's text
 * shows, from the string table; afterwards the API's `disclaimer_en` / `_me` in the shell's
 * language. Reused wherever the panel shows figures.
 */
import { pickLang, useLang, useT } from "@/lib/i18n";

export function Disclaimer({
  approved,
  en,
  me,
  style,
}: {
  /** The API's `disclaimer_status === "client_approved"`. */
  approved: boolean;
  en?: string | null;
  me?: string | null;
  style?: React.CSSProperties;
}) {
  const { lang } = useLang();
  const t = useT();
  const text = approved && en ? pickLang(lang, en, me) : t("disclaimer.placeholder");
  return (
    <p className="disclaimer" style={{ fontSize: "10.5px", color: "var(--ink-2)", opacity: 0.8, marginTop: 10, lineHeight: 1.5, ...style }}>
      {text}
    </p>
  );
}
