/**
 * Language settings the server needs too (the root layout reads the cookie): kept out of the
 * client module `index.tsx`.
 */
import { isLang, type Lang } from "./strings";

export const LANG_COOKIE = "uv.lang";
/** `NEXT_PUBLIC_DEFAULT_LANG` (`en` until the client approves the Montenegrin copy). */
export const DEFAULT_LANG: Lang = isLang(process.env.NEXT_PUBLIC_DEFAULT_LANG) ? process.env.NEXT_PUBLIC_DEFAULT_LANG : "en";
/** `<html lang>`: English, and Montenegrin (ISO 639-3 `cnr`) in Latin script. */
export const HTML_LANG: Record<Lang, string> = { en: "en", me: "cnr-Latn" };

/** The language a request asks for (the cookie's value), else the default. */
export function langFrom(cookieValue: string | undefined | null): Lang {
  return isLang(cookieValue) ? cookieValue : DEFAULT_LANG;
}
