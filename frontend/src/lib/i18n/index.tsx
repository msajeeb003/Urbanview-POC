"use client";

/**
 * The visitor's language (English / Montenegrin) for the shell's strings (`strings.ts`).
 *
 * - The choice is a cookie (`uv.lang`, a year), so the server renders the page in it and the first
 *   paint never flips; `app/layout.tsx` reads it and hands it to `LangProvider`.
 * - Without a choice the default is `NEXT_PUBLIC_DEFAULT_LANG` (`en` until the client approves the
 *   Montenegrin copy; the ticket's target is `me`).
 * - Switching re-renders every string at once, no reload; `<html lang>` follows.
 * - Code outside React (toasts from the selection flow) reads the current language with `getLang()`.
 * - Text the API sends in both languages (`_en` / `_me`) picks its side with `pickLang`.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { DEFAULT_LANG, HTML_LANG, LANG_COOKIE } from "./config";
import { translate, type Lang, type StringKey, type Translate, type Vars } from "./strings";

export { LANGS, isLang, translate, type Lang, type StringKey, type Translate } from "./strings";

let current: Lang = DEFAULT_LANG;
/** The language on screen, for code outside React. */
export const getLang = (): Lang => current;

interface LangState {
  lang: Lang;
  setLang(lang: Lang): void;
}

const LangContext = createContext<LangState>({ lang: DEFAULT_LANG, setLang: () => undefined });

export function LangProvider({ initialLang, children }: { initialLang: Lang; children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(initialLang);

  // in an effect: the server renders many requests with one module, the browser only one visitor
  useEffect(() => {
    current = lang;
    document.documentElement.lang = HTML_LANG[lang];
  }, [lang]);

  const setLang = useCallback((next: Lang) => {
    document.cookie = `${LANG_COOKIE}=${next}; path=/; max-age=${365 * 24 * 3600}; samesite=lax`;
    setLangState(next);
  }, []);

  const value = useMemo(() => ({ lang, setLang }), [lang, setLang]);
  return <LangContext.Provider value={value}>{children}</LangContext.Provider>;
}

export function useLang(): LangState {
  return useContext(LangContext);
}

/** `t("nav.map")`, `t("nav.returnTo", { name })` in the current language. */
export function useT(): Translate {
  const { lang } = useLang();
  return useCallback((key: StringKey, vars?: Vars) => translate(lang, key, vars), [lang]);
}

/** Outside React: the current language's string. */
export const tNow: Translate = (key, vars) => translate(current, key, vars);

/** The side of a bilingual API text for a language (English when the other side is missing). */
export function pickLang(lang: Lang, en: string, me: string | null | undefined): string {
  return lang === "me" && me ? me : en;
}
